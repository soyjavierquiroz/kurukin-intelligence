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


def test_first_singleton_owner_uses_dedicated_psycopg_connection():
    calls=[]; connection=LockConnection(True)
    def connector(**kwargs): calls.append(kwargs); return connection
    assert worker.try_acquire_singleton(settings(), connector) is connection
    assert not connection.closed
    assert calls == [{'host':'postgres','port':5432,'dbname':'kurukin_tiktok','user':'kurukin_tiktok',
                      'password':'test','application_name':worker.SINGLETON_APPLICATION_NAME,
                      'autocommit':True}]
    assert connection.cursor_value.calls == [('SELECT pg_try_advisory_lock(%s)', (worker.SINGLETON_LOCK_KEY,))]


def test_second_owner_waits_without_consumer_or_crash(monkeypatch):
    occupied=LockConnection(False); acquired=LockConnection(True); connections=iter([occupied, acquired])
    monkeypatch.setattr(worker, 'open_singleton_connection', lambda *args, **kwargs: next(connections))
    assert worker.try_acquire_singleton() is None
    assert occupied.closed
    assert worker.try_acquire_singleton() is acquired


def test_closing_dedicated_owner_frees_lock_without_touching_orm_pool():
    first=LockConnection(True); second=LockConnection(True)
    assert worker.try_acquire_singleton(settings(), lambda **kwargs:first) is first
    # This represents an independent ORM Session lifecycle; it never sees the lock connection.
    orm_connection=object()
    assert orm_connection is not first
    first.close()
    assert first.closed
    assert worker.try_acquire_singleton(settings(), lambda **kwargs:second) is second


class RunningLockConnection(LockConnection):
    def __init__(self, events):
        super().__init__(True)
        self.events = events

    def execute(self, query):
        assert query == 'SELECT 1'
        return SimpleNamespace(fetchone=lambda: (1,))

    def close(self):
        self.events.append('lock_closed')
        super().close()


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


def test_sigterm_stops_rabbit_then_closes_dedicated_lock_and_allows_next_worker(monkeypatch):
    events = []
    first = RunningLockConnection(events)
    second = LockConnection(True)
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
    connections = iter([first, second])
    monkeypatch.setattr(worker, 'open_singleton_connection', lambda *_, **__: next(connections))

    worker.run(sleep=lambda _: pytest.fail('shutdown must not sleep'))

    assert first.closed
    assert events.index('rabbit_closed') < events.index('lock_closed')
    # The first lifecycle has released its dedicated session, so a new worker
    # acquires with a new connection rather than a pooled/reused one.
    assert worker.try_acquire_singleton() is second


def test_lost_dedicated_connection_is_not_treated_as_owned():
    connection = LockConnection(True)
    connection.closed = True
    with pytest.raises(worker.SingletonLockLost):
        worker.assert_singleton_ownership(connection)


def test_singleton_connection_failure_is_closed():
    connection=LockConnection(True)
    def fail(*args, **kwargs): raise OSError('database unavailable')
    with pytest.raises(OSError): worker.try_acquire_singleton(settings(), fail)
    assert not connection.closed  # No connection was created to leak.
