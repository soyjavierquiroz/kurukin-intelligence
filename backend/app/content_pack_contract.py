"""Contract and validator for private Kurukin content packs.

The pack deliberately references global intelligence rather than duplicating
competitor metrics.  It is an import boundary for user-owned creative work.
"""
from __future__ import annotations

from typing import Any


CONTENT_PACK_SCHEMA_VERSION = 'kurukin-content-pack-v1'


def _text(value: Any, path: str, errors: list[str], maximum: int = 6000) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f'{path} must be a non-empty string')
    elif len(value) > maximum:
        errors.append(f'{path} is too long')


def _text_list(value: Any, path: str, errors: list[str], maximum: int = 30) -> None:
    if not isinstance(value, list) or not value or len(value) > maximum:
        errors.append(f'{path} must be a non-empty list')
        return
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f'{path} must contain non-empty strings')
    if len(set(value)) != len(value):
        errors.append(f'{path} must not contain duplicates')


def _entry(value: Any, path: str, required: tuple[str, ...], patterns: set[str], video_ids: set[str], errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f'{path} must be an object')
        return
    for field in required:
        _text(value.get(field), f'{path}.{field}', errors)
    _text_list(value.get('source_patterns'), f'{path}.source_patterns', errors)
    cited_patterns = value.get('source_patterns')
    if isinstance(cited_patterns, list):
        unknown = sorted(set(cited_patterns) - patterns)
        if unknown:
            errors.append(f'{path}.source_patterns contains unknown patterns: {", ".join(unknown[:10])}')
    evidence = value.get('source_evidence_video_ids', [])
    if evidence is not None:
        if not isinstance(evidence, list) or any(not isinstance(item, str) or not item for item in evidence):
            errors.append(f'{path}.source_evidence_video_ids must be a list of video IDs')
        elif len(evidence) != len(set(evidence)):
            errors.append(f'{path}.source_evidence_video_ids must not contain duplicates')
        else:
            unknown = sorted(set(evidence) - video_ids)
            if unknown:
                errors.append(f'{path}.source_evidence_video_ids contains unknown evidence IDs: {", ".join(unknown[:10])}')


def validate_content_pack(value: Any, *, channel_id: str, username: str,
                          known_patterns: set[str], known_video_ids: set[str]) -> list[str]:
    """Validate a private content pack without writing it."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return ['The upload must be one JSON object.']
    allowed_root = {'schema', 'source_channel', 'strategy', 'content_ideas', 'scripts'}
    required_root = allowed_root
    for field in sorted(required_root - set(value)):
        errors.append(f'$.{field} is required')
    for field in sorted(set(value) - allowed_root):
        errors.append(f'$.{field} is not allowed')
    if value.get('schema') != CONTENT_PACK_SCHEMA_VERSION:
        errors.append(f'$.schema must equal {CONTENT_PACK_SCHEMA_VERSION}')
    source = value.get('source_channel')
    if not isinstance(source, dict):
        errors.append('$.source_channel must be an object')
    else:
        if source.get('channel_id') != channel_id:
            errors.append('$.source_channel.channel_id does not match this channel')
        if source.get('username') != username:
            errors.append('$.source_channel.username does not match this channel')
    strategy = value.get('strategy')
    if not isinstance(strategy, dict):
        errors.append('$.strategy must be an object')
    else:
        for field in ('recommended_positioning', 'content_formula', 'recommended_cta_strategy', 'recommended_content_mix'):
            _text(strategy.get(field), f'$.strategy.{field}', errors)
        _text_list(strategy.get('primary_patterns'), '$.strategy.primary_patterns', errors)
        if isinstance(strategy.get('primary_patterns'), list):
            unknown = sorted(set(strategy['primary_patterns']) - known_patterns)
            if unknown:
                errors.append('$.strategy.primary_patterns contains unknown patterns: ' + ', '.join(unknown[:10]))
    entries = (('content_ideas', ('title', 'objective', 'hook', 'angle', 'pain', 'desire', 'mechanism', 'cta')),
               ('scripts', ('title', 'objective', 'duration_target', 'hook', 'body', 'cta')))
    titles: set[str] = set()
    for collection, fields in entries:
        values = value.get(collection)
        if not isinstance(values, list) or not values:
            errors.append(f'$.{collection} must be a non-empty list')
            continue
        if len(values) > 100:
            errors.append(f'$.{collection} has too many entries')
        local_titles: list[str] = []
        for index, item in enumerate(values):
            _entry(item, f'$.{collection}[{index}]', fields, known_patterns, known_video_ids, errors)
            if isinstance(item, dict) and isinstance(item.get('title'), str):
                local_titles.append(item['title'].strip().casefold())
        if len(local_titles) != len(set(local_titles)):
            errors.append(f'$.{collection} contains duplicate titles')
        overlap = titles.intersection(local_titles)
        if overlap:
            errors.append('Duplicate titles across ideas and scripts: ' + ', '.join(sorted(overlap)[:10]))
        titles.update(local_titles)
    return errors[:100]
