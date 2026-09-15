import multiprocessing
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app import whisper
from app.workers import whisper as worker


def fake_worker(connection, config):
    try:
        while True:
            path = connection.recv()
            if path == 'slow':
                time.sleep(60)
            connection.send(('ok', {'text': 'ok'}))
    except EOFError:
        pass
    finally:
        connection.close()


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(whisper, 'model_process', fake_worker)
    monkeypatch.setattr(whisper, 'get_settings', lambda:SimpleNamespace(whisper_timeout_seconds=1))
    service=whisper.WhisperService()
    yield service
    service.close()


def test_timeout_reaps_child_releases_lock_and_next_inference(service, monkeypatch):
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(TimeoutError):
        service.transcribe('slow')
    assert service._process is None and service._connection is None
    assert not service._lock.locked()
    assert {p.pid for p in multiprocessing.active_children()} == before
    # Recovery startup on a throttled container is independent of the forced 1s timeout.
    monkeypatch.setattr(whisper, 'get_settings', lambda:SimpleNamespace(whisper_timeout_seconds=5))
    assert service.transcribe('fast') == {'text':'ok'}
    pid=service._process.pid
    assert service.transcribe('fast') == {'text':'ok'}
    assert service._process.pid == pid
    service.close()
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_concurrency_one(service):
    errors=[]
    def run():
        try:
            service.transcribe('slow')
        except TimeoutError:
            errors.append('timeout')
    thread=threading.Thread(target=run)
    thread.start()
    deadline=time.monotonic()+2
    while not service._lock.locked() and time.monotonic()<deadline:
        time.sleep(.005)
    with pytest.raises(BlockingIOError):
        service.transcribe('fast')
    thread.join(5)
    assert not thread.is_alive() and errors==['timeout']
    assert not service._lock.locked()


class LockCursor:
    def __init__(self, result): self.result=result; self.calls=[]
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, query, params): self.calls.append((query, params))
    def fetchone(self): return (self.result,)


class LockConnection:
    def __init__(self, result): self.cursor_value=LockCursor(result); self.closed=False
    def cursor(self): return self.cursor_value
    def close(self): self.closed=True


def settings():
    class Url:
        host='postgres'; port=5432; database='kurukin_tiktok'; username='kurukin_tiktok'; password='test'
    return SimpleNamespace(resolve_database_url=lambda: Url())


def test_video_lock_owner_uses_dedicated_psycopg_connection():
    calls=[]; connection=LockConnection(True)
    def connector(**kwargs): calls.append(kwargs); return connection
    video_id = '00000000-0000-0000-0000-000000000001'
    assert worker.try_acquire_video_lock(video_id, settings(), connector) is connection
    assert not connection.closed
    assert calls == [{'host':'postgres','port':5432,'dbname':'kurukin_tiktok','user':'kurukin_tiktok',
                      'password':'test','application_name':worker.VIDEO_LOCK_APPLICATION_NAME,
                      'autocommit':True}]
    assert connection.cursor_value.calls == [('SELECT pg_try_advisory_lock(%s)', (1,))]


def test_same_video_lock_is_not_acquired_twice(monkeypatch):
    occupied=LockConnection(False); acquired=LockConnection(True); connections=iter([occupied, acquired])
    monkeypatch.setattr(worker, 'open_video_lock_connection', lambda *args, **kwargs: next(connections))
    video_id = '00000000-0000-0000-0000-000000000001'
    assert worker.try_acquire_video_lock(video_id) is None
    assert occupied.closed
    assert worker.try_acquire_video_lock(video_id) is acquired


def test_different_videos_have_different_lock_keys_and_release_cleanly():
    first=LockConnection(True); second=LockConnection(True)
    first_id, second_id = '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002'
    assert worker.video_lock_key(first_id) != worker.video_lock_key(second_id)
    assert worker.try_acquire_video_lock(first_id, settings(), lambda **kwargs:first) is first
    worker.close_video_lock_connection(first)
    assert first.closed
    assert worker.try_acquire_video_lock(second_id, settings(), lambda **kwargs:second) is second


class BrokerChannel:
    def __init__(self, events, trigger_stop):
        self.events = events
        self.trigger_stop = trigger_stop

    def basic_qos(self, **kwargs): pass
    def basic_consume(self, **kwargs): pass

    def start_consuming(self):
        self.trigger_stop()

    def stop_consuming(self):
        self.events.append('rabbit_stopped')


class BrokerConnection:
    def __init__(self, channel, events):
        self.channel_value = channel
        self.events = events
        self.is_open = True

    def channel(self): return self.channel_value

    def close(self):
        self.events.append('rabbit_closed')
        self.is_open = False


def test_sigterm_stops_rabbit_without_global_worker_lock(monkeypatch):
    events = []
    handlers = {}

    def install(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    channel = BrokerChannel(events, lambda: handlers[worker.signal.SIGTERM](worker.signal.SIGTERM, None))
    broker = BrokerConnection(channel, events)
    monkeypatch.setattr(worker.signal, 'signal', install)
    monkeypatch.setattr(worker, 'connect', lambda: broker)
    monkeypatch.setattr(worker, 'declare', lambda _: None)
    monkeypatch.setattr(worker.whisper_service, 'close', Mock())
    worker.run(sleep=lambda _: pytest.fail('shutdown must not sleep'))

    assert events == ['rabbit_stopped', 'rabbit_stopped', 'rabbit_closed']
