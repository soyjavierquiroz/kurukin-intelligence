#!/usr/bin/env python3
"""Read-only, one-video-per-call Semantic DNA provider benchmark."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
from dataclasses import dataclass
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
from app.viral_dna import SemanticOutputValidationError, semantic_payload, validate_semantic_output


@dataclass(frozen=True)
class BenchmarkInputRecord:
    """The only text assets a file-based benchmark is allowed to use."""

    video_id: UUID
    language: str
    caption: str
    transcript: str


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


def load_input_records(*, input_file: Path, video_ids: Iterable[UUID] = (),
                       limit: int | None = None) -> list[BenchmarkInputRecord]:
    """Load a safe, DB-independent input set, ignoring all non-text metadata."""
    try:
        document = json.loads(input_file.read_text(encoding='utf-8'))
        if not isinstance(document, list):
            raise ValueError()
        inputs = [
            BenchmarkInputRecord(
                video_id=UUID(item['video_id']), language=item['language'],
                caption=item['caption'], transcript=item['transcript'],
            )
            for item in document
        ]
        if any(not all(isinstance(value, str) for value in (
            item.language, item.caption, item.transcript,
        )) for item in inputs):
            raise ValueError()
    except (AttributeError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ValueError('invalid_input_file') from error

    requested_ids = list(dict.fromkeys(video_ids))
    if requested_ids:
        by_id = {item.video_id: item for item in inputs}
        missing_ids = [str(video_id) for video_id in requested_ids if video_id not in by_id]
        if missing_ids:
            raise ValueError(f'missing_video_id_in_input_file: {", ".join(missing_ids)}')
        return [by_id[video_id] for video_id in requested_ids]
    return inputs if limit is None else inputs[:limit]


def _record_for_provider_call(provider: SemanticProvider, *, video_id: UUID, payload: dict[str, str],
                              provider_name: str, model: str) -> dict[str, object]:
    """Execute and centrally validate one provider call without exposing source text."""
    record: dict[str, object] = {
        'video_id': str(video_id), 'provider': provider_name, 'model': model,
        'valid': False, 'latency_ms': None, 'usage': {}, 'attempts': 0,
        'semantic': None, 'error_code': None, 'validation_errors': None,
    }
    try:
        record['semantic'] = validate_semantic_output(provider.extract(payload))
        record['valid'] = True
    except SemanticProviderError as error:
        record['error_code'] = error.code
        record['attempts'] = error.attempts
    except SemanticOutputValidationError as error:
        record['error_code'] = 'schema_validation_failed'
        record['validation_errors'] = [dict(diagnostic) for diagnostic in error.validation_errors]
    except Exception:
        # Central validation intentionally exposes no provider response.
        record['error_code'] = 'schema_validation_failed'
    metadata = getattr(provider, 'last_metadata', None)
    if metadata is not None:
        record['latency_ms'] = metadata.latency_ms
        record['usage'] = dict(metadata.usage)
        record['attempts'] = metadata.attempts
    return record


def _benchmark_summary(records: list[dict[str, object]]) -> dict[str, object]:
    """Build benchmark-wide, safe metrics from normalized per-video records."""
    valid = sum(record['valid'] is True for record in records)
    latencies = [record['latency_ms'] for record in records if isinstance(record['latency_ms'], int)]
    usages = [record['usage'] for record in records if isinstance(record['usage'], dict)]
    return {
        'processed': len(records), 'valid': valid, 'failed': len(records) - valid,
        'valid_rate': (valid / len(records)) if records else 0.0,
        'total_input_tokens': sum(usage.get('input_tokens', 0) for usage in usages),
        'total_output_tokens': sum(usage.get('output_tokens', 0) for usage in usages),
        'total_tokens': sum(usage.get('total_tokens', 0) for usage in usages),
        'latency_ms_avg': round(statistics.mean(latencies)) if latencies else None,
        'latency_ms_p50': _percentile(latencies, 0.50), 'latency_ms_p95': _percentile(latencies, 0.95),
    }


def benchmark_semantic_models(session: Session, provider: SemanticProvider, *, provider_name: str, model: str,
                              video_ids: Iterable[UUID]) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Run benchmark extraction without writing, flushing, or committing the DB session."""
    records: list[dict[str, object]] = []
    for video_id in video_ids:
        video = session.get(Video, video_id)
        transcript = None if video is None else session.scalar(select(Transcript).where(Transcript.video_id == video.id))
        if video is None:
            record = _skipped_record(video_id, provider_name=provider_name, model=model, error_code='video_not_found')
        elif transcript is None:
            record = _skipped_record(video_id, provider_name=provider_name, model=model, error_code='missing_transcript')
        else:
            record = _record_for_provider_call(
                provider, video_id=video_id, payload=semantic_payload(video, transcript),
                provider_name=provider_name, model=model,
            )
        records.append(record)

    return records, _benchmark_summary(records)


def _skipped_record(video_id: UUID, *, provider_name: str, model: str,
                    error_code: str) -> dict[str, object]:
    """Create a locally skipped record without inherited provider metadata."""
    return {
        'video_id': str(video_id), 'provider': provider_name, 'model': model,
        'valid': False, 'latency_ms': None, 'usage': {}, 'attempts': 0,
        'semantic': None, 'error_code': error_code, 'validation_errors': None,
    }


def benchmark_semantic_input_records(inputs: Iterable[BenchmarkInputRecord], provider: SemanticProvider, *,
                                     provider_name: str, model: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Run a read-only benchmark from file input without creating a DB session."""
    records = [
        _record_for_provider_call(
            provider,
            video_id=item.video_id,
            # Match semantic_payload() exactly: trim language and caption only.
            payload={'language': item.language.strip(), 'caption': item.caption.strip(), 'transcript': item.transcript},
            provider_name=provider_name,
            model=model,
        )
        for item in inputs
    ]
    return records, _benchmark_summary(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', required=True, choices=('openai', 'google', 'moonshot', 'deepseek'))
    parser.add_argument('--model', required=True)
    parser.add_argument('--video-id', action='append', type=UUID, default=[])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--ids-file', type=Path)
    parser.add_argument('--input-file', type=Path)
    parser.add_argument('--input-cost-per-million', type=float)
    parser.add_argument('--output-cost-per-million', type=float)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error('--limit must be non-negative')
    if (args.input_cost_per_million is None) != (args.output_cost_per_million is None):
        parser.error('supply both input and output costs together')
    try:
        inputs = None
        if args.input_file is not None:
            # Resolve file selection before provider setup, so a bad/missing ID
            # fails locally without a credential or provider interaction.
            inputs = load_input_records(
                input_file=args.input_file, video_ids=args.video_id, limit=args.limit,
            )
        register_builtin_semantic_providers()
        config = SemanticProviderConfig(provider_name=args.provider, model=args.model)
        provider = get_semantic_provider(args.provider, config)
        if inputs is not None:
            records, summary = benchmark_semantic_input_records(
                inputs, provider, provider_name=args.provider, model=args.model,
            )
        else:
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
