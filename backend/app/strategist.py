"""Compact internal Strategist. Global inputs never contain private context."""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .channel_intelligence_contract import canonical_json_sha256
from .models import ChannelStrategicPlaybook

LOGGER = logging.getLogger(__name__)
PLAYBOOK_SCHEMA_VERSION = 'kurukin-channel-playbook-v1'
PLAYBOOK_PROMPT_VERSION = 'kurukin-channel-playbook-prompt-v1'
PERSONAL_STRATEGY_V1_SCHEMA_VERSION = 'kurukin-personal-strategy-v1'
PERSONAL_STRATEGY_SCHEMA_VERSION = 'kurukin-personal-strategy-v2'
PERSONAL_STRATEGY_PROMPT_VERSION = 'kurukin-personal-strategy-prompt-v2'


def diagnostics(mode: str, *, total: int, reused: int, changed: int, raw: int, structured: int) -> dict[str, int | str]:
    result = {'mode': mode, 'videos_total': total, 'videos_reused': reused, 'videos_new_or_changed': changed,
              'raw_transcripts_included': raw, 'structured_prior_video_analyses_included': structured}
    LOGGER.info('knowledge_preparation %s', result)
    return result


def _representative(channel_intelligence: dict[str, Any], records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for section in ('winning_patterns', 'hooks', 'narratives', 'pains', 'desires', 'ctas', 'repetition_clusters'):
        for item in channel_intelligence.get(section, [])[:5]:
            if not isinstance(item, dict): continue
            ids = [video_id for evidence in item.get('evidence', []) if isinstance(evidence, dict) for video_id in evidence.get('video_ids', [])][:4]
            output.append({'section': section, 'pattern': item.get('name'), 'description': item.get('description'),
                           'evidence_video_ids': ids, 'metrics': [{key: records.get(video_id, {}).get(key) for key in ('video_id', 'views', 'engagement_rate', 'outlier_score')} for video_id in ids]})
    return output[:30]


def playbook_input(analysis: Any, records: list[dict[str, Any]], videos: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {row['video_id']: row for row in records}
    return {'schema': PLAYBOOK_SCHEMA_VERSION, 'source_analysis_id': str(analysis.id),
            'channel_intelligence': analysis.channel_intelligence, 'representative_evidence': _representative(analysis.channel_intelligence, by_id),
            'known_video_intelligence': videos[:40],
            'diagnostics': diagnostics('reuse', total=len(records), reused=len(videos), changed=0, raw=0, structured=len(videos))}


def _openai_json(payload: dict[str, Any], *, model: str, api_key: str, contract: str, prompt: str) -> dict[str, Any]:
    request = {'model': model, 'input': [{'role': 'system', 'content': [{'type': 'input_text', 'text': prompt}]},
        {'role': 'user', 'content': [{'type': 'input_text', 'text': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}]}],
        'text': {'format': {'type': 'json_object'}}}
    response = httpx.post('https://api.openai.com/v1/responses', headers={'Authorization': f'Bearer {api_key}'}, json=request, timeout=60)
    response.raise_for_status(); decoded = response.json(); raw = decoded.get('output_text')
    if not isinstance(raw, str):
        # Some configured Responses models return text only in the completed
        # message content, rather than exposing the convenience output_text key.
        for item in decoded.get('output', []):
            if not isinstance(item, dict) or item.get('type') != 'message':
                continue
            for content in item.get('content', []):
                if isinstance(content, dict) and content.get('type') == 'output_text' and isinstance(content.get('text'), str):
                    raw = content['text']; break
            if isinstance(raw, str):
                break
    if not isinstance(raw, str): raise ValueError('strategist_invalid_response')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('schema') != contract: raise ValueError('strategist_invalid_contract')
    return value


def get_or_create_playbook(db: Session, *, channel_id: Any, analysis: Any, records: list[dict[str, Any]], videos: list[dict[str, Any]],
                           provider: str, model: str, generate: Callable[[dict[str, Any]], dict[str, Any]]) -> tuple[Any, bool]:
    existing = db.scalar(select(ChannelStrategicPlaybook).where(ChannelStrategicPlaybook.channel_id == channel_id,
        ChannelStrategicPlaybook.source_payload_sha == analysis.payload_sha256,
        ChannelStrategicPlaybook.performance_state_hash == analysis.performance_state_hash,
        ChannelStrategicPlaybook.prompt_version == PLAYBOOK_PROMPT_VERSION,
        ChannelStrategicPlaybook.provider == provider, ChannelStrategicPlaybook.model == model))
    if existing is not None: return existing, True
    payload = playbook_input(analysis, records, videos)
    value = generate(payload)
    if value.get('schema') != PLAYBOOK_SCHEMA_VERSION: raise ValueError('strategist_invalid_contract')
    row = ChannelStrategicPlaybook(channel_id=channel_id, source_analysis_id=analysis.id,
        source_payload_sha=analysis.payload_sha256, semantic_corpus_hash=analysis.semantic_corpus_hash,
        performance_state_hash=analysis.performance_state_hash, schema_version=PLAYBOOK_SCHEMA_VERSION,
        prompt_version=PLAYBOOK_PROMPT_VERSION, provider=provider, model=model,
        payload_sha256=canonical_json_sha256(payload), payload_json=value)
    db.add(row); db.commit(); return row, False


def configured_playbook_generator(settings: Any) -> tuple[str, str, Callable[[dict[str, Any]], dict[str, Any]]]:
    policy = settings.semantic_viral_dna_routing_policy
    if policy is None or policy.primary.provider_name != 'openai': raise RuntimeError('strategist_provider_not_configured')
    provider, model = policy.primary.provider_name, policy.primary.model
    key = settings.semantic_provider_api_key(provider)
    prompt = ('You are Kurukin AI Strategist. Return only valid JSON for kurukin-channel-playbook-v1. '
              'Use only supplied structured public evidence. Distinguish observation, interpretation, and hypothesis; do not make causal claims from correlation. '
              'Required fields: schema, executive_thesis, dominant_formula, top_moves, what_to_repeat, what_to_avoid, hook_playbook, narrative_playbook, pain_desire_playbook, conversion_playbook, repetition_playbook, recommended_experiments, confidence_notes.')
    return provider, model, lambda payload: _openai_json(payload, model=model, api_key=key, contract=PLAYBOOK_SCHEMA_VERSION, prompt=prompt)


def configured_personal_generator(settings: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    policy = settings.semantic_viral_dna_routing_policy
    if policy is None or policy.primary.provider_name != 'openai': raise RuntimeError('strategist_provider_not_configured')
    key = settings.semantic_provider_api_key(policy.primary.provider_name)
    prompt = ('You are Kurukin AI Strategist. Return only valid JSON for kurukin-personal-strategy-v2. '
              'Use the compact public channel playbook, selected public evidence, and PRIVATE business profile. Do not claim private context is public channel knowledge and do not copy the creator identity, claims, wording, or creative expression. '
              'Recommendations are hypotheses, not causal certainty. Every important recommendation must cite only supplied evidence_video_ids. '
              'Required fields: schema,strategic_fit,patterns_to_adapt,patterns_to_avoid,audience_opportunities,pain_opportunities,desire_opportunities,hook_adaptations,narrative_adaptations,cta_strategy,offer_alignment,content_pillars,test_priorities,first_content_plan,executive_recommendation. '
              'Each patterns_to_adapt item requires name,source_pattern,why_it_works_in_source,fit_for_business,adaptation,what_not_to_copy,recommended_use,evidence_video_ids,confidence. '
              'Each test_priorities item requires priority,hypothesis,what_to_test,success_signal,source_patterns,evidence_video_ids.')
    return lambda payload: _openai_json(payload, model=policy.primary.model, api_key=key, contract=PERSONAL_STRATEGY_SCHEMA_VERSION, prompt=prompt)


def _compact_playbook(playbook: dict[str, Any]) -> dict[str, Any]:
    """Keep the private call on structured knowledge, never a Research Pack."""
    keys = ('executive_thesis', 'dominant_formula', 'top_moves', 'what_to_repeat', 'what_to_avoid',
            'hook_playbook', 'narrative_playbook', 'pain_desire_playbook', 'conversion_playbook',
            'repetition_playbook', 'recommended_experiments', 'confidence_notes')
    return {key: playbook.get(key) for key in keys if key in playbook}


def personal_strategy_input(playbook: dict[str, Any], context: dict[str, str],
                            supporting_evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Private V2 input: business profile plus compact public knowledge only."""
    evidence = supporting_evidence or []
    safe_evidence = [{key: item.get(key) for key in ('video_id', 'title', 'url', 'views', 'engagement_rate',
                      'outlier_score', 'shares', 'source_pattern', 'why_it_matters') if key in item}
                     for item in evidence if isinstance(item, dict)]
    return {
        'schema': PERSONAL_STRATEGY_SCHEMA_VERSION,
        'private_business_profile': context,
        'compact_channel_playbook': _compact_playbook(playbook),
        'selected_supporting_evidence': safe_evidence[:20],
        'diagnostics': {'raw_research_pack_included': False, 'raw_transcripts_included': 0,
                        'structured_knowledge_reused': True},
    }


def validate_personal_strategy(value: Any, *, known_video_ids: set[str] | None = None) -> list[str]:
    """Validate V2 before persistence; legacy V1 stays readable but is never generated."""
    if not isinstance(value, dict):
        return ['strategy must be one JSON object']
    if value.get('schema') == PERSONAL_STRATEGY_V1_SCHEMA_VERSION:
        return []
    if value.get('schema') != PERSONAL_STRATEGY_SCHEMA_VERSION:
        return ['strategy schema is invalid']
    required_narratives = ('cta_strategy', 'offer_alignment', 'executive_recommendation')
    required_lists = ('patterns_to_adapt', 'patterns_to_avoid', 'audience_opportunities', 'pain_opportunities',
                      'desire_opportunities', 'hook_adaptations', 'narrative_adaptations', 'content_pillars',
                      'test_priorities', 'first_content_plan')
    errors = []
    if not isinstance(value.get('strategic_fit'), (str, dict)):
        errors.append('strategic_fit is required')
    def meaningful_narrative(item: Any) -> bool:
        return (isinstance(item, str) and bool(item.strip())) or (isinstance(item, dict) and bool(item))
    errors.extend(f'{key} is required' for key in required_narratives if not meaningful_narrative(value.get(key)))
    errors.extend(f'{key} must be a list' for key in required_lists if not isinstance(value.get(key), list))
    allowed_ids = known_video_ids or set()
    def evidence_errors(rows: Any, required: tuple[str, ...], label: str):
        if not isinstance(rows, list):
            return
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f'{label}[{index}] must be an object'); continue
            missing = [key for key in required if key not in row or row[key] in ('', None)]
            if missing: errors.append(f'{label}[{index}] missing ' + ', '.join(missing))
            ids = row.get('evidence_video_ids', [])
            if not isinstance(ids, list) or not all(isinstance(video_id, str) and video_id for video_id in ids):
                errors.append(f'{label}[{index}] evidence_video_ids must be a list of IDs')
            elif allowed_ids and not set(ids) <= allowed_ids:
                errors.append(f'{label}[{index}] cites unknown evidence IDs')
    evidence_errors(value.get('patterns_to_adapt'), ('name', 'source_pattern', 'why_it_works_in_source',
                    'fit_for_business', 'adaptation', 'what_not_to_copy', 'recommended_use', 'confidence',
                    'evidence_video_ids'), 'patterns_to_adapt')
    evidence_errors(value.get('test_priorities'), ('priority', 'hypothesis', 'what_to_test', 'success_signal',
                    'source_patterns', 'evidence_video_ids'), 'test_priorities')
    return errors[:100]


def personal_strategy_display(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize historical V1 payloads for the V2 product renderer without rewriting them."""
    if value.get('schema') != PERSONAL_STRATEGY_V1_SCHEMA_VERSION:
        return value
    mechanisms = value.get('selected_mechanisms', [])
    rows = []
    for item in mechanisms if isinstance(mechanisms, list) else []:
        name = item if isinstance(item, str) else item.get('name', 'Mecanismo') if isinstance(item, dict) else 'Mecanismo'
        rows.append({'name': name, 'source_pattern': name, 'why_it_works_in_source': '', 'fit_for_business': '',
                     'adaptation': '', 'what_not_to_copy': '', 'recommended_use': '', 'evidence_video_ids': [], 'confidence': 'legacy'})
    return {'schema': PERSONAL_STRATEGY_V1_SCHEMA_VERSION, 'strategic_fit': value.get('business_summary', ''),
            'patterns_to_adapt': rows, 'patterns_to_avoid': value.get('risks_or_constraints', []),
            'audience_opportunities': value.get('content_positioning', []),
            'pain_opportunities': value.get('recommended_content_pillars', []), 'desire_opportunities': [],
            'hook_adaptations': value.get('recommended_hook_mix', []),
            'narrative_adaptations': value.get('recommended_narrative_mix', []),
            'cta_strategy': value.get('recommended_cta_strategy', ''), 'offer_alignment': '',
            'content_pillars': value.get('recommended_content_pillars', []),
            'test_priorities': value.get('recommended_experiments', []), 'first_content_plan': [],
            'executive_recommendation': value.get('content_brief_for_creator', ''), 'legacy': True}
