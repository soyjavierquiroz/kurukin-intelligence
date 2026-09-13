#!/usr/bin/env python3
"""Run only the approved three-video Semantic DNA production canary.

The UUID list is intentionally closed: this command accepts no video-ID,
batch, or limit flags.  It reads only the existing global Video and Transcript
rows, checks migration 0007 before any provider invocation, and delegates
idempotence to the versioned Phase B extractor.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from alembic.runtime.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.llm.providers import register_builtin_semantic_providers
from app.llm.semantic import SemanticProvider, SemanticProviderConfig, get_semantic_provider
from app.models import Transcript, Video
from app.viral_dna import extract_semantic_viral_dna_for_video


REQUIRED_REVISION = '0007_semantic_viral_dna'
CANARY_PROVIDER = 'openai'
CANARY_MODEL = 'gpt-5.6-luna'
CANARY_VIDEO_IDS = (
    UUID('3073174b-3919-457c-9dab-74d1e0316225'),
    UUID('87f2fd92-b2e6-434b-8a25-9cdbcf275849'),
    UUID('f141413b-7423-4d76-b309-04c9c44b9e5f'),
)


def require_database_revision(session: Session) -> None:
    """Reject any database state other than the reviewed canary revision."""
    current = MigrationContext.configure(session.connection()).get_current_revision()
    if current != REQUIRED_REVISION:
        raise RuntimeError('semantic_canary_requires_migration_0007')


def require_canary_transcripts(session: Session) -> list[Video]:
    """Verify all and only the fixed corpus inputs before an API call is possible."""
    videos = {
        video.id: video for video in session.scalars(
            select(Video).where(Video.id.in_(CANARY_VIDEO_IDS))
        )
    }
    transcript_ids = set(session.scalars(
        select(Transcript.video_id).where(Transcript.video_id.in_(CANARY_VIDEO_IDS))
    ))
    if set(videos) != set(CANARY_VIDEO_IDS):
        raise RuntimeError('semantic_canary_video_not_found')
    if transcript_ids != set(CANARY_VIDEO_IDS):
        raise RuntimeError('semantic_canary_transcript_not_found')
    return [videos[video_id] for video_id in CANARY_VIDEO_IDS]


def require_canary_routing() -> SemanticProvider:
    """Construct exactly the approved OpenAI adapter without contacting it."""
    settings = get_settings()
    policy = settings.semantic_viral_dna_routing_policy
    if (
        policy is None
        or policy.fallback is not None
        or policy.primary.provider_name != CANARY_PROVIDER
        or policy.primary.model != CANARY_MODEL
    ):
        raise RuntimeError('semantic_canary_requires_openai_gpt_5_6_luna')
    register_builtin_semantic_providers()
    config = SemanticProviderConfig(provider_name=CANARY_PROVIDER, model=CANARY_MODEL)
    return get_semantic_provider(CANARY_PROVIDER, config)


def run_canary(session: Session, provider: SemanticProvider) -> dict[str, int]:
    """Extract the fixed three global records and commit their canonical result."""
    require_database_revision(session)
    videos = require_canary_transcripts(session)
    counts = {'completed': 0, 'unchanged': 0, 'failed': 0, 'skipped_no_transcript': 0}
    for video in videos:
        with session.begin_nested():
            result = extract_semantic_viral_dna_for_video(
                session,
                video,
                provider,
                semantic_provider=CANARY_PROVIDER,
                semantic_model=CANARY_MODEL,
            )
        counts[result.status] += 1
    session.commit()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--execute', action='store_true',
        help='perform the closed three-video canary; omitted means no database or provider work',
    )
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute is required; this canary never selects videos dynamically')
    try:
        provider = require_canary_routing()
        with Session(get_engine()) as session:
            counts = run_canary(session, provider)
    except Exception:
        # Provider, configuration and database details can contain operational
        # context.  The stable code is sufficient for a runbook diagnosis.
        print(json.dumps({'status': 'failed', 'code': 'semantic_canary_failed'}, sort_keys=True))
        return 1
    print(json.dumps({
        'status': 'ok', 'video_count': len(CANARY_VIDEO_IDS),
        'provider': CANARY_PROVIDER, 'model': CANARY_MODEL, **counts,
    }, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
