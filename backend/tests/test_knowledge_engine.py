import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import admin
from app.channel_intelligence_contract import canonical_json_sha256
from app.channel_update_contract import CHANNEL_UPDATE_PROMPT_VERSION, CHANNEL_UPDATE_SCHEMA_VERSION
from app.knowledge_engine import ANALYSIS_CONTRACT_VERSION, freshness, incremental_pack, performance_state_hash, semantic_corpus_hash, semantic_video
from app.models import Analysis, ChannelIntelligenceAnalysis, ChannelStrategicPlaybook, ChannelVideoIntelligence
from app import strategist
from app.strategist import PLAYBOOK_SCHEMA_VERSION, get_or_create_playbook, personal_strategy_input
from tests.test_research_backoffice import _channel_analysis_payload, corpus, make_video


def test_freshness_separates_semantics_metrics_and_contract():
    records = [{'video_id': '1', 'caption': 'A', 'transcript': 'T', 'transcript_source': 'manual', 'transcript_language': 'es',
                'duration_seconds': 30, 'published_at': '2026-01-01', 'views': 10, 'likes': 1, 'comments': 0, 'shares': 0,
                'favorites': 0, 'engagement_rate': .1, 'outlier_score': 1, 'overall_rank': 1}]
    row = SimpleNamespace(semantic_corpus_hash=semantic_corpus_hash(records), performance_state_hash=performance_state_hash(records),
                          analysis_contract_version=ANALYSIS_CONTRACT_VERSION)
    assert freshness(row, records)['state'] == 'FRESH'
    metrics = [dict(records[0], views=99)]
    assert freshness(row, metrics)['state'] == 'PERFORMANCE_CHANGED'
    changed = [dict(records[0], transcript='changed')]
    assert freshness(row, changed)['state'] == 'SEMANTIC_DELTA'
    row.analysis_contract_version = 'old'
    assert freshness(row, records)['state'] == 'CONTRACT_STALE'


def test_incremental_pack_sends_knowledge_not_old_transcripts(db):
    channel, _videos = corpus(db); data = admin._summary_for_channel(db, channel.id)
    payload = _channel_analysis_payload(data)
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(channel.id, payload, 'all', canonical_json_sha256(payload), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    prior = db.scalar(select(ChannelIntelligenceAnalysis)); records = admin._selection(data, 'all')
    known = [row.intelligence for row in db.scalars(select(ChannelVideoIntelligence))]
    body = incremental_pack(channel=channel, prior=prior, known=known, records=records, delta_ids=[records[0]['video_id']],
        semantic_hash=semantic_corpus_hash(records), performance_hash=performance_state_hash(records))
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert {'manifest.json', 'previous_channel_intelligence.json', 'known_video_intelligence.jsonl', 'new_or_changed_videos.jsonl', 'kurukin-channel-update.schema.json'} <= set(archive.namelist())
        assert 'Transcript 2' not in archive.read('known_video_intelligence.jsonl').decode()
        assert 'Transcript 1' in archive.read('new_or_changed_videos.jsonl').decode()
        assert prior.payload_sha256 in archive.read('previous_channel_intelligence.json').decode()


def test_delta_rejects_wrong_base_and_merges_immutable_snapshot(db):
    channel, videos = corpus(db); data = admin._summary_for_channel(db, channel.id)
    payload = _channel_analysis_payload(data)
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(channel.id, payload, 'all', canonical_json_sha256(payload), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    prior = db.scalar(select(ChannelIntelligenceAnalysis)); original = prior.channel_intelligence
    analysis = db.scalar(select(Analysis).where(Analysis.channel_id == channel.id))
    new = make_video(db, channel, analysis, 9, 500, 'New semantic transcript'); db.commit()
    data = admin._summary_for_channel(db, channel.id); state, records, delta = admin._knowledge_state(db, data, prior)
    assert state['state'] == 'SEMANTIC_DELTA' and delta == [new.tiktok_id]
    update = {'schema': CHANNEL_UPDATE_SCHEMA_VERSION, 'prompt_version': CHANNEL_UPDATE_PROMPT_VERSION, 'processor': 'test',
        'base_state': {'analysis_id': str(prior.id), 'payload_sha256': prior.payload_sha256, 'semantic_corpus_hash': prior.semantic_corpus_hash},
        'target_state': {'semantic_corpus_hash': state['semantic_corpus_hash']},
        'upsert_videos': [{**payload['videos'][0], 'video_id': new.tiktok_id}], 'channel_intelligence': original}
    errors, *_ = admin.dry_run_channel_intelligence_update(db, channel.id, update)
    assert errors == []
    bad = json.loads(json.dumps(update)); bad['base_state']['semantic_corpus_hash'] = '0' * 64
    errors, *_ = admin.dry_run_channel_intelligence_update(db, channel.id, bad)
    assert any('base_state.semantic_corpus_hash' in error for error in errors)
    merged = admin.import_channel_intelligence_update(db, channel.id, update)
    assert merged.id != prior.id and prior.channel_intelligence == original
    assert len(list(db.scalars(select(ChannelVideoIntelligence).where(ChannelVideoIntelligence.analysis_id == merged.id)))) == 3


def test_playbook_cache_and_private_strategy_boundary(db):
    channel, _videos = corpus(db); data = admin._summary_for_channel(db, channel.id); payload = _channel_analysis_payload(data)
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(channel.id, payload, 'all', canonical_json_sha256(payload), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis)); records = admin._selection(data, 'all')
    calls = []
    def generate(_payload):
        calls.append(1)
        return {'schema': PLAYBOOK_SCHEMA_VERSION, 'executive_thesis': 'Thesis', 'dominant_formula': 'Formula', 'top_moves': [],
                'what_to_repeat': [], 'what_to_avoid': [], 'hook_playbook': [], 'narrative_playbook': [], 'pain_desire_playbook': [],
                'conversion_playbook': [], 'repetition_playbook': [], 'recommended_experiments': [], 'confidence_notes': []}
    videos = [child.intelligence for child in db.scalars(select(ChannelVideoIntelligence))]
    first, reused = get_or_create_playbook(db, channel_id=channel.id, analysis=analysis, records=records, videos=videos, provider='fake', model='fake', generate=generate)
    second, cached = get_or_create_playbook(db, channel_id=channel.id, analysis=analysis, records=records, videos=videos, provider='fake', model='fake', generate=generate)
    assert not reused and cached and first.id == second.id and len(calls) == 1
    private = personal_strategy_input(first.payload_json, {'business': 'private business', 'offer': 'private offer'})
    assert 'private business' in private['private_business_profile']['business']
    assert private['diagnostics']['raw_research_pack_included'] is False
    assert private['diagnostics']['raw_transcripts_included'] == 0
    assert 'private business' not in first.payload_json.get('executive_thesis', '')
    prompt = admin.content_pack_prompt(data, analysis, {'business': 'private business', 'offer': 'private offer', 'audience': 'a', 'goal': 'g', 'tone': 't', 'constraints': ''}, private)
    assert 'private business' in prompt and 'Research Pack' not in prompt


def test_strategist_reads_nested_responses_output_text(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'output': [{'type': 'reasoning', 'content': []}, {'type': 'message', 'content': [
                {'type': 'output_text', 'text': '{"schema":"test-contract"}'}]}]}
    monkeypatch.setattr(strategist.httpx, 'post', lambda *args, **kwargs: Response())
    assert strategist._openai_json({}, model='test', api_key='test', contract='test-contract', prompt='test') == {
        'schema': 'test-contract'}
