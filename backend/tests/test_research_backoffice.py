import io
import json
import zipfile
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import admin
from app.config import get_settings
from app.main import app
from app.models import Analysis, Channel, Transcript, Video, VideoSnapshot


def make_channel(db, username='creator', nickname='Creator', author_id='stable-creator'):
    channel = Channel(platform='tiktok', tiktok_user_id=author_id, username=username, nickname=nickname)
    db.add(channel); db.flush()
    analysis = Analysis(channel_id=channel.id, status='transcribed', video_count=4,
                        median_views=Decimal('100'), requested_transcripts=0)
    db.add(analysis); db.flush()
    return channel, analysis


def make_video(db, channel, analysis, number, views, transcript=None, duration=30.0):
    video = Video(channel_id=channel.id, tiktok_id=str(7483961220622503000 + number), author=channel.username,
                  nickname=channel.nickname, caption=f'Caption {number}',
                  published_at=datetime(2026, 1, number, tzinfo=timezone.utc), duration=duration,
                  url=f'https://www.tiktok.com/@{channel.username}/video/{7483961220622503000 + number}')
    db.add(video); db.flush()
    db.add(VideoSnapshot(analysis_id=analysis.id, video_id=video.id, views=views, likes=views // 10,
                          comments=views // 100, shares=views // 200, favorites=views // 100,
                          like_rate=Decimal('.1'), comment_rate=Decimal('.01'), share_rate=Decimal('.005'),
                          favorite_rate=Decimal('.01'), engagement_rate=Decimal('.125'),
                          outlier_score=Decimal(str(views / 100)), overall_rank=number,
                          transcription_rank=None, transcript_eligible=True, transcript_skip_reason=None))
    if transcript is not None:
        db.add(Transcript(video_id=video.id, text=transcript, language='es', duration=duration, model='small'))
    db.flush()
    return video


def corpus(db):
    channel, analysis = make_channel(db)
    videos = [make_video(db, channel, analysis, index, views, f'Transcript {index}' if index < 3 else None)
              for index, views in enumerate((100, 100, 100, 1000), start=1)]
    db.commit()
    return channel, videos


def test_global_corpus_aggregates_priority_and_empty_channel(db, monkeypatch):
    monkeypatch.setenv('ENRICHMENT_MIN_OUTLIER_SCORE', '2')
    get_settings.cache_clear()
    channel, videos = corpus(db)
    empty, _analysis = make_channel(db, 'empty', 'Empty', 'stable-empty')
    db.commit()

    summary = admin._summary_for_channel(db, channel.id)
    empty_summary = admin._summary_for_channel(db, empty.id)

    assert summary['total_videos'] == 4
    assert summary['transcripts_total'] == 2
    assert summary['priority_count'] == 1
    assert summary['priority_transcripts'] == 0
    assert summary['missing_priority'] == 1
    assert summary['status'] == 'PARTIAL'
    assert empty_summary['total_videos'] == 0
    assert empty_summary['status'] == 'NO_CORPUS'


def test_global_corpus_search(db):
    corpus(db)
    make_channel(db, 'another_creator', 'Another', 'another')
    db.commit()
    ids, total = admin._channels_page(db, 'another', 1)
    assert total == 1
    assert len(ids) == 1
    assert db.get(Channel, ids[0]).username == 'another_creator'


def test_import_dry_run_classifications_and_idempotency(db):
    channel, videos = corpus(db)
    other, other_analysis = make_channel(db, 'other', 'Other', 'stable-other')
    other_video = make_video(db, other, other_analysis, 9, 500)
    # Existing global transcript must never be overwritten.
    existing_id = videos[0].tiktok_id
    new_id = videos[3].tiktok_id
    body = '\n'.join((
        json.dumps({'video_id': new_id, 'transcript': 'Imported body', 'language': 'es'}),
        json.dumps({'video_id': existing_id, 'transcript': 'Replace me'}),
        json.dumps({'video_id': '9999999999999999999', 'transcript': 'Unknown'}),
        json.dumps({'video_id': other_video.tiktok_id, 'transcript': 'Wrong channel'}),
        json.dumps({'video_id': new_id, 'transcript': 'Duplicate'}),
        '{not json',
        json.dumps({'video_id': videos[1].tiktok_id, 'transcript': '   '}),
    )).encode()
    counts, rejected, rows = admin.dry_run_import(db, channel.id, body)
    assert counts == {'total': 7, 'matched': 2, 'new': 1, 'already_existing': 1,
                      'not_found': 1, 'channel_mismatch': 1, 'invalid': 2, 'duplicates': 1}
    assert rows[0]['model'] == 'manual-ai-import-v1'
    assert {row['status'] for row in rejected} == {'ALREADY_EXISTS', 'VIDEO_NOT_FOUND', 'CHANNEL_MISMATCH', 'DUPLICATE_IN_FILE', 'INVALID'}

    token = admin._store_pending_import(admin.PendingImport(channel.id, rows, rejected, 1e20))
    admin.import_confirm(channel.id, token, db=db)
    imported = db.get(Video, videos[3].id)
    assert db.scalar(select(Transcript).where(Transcript.video_id == imported.id)).text == 'Imported body'
    assert db.scalar(select(Transcript).where(Transcript.video_id == videos[0].id)).text == 'Transcript 1'
    assert db.scalar(select(Transcript).where(Transcript.video_id == imported.id)).model == 'manual-ai-import-v1'

    repeated, _rejected, repeated_rows = admin.dry_run_import(db, channel.id, body)
    assert repeated['new'] == 0
    assert repeated_rows == []
    assert admin._summary_for_channel(db, channel.id)['transcripts_total'] == 3


def test_import_rejects_bad_utf8_and_transcript_too_long(db):
    channel, videos = corpus(db)
    with pytest.raises(HTTPException, match='UTF-8'):
        admin.dry_run_import(db, channel.id, b'\xff')
    counts, _rejected, rows = admin.dry_run_import(
        db, channel.id, json.dumps({'video_id': videos[3].tiktok_id,
                                    'transcript': 'x' * (admin.IMPORT_MAX_TRANSCRIPT_CHARS + 1)}).encode())
    assert counts['invalid'] == 1
    assert rows == []


def test_research_pack_selection_zip_and_partitioning(db, monkeypatch):
    monkeypatch.setenv('ENRICHMENT_MIN_OUTLIER_SCORE', '2')
    get_settings.cache_clear()
    channel, videos = corpus(db)
    # Resolve the priority video, then recommended exports exactly that corpus.
    db.add(Transcript(video_id=videos[3].id, text='Winner transcript ' * 20,
                      language='es', duration=30, model='manual-ai-import-v1'))
    db.commit()
    data = admin._summary_for_channel(db, channel.id)
    assert len(admin._selection(data, 'recommended')) == 1
    assert len(admin._selection(data, 'all')) == 3
    assert len(admin._selection(data, 'top25')) == 3
    monkeypatch.setattr(admin, 'TRANSCRIPT_PART_CHARS', 200)
    output = admin.research_pack(data, 'all')
    with zipfile.ZipFile(io.BytesIO(output)) as archive:
        names = set(archive.namelist())
        assert {'README.md', 'manifest.json', 'videos.jsonl'} <= names
        assert {'transcripts_001.md', 'transcripts_002.md'} <= names
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['schema'] == 'kurukin-research-pack-v1'
        assert manifest['selection'] == {'mode': 'all', 'video_count': 3}
        records = [json.loads(line) for line in archive.read('videos.jsonl').decode().splitlines()]
        assert len(records) == 3 and all(record['video_id'] for record in records)
        markdown = ''.join(archive.read(name).decode() for name in manifest['transcript_parts'])
        assert videos[3].tiktok_id in markdown
        assert 'Rabbit' not in archive.read('videos.jsonl').decode()
        assert 'audio_path' not in archive.read('videos.jsonl').decode()


@pytest.mark.parametrize('preset', list(admin.PROMPT_PRESETS))
def test_prompt_presets_include_evidence_rules(db, preset):
    channel, _videos = corpus(db)
    prompt = admin.generated_prompt(channel, preset, '<business>', 'growth', 'Use Spanish')
    assert '<business>' in prompt
    assert 'video_id' in prompt
    assert 'Do not invent examples' in prompt
    assert 'Creator claims are not verified facts' in prompt
    assert 'transcripts may contain' in prompt.lower()


def test_historical_prompt_and_file_backed_basic_auth(db, monkeypatch, tmp_path):
    username = tmp_path / 'username'; password = tmp_path / 'password'
    username.write_text('admin'); password.write_text('correct horse battery staple')
    monkeypatch.setenv('ADMIN_USERNAME_FILE', str(username))
    monkeypatch.setenv('ADMIN_PASSWORD_FILE', str(password))
    get_settings.cache_clear()
    admin.require_admin(HTTPBasicCredentials(username='admin', password='correct horse battery staple'))
    with pytest.raises(HTTPException) as failure:
        admin.require_admin(HTTPBasicCredentials(username='admin', password='wrong'))
    assert failure.value.status_code == 401
    assert failure.value.headers['WWW-Authenticate'] == 'Basic'
    assert 'metadata.json' in admin.HISTORICAL_IMPORT_PROMPT
    assert 'JSONL only' in admin.HISTORICAL_IMPORT_PROMPT


def test_admin_html_route_requires_basic_auth(db, monkeypatch, tmp_path):
    username = tmp_path / 'username'; password = tmp_path / 'password'
    username.write_text('admin'); password.write_text('pass')
    monkeypatch.setenv('ADMIN_USERNAME_FILE', str(username))
    monkeypatch.setenv('ADMIN_PASSWORD_FILE', str(password))
    get_settings.cache_clear()

    def test_db():
        yield db

    app.dependency_overrides[admin.get_db] = test_db
    try:
        client = TestClient(app)
        assert client.get('/admin/research').status_code == 401
        response = client.get('/admin/research', auth=('admin', 'pass'))
        assert response.status_code == 200
        assert 'Kurukin Internal Research' in response.text
    finally:
        app.dependency_overrides.clear()
