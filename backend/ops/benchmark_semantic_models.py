#!/usr/bin/env python3
"""Read-only, one-video-per-call Semantic DNA provider benchmark."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
from typing import Iterable
from uuid import UUID

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.llm.semantic import SemanticProvider, SemanticProviderConfig
from app.llm.providers import register_builtin_semantic_providers
from app.llm.semantic import get_semantic_provider
from app.llm.providers._common import SemanticProviderError
from app.models import Transcript, Video
from app.viral_dna import semantic_payload, validate_semantic_output


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def load_video_ids(*, video_ids: Iterable[UUID] = (), limit: int | None = None,
                   ids_file: Path | None = None, session: Session) -> list[UUID]:
    """Resolve explicit IDs, a Golden Set file, or a UUID-ordered DB selection."""
    result = list(video_ids)
    if ids_file is not None:
        try:
            document = json.loads(ids_file.read_text(encoding='utf-8'))
            values = document['video_ids']
            if not isinstance(values, list):
                raise ValueError()
            result.extend(UUID(value) for value in values)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise ValueError('invalid_ids_file') from error
    if result:
        # Stable deduplication lets a Golden Set and explicitly supplied IDs coexist.
        return list(dict.fromkeys(result))
    statement = select(Video.id).order_by(Video.id)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def benchmark_semantic_models(session: Session, provider: SemanticProvider, *, provider_name: str, model: str,
                              video_ids: Iterable[UUID]) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Run benchmark extraction without writing, flushing, or committing the DB session."""
    records: list[dict[str, object]] = []
    for video_id in video_ids:
        video = session.get(Video, video_id)
        transcript = None if video is None else session.scalar(select(Transcript).where(Transcript.video_id == video.id))
        record: dict[str, object] = {
            'video_id': str(video_id), 'provider': provider_name, 'model': model,
            'valid': False, 'latency_ms': None, 'usage': {}, 'attempts': 0,
            'semantic': None, 'error_code': None,
        }
        if video is None:
            record['error_code'] = 'video_not_found'
        elif transcript is None:
            record['error_code'] = 'missing_transcript'
        else:
            try:
                semantic = validate_semantic_output(provider.extract(semantic_payload(video, transcript)))
                record['valid'] = True
                record['semantic'] = semantic
            except SemanticProviderError as error:
                record['error_code'] = error.code
                record['attempts'] = error.attempts
            except Exception as error:
                # Central validation intentionally exposes no provider response.
                record['error_code'] = 'schema_validation_failed'
        # Metadata belongs only to a provider invocation.  A locally skipped
        # video must never inherit token counters from its preceding call.
        metadata = getattr(provider, 'last_metadata', None) if transcript is not None and video is not None else None
        if metadata is not None:
            record['latency_ms'] = metadata.latency_ms
            record['usage'] = dict(metadata.usage)
            record['attempts'] = metadata.attempts
        records.append(record)

    valid = sum(record['valid'] is True for record in records)
    latencies = [record['latency_ms'] for record in records if isinstance(record['latency_ms'], int)]
    usages = [record['usage'] for record in records if isinstance(record['usage'], dict)]
    summary: dict[str, object] = {
        'processed': len(records), 'valid': valid, 'failed': len(records) - valid,
        'valid_rate': (valid / len(records)) if records else 0.0,
        'total_input_tokens': sum(usage.get('input_tokens', 0) for usage in usages),
        'total_output_tokens': sum(usage.get('output_tokens', 0) for usage in usages),
        'total_tokens': sum(usage.get('total_tokens', 0) for usage in usages),
        'latency_ms_avg': round(statistics.mean(latencies)) if latencies else None,
        'latency_ms_p50': _percentile(latencies, 0.50), 'latency_ms_p95': _percentile(latencies, 0.95),
    }
    return records, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', required=True, choices=('openai', 'google', 'moonshot', 'deepseek'))
    parser.add_argument('--model', required=True)
    parser.add_argument('--video-id', action='append', type=UUID, default=[])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--ids-file', type=Path)
    parser.add_argument('--input-cost-per-million', type=float)
    parser.add_argument('--output-cost-per-million', type=float)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error('--limit must be non-negative')
    if (args.input_cost_per_million is None) != (args.output_cost_per_million is None):
        parser.error('supply both input and output costs together')
    try:
        register_builtin_semantic_providers()
        config = SemanticProviderConfig(provider_name=args.provider, model=args.model)
        provider = get_semantic_provider(args.provider, config)
        with Session(get_engine(), autoflush=False) as session:
            ids = load_video_ids(video_ids=args.video_id, limit=args.limit, ids_file=args.ids_file, session=session)
            records, summary = benchmark_semantic_models(session, provider, provider_name=args.provider,
                                                          model=args.model, video_ids=ids)
    except ValueError as error:
        parser.error(str(error))
    for record in records:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    if args.input_cost_per_million is not None:
        summary['estimated_cost_usd'] = round(
            (summary['total_input_tokens'] * args.input_cost_per_million
             + summary['total_output_tokens'] * args.output_cost_per_million) / 1_000_000, 8
        )
    print(json.dumps({'summary': summary}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
