"""Semantic Viral DNA v1 contract, provenance, and global ownership."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.llm import SEMANTIC_VIRAL_DNA_JSON_SCHEMA
from app.llm.semantic import (
    SemanticProviderCapabilities, SemanticProviderConfig, get_semantic_provider,
    register_semantic_provider,
)
from app.llm.semantic_contract import (
    SEMANTIC_ENUM_FIELDS, SEMANTIC_OUTPUT_FIELDS, SEMANTIC_TEXT_FIELDS,
)
from app.models import Analysis, Channel, Transcript, Video, VideoSnapshot, ViralDNA
from app.viral_dna import (
    SEMANTIC_PROMPT, SEMANTIC_PROMPT_VERSION, extract_semantic_viral_dna_for_video,
    extract_viral_dna_for_video, semantic_input_sha256, validate_semantic_output,
)


VALID_OUTPUT = {
    'hook_text': '¿Quieres aprender más rápido?',
    'topic': 'aprendizaje',
    'subtopic': 'estudio autodidacta',
    'angle_summary': 'priorizar práctica espaciada sobre releer apuntes',
    'pain': 'estudiar mucho sin retener',
    'desire': 'aprender más rápido',
    'fear': 'perder tiempo estudiando mal',
    'audience_identity': 'estudiantes autodidactas',
    'belief': 'más horas de estudio producen mejores resultados',
    'objection': 'no tengo tiempo para cambiar mi método',
    'promise': 'mejor retención con práctica espaciada',
    'reframe': 'no es estudiar más, es recuperar activamente',
    'emotional_arc': 'frustration -> confidence',
    'cta_text': 'Guarda este método para tu próxima sesión.',
    'hook_type': 'question',
    'hook_mechanism': 'curiosity_gap',
    'hook_target': 'outcome',
    'audience_specificity': 'niche',
    'angle_type': 'how_to',
    'emotion_primary': 'curiosity',
    'emotion_secondary': 'confidence',
    'content_function_primary': 'educate',
    'content_function_secondary': 'inspire_motivate',
    'content_role_primary': 'nurture',
    'content_role_secondary': 'positioning',
    'content_format': 'how_to',
    'narrative_structure': 'problem_solution',
    'awareness_stage': 'problem_aware',
    'proof_type': 'demonstration',
    'authority_mechanism': 'teaching',
    'creator_positioning_signal': 'educator',
    'cta_type': 'save',
    'cta_secondary_type': 'follow',
    'commercial_intent': 'none',
    'offer_integration': 'none',
    'offer_type': 'none',
    'monetization_model': 'none',
}
assert set(VALID_OUTPUT) == set(SEMANTIC_OUTPUT_FIELDS)


def minimum_output():
    output = {field: None for field in SEMANTIC_TEXT_FIELDS}
    output.update({
        'hook_type': 'none_unclear', 'hook_mechanism': 'none_unclear',
        'hook_target': 'none_unclear', 'audience_specificity': 'unclear',
        'angle_type': 'other_unclear', 'emotion_primary': 'neutral_unclear',
        'emotion_secondary': None, 'content_function_primary': 'other_unclear',
        'content_function_secondary': None, 'content_role_primary': 'unclear',
        'content_role_secondary': None, 'content_format': 'other_unclear',
        'narrative_structure': 'other_unclear', 'awareness_stage': 'mixed_unclear',
        'proof_type': 'none', 'authority_mechanism': 'none_unclear',
        'creator_positioning_signal': 'none_unclear', 'cta_type': 'none',
        'cta_secondary_type': None, 'commercial_intent': 'none',
        'offer_integration': 'none', 'offer_type': 'none', 'monetization_model': 'none',
    })
    return output


class FakeProvider:
    capabilities = SemanticProviderCapabilities()

    def __init__(self, result=VALID_OUTPUT, exception=None):
        self.result = result
        self.exception = exception
        self.calls = []

    def extract(self, payload):
        self.calls.append(dict(payload))
        if self.exception:
            raise self.exception
        return self.result


def make_video(db, *, caption='  Public caption  '):
    channel = Channel(platform='tiktok', username=f'creator-{uuid4()}', nickname='Creator')
    db.add(channel); db.flush()
    video = Video(
        channel_id=channel.id, tiktok_id=str(uuid4().int)[:30], author='creator', nickname='Creator',
        caption=caption, published_at=datetime(2026, 1, 1, tzinfo=timezone.utc), duration=39.0,
        url='https://www.tiktok.com/@creator/video/1', enrichment_status='missing',
    )
    db.add(video); db.flush()
    return video


def add_transcript(db, video, *, text='  Hola mundo  ', language=' es '):
    transcript = Transcript(video_id=video.id, text=text, language=language, duration=2.0, model='small')
    db.add(transcript); db.flush()
    return transcript


def extract(db, video, provider, **kwargs):
    return extract_semantic_viral_dna_for_video(
        db, video, provider, semantic_provider='fake', semantic_model='fake-semantic-model', **kwargs
    )


def test_complete_output_persists_all_normalized_semantic_dimensions(db):
    video = make_video(db); add_transcript(db, video)
    result = extract(db, video, FakeProvider({**VALID_OUTPUT, 'hook_text': '  hook\ntext  '}))
    assert result.status == 'completed'
    assert result.row.hook_text == 'hook text'
    assert {field: getattr(result.row, field) for field in SEMANTIC_OUTPUT_FIELDS} == {
        **VALID_OUTPUT, 'hook_text': 'hook text',
    }
    assert result.row.semantic_model == 'fake:fake-semantic-model'
    assert result.row.semantic_prompt_version == SEMANTIC_PROMPT_VERSION
    assert result.row.semantic_extracted_at is not None
    assert len(result.row.semantic_input_sha256) == 64


def test_minimum_output_accepts_null_text_and_nonapplicable_enums(db):
    video = make_video(db); add_transcript(db, video)
    result = extract(db, video, FakeProvider(minimum_output()))
    assert result.status == 'completed'
    assert result.row.objection is None and result.row.reframe is None
    assert result.row.offer_type == result.row.monetization_model == 'none'


@pytest.mark.parametrize('field,value', [
    ('hook_type', 'warning'), ('hook_mechanism', 'fear_loss'), ('hook_target', 'mistake'),
    ('audience_specificity', 'micro_niche'), ('angle_type', 'contrarian'),
    ('emotion_primary', 'fear'), ('emotion_secondary', 'hope'),
    ('content_function_primary', 'sell'), ('content_function_secondary', 'social_proof'),
    ('content_role_primary', 'conversion'), ('content_role_secondary', 'community'),
    ('content_format', 'myth_busting'), ('narrative_structure', 'myth_reframe'),
    ('awareness_stage', 'offer_aware'), ('proof_type', 'client_result'),
    ('authority_mechanism', 'results'), ('creator_positioning_signal', 'challenger'),
    ('cta_type', 'book_call'), ('cta_secondary_type', 'download'),
    ('commercial_intent', 'direct'), ('offer_integration', 'explicit_pitch'),
    ('offer_type', 'consulting_coaching'), ('monetization_model', 'lead_generation'),
])
def test_each_semantic_enum_dimension_accepts_closed_contract_member(db, field, value):
    video = make_video(db); add_transcript(db, video)
    output = {**VALID_OUTPUT, field: value}
    # Keep the paired value distinct in the few parameterized secondary cases.
    if field == 'emotion_primary': output['emotion_secondary'] = 'hope'
    assert extract(db, video, FakeProvider(output)).status == 'completed'


@pytest.mark.parametrize('primary,secondary', [
    ('emotion_primary', 'emotion_secondary'),
    ('content_function_primary', 'content_function_secondary'),
    ('content_role_primary', 'content_role_secondary'),
    ('cta_type', 'cta_secondary_type'),
])
def test_secondary_must_not_repeat_primary(db, primary, secondary):
    video = make_video(db); add_transcript(db, video)
    output = {**VALID_OUTPUT, secondary: VALID_OUTPUT[primary]}
    assert extract(db, video, FakeProvider(output)).status == 'failed'


@pytest.mark.parametrize('field', SEMANTIC_TEXT_FIELDS)
def test_text_fields_enforce_maximum_length(db, field):
    video = make_video(db); add_transcript(db, video)
    output = {**VALID_OUTPUT, field: 'x' * (SEMANTIC_TEXT_FIELDS[field] + 1)}
    assert extract(db, video, FakeProvider(output)).status == 'failed'


def test_extra_missing_invalid_and_markdown_fields_are_rejected(db):
    invalid_outputs = (
        {**VALID_OUTPUT, 'extra': 'no'},
        {key: value for key, value in VALID_OUTPUT.items() if key != 'topic'},
        {**VALID_OUTPUT, 'hook_type': 'visual'},
        {**VALID_OUTPUT, 'hook_type': None},
        {**VALID_OUTPUT, 'pain': '- fabricated list item'},
    )
    for output in invalid_outputs:
        video = make_video(db); add_transcript(db, video)
        assert extract(db, video, FakeProvider(output)).status == 'failed'


def test_no_transcript_skips_and_never_calls_provider(db):
    provider = FakeProvider()
    result = extract(db, make_video(db), provider)
    assert result.status == 'skipped_no_transcript'
    assert provider.calls == []


def test_provider_exception_fails_without_persisting_output(db):
    video = make_video(db); add_transcript(db, video)
    result = extract(db, video, FakeProvider(exception=RuntimeError('do not log provider response')))
    assert result.status == 'failed' and result.error_code == 'semantic_provider_exception'
    assert result.row.hook_text is None and result.row.semantic_input_sha256 is None


def test_same_hash_is_unchanged_without_provider_call(db):
    video = make_video(db); add_transcript(db, video); provider = FakeProvider()
    assert extract(db, video, provider).status == 'completed'
    assert extract(db, video, provider).status == 'unchanged'
    assert len(provider.calls) == 1


@pytest.mark.parametrize('change', ['provider', 'model', 'prompt', 'caption', 'transcript'])
def test_each_versioned_or_text_input_change_reextracts(db, change):
    video = make_video(db); transcript = add_transcript(db, video); provider = FakeProvider()
    assert extract(db, video, provider).status == 'completed'
    if change == 'provider':
        result = extract_semantic_viral_dna_for_video(
            db, video, provider, semantic_provider='other-fake', semantic_model='fake-semantic-model'
        )
    elif change == 'model':
        result = extract_semantic_viral_dna_for_video(
            db, video, provider, semantic_provider='fake', semantic_model='other-model'
        )
    elif change == 'prompt':
        result = extract(db, video, provider, semantic_prompt_version='viral-dna-semantic-v2')
    elif change == 'caption':
        video.caption = 'changed caption'; db.flush(); result = extract(db, video, provider)
    else:
        transcript.text = 'changed transcript'; db.flush(); result = extract(db, video, provider)
    assert result.status == 'completed' and len(provider.calls) == 2


def test_contract_change_reextracts(monkeypatch, db):
    video = make_video(db); add_transcript(db, video); provider = FakeProvider()
    assert extract(db, video, provider).status == 'completed'
    monkeypatch.setattr('app.viral_dna.SEMANTIC_CONTRACT_VERSION', 'viral-dna-semantic-contract-v-next')
    assert extract(db, video, provider).status == 'completed'
    assert len(provider.calls) == 2


def test_hash_covers_only_contract_policy_and_text_inputs():
    base = semantic_input_sha256(
        language='es', caption='caption', transcript='texto', semantic_provider='fake', semantic_model='model'
    )
    assert base == semantic_input_sha256(
        language='es', caption='caption', transcript='texto', semantic_provider='fake', semantic_model='model'
    )
    assert base != semantic_input_sha256(
        language='en', caption='caption', transcript='texto', semantic_provider='fake', semantic_model='model'
    )
    assert base != semantic_input_sha256(
        language='es', caption='caption', transcript='texto', semantic_provider='fake', semantic_model='other'
    )


def test_phase_a_remains_unchanged_after_semantic_failure(db):
    video = make_video(db); add_transcript(db, video)
    deterministic = extract_viral_dna_for_video(db, video).row
    fields = ('deterministic_input_sha256', 'duration_seconds', 'caption_present', 'caption_char_count',
              'transcript_id', 'transcript_word_count', 'transcript_duration_seconds',
              'words_per_second', 'audio_assessment_id')
    before = {field: getattr(deterministic, field) for field in fields}
    failed = extract(db, video, FakeProvider({**VALID_OUTPUT, 'cta_type': 'bad'})).row
    assert {field: getattr(failed, field) for field in fields} == before


def test_semantic_dna_is_global_and_provider_receives_no_analysis_or_performance_data(db):
    video = make_video(db); add_transcript(db, video)
    for rank in (1, 2):
        analysis = Analysis(channel_id=video.channel_id, status='awaiting_audio', video_count=1,
                            median_views=Decimal('10'), requested_transcripts=0, completed_transcripts=0)
        db.add(analysis); db.flush()
        db.add(VideoSnapshot(
            analysis_id=analysis.id, video_id=video.id, views=rank, likes=0, comments=0, shares=0,
            favorites=0, like_rate=0, comment_rate=0, share_rate=0, favorite_rate=0,
            engagement_rate=0, outlier_score=0, overall_rank=rank, transcription_rank=None,
            transcript_eligible=False, transcript_skip_reason=None,
        ))
    db.flush(); provider = FakeProvider()
    assert extract(db, video, provider).status == 'completed'
    assert extract(db, video, provider).status == 'unchanged'
    assert db.scalar(select(func.count()).select_from(ViralDNA)) == 1
    assert provider.calls == [{'language': 'es', 'caption': 'Public caption', 'transcript': '  Hola mundo  '}]
    assert not ({'analysis', 'user', 'views', 'likes', 'comments', 'shares', 'favorites', 'engagement',
                 'outlier_score', 'ranking', 'follower_count'} & set(provider.calls[0]))


def test_contract_schema_is_final_transport_neutral_and_prompt_has_boundaries():
    assert SEMANTIC_VIRAL_DNA_JSON_SCHEMA['additionalProperties'] is False
    assert set(SEMANTIC_VIRAL_DNA_JSON_SCHEMA['required']) == set(SEMANTIC_OUTPUT_FIELDS)
    assert 'visual' in SEMANTIC_PROMPT and 'performance' in SEMANTIC_PROMPT
    assert 'reasoning' in SEMANTIC_PROMPT


def test_semantic_prompt_has_provider_consistency_rules():
    assert 'derive hook_text, hook_type, hook_mechanism, and hook_target from its opening spoken words' in SEMANTIC_PROMPT
    assert 'never replace that hook with the caption' in SEMANTIC_PROMPT
    assert 'Write every free-text field in the supplied language' in SEMANTIC_PROMPT
    assert 'exact canonical English enum tokens without translating them' in SEMANTIC_PROMPT
    assert 'set secondary to null unless a distinct clear second signal exists, and never repeat primary' in SEMANTIC_PROMPT
    assert 'write only what has reasonable caption or transcript evidence; otherwise use null' in SEMANTIC_PROMPT
    assert 'Never infer merely plausible objections, fears, audience identities, promises' in SEMANTIC_PROMPT


def test_registry_resolves_an_injected_fake_adapter_without_core_provider_imports():
    config = SemanticProviderConfig(provider_name='registry-fake', model='model-a')
    register_semantic_provider('registry-fake', lambda received: FakeProvider(), replace=True)
    assert isinstance(get_semantic_provider('registry-fake', config), FakeProvider)


def test_validate_semantic_output_returns_compact_nullable_contract():
    assert validate_semantic_output(minimum_output()) == minimum_output()
