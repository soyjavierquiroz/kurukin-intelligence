"""Closed import contract for incremental Channel Intelligence updates."""
from __future__ import annotations
from typing import Any
from .channel_intelligence_contract import (CHANNEL_INTELLIGENCE_PROMPT_VERSION, CHANNEL_INTELLIGENCE_SCHEMA,
    VIDEO_INTELLIGENCE_SCHEMA, _check, canonical_json_sha256)

CHANNEL_UPDATE_SCHEMA_VERSION = 'kurukin-channel-update-v1'
CHANNEL_UPDATE_PROMPT_VERSION = 'kurukin-channel-update-prompt-v1'
CHANNEL_UPDATE_JSON_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['schema', 'prompt_version', 'processor', 'base_state', 'target_state', 'upsert_videos', 'channel_intelligence'],
    'properties': {
        'schema': {'const': CHANNEL_UPDATE_SCHEMA_VERSION}, 'prompt_version': {'const': CHANNEL_UPDATE_PROMPT_VERSION},
        'processor': {'type': 'string', 'minLength': 1, 'maxLength': 160},
        'base_state': {'type': 'object', 'additionalProperties': False, 'required': ['analysis_id', 'payload_sha256', 'semantic_corpus_hash'],
            'properties': {'analysis_id': {'type': 'string', 'minLength': 1, 'maxLength': 64}, 'payload_sha256': {'type': 'string', 'minLength': 64, 'maxLength': 64}, 'semantic_corpus_hash': {'type': 'string', 'minLength': 64, 'maxLength': 64}}},
        'target_state': {'type': 'object', 'additionalProperties': False, 'required': ['semantic_corpus_hash'],
            'properties': {'semantic_corpus_hash': {'type': 'string', 'minLength': 64, 'maxLength': 64}}},
        'upsert_videos': {'type': 'array', 'minItems': 1, 'maxItems': 10000, 'items': VIDEO_INTELLIGENCE_SCHEMA},
        'channel_intelligence': CHANNEL_INTELLIGENCE_SCHEMA,
    },
}

def validate_channel_update(value: Any, *, base_analysis_id: str, base_payload_sha: str, base_semantic_hash: str,
                            target_semantic_hash: str, expected_ids: set[str], merged_ids: set[str]) -> list[str]:
    errors: list[str] = []
    _check(value, CHANNEL_UPDATE_JSON_SCHEMA, '$', errors)
    if not isinstance(value, dict): return errors[:100]
    base = value.get('base_state', {}); target = value.get('target_state', {})
    if isinstance(base, dict):
        if base.get('analysis_id') != base_analysis_id: errors.append('$.base_state.analysis_id does not match the base analysis')
        if base.get('payload_sha256') != base_payload_sha: errors.append('$.base_state.payload_sha256 does not match the base analysis')
        if base.get('semantic_corpus_hash') != base_semantic_hash: errors.append('$.base_state.semantic_corpus_hash does not match the base state')
    if isinstance(target, dict) and target.get('semantic_corpus_hash') != target_semantic_hash:
        errors.append('$.target_state.semantic_corpus_hash does not match the current semantic corpus')
    videos = value.get('upsert_videos', [])
    if isinstance(videos, list):
        ids = [row.get('video_id') for row in videos if isinstance(row, dict)]
        if set(ids) != expected_ids:
            missing, unknown = expected_ids - set(ids), set(ids) - expected_ids
            if missing: errors.append('$.upsert_videos is missing expected delta videos: ' + ', '.join(sorted(missing)))
            if unknown: errors.append('$.upsert_videos includes unexpected video IDs: ' + ', '.join(sorted(unknown)))
        if len(ids) != len(set(ids)): errors.append('$.upsert_videos video_id values must be unique')
    def evidence_ids(item):
        if isinstance(item, dict):
            if isinstance(item.get('video_ids'), list): yield from item['video_ids']
            for child in item.values(): yield from evidence_ids(child)
        elif isinstance(item, list):
            for child in item: yield from evidence_ids(child)
    unknown_evidence = set(evidence_ids(value)) - merged_ids
    if unknown_evidence: errors.append('Evidence references videos outside merged corpus: ' + ', '.join(sorted(unknown_evidence)))
    return errors[:100]
