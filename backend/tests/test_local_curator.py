import io
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import admin, local_curator as lc
from app.db import get_db
from app.main import app
from app.models import Analysis, Channel, Transcript, TranscriptionJob, Video, VideoSnapshot
from app.schemas import AnalysisInput
from app.services import create_analysis, global_ranked_videos

FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'local_curator'


def inputs(metrics=False):
    rows = json.loads((FIXTURE / 'metadata.json').read_text())
    if not metrics:
        for row in rows:
            row.pop('views', None)
            row.pop('likes', None)
    return rows, (FIXTURE / 'transcripciones.md').read_bytes()


def bundle(rows=None, markdown=None, metrics=False):
    sample, text = inputs(metrics)
    return lc.parse_bundle(json.dumps(sample if rows is None else rows).encode(), text if markdown is None else markdown)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_parse_native_fixture_and_hash():
    b = bundle(metrics=True)
    assert len(b.rows) == len(b.transcripts) == 3
    assert b.transcripts['7685102680854875424'] == 'Texto sintético uno. Se conserva la afirmación del creador.'
    assert b.rows[1]['likes'] is None
    rows, md = inputs(True)
    rows.reverse()
    rows[0]['media_file'] = 'totally-different.mp4'
    assert b.digest == bundle(rows, md).digest
    assert len(b.digest) == 64


def test_new_assets_idempotency_and_ranking_isolation(db):
    b = bundle()
    result = lc.preview(db, b)
    assert result['counts']['VIDEO_NEW'] == result['counts']['TRANSCRIPT_NEW'] == 3
    assert count(db, Channel) == count(db, Video) == count(db, Transcript) == 0
    lc.persist(db, b)
    db.commit()
    assert (count(db, Channel), count(db, Video), count(db, Transcript)) == (1, 3, 3)
    assert count(db, Analysis) == count(db, VideoSnapshot) == count(db, TranscriptionJob) == 0
    channel = db.scalar(select(Channel))
    assert global_ranked_videos(db, channel.id)[1] == []
    result = lc.persist(db, b)
    db.commit()
    assert result['counts']['VIDEO_NEW'] == result['counts']['TRANSCRIPT_NEW'] == 0
    assert result['counts']['TRANSCRIPT_EXISTS'] == 3
    assert count(db, Video) == 3
    assert db.scalar(select(Transcript)).model == 'local-curator-import-v1'


def test_existing_channel_new_videos_username_fallback_and_stable_attach(db):
    channel = Channel(platform='tiktok', username='larutadeldinero52', nickname='Canonical', tiktok_user_id=None)
    db.add(channel); db.commit()
    lc.persist(db, bundle()); db.commit()
    assert count(db, Channel) == 1
    assert channel.tiktok_user_id == '7663931081569797142'
    assert channel.nickname == 'Canonical'


def test_stable_id_wins_preserves_username_and_existing_metadata(db):
    lc.persist(db, bundle()); db.commit()
    channel = db.scalar(select(Channel))
    channel.username = 'canonical_handle'
    video = db.scalar(select(Video))
    video.caption = 'Canonical caption'
    transcript = db.scalar(select(Transcript).where(Transcript.video_id == video.id))
    transcript.text = 'Canonical transcript'
    db.commit()
    result = lc.persist(db, bundle()); db.commit()
    assert result['warnings']
    assert result['counts']['TRANSCRIPT_EXISTS_DIFFERENT'] == 1
    assert channel.username == 'canonical_handle'
    assert video.caption == 'Canonical caption'
    assert transcript.text == 'Canonical transcript'


def test_missing_transcript_and_metadata_only_and_orphan(db):
    rows, _ = inputs()
    md = f'- ID: `{rows[0]["id"]}`\n\n### Transcripción:\n  Exact text  \n\n---\n\n## Other\n- ID: `7685102680854875499`\n\n### Transcripción:\nOrphan'
    b = bundle(rows, md.encode())
    p = lc.preview(db, b)
    assert p['counts']['METADATA_ONLY'] == 2
    assert p['matched'] == 1
    assert 'TRANSCRIPT_WITHOUT_METADATA' in p['warnings'][0]
    lc.persist(db, b); db.commit()
    assert count(db, Video) == 3 and count(db, Transcript) == 1
    assert db.scalar(select(Transcript)).text == '  Exact text  '
    lc.persist(db, bundle()); db.commit()
    assert count(db, Transcript) == 3


@pytest.mark.parametrize('change,code', [
    (lambda r: r.append(dict(r[0])), 'DUPLICATE_METADATA_ID'),
    (lambda r: r[1].update(author_id='1234567'), 'CHANNEL_CONFLICT'),
    (lambda r: [x.update(author_id=None, author='other' if i else x['author']) for i,x in enumerate(r)], 'CHANNEL_CONFLICT'),
    (lambda r: r[0].update(id=7685102680854875424), 'INVALID_METADATA'),
    (lambda r: r[0].update(author=''), 'INVALID_METADATA'),
    (lambda r: r[0].update(views=-1), 'INVALID_METADATA'),
    (lambda r: r[0].update(likes=True), 'INVALID_METADATA'),
    (lambda r: r[0].update(created_at='bad'), 'INVALID_METADATA'),
])
def test_invalid_metadata(change, code):
    rows, _ = inputs(); change(rows)
    with pytest.raises(HTTPException, match=code):
        bundle(rows)


@pytest.mark.parametrize('md,code', [
    (b'- ID: `not-an-id`\n### Transcripci\xc3\xb3n:\ntext', 'INVALID_TRANSCRIPT_SECTION'),
    (b'filename.mp4\ncaption', 'INVALID_TRANSCRIPT_SECTION'),
    ('- ID: `7685102680854875424`\n### Transcripción:\n' .encode(), 'INVALID_TRANSCRIPT_SECTION'),
    (('- ID: `7685102680854875424`\n### Transcripción:\ntext\n' * 2).encode(), 'DUPLICATE_TRANSCRIPT_ID'),
    (('- ID: `7685102680854875424`\n### Transcripción:\n' + 'a' * (lc.MAX_TEXT + 1)).encode(), 'INVALID_TRANSCRIPT_SECTION'),
])
def test_invalid_transcript(md, code):
    with pytest.raises(HTTPException, match=code):
        bundle(markdown=md)


def test_channel_and_video_conflicts(db):
    channel = Channel(platform='tiktok', username='larutadeldinero52', nickname='', tiktok_user_id='999')
    db.add(channel); db.commit()
    assert any('CHANNEL_CONFLICT' in e for e in lc.preview(db, bundle())['errors'])
    with pytest.raises(HTTPException):
        lc.persist(db, bundle())
    channel.username = 'other'; channel.tiktok_user_id = None
    db.commit()
    lc.persist(db, bundle()); db.commit()
    video = db.scalar(select(Video)); video.channel_id = channel.id; db.commit()
    assert any('VIDEO_CHANNEL_CONFLICT' in e for e in lc.preview(db, bundle())['errors'])


def test_partial_metrics_and_unknown_date_blocked_without_fabrication(db):
    b = bundle(metrics=True)
    p = lc.preview(db, b)
    assert p['metrics_pending'] == 1
    assert p['counts']['METRIC_NEW_OBSERVATION'] == 0
    with pytest.raises(HTTPException, match='METRIC_SCHEMA_REQUIRED'):
        lc.persist(db, b)
    assert count(db, VideoSnapshot) == count(db, Analysis) == count(db, Video) == 0
    rows, _ = inputs(); rows[0]['created_at'] = None
    assert any('published_at' in e for e in lc.preview(db, bundle(rows))['errors'])


def test_future_scan_reuses_video_and_skips_whisper(db):
    b = bundle(); lc.persist(db, b); db.commit()
    original_ids = set(db.scalars(select(Video.id)))
    videos = [dict(id=r['id'], author=r['author'], nickname=r['nickname'], caption='scan',
        created_at=1789257600, views=10000, likes=1000, comments=10, shares=20, favorites=30,
        duration=30, url=f'https://www.tiktok.com/@{r["author"]}/video/{r["id"]}') for r in b.rows]
    payload = AnalysisInput(profile=dict(username=b.rows[0]['author'], nickname=b.rows[0]['nickname'], author_id=b.rows[0]['author_id']), videos=videos)
    create_analysis(db, payload); db.commit()
    assert set(db.scalars(select(Video.id))) == original_ids
    assert count(db, Transcript) == 3
    assert count(db, VideoSnapshot) == 3
    assert count(db, TranscriptionJob) == 0


def test_admin_workflow_auth_dry_run_confirm_and_types(db, monkeypatch):
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            route = '/admin/research/import/local-curator'
            assert client.get(route).status_code == 401
            assert client.post(route + '/confirm', data={'token':'x', 'digest':'x'}).status_code == 401
            app.dependency_overrides[admin.require_admin] = lambda: None
            rows, md = inputs()
            files = {'metadata': ('metadata.json', json.dumps(rows), 'application/json'), 'transcripts': ('transcripciones.md', md, 'text/markdown')}
            assert client.get(route).status_code == 200
            page = client.post(route + '/dry-run', files=files)
            assert page.status_code == 200 and 'CURACIÓN LOCAL DETECTADA' in page.text
            assert count(db, Video) == 0
            import re
            token = re.search(r'name="token" value="([^"]+)"', page.text)[1]
            digest = re.search(r'name="digest" value="([^"]+)"', page.text)[1]
            assert client.post(route + '/confirm', data=dict(token=token, digest=digest)).status_code == 200
            assert count(db, Video) == 3
            assert client.post(route + '/confirm', data=dict(token=token, digest=digest)).status_code == 410
            files['metadata'] = ('wrong.json', json.dumps(rows), 'application/json')
            assert client.post(route + '/dry-run', files=files).status_code == 422
            files['metadata'] = ('metadata.json', json.dumps(rows), 'image/png')
            assert client.post(route + '/dry-run', files=files).status_code == 422
            monkeypatch.setattr(lc, 'MAX_BYTES', 10)
            files['metadata'] = ('metadata.json', json.dumps(rows), 'application/json')
            assert client.post(route + '/dry-run', files=files).status_code == 413
    finally:
        app.dependency_overrides.clear()


def test_username_only_fallback(db):
    rows, _ = inputs()
    for row in rows:
        row['author_id'] = None
    lc.persist(db, bundle(rows)); db.commit()
    lc.persist(db, bundle(rows)); db.commit()
    assert count(db, Channel) == 1
    assert db.scalar(select(Channel)).tiktok_user_id is None


def test_no_filename_caption_or_position_matching(db):
    rows, _ = inputs()
    rows[0]['media_file'] = rows[1]['id'] + '.mp4'
    rows[0]['caption'] = 'Not a transcript'
    md = f'- ID: `{rows[1]["id"]}`\n### Transcripción:\nOnly the exact ID'
    lc.persist(db, bundle(rows, md.encode())); db.commit()
    video = db.scalar(select(Video).where(Video.tiktok_id == rows[1]['id']))
    assert db.scalar(select(Transcript)).video_id == video.id
    assert count(db, Transcript) == 1


def test_conservative_missing_metadata_enrichment(db):
    lc.persist(db, bundle()); db.commit()
    video = db.scalar(select(Video).where(Video.tiktok_id == '7685102680854875424'))
    video.caption = ''; video.nickname = ''; video.url = ''
    db.commit()
    lc.persist(db, bundle()); db.commit()
    assert video.caption == 'Ejemplo de prueba'
    assert video.nickname == 'La Ruta del dinero'
    assert video.url.endswith(video.tiktok_id)
    assert video.enrichment_status == 'missing' and video.enrichment_analysis_id is None


def test_limits_root_utf8_and_transcript_content():
    for raw in (b'{}', b'null', b'[]', b'\xff'):
        with pytest.raises(HTTPException):
            lc.parse_bundle(raw, b'')
    rows, _ = inputs()
    md = f'- ID: `{rows[0]["id"]}`\n### Transcripción:\nCreator claims!\n\n### A heading within the transcript\nKeep **everything** here.\n'
    assert bundle(rows, md.encode()).transcripts[rows[0]['id']] == md.split('### Transcripción:\n')[1].rstrip('\n')


def test_confirmation_revalidates_conflicts(db):
    b = bundle()
    assert not lc.preview(db, b)['errors']
    db.add(Channel(platform='tiktok', username='larutadeldinero52', nickname='', tiktok_user_id='1234'))
    db.commit()
    with pytest.raises(HTTPException, match='CHANNEL_CONFLICT'):
        lc.persist(db, b)
    assert count(db, Video) == 0


def test_postgres_confirmation_serializes_before_revalidation(monkeypatch):
    from types import SimpleNamespace
    statements = []
    fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
                           execute=lambda statement: statements.append(str(statement)))
    monkeypatch.setattr(lc, 'preview', lambda db, b: {'errors': ['stop after lock']})
    with pytest.raises(HTTPException):
        lc.persist(fake, bundle())
    assert statements == ['SELECT pg_advisory_xact_lock(740183279124)']
