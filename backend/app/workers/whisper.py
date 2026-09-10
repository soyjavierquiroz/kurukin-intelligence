"""Single delivery/inference at a time; broker I/O stays alive during CPU work."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import signal
import time
from uuid import UUID
import psycopg
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_engine
from ..jobs import job_path, validate_audio
from ..models import AudioAssessment, Transcript, TranscriptionJob, Video, now
from ..queue import QUEUE, connect, declare
from ..whisper import whisper_service
from ..yamnet import CLASSIFIER, VERSION, yamnet_service

log = logging.getLogger('kurukin.worker')
SINGLETON_LOCK_KEY = 742819502
SINGLETON_APPLICATION_NAME = 'kurukin-tiktok-whisper-lock'
LOCK_RETRY_SECONDS = 5


class SingletonLockLost(RuntimeError):
    """The dedicated PostgreSQL session which owned the worker lock died."""


def open_singleton_connection(settings=None, connector=psycopg.connect):
    """Open the one connection which owns the session advisory lock.

    This deliberately bypasses SQLAlchemy's pool. PostgreSQL releases the lock
    when this exact connection closes or is lost.
    """
    settings = settings or get_settings()
    url = settings.resolve_database_url()
    return connector(host=url.host, port=url.port, dbname=url.database,
                     user=url.username, password=url.password,
                     application_name=SINGLETON_APPLICATION_NAME,
                     autocommit=True)


def try_acquire_singleton(settings=None, connector=psycopg.connect):
    connection = open_singleton_connection(settings, connector)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', (SINGLETON_LOCK_KEY,))
            acquired = cursor.fetchone()[0]
        if acquired:
            return connection
    except Exception:
        connection.close()
        raise
    connection.close()
    return None


def close_singleton_connection(connection):
    """Release the session lock by closing its dedicated connection.

    Session advisory locks are released by PostgreSQL when their owning socket
    closes.  Do not use pg_advisory_unlock here: a failed socket must never be
    treated as proof that this process still owns the lock.
    """
    if connection is None:
        return
    try:
        if not connection.closed:
            connection.close()
    finally:
        log.info('whisper_lock_released')


def assert_singleton_ownership(connection):
    """Fail closed when the dedicated advisory-lock connection is unusable."""
    if connection is None or connection.closed:
        raise SingletonLockLost('Worker ownership connection lost')
    try:
        connection.execute('SELECT 1').fetchone()
    except Exception as exc:
        raise SingletonLockLost('Worker ownership connection lost') from exc


def close_broker_connection(channel, connection):
    """Stop deliveries before closing Rabbit, leaving any unacked work requeued."""
    if channel is not None:
        try:
            channel.stop_consuming()
        except Exception:
            pass
    if connection is not None and connection.is_open:
        connection.close()


def delete_audio(job):
    try:
        job_path(job).unlink(missing_ok=True)
        job.audio_path = None
        job.last_error_code = None
    except (OSError, ValueError):
        job.last_error_code = 'cleanup_pending'


def process_job(db, job_id, video_id, service=whisper_service, classifier=yamnet_service, settings=None):
    settings = settings or get_settings()
    # Serialize duplicates (including accidental extra workers) using the global Video row.
    video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
    job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id).with_for_update())
    if job is None or video is None or job.video_id != video_id:
        db.rollback()
        return 'reject'
    if db.scalar(select(Transcript.id).where(Transcript.video_id == video_id)):
        job.status = 'completed'
        job.completed_at = job.completed_at or now()
        video.enrichment_status = 'completed'
        video.enrichment_lease_until = None
        db.commit()
        delete_audio(job)
        db.commit()
        return 'ack'
    if job.status in ('reserved', 'expired', 'failed', 'skipped'):
        db.commit()
        return 'ack'
    # Persist attempt before inference so a killed worker cannot retry indefinitely.
    job.status = 'processing'
    job.attempts += 1
    job.started_at = now()
    video.enrichment_status = 'processing'
    db.commit()
    # Do not hold Video locks while Whisper runs: acquisition remains independent.
    attempts = job.attempts
    db.commit()
    try:
        path = job_path(job)
        if attempts > settings.transcription_max_attempts:
            raise ValueError('Attempts exhausted')
        with path.open('rb') as stream:
            data = stream.read(settings.max_audio_mb * 1024**2 + 1)
        validate_audio(data)
        assessment = db.scalar(select(AudioAssessment).where(AudioAssessment.video_id == video_id,
            AudioAssessment.classifier == CLASSIFIER, AudioAssessment.classifier_version == VERSION))
        if assessment is None:
            try:
                metrics = classifier.classify(path)
                assessment = AudioAssessment(video_id=video_id, classifier=metrics['classifier'],
                    classifier_version=metrics['classifier_version'], model_sha256=metrics['model_sha256'],
                    classification=metrics['classification'], speech_score=metrics['speech_score'],
                    music_score=metrics['music_score'], singing_score=metrics['singing_score'],
                    speech_patch_ratio=metrics['speech_patch_ratio'], music_patch_ratio=metrics['music_patch_ratio'],
                    singing_patch_ratio=metrics['singing_patch_ratio'], top_classes=metrics['top_classes'],
                    processing_ms=metrics['processing_ms'])
                db.add(assessment); db.flush()
            except Exception:
                # Classification is advisory until explicitly validated: fail open to Whisper.
                job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id).with_for_update())
                job.classifier_error_code = 'yamnet_failed'
                db.commit()
                assessment = None
        if assessment is not None:
            job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id).with_for_update())
            video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
            job.assessment_id = assessment.id
            if settings.audio_gate_mode == 'enforce' and assessment.classification == 'music':
                job.status='skipped'; job.skip_reason='music'; job.completed_at=now()
                video.enrichment_status='skipped'; video.enrichment_lease_until=None
                db.commit()  # Terminal skip and assessment relation are durable before cleanup/ACK.
                delete_audio(job); db.commit()
                return 'ack'
        db.commit()
        result = service.transcribe(path)
        if not result.get('text', '').strip():
            raise RuntimeError('Empty transcript')
        video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
        job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id).with_for_update())
        if not db.scalar(select(Transcript.id).where(Transcript.video_id == video_id)):
            db.add(Transcript(video_id=video.id, **result))
        job.status = 'completed'
        job.completed_at = now()
        job.last_error_code = None
        video.enrichment_status = 'completed'
        video.enrichment_lease_until = None
        db.commit()  # Transcript and completed are one transaction.
    except Exception as exc:
        db.rollback()
        video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
        job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id).with_for_update())
        if db.scalar(select(Transcript.id).where(Transcript.video_id == video_id)):
            job.status = 'completed'
            job.completed_at = job.completed_at or now()
            video.enrichment_status = 'completed'
            video.enrichment_lease_until = None
            db.commit()
            delete_audio(job)
            db.commit()
            return 'ack'
        job.last_error_code = 'whisper_timeout' if isinstance(exc, TimeoutError) else 'transcription_failed'
        terminal = job.attempts >= settings.transcription_max_attempts
        job.status = 'failed' if terminal else 'queued'
        video.enrichment_status = job.status
        job.failed_at = now() if terminal else None
        if not terminal:
            job.queued_at = now()
        db.commit()
        return 'ack' if terminal else 'retry'
    delete_audio(job)
    db.commit()  # Cleanup successful or durably recorded before ACK.
    return 'ack'


def decode(body):
    if len(body) > 256:
        raise ValueError('Message too large')
    message = json.loads(body)
    if set(message) != {'job_id', 'video_id'}:
        raise ValueError('Invalid message fields')
    return UUID(message['job_id']), UUID(message['video_id'])


def handle_delivery(channel, method, body, executor, connection, ownership_check=lambda: None):
    try:
        job_id, video_id = decode(body)
    except (ValueError, TypeError, KeyError):
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return
    def work():
        with Session(get_engine()) as db:
            return process_job(db, job_id, video_id, whisper_service)
    future = executor.submit(work)
    while not future.done():
        ownership_check()
        connection.process_data_events(time_limit=1)
    try:
        outcome = future.result()
    except Exception:
        # No ACK on uncertain DB commit. Close connection and reconnect with backoff.
        raise RuntimeError('Job database unavailable') from None
    if outcome == 'ack':
        channel.basic_ack(delivery_tag=method.delivery_tag)
    else:
        if outcome == 'retry':
            connection.sleep(5)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=outcome == 'retry')


def run(sleep=time.sleep, connector=psycopg.connect):
    logging.basicConfig(level=logging.INFO)
    stopping = False
    broker_connection = None
    broker_channel = None
    lock_connection = None

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        # Tell pika to stop dispatching before the lifecycle finally block
        # closes Rabbit and then the PostgreSQL session that owns the lock.
        if broker_channel is not None:
            try:
                broker_channel.stop_consuming()
            except Exception:
                pass

    previous = {signum: signal.signal(signum, stop) for signum in (signal.SIGTERM, signal.SIGINT)}
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            while not stopping:
                # A session advisory lock only exists while this exact,
                # non-pooled connection remains healthy.  Reacquire from a
                # fresh connection after any loss; never reuse or assume it.
                while not stopping and lock_connection is None:
                    lock_connection = try_acquire_singleton(connector=connector)
                    if lock_connection is None:
                        log.info('whisper_lock_waiting')
                        sleep(LOCK_RETRY_SECONDS)
                    else:
                        log.info('whisper_lock_acquired')
                if stopping:
                    break
                try:
                    assert_singleton_ownership(lock_connection)
                    broker_connection = connect()
                    broker_channel = broker_connection.channel()
                    declare(broker_channel)
                    broker_channel.basic_qos(prefetch_count=1)

                    def assert_ownership():
                        assert_singleton_ownership(lock_connection)

                    def delivery(ch, method, props, body):
                        if stopping:
                            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                            return
                        assert_ownership()
                        handle_delivery(ch, method, body, executor, broker_connection, assert_ownership)

                    broker_channel.basic_consume(queue=QUEUE, auto_ack=False, on_message_callback=delivery)
                    broker_channel.start_consuming()
                except SingletonLockLost:
                    # The database releases the old session lock as the socket
                    # dies.  Close local resources and acquire again before
                    # accepting another Rabbit delivery.
                    close_singleton_connection(lock_connection)
                    lock_connection = None
                    if not stopping:
                        log.warning('Whisper singleton lock lost; reacquiring')
                except Exception:
                    if not stopping:
                        log.warning('Worker connection unavailable; reconnecting')
                finally:
                    close_broker_connection(broker_channel, broker_connection)
                    broker_channel = None
                    broker_connection = None
                if not stopping:
                    sleep(LOCK_RETRY_SECONDS)
    finally:
        whisper_service.close()
        # Shutdown order is intentional: Rabbit stops first so no new message
        # can start, then closing this dedicated connection releases the lock.
        close_broker_connection(broker_channel, broker_connection)
        close_singleton_connection(lock_connection)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    run()
