#!/usr/bin/env python3
"""Idempotently backfill global Phase A Viral DNA without invoking ingestion."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Direct execution places ops/ on sys.path, not the backend application root.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import Video
from app.viral_dna import EXTRACTOR_VERSION, extract_viral_dna_for_video


def backfill_viral_dna(
    session: Session,
    *,
    batch_size: int = 100,
    limit: int | None = None,
    extractor_version: str = EXTRACTOR_VERSION,
    commit: bool = True,
) -> dict[str, int]:
    """Process global videos in UUID-ordered batches and return safe counters.

    Each video is isolated in a savepoint, so a malformed legacy row increments
    only ``failed``.  No identifiers, captions, transcripts, or database URLs
    are emitted by this operation.
    """
    if batch_size <= 0:
        raise ValueError('batch_size must be positive')
    if limit is not None and limit < 0:
        raise ValueError('limit must be non-negative')
    metrics = {'processed': 0, 'inserted': 0, 'updated': 0, 'unchanged': 0, 'failed': 0}
    last_id = None
    while limit is None or metrics['processed'] < limit:
        size = batch_size if limit is None else min(batch_size, limit - metrics['processed'])
        statement = select(Video.id).order_by(Video.id).limit(size)
        if last_id is not None:
            statement = statement.where(Video.id > last_id)
        ids = list(session.scalars(statement))
        if not ids:
            break
        for video_id in ids:
            metrics['processed'] += 1
            try:
                with session.begin_nested():
                    result = extract_viral_dna_for_video(
                        session, video_id, extractor_version=extractor_version
                    )
            except Exception:
                metrics['failed'] += 1
            else:
                metrics[result.action] += 1
        last_id = ids[-1]
        if commit:
            session.commit()
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    try:
        with Session(get_engine()) as session:
            metrics = backfill_viral_dna(session, batch_size=args.batch_size, limit=args.limit)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
