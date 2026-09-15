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
from ..audio_storage import AudioStorageError, get_audio_storage, parse_minio_uri
from ..jobs import validate_audio
from ..models import AudioAssessment, Transcript, TranscriptionJob, Video, now
from ..queue import QUEUE, connect, declare
from ..whisper import whisper_service
from ..yamnet import CLASSIFIER, VERSION, yamnet_service

log = logging.getLogger('kurukin.worker')
VIDEO_LOCK_APPLICATION_NAME = 'kurukin-tiktok-whisper-video-lock'
LOCK_RETRY_SECONDS = 5


def video_lock_key(video_id):
    """Stable signed bigint suitable for PostgreSQL's session advisory lock."""
    value = UUID(str(video_id)).int & ((1 << 64) - 1)
    return value - (1 << 64) if value >= (1 << 63) else value


def open_video_lock_connection(settings=None, connector=psycopg.connect):
    """A non-pooled session owns one video lock during inference only."""
    settings = settings or get_settings()
    url = settings.resolve_database_url()
    return connector(host=url.host, port=url.port, dbname=url.database,
                     user=url.username, password=url.password,
                     application_name=VIDEO_LOCK_APPLICATION_NAME,
                     autocommit=True)


def try_acquire_video_lock(video_id, settings=None, connector=psycopg.connect):
    connection = open_video_lock_connection(settings, connector)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', (video_lock_key(video_id),))
            acquired = cursor.fetchone()[0]
        if acquired:
            return connection
    except Exception:
        connection.close()
        raise
    connection.close()
    return None


def close_video_lock_connection(connection):
    """Closing the owning session releases its video advisory lock."""
    if connection is None:
        return
    try:
        if not connection.closed:
            connection.close()
    finally:
        log.info('whisper_video_lock_released')


def close_broker_connection(channel, connection):
    """Stop deliveries before closing Rabbit, leaving any unacked work requeued."""
    if channel is not None:
        try:
            channel.stop_consuming()
        except Exception:
            pass
    if connection is not None and connection.is_open:
        connection.close()


def delete_audio(job, settings=None, storage=None):
    try:
        (storage or get_audio_storage(settings)).delete(job)
        job.audio_path = None
        job.last_error_code = None
    except (AudioStorageError, OSError, ValueError):
        job.last_error_code = 'cleanup_pending'


def process_job(db, job_id, video_id, service=whisper_service, classifier=yamnet_service, settings=None,
                audio_object_key=None, storage=None):
    settings = settings or get_settings()
    # Tests may inject inference-only settings; storage configuration remains
    # process-wide and is resolved independently from Whisper tuning.
    storage = storage or get_audio_storage()
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
        delete_audio(job, settings, storage)
        db.commit()
        return 'ack'
    if job.status in ('reserved', 'expired', 'failed', 'skipped'):
        db.commit()
        return 'ack'
    if audio_object_key is not None:
        try:
            _bucket, stored_key = parse_minio_uri(job.audio_path)
            if stored_key != audio_object_key:
                raise AudioStorageError('Message audio object key mismatch')
        except AudioStorageError:
            db.rollback()
            return 'reject'
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
        if attempts > settings.transcription_max_attempts:
            raise ValueError('Attempts exhausted')
        with storage.materialize(job, audio_object_key) as path:
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
                    delete_audio(job, settings, storage); db.commit()
                    return 'ack'
            db.commit()
            result = service.transcribe(path)
        if not result.get('text', '').strip():
            video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
            job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id).with_for_update())
            job.status = 'skipped'
            job.skip_reason = 'no_speech'
            job.completed_at = now()
            job.last_error_code = None
            video.enrichment_status = 'skipped'
            video.enrichment_lease_until = None
            db.commit()  # Terminal empty-transcript skip is durable before cleanup/ACK.
            delete_audio(job, settings, storage)
            db.commit()
            return 'ack'
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
        error_code = 'whisper_timeout' if isinstance(exc, TimeoutError) else 'transcription_failed'
        log.warning('transcription_job_failure job_id=%s video_id=%s attempt=%s exception_class=%s error_code=%s',
                    job_id, video_id, attempts, type(exc).__name__, error_code)
        db.rollback()
        video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
        job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id).with_for_update())
        if db.scalar(select(Transcript.id).where(Transcript.video_id == video_id)):
            job.status = 'completed'
            job.completed_at = job.completed_at or now()
            video.enrichment_status = 'completed'
            video.enrichment_lease_until = None
            db.commit()
            delete_audio(job, settings, storage)
            db.commit()
            return 'ack'
        job.last_error_code = error_code
        terminal = job.attempts >= settings.transcription_max_attempts
        job.status = 'failed' if terminal else 'queued'
        video.enrichment_status = job.status
        job.failed_at = now() if terminal else None
        if not terminal:
            job.queued_at = now()
        db.commit()
        return 'ack' if terminal else 'retry'
    delete_audio(job, settings, storage)
    db.commit()  # Cleanup successful or durably recorded before ACK.
    return 'ack'


def decode(body):
    if len(body) > 256:
        raise ValueError('Message too large')
    message = json.loads(body)
    if set(message) not in ({'job_id', 'video_id'}, {'job_id', 'video_id', 'audio_object_key'}):
        raise ValueError('Invalid message fields')
    key = message.get('audio_object_key')
    if key is not None:
        from ..audio_storage import validate_object_key
        validate_object_key(key)
    return UUID(message['job_id']), UUID(message['video_id']), key


def handle_delivery(channel, method, body, executor, connection, delivery_check=lambda: None,
                    lock_acquirer=None):
    try:
        job_id, video_id, audio_object_key = decode(body)
    except (ValueError, TypeError, KeyError, AudioStorageError):
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return
    def work():
        lock_connection = lock_acquirer(video_id) if lock_acquirer else None
        if lock_acquirer and lock_connection is None:
            return 'retry'
        try:
            with Session(get_engine()) as db:
                return process_job(db, job_id, video_id, whisper_service,
                                   audio_object_key=audio_object_key)
        finally:
            close_video_lock_connection(lock_connection)
    future = executor.submit(work)
    while not future.done():
        delivery_check()
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
                try:
                    broker_connection = connect()
                    broker_channel = broker_connection.channel()
                    declare(broker_channel)
                    broker_channel.basic_qos(prefetch_count=1)

                    def delivery(ch, method, props, body):
                        if stopping:
                            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                            return
                        handle_delivery(ch, method, body, executor, broker_connection,
                                        lock_acquirer=lambda video_id: try_acquire_video_lock(video_id, connector=connector))

                    broker_channel.basic_consume(queue=QUEUE, auto_ack=False, on_message_callback=delivery)
                    broker_channel.start_consuming()
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
        # Rabbit stops first so no new message can start; each video lock is
        # owned by its delivery thread and closes when that delivery ends.
        close_broker_connection(broker_channel, broker_connection)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    run()
