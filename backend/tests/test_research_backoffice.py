import asyncio
import io
import json
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import admin
from app.config import get_settings
from app.main import app
from app.models import (Analysis, Channel, ChannelIntelligenceAnalysis, ChannelStrategicPlaybook, ChannelVideoIntelligence,
                        PrivateContentPack, PrivatePersonalStrategy, Transcript, Video, VideoSnapshot)


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
        assert manifest['research_pack_hash'] == admin.research_pack_hash(data, 'all')
        assert manifest['research_pack_hash'] in archive.read('README.md').decode()
        records = [json.loads(line) for line in archive.read('videos.jsonl').decode().splitlines()]
        assert len(records) == 3 and all(record['video_id'] for record in records)
        markdown = ''.join(archive.read(name).decode() for name in manifest['transcript_parts'])
        assert videos[3].tiktok_id in markdown
        assert 'Rabbit' not in archive.read('videos.jsonl').decode()
        assert 'audio_path' not in archive.read('videos.jsonl').decode()


def _channel_analysis_payload(data, mode='all'):
    records = admin._selection(data, mode)
    return {
        'schema': 'kurukin-channel-analysis-v1',
        'prompt_version': 'kurukin-channel-analysis-prompt-v1', 'processor': 'Test processor',
        'research_pack': {'hash': admin.research_pack_hash(data, mode), 'channel_username': data['channel'].username,
                          'selection_mode': mode, 'video_count': len(records)},
        'videos': [{
            'video_id': record['video_id'], 'analysis_status': 'analyzed', 'summary': 'Concise summary',
            'hooks': ['A hook'], 'pains': [], 'desires': [], 'topics': ['Topic'], 'narratives': [], 'ctas': ['Follow'], 'offers': [],
            'evidence': [{'claim': 'The video demonstrates the pattern.', 'video_ids': [record['video_id']]}],
        } for record in records],
        'channel_intelligence': {
            'summary': 'An evidence-led channel summary.', 'audience': 'The likely audience.',
            'pains': [], 'desires': [], 'hooks': [], 'topics': [], 'winning_patterns': [], 'narratives': [],
            'repetition_clusters': [], 'ctas': [], 'offers': [], 'opportunities': [], 'caveats': ['Metrics are observations.'],
        },
    }


def test_channel_intelligence_contract_dry_run_confirm_idempotency_and_evidence(db):
    channel, videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    payload = _channel_analysis_payload(data)
    value, errors, mode, status = admin.dry_run_channel_intelligence_import(
        db, channel.id, json.dumps(payload).encode())
    assert value == payload and errors == [] and mode == 'all' and status == 'NEW'
    prompt = admin.channel_intelligence_prompt(data)
    assert 'videos[]` MUST contain exactly N video objects' in prompt
    expected_filename = admin.channel_analysis_filename(channel.username, payload['research_pack']['hash'])
    assert f'Required output filename: `{expected_filename}`' in prompt
    assert f'Create and attach/downloadable file: `{expected_filename}`' in prompt
    assert 'DO NOT repeat or paraphrase `description` inside `why_it_matters`.' in prompt
    assert 'video_intelligence' in prompt and 'performance_interpretation' in prompt

    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, payload, mode, admin.canonical_json_sha256(payload), 1e20))
    response = admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    assert response.body and b'"ok":true' in response.body
    analysis = db.scalar(select(ChannelIntelligenceAnalysis))
    assert analysis.research_pack_hash == payload['research_pack']['hash']
    assert db.scalar(select(ChannelVideoIntelligence).where(ChannelVideoIntelligence.analysis_id == analysis.id))

    _value, errors, mode, status = admin.dry_run_channel_intelligence_import(db, channel.id, json.dumps(payload).encode())
    assert errors == [] and mode == 'all' and status == 'ALREADY_IMPORTED'
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, payload, mode, admin.canonical_json_sha256(payload), 1e20))
    response = admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    assert b'"already_imported":true' in response.body

    incomplete = {**payload, 'videos': payload['videos'][:-1]}
    _value, errors, _mode, _status = admin.dry_run_channel_intelligence_import(db, channel.id, json.dumps(incomplete).encode())
    assert any('missing Research Pack videos' in error for error in errors)
    detail = admin.channel_intelligence_detail(channel.id, analysis.id, db=db)
    assert videos[0].url.encode() in detail.body


def test_product_lite_channel_workflow_and_schema_drift(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    sci['channel_intelligence']['winning_patterns'] = [{
        'name': 'Pain → meaning → hope', 'description': 'Moves from a specific pain to a hopeful resolution.',
        'evidence': [{'claim': 'Repeated in strong videos.', 'video_ids': [sci['videos'][0]['video_id']]}],
    }]
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    page = admin.channel_intelligence_page(channel.id, db=db).body.decode()
    assert page.index('¿POR QUÉ FUNCIONA ESTE CANAL?') < page.index('Ver análisis completo')
    assert 'USAR ESTA INTELIGENCIA' in page and 'Videos representativos' in page
    assert 'CREAR CONTENIDO' not in page and 'IDEAS DE CONTENIDO Y GUIONES' not in page
    assert 'kurukin-creative-context.md' in page
    assert 'https://chatgpt.com/g/g-68a6de0c7ec48191876f8297e467fc7c-alex-hormozi-100m' in page
    payload = _channel_analysis_payload(data)
    payload['video_intelligence'] = payload.pop('videos')
    _value, errors, _mode, _status = admin.dry_run_channel_intelligence_import(db, channel.id, json.dumps(payload).encode())
    assert any("se recibió 'video_intelligence'" in error.lower() for error in errors)


def test_external_ai_handoff_is_an_explicit_mobile_safe_three_step_flow(db):
    channel, _videos = corpus(db)
    page = admin.channel_intelligence_prompt_page(channel.id, db=db).body.decode()
    data = admin._summary_for_channel(db, channel.id)
    expected_filename = admin.channel_analysis_filename(channel.username, admin.research_pack_hash(data, 'all'))
    hormozi_url = 'https://chatgpt.com/g/g-68a6de0c7ec48191876f8297e467fc7c-alex-hormozi-100m'

    assert 'Paso 1' in page and 'Preparar evidencia' in page
    assert 'Paso 2' in page and 'Analizar con IA' in page
    assert 'Paso 3' in page and 'Importar resultado' in page
    assert 'Descargar paquete de investigación' in page
    assert f'href="/admin/research/channels/{channel.id}/export.zip?mode=all"' in page
    assert hormozi_url in page
    assert 'Copiar instrucciones y abrir Alex Hormozi GPT' in page
    assert page.index("window.open(gptUrl,'_blank','noopener')") < page.index('copyCurrentPrompt().then')
    assert page.count(expected_filename) >= 4
    assert f'Required output filename: `{expected_filename}`' in page
    assert 'La IA debe devolverte:' in page and f'Seleccionar {expected_filename}' in page
    assert 'id="analysis-upload"' in page
    assert page.count('>Copiar instrucciones<') >= 2
    assert '<details class="card technical" id="channel-prompt-details">' in page
    assert '<details class="card technical" id="channel-prompt-details" open>' not in page
    assert 'No se pudieron copiar automáticamente. Abre “Ver instrucciones”' in page
    layout = admin._layout('test', '').body.decode()
    assert '@media(max-width:650px)' in page and '.actions>*{width:100%;text-align:center}' in layout
    assert 'class="channel-context"' in page and 'Trabajando en:' in page
    assert f'@{channel.username}' in page and channel.nickname in page
    assert '@media(max-width:650px){.channel-context' in layout
    assert 'KURUKIN PRODUCT LITE v1.1 · SCI v1' in page


def test_channel_context_is_visible_on_product_lite_channel_screens(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    payload = _channel_analysis_payload(data)
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, payload, 'all', admin.canonical_json_sha256(payload), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis))

    for page in (
        admin.channel_intelligence_page(channel.id, db=db).body.decode(),
        admin.channel_intelligence_detail(channel.id, analysis.id, db=db).body.decode(),
    ):
        assert 'class="channel-context"' in page
        assert 'Trabajando en:' in page
        assert f'@{channel.username}' in page and channel.nickname in page


def test_channel_analysis_filename_sanitizes_username_and_uses_short_pack_hash():
    pack_hash = '1e024284' + 'a' * 56
    assert admin.channel_analysis_filename('@constela_y_sana', pack_hash) == (
        'kurukin-channel-analysis-constela_y_sana-1e024284.json')
    assert admin.channel_analysis_filename('@Constela/y sana!', pack_hash) == (
        'kurukin-channel-analysis-constela-y-sana-1e024284.json')


def test_channel_intelligence_import_ignores_the_uploaded_filename(db):
    channel, _videos = corpus(db)
    payload = _channel_analysis_payload(admin._summary_for_channel(db, channel.id))
    upload = UploadFile(filename='renamed-by-browser.json', file=io.BytesIO(json.dumps(payload).encode()))

    response = asyncio.run(admin.channel_intelligence_import_dry_run(channel.id, upload, db=db))

    assert response.status_code == 200 and b'"ok":true' in response.body


def test_external_ai_handoff_update_keeps_current_intelligence_separate_from_new_import(db):
    channel, videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    payload = _channel_analysis_payload(data)
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, payload, 'all', admin.canonical_json_sha256(payload), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    videos[0].caption = 'Contenido nuevo para actualizar'; db.commit()

    page = admin.channel_intelligence_action(channel.id, db=db).body.decode()
    assert 'Inteligencia actual:</b> <span class="badge ok">✓ disponible</span>' in page
    assert 'Nuevo análisis:</b> <span id="new-analysis-status" class="badge warn">pendiente de importar</span>' in page
    assert 'Paso 1' in page and 'Paso 2' in page and 'Paso 3' in page
    assert f'href="/admin/research/channels/{channel.id}/intelligence/update.zip"' in page
    assert "base='/admin/research/channels/%s/intelligence/update/import'" % channel.id in page
    assert 'class="channel-context"' in page and f'@{channel.username}' in page and channel.nickname in page
    assert 'Importar inteligencia terminada' not in page


def _v2_strategy(evidence_id):
    pattern = {
        'name': 'Problema específico → explicación útil', 'source_pattern': 'Reframe de dolor concreto',
        'why_it_works_in_source': 'Hace visible una tensión reconocible.', 'fit_for_business': 'Encaja con la oferta.',
        'adaptation': 'Empieza con el problema cotidiano de tu cliente.', 'what_not_to_copy': 'No copies claims or identity.',
        'recommended_use': 'Úsalo en videos de descubrimiento.', 'evidence_video_ids': [evidence_id], 'confidence': 'media',
    }
    return {
        'schema': 'kurukin-personal-strategy-v2', 'strategic_fit': 'Buen ajuste para una oferta consultiva.',
        'patterns_to_adapt': [pattern], 'patterns_to_avoid': ['No copiar afirmaciones del creador.'],
        'audience_opportunities': ['Hablar del problema antes de presentar la oferta.'],
        'pain_opportunities': ['Fricción concreta'], 'desire_opportunities': ['Claridad'],
        'hook_adaptations': ['Si te pasa X, empieza por Y.'], 'narrative_adaptations': ['Problema → mecanismo → siguiente paso'],
        'cta_strategy': 'Invita a una conversación diagnóstica.', 'offer_alignment': 'Conecta el diagnóstico con la oferta.',
        'content_pillars': ['Diagnóstico'],
        'test_priorities': [{'priority': '1', 'hypothesis': 'El dolor específico atrae comentarios cualificados.',
                             'what_to_test': 'Tres variaciones del mismo dolor.', 'success_signal': 'Más comentarios cualificados.',
                             'source_patterns': ['Reframe de dolor concreto'], 'evidence_video_ids': [evidence_id]}],
        'first_content_plan': ['Publica tres videos del mismo dolor con hooks distintos.'],
        'executive_recommendation': 'Adapta el mecanismo, no la identidad del creador.',
    }


def _private_playbook(db, channel, analysis, evidence_id):
    row = ChannelStrategicPlaybook(
        channel_id=channel.id, source_analysis_id=analysis.id, source_payload_sha=analysis.payload_sha256,
        semantic_corpus_hash=analysis.semantic_corpus_hash, performance_state_hash=analysis.performance_state_hash,
        schema_version='kurukin-channel-playbook-v1', prompt_version='kurukin-channel-playbook-prompt-v1',
        provider='test', model='test', payload_sha256='f' * 64,
        payload_json={'schema': 'kurukin-channel-playbook-v1', 'executive_thesis': 'Public thesis',
                      'dominant_formula': 'Problem → explanation',
                      'top_moves': [{'name': 'Reframe de dolor concreto', 'why_it_works': 'Public evidence',
                                     'evidence_video_ids': [evidence_id]}]},
    )
    db.add(row); db.commit()
    return row


def test_v19_intelligence_hierarchy_prioritizes_thesis_mechanisms_and_collapsed_evidence(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    evidence_id = sci['videos'][0]['video_id']
    expanded = json.loads(json.dumps(sci))
    expanded['channel_intelligence']['winning_patterns'] = [
        {'name': f'Mecanismo {number}', 'description': f'Explicación {number}',
         'evidence': [{'claim': 'Prueba pública.', 'video_ids': [evidence_id]}]}
        for number in range(1, 6)
    ]
    expanded['channel_intelligence']['pains'] = [{'name': 'Dolor ' + str(number), 'description': ''} for number in range(7)]
    expanded['channel_intelligence']['desires'] = [{'name': 'Deseo ' + str(number), 'description': ''} for number in range(7)]
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    page = admin.channel_intelligence_page(channel.id, db=db).body.decode()
    executive = page[:page.index('Ver análisis completo')]
    hierarchy = admin._actionable_playbook(data, SimpleNamespace(channel_intelligence=expanded['channel_intelligence'], selection_mode='all'))
    assert executive.index('¿POR QUÉ FUNCIONA ESTE CANAL?') < executive.index('MECANISMOS PRINCIPALES')
    assert executive.index('USAR ESTA INTELIGENCIA') < executive.index('MECANISMOS PRINCIPALES')
    assert hierarchy.count('class="mechanism-rank"') == 5
    assert 'DOLORES QUE ACTIVAN' in hierarchy and 'DESEOS QUE ACTIVAN' in hierarchy and 'Ver todos' in hierarchy
    assert '<details class="evidence-detail"><summary>Ver evidencia</summary>' in hierarchy
    assert '<details class="card"><summary><b>Ver análisis completo</b></summary>' in page
    assert '@media(max-width:650px)' in page and 'compact-columns' in page


def test_mechanism_renderer_suppresses_duplicate_why_it_matters_and_keeps_distinct_copy(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    evidence_id = admin._selection(data, 'all')[0]['video_id']
    base = {
        'summary': 'Resumen.', 'pains': [], 'desires': [], 'hooks': [], 'topics': [], 'narratives': [],
        'repetition_clusters': [], 'ctas': [], 'offers': [],
    }
    duplicated = {**base, 'winning_patterns': [{
        'name': 'Mecanismo duplicado', 'description': 'El patrón más consistente.',
        'why_it_matters': ' el patrón más consistente! ',
        'evidence': [{'claim': 'Prueba.', 'video_ids': [evidence_id]}],
    }]}
    distinct = {**base, 'winning_patterns': [{
        'name': 'Mecanismo distinto', 'description': 'Convierte una idea abstracta en una acción concreta.',
        'why_it_matters': 'Facilita que el contenido sea aplicable, memorable y compartible.',
        'evidence': [{'claim': 'Prueba.', 'video_ids': [evidence_id]}],
    }]}

    duplicate_html = admin._actionable_playbook(data, SimpleNamespace(channel_intelligence=duplicated, selection_mode='all'))
    distinct_html = admin._actionable_playbook(data, SimpleNamespace(channel_intelligence=distinct, selection_mode='all'))

    assert duplicate_html.count('El patrón más consistente.') == 1
    assert '<b>Por qué importa:</b>' not in duplicate_html
    assert '<b>Por qué importa:</b> Facilita que el contenido sea aplicable, memorable y compartible.' in distinct_html


def test_evidence_summary_uses_natural_spanish_video_pluralization():
    one = {'only': {'views': 12_200}}
    many = {'one': {'views': 100}, 'two': {'views': 200}}
    one_item = {'evidence': [{'video_ids': ['only']}]}
    many_item = {'evidence': [{'video_ids': ['one', 'two']}]}

    assert admin._proof_summary(one_item, one) == '1 video con evidencia · mejor ejemplo 12,200 vistas'
    assert admin._proof_summary(many_item, many) == '2 videos con evidencia · mejor ejemplo 200 vistas'


def test_personal_strategy_v2_validates_persists_private_context_and_renders(db, monkeypatch):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    evidence_id = sci['videos'][0]['video_id']
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis))
    _private_playbook(db, channel, analysis, evidence_id)
    strategy = _v2_strategy(evidence_id)
    monkeypatch.setattr(admin, 'configured_personal_generator', lambda _settings: lambda payload: strategy)
    response = admin.generate_personal_strategy(channel.id, 'Consultoría privada', 'Diagnóstico', 'Operadores', 'España',
                                                'Leads', 'Tres videos por semana', 'Directo', '', 'Sin promesas', db=db)
    assert b'"ok":true' in response.body
    stored = db.scalar(select(PrivatePersonalStrategy))
    assert stored.strategy_json == strategy and stored.private_context['business'] == 'Consultoría privada'
    assert 'Consultoría privada' not in db.scalar(select(ChannelStrategicPlaybook)).payload_json['executive_thesis']
    page = admin.channel_intelligence_page(channel.id, db=db).body.decode()
    assert 'TU ESTRATEGIA' not in page and 'QUÉ ADAPTAR' not in page
    assert 'CREAR CONTENIDO' not in page and 'kurukin-creative-context.md' in page
    assert 'Advanced / Legacy' in page


def test_personal_strategy_v1_is_readable_and_invalid_v2_is_not_persisted(db, monkeypatch):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    evidence_id = sci['videos'][0]['video_id']
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis)); playbook = _private_playbook(db, channel, analysis, evidence_id)
    legacy = {'schema': 'kurukin-personal-strategy-v1', 'business_summary': 'Legacy strategy', 'selected_mechanisms': ['Legacy move'],
              'content_positioning': [], 'recommended_content_pillars': [], 'recommended_hook_mix': [],
              'recommended_narrative_mix': [], 'recommended_cta_strategy': 'CTA', 'recommended_experiments': [],
              'risks_or_constraints': [], 'content_brief_for_creator': 'Legacy brief'}
    db.add(PrivatePersonalStrategy(channel_id=channel.id, playbook_id=playbook.id, payload_sha256='e' * 64,
                                   private_context={'business': 'Legacy'}, strategy_json=legacy)); db.commit()
    page = admin.channel_intelligence_page(channel.id, db=db).body.decode()
    assert 'Estrategia histórica V1: se conserva legible' not in page and 'Legacy brief' not in page
    assert 'Advanced / Legacy' in page
    bad = _v2_strategy(evidence_id); bad['patterns_to_adapt'][0].pop('evidence_video_ids')
    assert admin.validate_personal_strategy(bad, known_video_ids={evidence_id})
    monkeypatch.setattr(admin, 'configured_personal_generator', lambda _settings: lambda payload: bad)
    response = admin.generate_personal_strategy(channel.id, 'Negocio', 'Oferta', 'Audiencia', 'México', 'Leads', 'Vídeos', '', '', '', db=db)
    assert response.status_code == 503
    assert len(list(db.scalars(select(PrivatePersonalStrategy)))) == 1


def _content_pack(channel, evidence_id, pattern='Pain → meaning → hope'):
    return {
        'schema': 'kurukin-content-pack-v1',
        'source_channel': {'channel_id': str(channel.id), 'username': channel.username},
        'strategy': {'primary_patterns': [pattern], 'recommended_positioning': 'Helpful operator',
                     'content_formula': 'Pain → meaning → hope', 'recommended_cta_strategy': 'Invite a reply',
                     'recommended_content_mix': 'Three educational, one offer'},
        'content_ideas': [{'title': 'A better way', 'objective': 'Start conversations', 'hook': 'You are not behind.',
                           'angle': 'Reframe', 'pain': 'Overwhelm', 'desire': 'Clarity', 'mechanism': 'Hope', 'cta': 'Reply PLAN',
                           'source_patterns': [pattern], 'source_evidence_video_ids': [evidence_id]}],
        'scripts': [{'title': 'A better way script', 'objective': 'Earn trust', 'duration_target': '45 seconds',
                     'hook': 'You are not behind.', 'body': 'Name the problem and show the next step.', 'cta': 'Reply PLAN',
                     'source_patterns': [pattern], 'source_evidence_video_ids': [evidence_id]}],
    }


def _real_cross_section_content_pack(channel, evidence_id, pattern='Pain → meaning → hope'):
    """Six ideas and their corresponding scripts intentionally share human titles."""
    payload = _content_pack(channel, evidence_id, pattern)
    titles = [
        'Cuando una conversación con tu madre termina en culpa',
        'Poner límites sin explicar todo',
        'La carga que no te corresponde',
        'Por qué repetir la discusión no resuelve nada',
        'Una forma más clara de pedir espacio',
        'El patrón familiar que puedes observar hoy',
    ]
    payload['content_ideas'] = [{
        'title': title, 'objective': 'Abrir conversación', 'hook': 'Esto puede estar pasando.',
        'angle': 'Educación práctica', 'pain': 'Culpa', 'desire': 'Claridad', 'mechanism': 'Reframe',
        'cta': 'Escribe CLARIDAD', 'source_patterns': [pattern], 'source_evidence_video_ids': [evidence_id],
    } for title in titles]
    payload['scripts'] = [{
        'title': title, 'objective': 'Abrir conversación', 'duration_target': '45 segundos',
        'hook': 'Esto puede estar pasando.', 'body': 'Nombra el patrón y ofrece un siguiente paso responsable.',
        'cta': 'Escribe CLARIDAD', 'source_patterns': [pattern], 'source_evidence_video_ids': [evidence_id],
    } for title in titles]
    return payload


def test_private_content_pack_contract_prompt_import_and_render(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    evidence_id = sci['videos'][0]['video_id']
    sci['channel_intelligence']['winning_patterns'] = [{'name': 'Pain → meaning → hope', 'description': 'A repeatable reframe.',
        'evidence': [{'claim': 'Repeated in strong videos.', 'video_ids': [evidence_id]}]}]
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis))
    context = admin._private_context_from_form('Product', 'Offer', 'Operators', 'Leads', 'Direct', '')
    prompt = admin.content_pack_prompt(data, analysis, context)
    assert 'kurukin-content-pack.json' in prompt and 'FILE_GENERATION_UNAVAILABLE' in prompt
    assert 'Do NOT paste a giant JSON blob' in prompt
    payload = _content_pack(channel, evidence_id)
    errors = admin.validate_content_pack(payload, channel_id=str(channel.id), username=channel.username,
                                         known_patterns=set(admin._known_patterns(analysis)), known_video_ids={evidence_id, sci['videos'][1]['video_id']})
    assert errors == []
    bad = json.loads(json.dumps(payload)); bad['scripts'][0]['source_evidence_video_ids'] = ['unknown']
    assert any('unknown evidence IDs' in error for error in admin.validate_content_pack(
        bad, channel_id=str(channel.id), username=channel.username, known_patterns=set(admin._known_patterns(analysis)), known_video_ids={evidence_id}))
    pending = admin.PendingContentPackImport(channel.id, analysis.id, payload, context, admin.canonical_json_sha256(payload), 1e20)
    token = admin._store_pending_content_pack(pending)
    response = admin.content_pack_import_confirm(channel.id, token, db=db)
    assert b'"ok":true' in response.body
    stored = db.scalar(select(PrivateContentPack))
    assert stored.private_context == context
    assert analysis.channel_intelligence == sci['channel_intelligence']  # private data never enters global SCI
    rendered = admin._content_pack_results(data, analysis, stored)
    assert 'A better way' in rendered and 'Ver patrón de origen' in rendered and 'Caption 1' in rendered


def test_content_pack_allows_matching_idea_and_script_titles_through_dry_run(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    evidence_id = sci['videos'][0]['video_id']
    sci['channel_intelligence']['winning_patterns'] = [{'name': 'Pain → meaning → hope', 'description': 'A repeatable reframe.',
        'evidence': [{'claim': 'Repeated in strong videos.', 'video_ids': [evidence_id]}]}]
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    payload = _real_cross_section_content_pack(channel, evidence_id)
    analysis = db.scalar(select(ChannelIntelligenceAnalysis))
    errors = admin.validate_content_pack(payload, channel_id=str(channel.id), username=channel.username,
                                         known_patterns=set(admin._known_patterns(analysis)), known_video_ids={evidence_id})
    assert errors == []
    upload = UploadFile(filename='kurukin-content-pack.json', file=io.BytesIO(json.dumps(payload).encode()))
    response = asyncio.run(admin.content_pack_import_dry_run(
        channel.id, upload, 'Producto', 'Oferta', 'Audiencia', 'Leads', 'Directo', '', '', '', '', db=db))
    assert response.status_code == 200 and b'"ok":true' in response.body


@pytest.mark.parametrize(('collection', 'expected'), [
    ('content_ideas', 'Hay títulos duplicados dentro de Ideas de contenido'),
    ('scripts', 'Hay títulos duplicados dentro de Guiones'),
])
def test_content_pack_rejects_titles_duplicated_within_the_same_section(db, collection, expected):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    evidence_id = sci['videos'][0]['video_id']
    pattern = 'Pain → meaning → hope'
    payload = _real_cross_section_content_pack(channel, evidence_id, pattern)
    payload[collection][1]['title'] = payload[collection][0]['title']
    errors = admin.validate_content_pack(payload, channel_id=str(channel.id), username=channel.username,
                                         known_patterns={pattern}, known_video_ids={evidence_id})
    assert any(error.startswith(expected) for error in errors)
    assert not any('across ideas and scripts' in error for error in errors)


def test_inline_dry_run_summary_and_human_video_label(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    payload = _channel_analysis_payload(data)
    summary = admin._dry_run_summary(payload, data, admin._selection(data, 'all'))
    assert summary['expected'] == summary['received'] == 2
    assert summary['missing'] == summary['unknown'] == summary['duplicates'] == 0
    assert admin._human_video_title(admin._selection(data, 'all')[0]).startswith('Caption')


def test_product_lite_marker_and_advanced_separation(db, monkeypatch):
    channel, _videos = corpus(db)
    monkeypatch.setenv('KURUKIN_BUILD_SHA', '193a52fbe8fd1d96336fdf9abc69fb8684452d28')
    get_settings.cache_clear()

    detail = admin.research_channel(channel.id, db=db).body.decode()
    assert 'CANAL' in detail and 'INTELIGENCIA' in detail
    assert 'Preparar Structured Channel Intelligence' in detail
    assert 'NO_INTELLIGENCE' not in detail
    assert 'Advanced / Legacy' in detail

    legacy = admin.prompt_page(channel.id, db=db).body.decode()
    assert 'LEGACY PROMPT GENERATOR' in legacy
    assert f'href="/admin/research/channels/{channel.id}/intelligence">Ir a Structured Channel Intelligence</a>' in legacy
    assert 'Opciones avanzadas' in legacy and 'Business/context' in legacy

    index = admin.research_index(db=db).body.decode()
    intelligence = admin.channel_intelligence_page(channel.id, db=db).body.decode()
    for page in (index, detail, intelligence):
        assert 'KURUKIN PRODUCT LITE v1.1 · SCI v1 · Build 193a52f' in page
        assert '1&nbsp; CANAL' in page and '3&nbsp; USAR INTELIGENCIA' in page
    assert 'Canales' in index and 'Siguiente paso' in index
    assert 'channel-list' in index and '<table>' not in index
    assert '@media(max-width:650px)' in index
    assert 'Preparar Structured Channel Intelligence' in intelligence and 'NO_INTELLIGENCE' not in intelligence
    assert 'Admin / Debug' in index

    monkeypatch.delenv('KURUKIN_BUILD_SHA')
    get_settings.cache_clear()
    assert 'Build dev' in admin._layout('test', '').body.decode()


@pytest.mark.parametrize(('technical', 'human'), [
    ('NO_INTELLIGENCE', 'Falta analizar'),
    ('FRESH', 'Inteligencia actualizada'),
    ('PERFORMANCE_CHANGED', 'Hay nuevas métricas'),
    ('SEMANTIC_DELTA', 'Hay contenido nuevo por analizar'),
    ('CONTRACT_STALE', 'La inteligencia necesita actualizarse'),
])
def test_product_ux_translates_knowledge_states(technical, human):
    assert admin._human_knowledge_state(technical) == human


def test_product_lite_fresh_executive_evidence_and_handoff(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    sci['channel_intelligence']['winning_patterns'] = [{
        'name': 'Problema → reframe → esperanza', 'description': 'Convierte un problema reconocible en esperanza.',
        'evidence': [{'claim': 'Patrón repetido.', 'video_ids': [sci['videos'][0]['video_id']]}],
    }]
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    page = admin.channel_intelligence_page(channel.id, db=db).body.decode()
    executive = page[:page.index('Ver análisis completo')]
    assert '¿POR QUÉ FUNCIONA ESTE CANAL?' in executive
    assert 'FÓRMULA DOMINANTE' in executive
    assert 'Preparar contexto creativo' in executive
    assert 'NO_INTELLIGENCE' not in executive and 'SEMANTIC_DELTA' not in executive
    assert 'USAR ESTA INTELIGENCIA' in page and 'Videos representativos' in page
    assert 'creative-context-form' in page and 'name="tone"' in page
    assert 'Copiar Super Prompt y abrir Alex Hormozi GPT' in page
    assert 'CREAR CONTENIDO' not in page and 'Generar mi estrategia' not in page
    assert 'Auto Curator' not in page
    assert admin._private_context_from_form('Producto', 'Oferta', 'Audiencia', 'Leads', '', '')['tone'] == ''


def test_product_lite_creative_context_and_super_prompt_use_existing_evidence_only(db):
    channel, _videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    evidence_id = sci['videos'][0]['video_id']
    sci['channel_intelligence']['winning_patterns'] = [{
        'name': 'Problema → mecanismo → siguiente paso', 'description': 'Convierte una tensión concreta en una acción.',
        'evidence': [{'claim': 'El patrón se repite en videos fuertes.', 'video_ids': [evidence_id]}],
    }]
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)

    context = admin._creative_context_from_form('Consultoría', 'Diagnóstico', 'Operadores', 'Leads', 'España',
                                                'Directo', 'Responder PLAN', 'Sin promesas', 'Contexto privado')
    analysis = db.scalar(select(ChannelIntelligenceAnalysis))
    markdown = admin.creative_context_markdown(data, analysis, context, db)
    prompt = admin.creative_super_prompt(context)
    download = admin.download_creative_context(channel.id, **context, db=db)

    assert '# CHANNEL INTELLIGENCE' in markdown and '# REPRESENTATIVE EVIDENCE' in markdown
    assert '# USER CONTEXT' in markdown and 'Consultoría' in markdown
    assert 'Caption 1' in markdown and 'TikTok:' in markdown and 'Extracto de transcripción' in markdown
    assert '5 strongest campaigns' in prompt and '3 hook options' in prompt
    assert 'Do not require JSON' in prompt and 'return or import anything into Kurukin' in prompt
    assert download.headers['content-disposition'] == 'attachment; filename="kurukin-creative-context.md"'
    assert download.body.decode() == markdown
    assert db.scalar(select(func.count()).select_from(PrivatePersonalStrategy)) == 0
    assert db.scalar(select(func.count()).select_from(PrivateContentPack)) == 0


def test_product_ux_pending_keeps_last_intelligence_and_admin_debug_is_separate(db):
    channel, videos = corpus(db)
    data = admin._summary_for_channel(db, channel.id)
    sci = _channel_analysis_payload(data)
    token = admin._store_pending_channel_intelligence(admin.PendingChannelIntelligenceImport(
        channel.id, sci, 'all', admin.canonical_json_sha256(sci), 1e20))
    admin.channel_intelligence_import_confirm(channel.id, token, db=db)
    videos[0].caption = 'Contenido nuevo para actualizar'; db.commit()
    page = admin.channel_intelligence_page(channel.id, db=db).body.decode()
    primary = page[:page.index('Ver análisis completo')]
    assert 'Hay evidencia nueva disponible. La última inteligencia sigue disponible.' in primary
    assert 'Actualizar inteligencia' in primary
    assert 'SEMANTIC_DELTA' not in primary
    system = admin.admin_system().body.decode()
    debug = admin.admin_debug().body.decode()
    assert 'Auto Curator' in system and 'Recorrido del producto' not in system
    assert 'Diagnóstico del sistema' in debug


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
        assert 'Kurukin' in response.text and 'Canales' in response.text
    finally:
        app.dependency_overrides.clear()
