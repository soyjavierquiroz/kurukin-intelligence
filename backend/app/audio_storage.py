"""Durable temporary audio backends; stored locations are never client supplied."""
from contextlib import contextmanager
from datetime import timezone
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import UUID

from .config import get_settings

OBJECT_PREFIX = 'transcription/v1/'
OBJECT_RE = re.compile(r'^transcription/v1/([0-9a-f-]{36})\.wav$')
BUCKET_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$')


class AudioStorageError(RuntimeError):
    pass


def object_key(job_id):
    return f'{OBJECT_PREFIX}{UUID(str(job_id))}.wav'


def minio_uri(bucket, key):
    validate_bucket(bucket)
    validate_object_key(key)
    return f'minio://{bucket}/{key}'


def parse_minio_uri(value):
    try:
        uri = urlsplit(value or '')
        if (uri.scheme != 'minio' or not uri.hostname or uri.username or uri.password or uri.port or
                uri.query or uri.fragment):
            raise ValueError()
        key = uri.path.lstrip('/')
        validate_bucket(uri.hostname)
        validate_object_key(key)
        return uri.hostname, key
    except Exception:
        raise AudioStorageError('Invalid MinIO audio location') from None


def validate_object_key(key):
    match = OBJECT_RE.fullmatch(key or '')
    if match is None:
        raise AudioStorageError('Invalid audio object key')
    UUID(match.group(1))
    return key


def validate_bucket(bucket):
    if BUCKET_RE.fullmatch(bucket or '') is None:
        raise AudioStorageError('Invalid MinIO bucket')
    return bucket


def local_audio_path(job, settings=None):
    settings = settings or get_settings()
    expected = Path(settings.audio_queue_dir) / f'{UUID(str(job.id))}.wav'
    if job.audio_path is not None and job.audio_path != str(expected):
        raise AudioStorageError('Invalid local audio path')
    if expected.is_symlink():
        raise AudioStorageError('Unsafe audio path')
    return expected


class AudioStorage:
    def __init__(self, settings=None, client=None):
        self.settings = settings or get_settings()
        self.backend = getattr(self.settings, 'audio_storage_backend', 'local')
        self.client = client

    def _client(self):
        if self.client is not None:
            return self.client
        try:
            import boto3
            from botocore.config import Config
            access_key, secret_key = self.settings.resolve_minio_credentials()
            self.client = boto3.client('s3', endpoint_url=self.settings.minio_endpoint,
                                       aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                                       region_name='us-east-1', config=Config(s3={'addressing_style': 'path'}))
            return self.client
        except AudioStorageError:
            raise
        except Exception:
            raise AudioStorageError('MinIO client unavailable') from None

    @property
    def bucket(self):
        return getattr(self.settings, 'minio_bucket', None)

    def put(self, job_id, data):
        if self.backend != 'minio':
            raise AudioStorageError('MinIO storage is not enabled')
        key = object_key(job_id)
        try:
            client = self._client()
            client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType='audio/wav')
            head = client.head_object(Bucket=self.bucket, Key=key)
            if head.get('ContentLength') != len(data):
                raise AudioStorageError('MinIO object size mismatch')
            return minio_uri(self.bucket, key)
        except AudioStorageError:
            raise
        except Exception:
            raise AudioStorageError('MinIO upload failed') from None

    def _read(self, bucket, key):
        try:
            response = self._client().get_object(Bucket=bucket, Key=key)
            body = response['Body']
            try:
                return body.read()
            finally:
                body.close()
        except Exception:
            raise AudioStorageError('MinIO download failed') from None

    def read_location(self, location):
        if location.startswith('minio://'):
            bucket, key = parse_minio_uri(location)
            return self._read(bucket, key)
        return Path(location).read_bytes()

    def exists_key(self, key):
        try:
            self._client().head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def recovery_location(self, job):
        if self.backend == 'minio' and self.exists_key(object_key(job.id)):
            return minio_uri(self.bucket, object_key(job.id))
        return str(local_audio_path(job, self.settings))

    def _prepare_scratch(self):
        root = Path(getattr(self.settings, 'audio_scratch_dir', '/tmp/kurukin-audio'))
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise AudioStorageError('Unsafe audio scratch directory')
        return root

    @contextmanager
    def materialize(self, job, message_key=None):
        location = job.audio_path
        if not location or not location.startswith('minio://'):
            yield local_audio_path(job, self.settings)
            return
        bucket, key = parse_minio_uri(location)
        if message_key is not None and message_key != key:
            raise AudioStorageError('Message audio object key mismatch')
        root = self._prepare_scratch()
        name = f'{UUID(str(job.id))}.wav'
        path = root / name
        fd = None
        directory = None
        try:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(existing.st_mode):
                    raise AudioStorageError('Unsafe audio scratch file')
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, 'wb') as stream:
                fd = None
                stream.write(self._read(bucket, key))
                stream.flush()
                os.fsync(stream.fileno())
            yield path
        finally:
            if fd is not None:
                os.close(fd)
            if directory is not None:
                os.close(directory)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def delete(self, job):
        location = job.audio_path
        try:
            if location and location.startswith('minio://'):
                bucket, key = parse_minio_uri(location)
                self._client().delete_object(Bucket=bucket, Key=key)
            else:
                local_audio_path(job, self.settings).unlink(missing_ok=True)
        except Exception:
            raise AudioStorageError('Audio cleanup failed') from None

    def orphan_locations(self, older_than):
        if self.backend != 'minio':
            return []
        try:
            result = []
            token = None
            while True:
                request = {'Bucket': self.bucket, 'Prefix': OBJECT_PREFIX}
                if token:
                    request['ContinuationToken'] = token
                response = self._client().list_objects_v2(**request)
                for entry in response.get('Contents', []):
                    key = entry.get('Key')
                    try:
                        validate_object_key(key)
                        modified = entry['LastModified'].replace(tzinfo=entry['LastModified'].tzinfo or timezone.utc)
                        if modified.timestamp() < older_than:
                            result.append(minio_uri(self.bucket, key))
                    except (KeyError, TypeError, ValueError, AudioStorageError):
                        continue
                if not response.get('IsTruncated'):
                    break
                token = response.get('NextContinuationToken')
                if not token:
                    break
            return result
        except Exception:
            return []

    def delete_location(self, location):
        bucket, key = parse_minio_uri(location)
        self._client().delete_object(Bucket=bucket, Key=key)

    def cleanup_scratch(self, older_than):
        try:
            root = self._prepare_scratch()
            for path in root.iterdir():
                if path.is_symlink() or not path.is_file() or path.suffix != '.wav':
                    continue
                UUID(path.stem)
                if path.stat().st_mtime < older_than:
                    path.unlink()
        except (OSError, ValueError, AudioStorageError):
            pass


def get_audio_storage(settings=None, client=None):
    return AudioStorage(settings, client)
