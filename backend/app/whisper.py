import logging
import multiprocessing
import threading

from .config import get_settings

log = logging.getLogger('kurukin.whisper')


def model_process(connection, config):
    """One lazy model per persistent child; IPC is local, no job queue."""
    model = None
    try:
        while True:
            path = connection.recv()
            try:
                if model is None:
                    from faster_whisper import WhisperModel
                    model = WhisperModel(config.whisper_model, device=config.whisper_device,
                                         compute_type=config.whisper_compute_type,
                                         cpu_threads=config.whisper_cpu_threads, num_workers=1,
                                         download_root=config.whisper_cache_dir)
                segments, info = model.transcribe(path, beam_size=5)
                text = ' '.join(' '.join(s.text.split()) for s in segments).strip()
                connection.send(('ok', {'text': text, 'language': info.language,
                                       'duration': info.duration, 'model': config.whisper_model}))
            except Exception:
                log.exception('Whisper inference failed')
                connection.send(('error', None))
    except EOFError:
        pass
    finally:
        connection.close()


class WhisperService:
    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._connection = None

    def close(self):
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(5)
                if self._process.is_alive():
                    self._process.kill()
            self._process.join()
            self._process.close()
            self._process = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def transcribe(self, path):
        if not self._lock.acquire(blocking=False):
            raise BlockingIOError('Whisper busy; retry later')
        try:
            config = get_settings()
            if self._process is None or not self._process.is_alive():
                self.close()
                context = multiprocessing.get_context('spawn')
                self._connection, child = context.Pipe()
                self._process = context.Process(target=model_process, args=(child, config), daemon=True)
                self._process.start()
                child.close()
            self._connection.send(str(path))
            if not self._connection.poll(config.whisper_timeout_seconds):
                self.close()  # Kill inference BEFORE the caller deletes the WAV.
                raise TimeoutError('Whisper timed out')
            status, result = self._connection.recv()
            if status != 'ok':
                raise RuntimeError('Whisper failed')
            return result
        except TimeoutError:
            raise
        except (EOFError, BrokenPipeError, OSError) as exc:
            self.close()
            raise RuntimeError('Whisper process unavailable') from exc
        finally:
            self._lock.release()


whisper_service = WhisperService()


def get_whisper():
    return whisper_service
