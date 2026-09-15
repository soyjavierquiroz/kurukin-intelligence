"""Operational state, atomic inbox, and conservative recovery.
Lock order: inbox flock -> Video row -> Job row. Worker releases database locks during inference.
"""
from contextlib import contextmanager
from datetime import timedelta, timezone
import fcntl
import os
from pathlib import Path
import shutil
import time
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from .audio import validate_wav
from .audio_storage import AudioStorageError, get_audio_storage, local_audio_path
from .config import get_settings
from .models import Analysis, AnalysisAcquisition, Transcript, TranscriptionJob, Video, VideoSnapshot, now

INTERACTIVE = 100
NORMAL = 50
BACKGROUND = 10
ACTIVE = ('reserved', 'audio_received', 'queued', 'processing')


@contextmanager
def inbox_lock():
    root = Path(get_settings().audio_queue_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise RuntimeError('Unsafe inbox root')
    fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def capacity(db, root, incoming=0, new_job=False):
    config = get_settings()
    if config.audio_storage_backend == 'minio':
        used = db.scalar(select(func.coalesce(func.sum(TranscriptionJob.audio_size_bytes), 0))
                         .where(TranscriptionJob.status.in_(ACTIVE)))
        free = float('inf')
    else:
        used = sum(p.lstat().st_size for p in root.iterdir() if p.is_file() and not p.is_symlink())
        free = shutil.disk_usage(root).free
    count = db.scalar(select(func.count()).select_from(TranscriptionJob).where(TranscriptionJob.status.in_(ACTIVE)))
    if (used + incoming >= config.audio_queue_max_bytes or free - incoming <= config.audio_queue_min_free_bytes
            or count + int(new_job) > config.audio_queue_max_jobs
            or (not incoming and count >= config.audio_queue_max_jobs)):
        raise HTTPException(503, {'code': 'audio_queue_capacity', 'retryable': True})


def validate_audio(data):
    from .services import eligibility
    if len(data) > get_settings().max_audio_mb * 1024**2:
        raise HTTPException(413, 'Audio exceeds MAX_AUDIO_MB')
    try:
        fmt = validate_wav(data)
        if not eligibility(fmt['duration'])[0]:
            raise ValueError('WAV duration is not eligible for automatic transcription')
        return fmt
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def job_path(job):
    try:
        return local_audio_path(job)
    except AudioStorageError as exc:
        raise ValueError(str(exc)) from exc


def atomic_store(root, job, data):
    target = root / f'{job.id}.wav'
    part = root / f'{job.id}.part'
    created = False
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, target)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return target
    finally:
        if created:
            part.unlink(missing_ok=True)


def receive_audio(analysis_id, tiktok_id, data, db, publisher=None):
    from .services import eligibility, lease_active
    from .queue import publish_job
    publisher = publisher or publish_job
    with inbox_lock() as root:
        row = db.execute(select(VideoSnapshot, Video).join(Video, Video.id == VideoSnapshot.video_id)
            .where(VideoSnapshot.analysis_id == analysis_id, Video.tiktok_id == tiktok_id)
            .with_for_update(of=Video)).first()
        if row is None:
            raise HTTPException(404, 'Video not in analysis')
        snapshot, video = row
        job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.video_id == video.id).with_for_update())
        if db.scalar(select(Transcript.id).where(Transcript.video_id == video.id)):
            if job:
                job.status = 'completed'
                job.completed_at = job.completed_at or now()
            db.commit()
            return {'status': 'already_transcribed', 'video_id': str(video.id), 'job_id': str(job.id) if job else None,
                    'audio_received': bool(job and job.audio_received_at)}
        if not eligibility(video.duration)[0]:
            raise HTTPException(422, 'Video duration is not eligible for automatic transcription')
        acquisition = db.scalar(select(AnalysisAcquisition).where(
            AnalysisAcquisition.analysis_id == analysis_id,
            AnalysisAcquisition.video_id == video.id))
        if video.enrichment_analysis_id != analysis_id or acquisition is None:
            raise HTTPException(409, 'No acquisition reservation for this analysis')
        if job and job.status in ('audio_received', 'queued', 'processing', 'completed'):
            db.commit()
            return receipt(job)
        if not lease_active(video) or job is None or job.status != 'reserved':
            raise HTTPException(409, 'No active acquisition reservation')
        fmt = validate_audio(data)
        capacity(db, root, len(data))
        storage = get_audio_storage()
        location = storage.put(job.id, data) if storage.backend == 'minio' else str(atomic_store(root, job, data))
        job.audio_path = location
        job.audio_size_bytes = len(data)
        job.audio_duration = fmt['duration']
        job.audio_received_at = now()
        job.status = 'audio_received'
        job.queued_at = None
        video.enrichment_status = 'audio_received'
        # A crash before this commit is recovered from the UUID WAV by reconciliation.
        db.commit()
    enqueue(db, job.id, publisher)
    db.refresh(job)
    return receipt(job)


def receipt(job):
    return dict(status=job.status, video_id=str(job.video_id), job_id=str(job.id), audio_received=job.audio_received_at is not None)


def release_reserved_acquisition(analysis_id, tiktok_id, error_code, db):
    """Release only the still-owned, pre-audio reservation reported by the browser.

    An ignored response is deliberately successful: a duplicate report, a late
    report after audio arrived, or an unrelated analysis must never undo work.
    """
    row = db.execute(select(AnalysisAcquisition, Video)
        .join(Video, Video.id == AnalysisAcquisition.video_id)
        .where(AnalysisAcquisition.analysis_id == analysis_id, Video.tiktok_id == tiktok_id)
        .with_for_update(of=Video)).first()
    if row is None:
        db.commit()
        return {'released': False}
    _acquisition, video = row
    job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.video_id == video.id).with_for_update())
    if (job is None or job.status != 'reserved' or
            video.enrichment_analysis_id != analysis_id):
        db.commit()
        return {'released': False}
    job.status = 'expired'
    job.last_error_code = error_code
    job.updated_at = now()
    video.enrichment_status = 'expired'
    video.enrichment_analysis_id = None
    video.enrichment_lease_until = None
    db.commit()
    return {'released': True}


def enqueue(db, job_id, publisher):
    # Do not overwrite processing/completed if consumer beats publisher's DB update.
    job = db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id).with_for_update())
    if job is None or job.status != 'audio_received':
        db.commit()
        return False
    try:
        publisher(job)
    except Exception:
        job.last_error_code = 'publish_unavailable'
        db.commit()
        return False
    job.status = 'queued'
    job.queued_at = now()
    job.last_error_code = None
    db.commit()
    return True


def older(value, cutoff):
    return value is not None and value.replace(tzinfo=value.tzinfo or timezone.utc) < cutoff


def requeue_pending_jobs(db, publisher=None, limit=20):
    from .queue import publish_job
    publisher = publisher or publish_job
    with inbox_lock() as root:
        # Recover rename-before-commit; expire only reservations that have no durable WAV.
        ids = db.scalars(select(TranscriptionJob.id).where(TranscriptionJob.status == 'reserved')
                         .order_by(TranscriptionJob.updated_at).limit(limit)).all()
        for job_id in ids:
            job = db.get(TranscriptionJob, job_id)
            video = db.scalar(select(Video).where(Video.id == job.video_id).with_for_update())
            from .services import lease_active
            storage = get_audio_storage()
            location = storage.recovery_location(job)
            if location.startswith('minio://') or Path(location).exists():
                try:
                    data = storage.read_location(location)[:get_settings().max_audio_mb * 1024**2 + 1]
                    fmt = validate_audio(data)
                    job.audio_path = location
                    job.audio_size_bytes = len(data)
                    job.audio_duration = fmt['duration']
                    job.audio_received_at = now()
                    job.status = 'audio_received'
                    video.enrichment_status = 'audio_received'
                except HTTPException:
                    job.status = 'failed'
                    job.audio_path = location
                    job.failed_at = now()
                    job.last_error_code = 'invalid_recovered_audio'
            elif not lease_active(video):
                job.status = 'expired'
            job.updated_at = now()
        db.commit()
    ids = db.scalars(select(TranscriptionJob.id).where(TranscriptionJob.status == 'audio_received',
        TranscriptionJob.queued_at.is_(None)).order_by(TranscriptionJob.updated_at).limit(limit)).all()
    db.commit()
    return sum(enqueue(db, job_id, publisher) for job_id in ids)


def cleanup_audio(db):
    with inbox_lock() as root:
        storage = get_audio_storage()
        cutoff = now() - timedelta(hours=get_settings().failed_audio_retention_hours)
        remote_jobs = db.scalars(select(TranscriptionJob).where(
            TranscriptionJob.audio_path.like('minio://%'))).all()
        for job in remote_jobs:
            removable = job.status == 'completed' or (job.status == 'failed' and older(job.failed_at, cutoff))
            if not removable:
                continue
            try:
                storage.delete(job)
                job.audio_path = None
                if job.last_error_code == 'cleanup_pending':
                    job.last_error_code = None
            except AudioStorageError:
                job.last_error_code = 'cleanup_pending'
        for path in root.iterdir():
            if path.is_symlink() or path.suffix not in ('.wav', '.part'):
                continue
            try:
                job_id = UUID(path.stem)
            except ValueError:
                continue
            job = db.get(TranscriptionJob, job_id)
            try:
                stale = path.stat().st_mtime < time.time() - 7200
            except FileNotFoundError:
                continue
            removable = ((path.suffix == '.part' and stale) or
                         (path.suffix == '.wav' and ((job is None and stale) or
                          (job is not None and (job.status == 'completed' or
                           (job.status == 'failed' and older(job.failed_at, cutoff)))))))
            if removable:
                try:
                    path.unlink(missing_ok=True)
                    if job and path.suffix == '.wav':
                        job.audio_path = None
                        if job.last_error_code == 'cleanup_pending':
                            job.last_error_code = None
                except OSError:
                    if job:
                        job.last_error_code = 'cleanup_pending'
        db.commit()
    # Object listings are best-effort reconciliation only.  A fresh object may
    # exist after a crash between PUT and DB commit, so only old UUID objects
    # without an operational row are eligible.
    for location in storage.orphan_locations(time.time() - 7200):
        key_job_id = UUID(location.rsplit('/', 1)[-1][:-4])
        if db.get(TranscriptionJob, key_job_id) is None:
            try:
                storage.delete_location(location)
            except AudioStorageError:
                pass
    storage.cleanup_scratch(time.time() - 7200)
