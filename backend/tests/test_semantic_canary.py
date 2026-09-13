import importlib.util
from pathlib import Path

from tests.test_semantic_viral_dna import FakeProvider, add_transcript, make_video


ROOT = Path(__file__).resolve().parents[1]


def load_canary_module():
    spec = importlib.util.spec_from_file_location(
        'semantic_viral_dna_canary_test', ROOT / 'ops' / 'semantic_viral_dna_canary.py'
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_canary_is_closed_to_the_approved_three_ids():
    canary = load_canary_module()
    assert tuple(map(str, canary.CANARY_VIDEO_IDS)) == (
        '3073174b-3919-457c-9dab-74d1e0316225',
        '87f2fd92-b2e6-434b-8a25-9cdbcf275849',
        'f141413b-7423-4d76-b309-04c9c44b9e5f',
    )


def test_canary_uses_existing_global_transcripts_and_is_idempotent(db, monkeypatch):
    canary = load_canary_module()
    videos = [make_video(db) for _ in range(3)]
    for video in videos:
        add_transcript(db, video)
    monkeypatch.setattr(canary, 'CANARY_VIDEO_IDS', tuple(video.id for video in videos))
    monkeypatch.setattr(canary, 'require_database_revision', lambda session: None)
    provider = FakeProvider()

    first = canary.run_canary(db, provider)
    assert first == {'completed': 3, 'unchanged': 0, 'failed': 0, 'skipped_no_transcript': 0}
    assert len(provider.calls) == 3

    second = canary.run_canary(db, provider)
    assert second == {'completed': 0, 'unchanged': 3, 'failed': 0, 'skipped_no_transcript': 0}
    assert len(provider.calls) == 3


def test_canary_aborts_before_provider_work_if_a_transcript_is_missing(db, monkeypatch):
    canary = load_canary_module()
    videos = [make_video(db) for _ in range(3)]
    for video in videos[:2]:
        add_transcript(db, video)
    monkeypatch.setattr(canary, 'CANARY_VIDEO_IDS', tuple(video.id for video in videos))
    monkeypatch.setattr(canary, 'require_database_revision', lambda session: None)
    provider = FakeProvider()

    try:
        canary.run_canary(db, provider)
    except RuntimeError as error:
        assert str(error) == 'semantic_canary_transcript_not_found'
    else:
        raise AssertionError('missing transcript should abort the canary')
    assert provider.calls == []
