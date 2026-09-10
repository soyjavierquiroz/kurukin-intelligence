import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.config import get_settings
from app.models import Base

@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIO_QUEUE_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("AUDIO_QUEUE_MIN_FREE_BYTES", "0")
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('DATABASE_URL_FILE', raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

@pytest.fixture
def db():
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)  # isolated test database only
    with Session(engine) as session:
        yield session
    engine.dispose()
