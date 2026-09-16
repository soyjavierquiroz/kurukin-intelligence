"""Frozen contract for externally generated Channel Intelligence v1.

The external model is a producer, never an authority on the shape of data we
persist. This module owns the JSON Schema and dependency-free dry-run validator.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


CHANNEL_INTELLIGENCE_SCHEMA_VERSION = 'kurukin-channel-analysis-v1'
CHANNEL_INTELLIGENCE_PROMPT_VERSION = 'kurukin-channel-analysis-prompt-v1'
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def _string(maximum: int) -> dict[str, Any]:
    return {'type': 'string', 'minLength': 1, 'maxLength': maximum}


def _text_list(maximum: int = 30) -> dict[str, Any]:
    return {'type': 'array', 'maxItems': maximum, 'items': _string(800), 'uniqueItems': True}


EVIDENCE_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False, 'required': ['claim', 'video_ids'],
    'properties': {'claim': _string(600), 'video_ids': {'type': 'array', 'minItems': 1, 'maxItems': 25, 'items': _string(64), 'uniqueItems': True}},
}
VIDEO_INTELLIGENCE_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False, 'required': ['video_id', 'analysis_status', 'evidence'],
    'properties': {
        'video_id': _string(64), 'analysis_status': {'type': 'string', 'enum': ['analyzed', 'insufficient_content']},
        'summary': _string(1000), 'hooks': _text_list(), 'pains': _text_list(), 'desires': _text_list(),
        'topics': _text_list(), 'narratives': _text_list(), 'ctas': _text_list(), 'offers': _text_list(),
        'evidence': {'type': 'array', 'maxItems': 20, 'items': EVIDENCE_SCHEMA},
    },
}
PATTERN_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False, 'required': ['name', 'description', 'evidence'],
    'properties': {'name': _string(160), 'description': _string(1400), 'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 30, 'items': EVIDENCE_SCHEMA}},
}
CHANNEL_INTELLIGENCE_SCHEMA: dict[str, Any] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['summary', 'audience', 'pains', 'desires', 'hooks', 'topics', 'winning_patterns', 'narratives', 'repetition_clusters', 'ctas', 'offers', 'opportunities', 'caveats'],
    'properties': {
        'summary': _string(2000), 'audience': _string(1600),
        'pains': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA}, 'desires': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA},
        'hooks': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA}, 'topics': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA},
        'winning_patterns': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA}, 'narratives': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA},
        'repetition_clusters': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA}, 'ctas': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA},
        'offers': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA}, 'opportunities': {'type': 'array', 'maxItems': 30, 'items': PATTERN_SCHEMA}, 'caveats': _text_list(),
    },
}
CHANNEL_ANALYSIS_JSON_SCHEMA: dict[str, Any] = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema', '$id': 'https://kurukin.internal/schemas/kurukin-channel-analysis-v1.json',
    'title': 'Kurukin Channel Analysis v1', 'type': 'object', 'additionalProperties': False,
    'required': ['schema', 'prompt_version', 'processor', 'research_pack', 'videos', 'channel_intelligence'],
    'properties': {
        'schema': {'const': CHANNEL_INTELLIGENCE_SCHEMA_VERSION}, 'prompt_version': {'const': CHANNEL_INTELLIGENCE_PROMPT_VERSION}, 'processor': _string(160),
        'research_pack': {'type': 'object', 'additionalProperties': False, 'required': ['hash', 'channel_username', 'selection_mode', 'video_count'],
            'properties': {'hash': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}, 'channel_username': _string(160), 'selection_mode': _string(32), 'video_count': {'type': 'integer', 'minimum': 1, 'maximum': 10000}}},
        'videos': {'type': 'array', 'minItems': 1, 'maxItems': 10000, 'items': VIDEO_INTELLIGENCE_SCHEMA}, 'channel_intelligence': CHANNEL_INTELLIGENCE_SCHEMA,
    },
}


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _check_object(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f'{path} must be an object'); return
    for key in sorted(set(schema['required']) - set(value)):
        errors.append(f'{path}.{key} is required')
    for key in sorted(set(value) - set(schema['properties'])):
        errors.append(f'{path}.{key} is not allowed')
    for key, child in schema['properties'].items():
        if key in value:
            _check(value[key], child, f'{path}.{key}', errors)


def _check(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    schema_type = schema.get('type')
    if schema_type == 'object':
        _check_object(value, schema, path, errors); return
    if schema_type == 'array':
        if not isinstance(value, list): errors.append(f'{path} must be an array'); return
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 2**31): errors.append(f'{path} has an invalid number of items')
        if schema.get('uniqueItems') and len({json.dumps(item, sort_keys=True) for item in value}) != len(value): errors.append(f'{path} must not contain duplicates')
        for index, item in enumerate(value): _check(item, schema['items'], f'{path}[{index}]', errors)
        return
    if schema_type == 'integer':
        if not isinstance(value, int) or isinstance(value, bool): errors.append(f'{path} must be an integer'); return
        if not schema.get('minimum', -(2**63)) <= value <= schema.get('maximum', 2**63 - 1): errors.append(f'{path} has an invalid value')
        return
    if schema.get('const') is not None:
        if value != schema['const']: errors.append(f'{path} must equal {schema["const"]}')
        return
    if schema_type == 'string':
        if not isinstance(value, str): errors.append(f'{path} must be a string'); return
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 2**31): errors.append(f'{path} has an invalid length')
        if schema.get('enum') and value not in schema['enum']: errors.append(f'{path} must be one of {", ".join(schema["enum"])}')
        if schema.get('pattern') and not re.fullmatch(schema['pattern'], value): errors.append(f'{path} has an invalid format')


def validate_channel_analysis(value: Any, expected_video_ids: set[str] | None = None) -> list[str]:
    """Return bounded, human-safe validation errors for one import object."""
    errors: list[str] = []
    _check(value, CHANNEL_ANALYSIS_JSON_SCHEMA, '$', errors)
    if not isinstance(value, dict): return errors[:100]
    if 'video_intelligence' in value:
        errors.insert(0, "El archivo fue generado con una estructura incorrecta. Se recibió 'video_intelligence' pero Kurukin requiere 'videos'. Vuelve a generar el archivo usando las instrucciones de Kurukin.")
    if any(key in value for key in ('video_analysis', 'items', 'results')):
        errors.insert(0, "El archivo usa un alias de estructura no permitido. Kurukin requiere la raíz canónica 'videos'.")
    def forbidden(item: Any) -> bool:
        if isinstance(item, dict): return any(key in item for key in ('performance_interpretation', 'views', 'likes', 'comments', 'shares', 'favorites', 'engagement_rate', 'outlier_score')) or any(forbidden(v) for v in item.values())
        return isinstance(item, list) and any(forbidden(v) for v in item)
    if forbidden(value.get('videos', [])):
        errors.insert(0, "El archivo duplica métricas canónicas o usa 'performance_interpretation'. Kurukin no acepta esas propiedades en videos.")
    pack = value.get('research_pack'); pack_hash = pack.get('hash') if isinstance(pack, dict) else None
    if isinstance(pack_hash, str) and not _SHA256.fullmatch(pack_hash): errors.append('$.research_pack.hash has an invalid format')
    videos = value.get('videos')
    if isinstance(videos, list):
        ids = [item.get('video_id') for item in videos if isinstance(item, dict)]
        if len(ids) != len(set(ids)): errors.append('$.videos video_id values must be unique')
        if expected_video_ids is not None and set(ids) != expected_video_ids:
            missing, extra = sorted(expected_video_ids - set(ids)), sorted(set(ids) - expected_video_ids)
            if missing: errors.append('$.videos is missing Research Pack videos: ' + ', '.join(missing[:10]))
            if extra: errors.append('$.videos includes videos outside the Research Pack: ' + ', '.join(extra[:10]))
        if isinstance(pack, dict) and pack.get('video_count') != len(videos): errors.append('$.research_pack.video_count must equal the number of $.videos')
    def evidence_ids(item: Any):
        if isinstance(item, dict):
            if isinstance(item.get('video_ids'), list): yield from item['video_ids']
            for child in item.values(): yield from evidence_ids(child)
        elif isinstance(item, list):
            for child in item: yield from evidence_ids(child)
    if expected_video_ids is not None:
        unknown = sorted({item for item in evidence_ids(value) if isinstance(item, str)} - expected_video_ids)
        if unknown: errors.append('Evidence references videos outside the Research Pack: ' + ', '.join(unknown[:10]))
    return errors[:100]
