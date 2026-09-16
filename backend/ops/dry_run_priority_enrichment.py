#!/usr/bin/env python3
"""Read-only report for the final diversified priority-enrichment selector.

This uses the application database configuration, never reserves, writes,
queues audio, contacts TikTok, or calls RabbitMQ/MinIO.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import PRIORITY_RATIO, get_settings
from app.db import get_engine
from app.models import Analysis, Transcript, TranscriptionJob
from app.ranking import diversified_selection, priority_view_cutoff
from app.services import (analysis_ranked_videos, eligibility, enrichment_eligibility,
                          new_enrichment_used_ids)


def begin_read_only(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == 'postgresql':
        session.execute(text('SET TRANSACTION READ ONLY'))


def report(session: Session, analysis_id: UUID) -> dict:
    begin_read_only(session)
    analysis = session.get(Analysis, analysis_id)
    if analysis is None:
        raise ValueError('Unknown analysis_id')
    _missing, diagnostics, _authorized = enrichment_eligibility(
        session, analysis, discovery_complete=True, has_more=False
    )
    rows = analysis_ranked_videos(session, analysis)
    jobs = {job.video_id: job for job in session.scalars(select(TranscriptionJob).where(
        TranscriptionJob.video_id.in_([video.id for video, *_ in rows]))) }
    target, cutoff = priority_view_cutoff(rows, PRIORITY_RATIO)
    threshold = Decimal(str(get_settings().enrichment_min_outlier_score))
    missing = []
    for row in rows:
        video, snapshot, rates, transcript = row
        top = cutoff is not None and snapshot.views >= cutoff
        if not eligibility(video.duration)[0] or not (top or rates['outlier_score'] >= threshold):
            continue
        job = jobs.get(video.id)
        if transcript is None and not (job is not None and job.status in ('completed', 'skipped')):
            missing.append(row)
    used_ids = new_enrichment_used_ids(session, analysis)
    available = [row for row in missing if row[0].id not in used_ids]
    selected, _quotas = diversified_selection(available, diagnostics['new_enrichment_remaining'])
    examples = [
        dict(tiktok_id=row[0].tiktok_id, views=row[1].views,
             outlier_score=float(row[2]['outlier_score']),
             engagement_rate=float(row[2]['engagement_rate']), selection_reason=reason)
        for row, reason in selected[:15]
    ]
    return dict(
        analysis_id=str(analysis.id), total_videos=analysis.video_count,
        priority_target_count=target, priority_view_cutoff=cutoff,
        top_views_pool_count=diagnostics['priority_top_views_count'],
        outlier_override_count=diagnostics['priority_outlier_override_count'],
        priority_pool_count=diagnostics['priority_pool_count'],
        global_resolved_in_priority_pool=diagnostics['globally_resolved_priority'],
        missing_priority_candidates=diagnostics['missing_priority_candidates'],
        new_enrichment_budget=diagnostics['new_enrichment_budget'],
        existing_incremental_used=diagnostics['new_enrichment_used'],
        remaining_budget=diagnostics['new_enrichment_remaining'],
        views_quota=diagnostics['selection_views_quota'],
        outlier_quota=diagnostics['selection_outlier_quota'],
        engagement_quota=diagnostics['selection_engagement_quota'],
        selected_by_views=diagnostics['selected_by_views'],
        selected_by_outlier=diagnostics['selected_by_outlier'],
        selected_by_engagement=diagnostics['selected_by_engagement'],
        selected_by_backfill=diagnostics['selected_by_backfill'],
        unique_selected_total=len(selected), excluded_by_budget=diagnostics['excluded_by_budget'],
        examples=examples,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('analysis_id', type=UUID)
    args = parser.parse_args()
    with Session(get_engine()) as session:
        print(json.dumps(report(session, args.analysis_id), indent=2, sort_keys=True))
        session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
