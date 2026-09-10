import logging
import os
from pathlib import Path
import struct
import time
from contextlib import contextmanager
from uuid import UUID

log = logging.getLogger('kurukin.audio')
TEMP_ROOT = Path('/tmp/kurukin')
MIMES = {'audio/wav', 'audio/x-wav', 'audio/wave'}


def validate_wav(data: bytes):
    if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('Expected RIFF/WAVE')
    if struct.unpack_from('<I', data, 4)[0] + 8 != len(data):
        raise ValueError('RIFF size mismatch')
    offset, fmt, audio_size = 12, None, None
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('Truncated WAV chunk')
        kind, size = struct.unpack_from('<4sI', data, offset)
        start = offset + 8
        end = start + size
        if end > len(data):
            raise ValueError('Truncated WAV data')
        if kind == b'fmt ':
            if fmt is not None or size < 16:
                raise ValueError('Invalid fmt chunk')
            fmt = struct.unpack_from('<HHIIHH', data, start)
        elif kind == b'data':
            if audio_size is not None or fmt is None:
                raise ValueError('Invalid data chunk order or duplicate')
            audio_size = size
        offset = end + size % 2
    if offset != len(data) or fmt is None or not audio_size:
        raise ValueError('Missing fmt/data or padding')
    encoding, channels, rate, byte_rate, block, bits = fmt
    if encoding != 1:
        raise ValueError('Only uncompressed integer PCM WAV is accepted')
    if not 1 <= channels <= 2:
        raise ValueError('Channels must be 1 or 2')
    if not 8000 <= rate <= 96000:
        raise ValueError('Sample rate must be 8000..96000 Hz')
    if bits not in (8, 16, 24, 32):
        raise ValueError('Bits must be 8, 16, 24 or 32')
    if block != channels * bits // 8 or byte_rate != rate * block or audio_size % block:
        raise ValueError('Inconsistent PCM frame format')
    return {'sample_rate': rate, 'channels': channels, 'bits': bits,
            'duration': audio_size / byte_rate}


def prepare_root():
    TEMP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if TEMP_ROOT.is_symlink() or not TEMP_ROOT.is_dir():
        raise RuntimeError('Unsafe Kurukin temporary root')


@contextmanager
def temporary_wav(analysis_id: UUID, video_id: UUID, data: bytes):
    prepare_root()
    directory = TEMP_ROOT / str(UUID(str(analysis_id)))
    directory.mkdir(mode=0o700, exist_ok=True)
    # Directory descriptors + O_NOFOLLOW prevent redirecting writes through symlinks.
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = f'{UUID(str(video_id))}.wav'
    path = directory / name
    created = False
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=descriptor)
        created = True
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        log.info('temp_created path=%s bytes=%s', path, len(data))
        yield path
    finally:
        if created:
            os.unlink(name, dir_fd=descriptor)
            log.info('temp_deleted path=%s', path)
        os.close(descriptor)
        try:
            directory.rmdir()
        except OSError:
            pass


def cleanup_stale():
    prepare_root()
    cutoff = time.time() - 2 * 60 * 60
    # Only our UUID/UUID.wav layout; never recurse or follow symlinks.
    for directory in TEMP_ROOT.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            UUID(directory.name)
        except ValueError:
            continue
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix != '.wav':
                continue
            try:
                UUID(path.stem)
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    log.info('stale_temp_deleted path=%s', path)
            except (ValueError, FileNotFoundError):
                continue
        try:
            directory.rmdir()
        except OSError:
            pass
