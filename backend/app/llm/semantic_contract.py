"""Kurukin-owned, provider-neutral Semantic Viral DNA v1 contract.

This is the one source of truth for the persisted semantic response shape.
Provider adapters may pass ``SEMANTIC_VIRAL_DNA_JSON_SCHEMA`` to a structured
output feature, but Kurukin always validates the returned value itself.
"""
from __future__ import annotations


# v1 was not executed in production. This final definition deliberately keeps
# the v1 name so the first production extraction uses the frozen contract.
SEMANTIC_CONTRACT_VERSION = 'viral-dna-semantic-contract-v1'

SEMANTIC_TEXT_FIELDS: dict[str, int] = {
    'hook_text': 280,
    'topic': 120,
    'subtopic': 120,
    'angle_summary': 240,
    'pain': 240,
    'desire': 240,
    'fear': 240,
    'audience_identity': 240,
    'belief': 240,
    'objection': 240,
    'promise': 240,
    'reframe': 280,
    'emotional_arc': 200,
    'cta_text': 280,
}

SEMANTIC_ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    'hook_type': (
        'question', 'bold_claim', 'problem', 'how_to', 'contrarian', 'story_open',
        'list_open', 'warning', 'case_result', 'direct_command', 'news_update', 'none_unclear',
    ),
    'hook_mechanism': (
        'curiosity_gap', 'pain_identification', 'self_identification', 'specificity', 'novelty',
        'contradiction', 'fear_loss', 'aspiration', 'social_proof', 'authority', 'urgency',
        'open_loop', 'challenge', 'surprise', 'none_unclear',
    ),
    'hook_target': (
        'problem', 'desire', 'identity', 'outcome', 'mistake', 'belief', 'opportunity', 'news',
        'story', 'none_unclear',
    ),
    'audience_specificity': ('generic', 'broad_segment', 'niche', 'micro_niche', 'unclear'),
    'angle_type': (
        'pain', 'mistake', 'myth', 'how_to', 'opportunity', 'contrarian', 'case_study',
        'personal_experience', 'trend_news', 'comparison', 'warning', 'aspiration', 'identity',
        'process', 'other_unclear',
    ),
    'emotion_primary': (
        'curiosity', 'fear', 'anger', 'frustration', 'hope', 'aspiration', 'validation', 'surprise',
        'urgency', 'belonging', 'humor', 'sadness', 'confidence', 'desire', 'neutral_unclear',
    ),
    'emotion_secondary': (
        'curiosity', 'fear', 'anger', 'frustration', 'hope', 'aspiration', 'validation', 'surprise',
        'urgency', 'belonging', 'humor', 'sadness', 'confidence', 'desire', 'neutral_unclear',
    ),
    'content_function_primary': (
        'educate', 'inform', 'inspire_motivate', 'entertain', 'relate_validate', 'provoke_challenge',
        'document', 'demonstrate', 'social_proof', 'sell', 'other_unclear',
    ),
    'content_function_secondary': (
        'educate', 'inform', 'inspire_motivate', 'entertain', 'relate_validate', 'provoke_challenge',
        'document', 'demonstrate', 'social_proof', 'sell', 'other_unclear',
    ),
    'content_role_primary': ('reach', 'positioning', 'nurture', 'community', 'conversion', 'unclear'),
    'content_role_secondary': ('reach', 'positioning', 'nurture', 'community', 'conversion', 'unclear'),
    'content_format': (
        'how_to', 'list', 'story', 'opinion', 'explanation', 'problem_solution', 'case_study',
        'testimonial', 'confession', 'q_and_a', 'myth_busting', 'warning', 'comparison', 'review_demo',
        'commentary', 'reaction', 'demonstration', 'other_unclear',
    ),
    'narrative_structure': (
        'problem_solution', 'symptom_cause_solution', 'story_lesson', 'claim_proof_action',
        'question_answer', 'myth_reframe', 'case_result_method', 'before_after', 'warning_fix',
        'opinion_argument', 'listicle', 'problem_agitation_solution', 'hook_value_cta', 'other_unclear',
    ),
    'awareness_stage': (
        'unaware', 'problem_aware', 'solution_aware', 'offer_aware', 'most_aware', 'mixed_unclear',
    ),
    'proof_type': (
        'none', 'personal_experience', 'client_result', 'case_study', 'numbers', 'demonstration',
        'testimonial', 'authority_reference', 'social_proof', 'other_unclear',
    ),
    'authority_mechanism': (
        'none_unclear', 'expertise', 'experience', 'results', 'process', 'teaching',
        'contrarian_criterion', 'transparency',
    ),
    'creator_positioning_signal': (
        'expert', 'relatable', 'aspirational', 'challenger', 'educator', 'documentarian',
        'entertainer', 'curator', 'none_unclear',
    ),
    'cta_type': (
        'follow', 'comment', 'save', 'share', 'dm', 'visit_profile', 'link_in_bio', 'subscribe',
        'download', 'book_call', 'purchase', 'multiple', 'none', 'unclear',
    ),
    'cta_secondary_type': (
        'follow', 'comment', 'save', 'share', 'dm', 'visit_profile', 'link_in_bio', 'subscribe',
        'download', 'book_call', 'purchase', 'none', 'unclear',
    ),
    'commercial_intent': ('none', 'soft', 'direct', 'unclear'),
    'offer_integration': (
        'none', 'implicit', 'contextual', 'method_reference', 'case_study', 'lead_magnet',
        'explicit_pitch', 'unclear',
    ),
    'offer_type': (
        'none', 'service', 'consulting_coaching', 'course', 'digital_product', 'membership', 'software',
        'physical_product', 'affiliate', 'event', 'real_estate', 'other', 'unknown',
    ),
    'monetization_model': (
        'none', 'service', 'lead_generation', 'digital_product', 'subscription', 'ecommerce', 'affiliate',
        'sponsorship', 'marketplace', 'advertising', 'other', 'unknown',
    ),
}

SEMANTIC_NULLABLE_FIELDS = frozenset({
    *SEMANTIC_TEXT_FIELDS,
    'emotion_secondary',
    'content_function_secondary',
    'content_role_secondary',
    'cta_secondary_type',
})
SEMANTIC_SECONDARY_PRIMARY_PAIRS = (
    ('emotion_primary', 'emotion_secondary'),
    ('content_function_primary', 'content_function_secondary'),
    ('content_role_primary', 'content_role_secondary'),
    ('cta_type', 'cta_secondary_type'),
)
_SEMANTIC_SECONDARY_CONSTRAINT_NAMES = {
    ('emotion_primary', 'emotion_secondary'): 'ck_vdna_emotion_pair_diff',
    ('content_function_primary', 'content_function_secondary'): 'ck_vdna_function_pair_diff',
    ('content_role_primary', 'content_role_secondary'): 'ck_vdna_role_pair_diff',
    ('cta_type', 'cta_secondary_type'): 'ck_vdna_cta_pair_diff',
}
SEMANTIC_OUTPUT_FIELDS = tuple((*SEMANTIC_TEXT_FIELDS, *SEMANTIC_ENUM_FIELDS))


def semantic_secondary_constraint_name(primary: str, secondary: str) -> str:
    """Return the PostgreSQL-safe database constraint name for one pair."""
    return _SEMANTIC_SECONDARY_CONSTRAINT_NAMES[(primary, secondary)]


def _property_schema(field: str) -> dict[str, object]:
    if field in SEMANTIC_TEXT_FIELDS:
        return {'type': ['string', 'null'], 'maxLength': SEMANTIC_TEXT_FIELDS[field]}
    values: list[str | None] = list(SEMANTIC_ENUM_FIELDS[field])
    if field in SEMANTIC_NULLABLE_FIELDS:
        values.append(None)
    schema: dict[str, object] = {'enum': values}
    schema['type'] = ['string', 'null'] if field in SEMANTIC_NULLABLE_FIELDS else 'string'
    return schema


SEMANTIC_VIRAL_DNA_JSON_SCHEMA: dict[str, object] = {
    'type': 'object',
    'additionalProperties': False,
    'required': list(SEMANTIC_OUTPUT_FIELDS),
    'properties': {field: _property_schema(field) for field in SEMANTIC_OUTPUT_FIELDS},
}
