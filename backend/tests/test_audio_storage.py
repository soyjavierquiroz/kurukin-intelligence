from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.audio_storage import AudioStorage, AudioStorageError, minio_uri, object_key
from app.jobs import cleanup_audio, receive_audio
from app.models import TranscriptionJob, now
from app.services import create_analysis
from app.workers.whisper import decode, process_job
from test_corpus import payload, wav


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.events = []
        self.fail_put = False
        self.fail_delete = False

    def put_object(self, **kwargs):
        self.events.append(('put', kwargs['Key']))
        if self.fail_put:
            raise RuntimeError('unavailable')
        self.objects[kwargs['Key']] = bytes(kwargs['Body'])

    def head_object(self, **kwargs):
        self.events.append(('head', kwargs['Key']))
        if kwargs['Key'] not in self.objects:
            raise RuntimeError('missing')
        return {'ContentLength': len(self.objects[kwargs['Key']])}

    def get_object(self, **kwargs):
        self.events.append(('get', kwargs['Key']))
        return {'Body': BytesIO(self.objects[kwargs['Key']])}

    def delete_object(self, **kwargs):
        self.events.append(('delete', kwargs['Key']))
        if self.fail_delete:
            raise RuntimeError('unavailable')
        self.objects.pop(kwargs['Key'], None)

    def list_objects_v2(self, **kwargs):
        self.events.append(('list', kwargs['Prefix']))
        return {'Contents': [
            {'Key': key, 'LastModified': datetime.now(timezone.utc) - timedelta(hours=3)}
            for key in self.objects if key.startswith(kwargs['Prefix'])
        ]}


def minio_storage(tmp_path, s3):
    settings = SimpleNamespace(audio_storage_backend='minio', minio_bucket='kurukin-transcription-audio',
                               audio_scratch_dir=str(tmp_path/'scratch'))
    return AudioStorage(settings, client=s3)


def gate_settings():
    return SimpleNamespace(audio_gate_mode='observe', transcription_max_attempts=3, max_audio_mb=10)


@pytest.fixture
def received(db):
    analysis = create_analysis(db, payload((39.,), [100])); db.commit()
    receive_audio(analysis.id, '10000', wav(), db, lambda _job: None)
    return db.scalar(select(TranscriptionJob))


def classifier():
    return SimpleNamespace(classify=lambda _path: dict(
        classifier='yamnet', classifier_version='1', model_sha256='a'*64, classification='speech',
        speech_score=.9, music_score=.1, singing_score=.1, speech_patch_ratio=1., music_patch_ratio=0.,
        singing_patch_ratio=0., top_classes=[], processing_ms=1))


def test_minio_put_precedes_publish_and_persists_object_uri(db, tmp_path, monkeypatch):
    from app import jobs
    s3 = FakeS3(); storage = minio_storage(tmp_path, s3)
    monkeypatch.setattr(jobs, 'get_audio_storage', lambda: storage)
    analysis = create_analysis(db, payload((39.,), [100])); db.commit()
    published = []

    def publisher(job):
        published.append(job.id)
        assert s3.events[:2] == [('put', object_key(job.id)), ('head', object_key(job.id))]
        assert job.audio_path == minio_uri(storage.bucket, object_key(job.id))

    receive_audio(analysis.id, '10000', wav(), db, publisher)
    job = db.scalar(select(TranscriptionJob))
    assert published == [job.id] and job.status == 'queued'
    assert s3.objects[object_key(job.id)] == wav()


def test_minio_put_failure_never_marks_received_or_publishes(db, tmp_path, monkeypatch):
    from app import jobs
    s3 = FakeS3(); s3.fail_put = True
    monkeypatch.setattr(jobs, 'get_audio_storage', lambda: minio_storage(tmp_path, s3))
    analysis = create_analysis(db, payload((39.,), [100])); db.commit()
    published = []
    with pytest.raises(AudioStorageError):
        receive_audio(analysis.id, '10000', wav(), db, lambda job: published.append(job.id))
    job = db.scalar(select(TranscriptionJob))
    assert published == [] and job.status == 'reserved' and job.audio_received_at is None


def test_new_and_legacy_rabbit_payloads_are_accepted():
    job_id = '00000000-0000-0000-0000-000000000001'
    video_id = '00000000-0000-0000-0000-000000000002'
    legacy = decode(f'{{"job_id":"{job_id}","video_id":"{video_id}"}}'.encode())
    current = decode(f'{{"job_id":"{job_id}","video_id":"{video_id}","audio_object_key":"transcription/v1/{job_id}.wav"}}'.encode())
    assert legacy == (legacy[0], legacy[1], None)
    assert current[2] == f'transcription/v1/{job_id}.wav'


def test_minio_download_uses_private_scratch_and_cleans_on_success_and_failure(tmp_path):
    s3 = FakeS3(); storage = minio_storage(tmp_path, s3)
    job_id = '00000000-0000-0000-0000-000000000001'; key = object_key(job_id)
    s3.objects[key] = wav()
    job = SimpleNamespace(id=job_id, audio_path=minio_uri(storage.bucket, key))
    with storage.materialize(job, key) as path:
        assert path.read_bytes() == wav() and path.parent.stat().st_mode & 0o777 == 0o700
    assert not path.exists()
    with pytest.raises(RuntimeError):
        with storage.materialize(job, key) as path:
            raise RuntimeError('worker failure')
    assert not path.exists()


def test_remote_object_cleanup_after_completed_and_cleanup_pending_on_failure(db, received, tmp_path):
    s3 = FakeS3(); storage = minio_storage(tmp_path, s3)
    job = received; key = object_key(job.id); job.audio_path = minio_uri(storage.bucket, key); s3.objects[key] = wav(); db.commit()
    service = SimpleNamespace(transcribe=lambda _: {'text':'ok', 'language':'es', 'duration':8., 'model':'small'})
    assert process_job(db, job.id, job.video_id, service, classifier(), settings=gate_settings(),
                       audio_object_key=key, storage=storage) == 'ack'
    assert key not in s3.objects and job.audio_path is None

    # A separate completed remote job records cleanup_pending but still permits ACK.
    job.audio_path = minio_uri(storage.bucket, key); job.status = 'queued'; job.attempts = 0; s3.objects[key] = wav(); s3.fail_delete = True; db.commit()
    assert process_job(db, job.id, job.video_id, service, classifier(), settings=gate_settings(),
                       audio_object_key=key, storage=storage) == 'ack'
    assert job.last_error_code == 'cleanup_pending' and key in s3.objects


def test_minio_failed_retention_and_orphan_cleanup(db, received, tmp_path, monkeypatch):
    from app import jobs
    s3 = FakeS3(); storage = minio_storage(tmp_path, s3)
    monkeypatch.setattr(jobs, 'get_audio_storage', lambda: storage)
    job = received; key = object_key(job.id); job.audio_path = minio_uri(storage.bucket, key)
    job.status = 'failed'; job.failed_at = now() - timedelta(hours=25); s3.objects[key] = wav(); db.commit()
    orphan = object_key('00000000-0000-0000-0000-000000000099'); s3.objects[orphan] = wav()
    cleanup_audio(db)
    assert key not in s3.objects and orphan not in s3.objects and job.audio_path is None


def test_minio_payload_is_published_only_for_remote_location(monkeypatch):
    from app import queue
    connection = MagicMock(); monkeypatch.setattr(queue, 'connect', lambda: connection)
    job = SimpleNamespace(id='00000000-0000-0000-0000-000000000001', video_id='00000000-0000-0000-0000-000000000002',
                          priority=50, audio_path='minio://kurukin-transcription-audio/transcription/v1/00000000-0000-0000-0000-000000000001.wav')
    queue.publish_job(job)
    message = connection.channel.return_value.basic_publish.call_args.kwargs['body']
    assert b'"audio_object_key": "transcription/v1/00000000-0000-0000-0000-000000000001.wav"' in message
