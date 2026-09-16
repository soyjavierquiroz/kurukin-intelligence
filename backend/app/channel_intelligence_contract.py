"""Frozen contract for externally generated Channel Intelligence v1.

The external model is a producer, never an authority on the shape of data we
persist.  This module owns both the machine-readable JSON Schema and the
small, dependency-free validator used by the import dry run.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


CHANNEL_INTELLIGENCE_SCHEMA_VERSION = 'kurukin-channel-analysis-v1'
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def _string(maximum: int) -> dict[str, Any]:
    return {'type': 'string', 'minLength': 1, 'maxLength': maximum}


EVIDENCE_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['claim', 'video_ids'],
    'properties': {
        'claim': _string(600),
        'video_ids': {'type': 'array', 'minItems': 1, 'maxItems': 25,
                      'items': _string(64), 'uniqueItems': True},
    },
}

VIDEO_INTELLIGENCE_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['video_id', 'summary', 'hook', 'topic', 'angle', 'target_audience',
                 'content_format', 'narrative_structure', 'cta',
                 'performance_interpretation', 'evidence'],
    'properties': {
        'video_id': _string(64), 'summary': _string(1000), 'hook': _string(500),
        'topic': _string(240), 'angle': _string(500), 'target_audience': _string(500),
        'content_format': _string(160), 'narrative_structure': _string(320),
        'cta': _string(500), 'performance_interpretation': _string(1000),
        'evidence': {'type': 'array', 'maxItems': 20, 'items': EVIDENCE_SCHEMA},
    },
}

PATTERN_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['name', 'description', 'evidence'],
    'properties': {
        'name': _string(160), 'description': _string(1400),
        'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 30, 'items': EVIDENCE_SCHEMA},
    },
}

OPPORTUNITY_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['opportunity', 'rationale', 'evidence'],
    'properties': {
        'opportunity': _string(600), 'rationale': _string(1400),
        'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 30, 'items': EVIDENCE_SCHEMA},
    },
}

CHANNEL_INTELLIGENCE_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['channel_summary', 'audience_profile', 'content_pillars',
                 'winning_patterns', 'performance_insights', 'opportunities', 'caveats'],
    'properties': {
        'channel_summary': _string(2000), 'audience_profile': _string(1600),
        'content_pillars': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA},
        'winning_patterns': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA},
        'performance_insights': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA},
        'opportunities': {'type': 'array', 'maxItems': 30, 'items': OPPORTUNITY_SCHEMA},
        'caveats': {'type': 'array', 'maxItems': 30, 'items': _string(800)},
    },
}

CHANNEL_ANALYSIS_JSON_SCHEMA: dict[str, Any] = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    '$id': 'https://kurukin.internal/schemas/kurukin-channel-analysis-v1.json',
    'title': 'Kurukin Channel Analysis v1',
    'type': 'object', 'additionalProperties': False,
    'required': ['schema', 'research_pack_hash', 'video_intelligence', 'channel_intelligence'],
    'properties': {
        'schema': {'const': CHANNEL_INTELLIGENCE_SCHEMA_VERSION},
        'research_pack_hash': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'video_intelligence': {'type': 'array', 'minItems': 1, 'maxItems': 10000,
                               'items': VIDEO_INTELLIGENCE_SCHEMA},
        'channel_intelligence': CHANNEL_INTELLIGENCE_SCHEMA,
    },
}


def canonical_json_sha256(value: Any) -> str:
    """Hash a JSON value in one stable representation."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _check_object(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f'{path} must be an object')
        return
    required = set(schema['required'])
    actual = set(value)
    for key in sorted(required - actual):
        errors.append(f'{path}.{key} is required')
    for key in sorted(actual - set(schema['properties'])):
        errors.append(f'{path}.{key} is not allowed')
    for key, child in schema['properties'].items():
        if key in value:
            _check(value[key], child, f'{path}.{key}', errors)


def _check(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    schema_type = schema.get('type')
    if schema_type == 'object':
        _check_object(value, schema, path, errors); return
    if schema_type == 'array':
        if not isinstance(value, list):
            errors.append(f'{path} must be an array'); return
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', 2**31):
            errors.append(f'{path} has an invalid number of items')
        if schema.get('uniqueItems') and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            errors.append(f'{path} must not contain duplicates')
        for index, item in enumerate(value):
            _check(item, schema['items'], f'{path}[{index}]', errors)
        return
    if schema.get('const') is not None:
        if value != schema['const']:
            errors.append(f'{path} must equal {schema["const"]}')
        return
    if schema_type == 'string':
        if not isinstance(value, str):
            errors.append(f'{path} must be a string'); return
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 2**31):
            errors.append(f'{path} has an invalid length')
        pattern = schema.get('pattern')
        if pattern and not re.fullmatch(pattern, value):
            errors.append(f'{path} has an invalid format')


def validate_channel_analysis(value: Any, expected_video_ids: set[str] | None = None) -> list[str]:
    """Return bounded, human-safe validation errors for one import object."""
    errors: list[str] = []
    _check(value, CHANNEL_ANALYSIS_JSON_SCHEMA, '$', errors)
    if not isinstance(value, dict):
        return errors[:100]
    pack_hash = value.get('research_pack_hash')
    if isinstance(pack_hash, str) and not _SHA256.fullmatch(pack_hash):
        errors.append('$.research_pack_hash has an invalid format')
    videos = value.get('video_intelligence')
    if isinstance(videos, list):
        ids = [item.get('video_id') for item in videos if isinstance(item, dict)]
        if len(ids) != len(set(ids)):
            errors.append('$.video_intelligence video_id values must be unique')
        if expected_video_ids is not None and set(ids) != expected_video_ids:
            missing = sorted(expected_video_ids - set(ids))
            extra = sorted(set(ids) - expected_video_ids)
            if missing:
                errors.append('$.video_intelligence is missing Research Pack videos: ' + ', '.join(missing[:10]))
            if extra:
                errors.append('$.video_intelligence includes videos outside the Research Pack: ' + ', '.join(extra[:10]))
    known_ids = expected_video_ids or set()
    def evidence_ids(item: Any):
        if isinstance(item, dict):
            if 'video_ids' in item and isinstance(item['video_ids'], list):
                yield from item['video_ids']
            for nested in item.values():
                yield from evidence_ids(nested)
        elif isinstance(item, list):
            for nested in item:
                yield from evidence_ids(nested)
    if expected_video_ids is not None:
        unknown = sorted({item for item in evidence_ids(value) if isinstance(item, str)} - known_ids)
        if unknown:
            errors.append('Evidence references videos outside the Research Pack: ' + ', '.join(unknown[:10]))
    return errors[:100]
