"""Compact internal Strategist v1.  Global inputs never contain private context."""
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
PERSONAL_STRATEGY_SCHEMA_VERSION = 'kurukin-personal-strategy-v1'
PERSONAL_STRATEGY_PROMPT_VERSION = 'kurukin-personal-strategy-prompt-v1'


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
    prompt = ('You are Kurukin AI Strategist. Return only valid JSON for kurukin-personal-strategy-v1. '
              'Use the public playbook plus PRIVATE business context. Do not claim private context is public channel knowledge. '
              'Required fields: schema,business_summary,selected_mechanisms,content_positioning,recommended_content_pillars,recommended_hook_mix,recommended_narrative_mix,recommended_cta_strategy,recommended_experiments,risks_or_constraints,content_brief_for_creator.')
    return lambda payload: _openai_json(payload, model=policy.primary.model, api_key=key, contract=PERSONAL_STRATEGY_SCHEMA_VERSION, prompt=prompt)


def personal_strategy_input(playbook: dict[str, Any], context: dict[str, str]) -> dict[str, Any]:
    return {'schema': PERSONAL_STRATEGY_SCHEMA_VERSION, 'global_playbook': playbook, 'private_business_context': context}
