from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from app.config import Settings, DatabaseConfigurationError
from app import main, whisper

URL = 'postgresql+psycopg://kurukin_tiktok:private-test-password@postgres:5432/kurukin_tiktok'


def test_file_priority(tmp_path, monkeypatch):
    p = tmp_path/'secret'; p.write_text(URL+'\n')
    monkeypatch.setenv('DATABASE_URL_FILE',str(p)); monkeypatch.setenv('DATABASE_URL','invalid')
    assert Settings().resolve_database_url().password == 'private-test-password'


def test_env_fallback(monkeypatch):
    monkeypatch.setenv('DATABASE_URL',URL)
    assert Settings().resolve_database_url().database == 'kurukin_tiktok'


@pytest.mark.parametrize('value', ['', 'invalid-private-test-password', URL.replace('kurukin_tiktok','n8n_v2_data'),
    URL+'?host=another', URL.replace('+psycopg',''), URL.replace(':5432',':9999')])
def test_malformed_secret(tmp_path, caplog, value):
    p=tmp_path/'secret'; p.write_text(value)
    config=Settings(database_url_file=str(p), database_url=URL)
    with pytest.raises(DatabaseConfigurationError) as exc:
        config.resolve_database_url()
    assert 'private-test-password' not in str(exc.value)+repr(config)+caplog.text


def test_missing_file_no_fallback(tmp_path):
    with pytest.raises(DatabaseConfigurationError):
        Settings(database_url_file=str(tmp_path/'missing'), database_url=URL).resolve_database_url()


def test_missing_configuration():
    with pytest.raises(DatabaseConfigurationError):
        Settings().resolve_database_url()


def test_alias_priority(monkeypatch):
    monkeypatch.setenv('TOP_TRANSCRIPTS','9')
    assert Settings().initial_enrichment_budget == 9
    monkeypatch.setenv('INITIAL_ENRICHMENT_BUDGET','4')
    assert Settings().initial_enrichment_budget == 4


def test_concurrency_configuration():
    with pytest.raises(ValueError):
        Settings(whisper_concurrency=2)


def test_health_no_db_or_whisper(monkeypatch):
    def forbidden():
        pytest.fail('Health accessed DB or Whisper')
    monkeypatch.setattr(main,'get_engine',forbidden)
    monkeypatch.setattr(whisper.whisper_service,'transcribe',forbidden)
    with TestClient(main.app) as client:
        assert client.get('/health').json() == {'status':'ok'}


def test_ready_select_one(monkeypatch):
    engine=MagicMock()
    monkeypatch.setattr(main,'get_engine',lambda:engine)
    response=TestClient(main.app).get('/ready')
    assert response.json()=={'status':'ready','database':'ok'}
    assert str(engine.connect.return_value.__enter__.return_value.execute.call_args.args[0]) == 'SELECT 1'
    assert whisper.whisper_service._process is None


def test_ready_failure_secret_never_logged(monkeypatch,caplog):
    def fail():
        raise RuntimeError(URL)
    monkeypatch.setattr(main,'get_engine',fail)
    response=TestClient(main.app).get('/ready')
    assert response.status_code==503
    assert 'private-test-password' not in response.text+caplog.text


def test_concurrency_env_one_and_file_priority(tmp_path, monkeypatch):
    monkeypatch.setenv('WHISPER_CONCURRENCY', '1')
    p = tmp_path / 'secret'
    p.write_text(URL)
    monkeypatch.setenv('DATABASE_URL_FILE', str(p))
    monkeypatch.setenv('DATABASE_URL', 'invalid')
    settings = Settings()
    assert settings.whisper_concurrency == 1
    assert type(settings.whisper_concurrency) is int
    assert settings.resolve_database_url().password == 'private-test-password'


@pytest.mark.parametrize('value', [1, '1'])
def test_concurrency_exact_one(value):
    assert Settings(whisper_concurrency=value).whisper_concurrency == 1


@pytest.mark.parametrize('value', [0, '0', 2, '2', -1, '01', 'true', '1.0', None, True, 1.0, ' 1'])
def test_concurrency_rejects_ambiguous_or_non_one(value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(whisper_concurrency=value)


@pytest.mark.parametrize('value', ['2', '0', 'true', '1.0', '01'])
def test_concurrency_invalid_env(value, monkeypatch):
    from pydantic import ValidationError
    monkeypatch.setenv('WHISPER_CONCURRENCY', value)
    with pytest.raises(ValidationError):
        Settings()
