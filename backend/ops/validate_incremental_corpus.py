#!/usr/bin/env python3
"""Read-only validation for an incremental channel-corpus scan.

The script deliberately uses the application's validated database configuration
(``DATABASE_URL_FILE`` in production) and never prints that configuration.
It does not call the API, TikTok, RabbitMQ, or make any database changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

# Direct invocation (``python ops/validate_incremental_corpus.py``) puts only
# ``ops/`` on sys.path, while application imports live one directory above.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import column, func, select, table, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import (Analysis, AnalysisAcquisition, AudioAssessment, Channel,
                        Transcript, TranscriptionJob, Video, VideoSnapshot)
from app.ranking import rank_snapshots
from app.services import eligibility


SCHEMA_VERSION = 1
ACTIVE_JOB_STATUSES = ('reserved', 'audio_received', 'queued', 'processing')


def _count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _alembic_revision(session: Session) -> str | None:
    """Return the revision without assuming the test database has Alembic tables."""
    try:
        return session.execute(
            select(func.max(column('version_num'))).select_from(table('alembic_version'))
        ).scalar_one()
    except SQLAlchemyError:
        return None


def _latest_rows(session: Session, channel_id: Any):
    """Get exactly the latest snapshot for each video in a channel."""
    latest = select(
        VideoSnapshot.id,
        VideoSnapshot.video_id,
        func.row_number().over(
            partition_by=VideoSnapshot.video_id,
            order_by=(VideoSnapshot.created_at.desc(), VideoSnapshot.id.desc()),
        ).label('position'),
    ).subquery()
    return session.execute(
        select(Video, VideoSnapshot, Transcript)
        .join(latest, (latest.c.position == 1) & (latest.c.video_id == Video.id))
        .join(VideoSnapshot, VideoSnapshot.id == latest.c.id)
        .outerjoin(Transcript, Transcript.video_id == Video.id)
        .where(Video.channel_id == channel_id)
    ).all()


def _channel_summary(session: Session, channel: Channel) -> dict[str, Any]:
    known = int(session.scalar(select(func.count()).select_from(Video).where(
        Video.channel_id == channel.id
    )) or 0)
    transcripts_available = int(session.scalar(
        select(func.count()).select_from(Transcript)
        .join(Video, Video.id == Transcript.video_id)
        .where(Video.channel_id == channel.id)
    ) or 0)
    return {
        'id': str(channel.id),
        'username': channel.username,
        'tiktok_user_id': channel.tiktok_user_id,
        'videos_total': known,
        'transcripts_available': transcripts_available,
    }


def _coverage(session: Session, channel: Channel) -> dict[str, Any]:
    rows = _latest_rows(session, channel.id)
    known = len(rows)
    available = sum(transcript is not None for _, _, transcript in rows)
    # The service owns the validated threshold setting.
    from app.config import get_settings
    threshold = get_settings().high_value_outlier_threshold
    high = [
        (video, transcript, snapshot)
        for video, snapshot, transcript in rows
        if eligibility(video.duration)[0] and float(snapshot.outlier_score) >= threshold
    ]
    skipped_music = set(session.scalars(
        select(TranscriptionJob.video_id).where(
            TranscriptionJob.status == 'skipped',
            TranscriptionJob.skip_reason == 'music',
        )
    ))
    assessments = int(session.scalar(
        select(func.count()).select_from(AudioAssessment)
        .join(Video, Video.id == AudioAssessment.video_id)
        .where(Video.channel_id == channel.id)
    ) or 0)
    return {
        'videos_known': known,
        'transcripts_available': available,
        'transcripts_missing': known - available,
        'high_value_candidates': len(high),
        'high_value_transcribed': sum(transcript is not None for _, transcript, _ in high),
        'resolved_enrichment': sum(
            transcript is not None or video.id in skipped_music
            for video, transcript, _ in rows
        ),
        'audio_assessments_total': assessments,
    }


def _latest_analysis_summary(session: Session) -> dict[str, Any] | None:
    analysis = session.scalars(
        select(Analysis).order_by(Analysis.created_at.desc(), Analysis.id.desc()).limit(1)
    ).first()
    if analysis is None:
        return None
    channel = session.get(Channel, analysis.channel_id)
    assert channel is not None  # enforced by analyses.channel_id foreign key
    return {
        'id': str(analysis.id),
        'created_at': analysis.created_at.isoformat(),
        'videos_seen_this_scan': analysis.video_count,
        'videos_new': analysis.videos_new,
        'videos_refreshed': analysis.videos_refreshed,
        'acquisition_requested': analysis.requested_transcripts,
        'username': channel.username,
        'tiktok_user_id': channel.tiktok_user_id,
        **_coverage(session, channel),
    }


def collect_snapshot(session: Session) -> dict[str, Any]:
    """Collect a JSON-serialisable, content-free baseline from a read-only session."""
    job_statuses = dict(sorted(Counter(session.scalars(
        select(TranscriptionJob.status)
    )).items()))
    duplicate_videos = int(session.scalar(
        select(func.count()).select_from(
            select(Video.tiktok_id).group_by(Video.tiktok_id).having(func.count() > 1).subquery()
        )
    ) or 0)
    duplicate_transcripts = int(session.scalar(
        select(func.count()).select_from(
            select(Transcript.video_id).group_by(Transcript.video_id).having(func.count() > 1).subquery()
        )
    ) or 0)
    channels = [_channel_summary(session, channel) for channel in session.scalars(
        select(Channel).order_by(Channel.username, Channel.id)
    )]
    return {
        'schema_version': SCHEMA_VERSION,
        'alembic_revision': _alembic_revision(session),
        'counts': {
            'channels': _count(session, Channel),
            'analyses': _count(session, Analysis),
            'videos': _count(session, Video),
            'video_snapshots': _count(session, VideoSnapshot),
            'transcripts': _count(session, Transcript),
            'audio_assessments': _count(session, AudioAssessment),
            'analysis_acquisitions': _count(session, AnalysisAcquisition),
        },
        'transcription_jobs_by_status': job_statuses,
        'duplicates': {
            'videos_tiktok_id': duplicate_videos,
            'transcripts_video_id': duplicate_transcripts,
        },
        'channels': channels,
        # IDs are deliberately metadata-only: they make F/G meaningful instead
        # of merely comparing counts, and no transcript/audio content is saved.
        'transcript_ids': sorted(str(value) for value in session.scalars(select(Transcript.id))),
        'audio_assessment_ids': sorted(str(value) for value in session.scalars(select(AudioAssessment.id))),
        'latest_analysis': _latest_analysis_summary(session),
    }


def begin_read_only(session: Session) -> None:
    """Ask PostgreSQL itself to reject writes for this audit transaction."""
    if session.bind is not None and session.bind.dialect.name == 'postgresql':
        session.execute(text('SET TRANSACTION READ ONLY'))


def _global_ranking_matches(session: Session, analysis: dict[str, Any] | None) -> bool:
    """Check current scan ranks against all latest observations of its channel."""
    if analysis is None:
        return False
    current = session.get(Analysis, UUID(analysis['id']))
    if current is None:
        return False
    rows = _latest_rows(session, current.channel_id)
    _, ranked = rank_snapshots((video, snapshot) for video, snapshot, _ in rows)
    expected = {video.id: position for position, (video, _, _) in enumerate(ranked, 1)}
    scan_snapshots = session.scalars(
        select(VideoSnapshot).where(VideoSnapshot.analysis_id == current.id)
    )
    return all(expected.get(snapshot.video_id) == snapshot.overall_rank for snapshot in scan_snapshots)


def compare_snapshots(session: Session, baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare a baseline with the current state and evaluate A--I."""
    if baseline.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Unsupported baseline schema')
    required = {'counts', 'transcript_ids', 'audio_assessment_ids', 'latest_analysis'}
    if not required <= baseline.keys():
        raise ValueError('Incomplete baseline')

    delta_keys = ('analyses', 'videos', 'video_snapshots', 'transcripts',
                  'audio_assessments', 'analysis_acquisitions')
    deltas = {key: current['counts'][key] - baseline['counts'][key] for key in delta_keys}
    latest = current['latest_analysis']
    failures: list[str] = []
    has_new_analysis = latest is not None and latest['id'] != (baseline['latest_analysis'] or {}).get('id')
    if not has_new_analysis:
        failures.append('NEW_ANALYSIS_AFTER_BASELINE: no new analysis is available to validate')
    if has_new_analysis:
        if deltas['videos'] != latest['videos_new']:
            failures.append('A: delta_videos != videos_new')
        if deltas['video_snapshots'] != latest['videos_seen_this_scan']:
            failures.append('B: delta_snapshots != videos_seen_this_scan')
        if latest['videos_new'] + latest['videos_refreshed'] != latest['videos_seen_this_scan']:
            failures.append('C: videos_new + videos_refreshed != videos_seen_this_scan')
    if current['duplicates']['videos_tiktok_id'] != 0:
        failures.append('D: duplicate videos.tiktok_id found')
    if current['duplicates']['transcripts_video_id'] != 0:
        failures.append('E: duplicate transcripts.video_id found')
    if not set(baseline['transcript_ids']) <= set(current['transcript_ids']):
        failures.append('F: existing transcripts disappeared')
    if not set(baseline['audio_assessment_ids']) <= set(current['audio_assessment_ids']):
        failures.append('G: existing audio_assessments disappeared')
    redundant = session.execute(
        select(Video.tiktok_id)
        .join(Transcript, Transcript.video_id == Video.id)
        .join(TranscriptionJob, TranscriptionJob.video_id == Video.id)
        .where(TranscriptionJob.status.in_(ACTIVE_JOB_STATUSES))
    ).scalars().all()
    if redundant:
        failures.append('H: transcript-bearing videos have active redundant enrichment jobs')
    if has_new_analysis and not _global_ranking_matches(session, latest):
        failures.append('I: current analysis ranking is not global for its channel')
    return {'deltas': deltas, 'latest_analysis': latest, 'failures': failures,
            'redundant_job_videos': sorted(redundant)}


def _print_baseline(snapshot: dict[str, Any], output: Path | None) -> None:
    print(f"Alembic revision: {snapshot['alembic_revision'] or 'unavailable'}")
    for name, value in snapshot['counts'].items():
        print(f'{name}: {value}')
    print(f"transcription_jobs: {json.dumps(snapshot['transcription_jobs_by_status'], sort_keys=True)}")
    print(f"duplicate videos.tiktok_id: {snapshot['duplicates']['videos_tiktok_id']}")
    print(f"duplicate transcripts.video_id: {snapshot['duplicates']['transcripts_video_id']}")
    for channel in snapshot['channels']:
        print('channel: username={username} tiktok_user_id={tiktok_user_id} videos={videos_total} '
              'transcripts_available={transcripts_available}'.format(**channel))
    if output:
        print(f'Baseline JSON saved: {output}')


def _print_comparison(result: dict[str, Any]) -> None:
    labels = {
        'analyses': 'delta_analyses',
        'videos': 'delta_videos',
        'video_snapshots': 'delta_snapshots',
        'transcripts': 'delta_transcripts',
        'audio_assessments': 'delta_assessments',
        'analysis_acquisitions': 'delta_analysis_acquisitions',
    }
    for name, value in result['deltas'].items():
        print(f'{labels[name]}: {value}')
    latest = result['latest_analysis']
    if latest is None:
        print('latest_analysis: unavailable')
    else:
        print('latest_analysis:')
        for key in ('videos_seen_this_scan', 'videos_new', 'videos_refreshed',
                    'acquisition_requested', 'username', 'tiktok_user_id',
                    'videos_known', 'transcripts_available', 'transcripts_missing',
                    'high_value_candidates', 'high_value_transcribed', 'resolved_enrichment'):
            print(f'  {key}: {latest[key]}')
    if result['failures']:
        print('Failed invariants:')
        for failure in result['failures']:
            print(f'  {failure}')


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    baseline = commands.add_parser('baseline', help='capture a read-only baseline')
    baseline.add_argument('--output', type=Path, help='local JSON output path, e.g. /tmp/kurukin-incremental-baseline.json')
    compare = commands.add_parser('compare', help='compare current state to a baseline JSON')
    compare.add_argument('baseline', type=Path, help='baseline JSON created by this script')
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.command == 'compare':
            baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
        engine = get_engine()
        with Session(engine) as session:
            begin_read_only(session)
            current = collect_snapshot(session)
            if args.command == 'baseline':
                if args.output:
                    args.output.write_text(json.dumps(current, indent=2, sort_keys=True) + '\n', encoding='utf-8')
                _print_baseline(current, args.output)
                print('RESULT: PASS')
                return 0
            result = compare_snapshots(session, baseline, current)
            _print_comparison(result)
            print('RESULT: PASS' if not result['failures'] else 'RESULT: FAIL')
            return 0 if not result['failures'] else 1
    except (OSError, ValueError, json.JSONDecodeError, SQLAlchemyError, RuntimeError):
        # Configuration and driver exceptions can contain a URL; retain the
        # operator-safe behaviour of the existing ops scripts.
        print('Validation could not be completed; configuration, baseline, or database access is unavailable.', file=sys.stderr)
        print('RESULT: FAIL')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
