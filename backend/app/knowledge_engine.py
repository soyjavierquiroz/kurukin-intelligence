"""Deterministic freshness, compact delta packs, and immutable delta merging."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

from .channel_intelligence_contract import canonical_json_sha256

ANALYSIS_CONTRACT_VERSION = 'sci-v1|kurukin-channel-analysis-prompt-v1|semantic-v1'


def semantic_video(record: dict[str, Any]) -> dict[str, Any]:
    """Only fields sent to semantic analysis; deliberately excludes metrics."""
    return {key: record.get(key) for key in (
        'video_id', 'caption', 'transcript', 'transcript_source', 'transcript_language',
        'duration_seconds', 'published_at',
    )}


def performance_video(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in (
        'video_id', 'views', 'likes', 'comments', 'shares', 'favorites',
        'engagement_rate', 'outlier_score', 'overall_rank',
    )}


def semantic_corpus_hash(records: list[dict[str, Any]]) -> str:
    return canonical_json_sha256(sorted((semantic_video(row) for row in records), key=lambda row: row['video_id']))


def performance_state_hash(records: list[dict[str, Any]]) -> str:
    return canonical_json_sha256(sorted((performance_video(row) for row in records), key=lambda row: row['video_id']))


def freshness(analysis: Any | None, records: list[dict[str, Any]]) -> dict[str, Any]:
    semantic = semantic_corpus_hash(records)
    performance = performance_state_hash(records)
    if analysis is None or not getattr(analysis, 'semantic_corpus_hash', None):
        return {'state': 'NO_INTELLIGENCE', 'semantic_corpus_hash': semantic, 'performance_state_hash': performance,
                'new_or_changed_ids': [row['video_id'] for row in records]}
    if analysis.analysis_contract_version != ANALYSIS_CONTRACT_VERSION:
        return {'state': 'CONTRACT_STALE', 'semantic_corpus_hash': semantic, 'performance_state_hash': performance,
                'new_or_changed_ids': []}
    if analysis.semantic_corpus_hash != semantic:
        return {'state': 'SEMANTIC_DELTA', 'semantic_corpus_hash': semantic, 'performance_state_hash': performance,
                'new_or_changed_ids': []}
    if analysis.performance_state_hash != performance:
        return {'state': 'PERFORMANCE_CHANGED', 'semantic_corpus_hash': semantic, 'performance_state_hash': performance,
                'new_or_changed_ids': []}
    return {'state': 'FRESH', 'semantic_corpus_hash': semantic, 'performance_state_hash': performance,
            'new_or_changed_ids': []}


def changed_ids(records: list[dict[str, Any]], known_semantic: dict[str, str]) -> list[str]:
    return [row['video_id'] for row in records if known_semantic.get(row['video_id']) != canonical_json_sha256(semantic_video(row))]


def compact_video_intelligence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = ('video_id', 'analysis_status', 'summary', 'hooks', 'pains', 'desires', 'topics',
            'narratives', 'ctas', 'offers', 'evidence')
    return [{key: item[key] for key in keep if key in item} for item in items]


def incremental_pack(*, channel: Any, prior: Any, known: list[dict[str, Any]], records: list[dict[str, Any]],
                     delta_ids: list[str], semantic_hash: str, performance_hash: str) -> bytes:
    delta = [row for row in records if row['video_id'] in set(delta_ids)]
    manifest = {
        'schema': 'kurukin-incremental-intelligence-pack-v1', 'channel': {'id': str(channel.id), 'username': channel.username},
        'base_analysis_id': str(prior.id), 'base_payload_sha': prior.payload_sha256,
        'base_semantic_corpus_hash': prior.semantic_corpus_hash, 'target_semantic_corpus_hash': semantic_hash,
        'performance_state_hash': performance_hash, 'analysis_contract_version': ANALYSIS_CONTRACT_VERSION,
        'videos_total': len(records), 'videos_reused': len(known), 'videos_new_or_changed': len(delta),
        'raw_transcripts_included': len(delta), 'structured_prior_video_analyses_included': len(known),
    }
    previous = {'analysis_id': str(prior.id), 'payload_sha256': prior.payload_sha256,
                'semantic_corpus_hash': prior.semantic_corpus_hash, 'performance_state_hash': prior.performance_state_hash,
                'analysis_contract_version': prior.analysis_contract_version, 'channel_intelligence': prior.channel_intelligence}
    readme = ('# Kurukin Incremental Intelligence Update Pack\n\nAnalyze only `new_or_changed_videos.jsonl`. '
              'Use prior structured intelligence and compact known video intelligence for synthesis. Old transcripts are deliberately absent.\n')
    from .channel_update_contract import CHANNEL_UPDATE_JSON_SCHEMA
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('README.md', readme)
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('previous_channel_intelligence.json', json.dumps(previous, ensure_ascii=False, indent=2))
        archive.writestr('known_video_intelligence.jsonl', ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in compact_video_intelligence(known)))
        archive.writestr('new_or_changed_videos.jsonl', ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in delta))
        archive.writestr('kurukin-channel-update.schema.json', json.dumps(CHANNEL_UPDATE_JSON_SCHEMA, ensure_ascii=False, indent=2))
    return output.getvalue()
