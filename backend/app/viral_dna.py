"""Global, deterministic Phase A Viral DNA extraction.

This module deliberately reads only a Video and its globally-owned Transcript
and AudioAssessment rows.  It has no Analysis, user, scan, or acquisition
inputs, so a video's Viral DNA is reusable across every scan that references it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AudioAssessment, Transcript, Video, ViralDNA
from .yamnet import CLASSIFIER as CURRENT_CLASSIFIER, VERSION as CURRENT_CLASSIFIER_VERSION


EXTRACTOR_VERSION = 'viral-dna-v1'
WORD_PATTERN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class DeterministicFeatures:
    """Computed Phase A fields plus the source hash used for idempotence."""

    deterministic_input_sha256: str
    duration_seconds: float | None
    caption_present: bool
    caption_char_count: int
    transcript_id: UUID | None
    transcript_word_count: int | None
    transcript_duration_seconds: float | None
    words_per_second: Decimal | None
    audio_assessment_id: UUID | None
    semantic_status: str

    def values(self) -> dict[str, object]:
        return {
            'deterministic_input_sha256': self.deterministic_input_sha256,
            'duration_seconds': self.duration_seconds,
            'caption_present': self.caption_present,
            'caption_char_count': self.caption_char_count,
            'transcript_id': self.transcript_id,
            'transcript_word_count': self.transcript_word_count,
            'transcript_duration_seconds': self.transcript_duration_seconds,
            'words_per_second': self.words_per_second,
            'audio_assessment_id': self.audio_assessment_id,
            'semantic_status': self.semantic_status,
        }


@dataclass(frozen=True)
class ViralDNAUpsertResult:
    row: ViralDNA
    action: Literal['inserted', 'updated', 'unchanged']


def count_transcript_words(text: str) -> int:
    """Count Unicode word tokens without introducing an NLP dependency.

    A word is a maximal run of Unicode alphanumeric characters (underscore is
    a separator); one internal straight or curly apostrophe may join such runs.
    Thus punctuation and arbitrary whitespace separate words, while Spanish
    accents remain part of their words.  An empty transcript has zero words.
    """
    return len(WORD_PATTERN.findall(text))


def select_global_audio_assessment(session: Session, video_id: UUID) -> AudioAssessment | None:
    """Select one globally-owned assessment reproducibly.

    The active server classifier/version (currently YAMNet) is preferred.  The
    schema allows only one assessment for that pair.  For historical rows with
    no active pair, the lexical tuple ``classifier, classifier_version,
    model_sha256, id`` is used ascending.  No Analysis or job provenance takes
    part in this choice.
    """
    current = session.scalar(
        select(AudioAssessment).where(
            AudioAssessment.video_id == video_id,
            AudioAssessment.classifier == CURRENT_CLASSIFIER,
            AudioAssessment.classifier_version == CURRENT_CLASSIFIER_VERSION,
        )
    )
    if current is not None:
        return current
    return session.scalars(
        select(AudioAssessment)
        .where(AudioAssessment.video_id == video_id)
        .order_by(
            AudioAssessment.classifier.asc(),
            AudioAssessment.classifier_version.asc(),
            AudioAssessment.model_sha256.asc(),
            AudioAssessment.id.asc(),
        )
        .limit(1)
    ).first()


def _canonical_input(
    video: Video,
    transcript: Transcript | None,
    assessment: AudioAssessment | None,
    extractor_version: str,
    effective_caption: str,
) -> str:
    """Return an order-independent canonical JSON encoding of Phase A inputs."""
    payload = {
        'audio_assessment': {'present': False} if assessment is None else {
            'classifier': assessment.classifier,
            'classifier_version': assessment.classifier_version,
            'id': str(assessment.id),
            'model_sha256': assessment.model_sha256,
            'present': True,
        },
        'caption_effective': effective_caption,
        'extractor_version': extractor_version,
        'transcript': {'present': False} if transcript is None else {
            'duration_seconds': transcript.duration,
            'id': str(transcript.id),
            'present': True,
            'text': transcript.text,
        },
        'video_duration_seconds': video.duration,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def compute_deterministic_features(
    video: Video,
    transcript: Transcript | None,
    audio_assessment: AudioAssessment | None,
    *,
    extractor_version: str = EXTRACTOR_VERSION,
) -> DeterministicFeatures:
    """Compute Phase A features from already-selected global assets only."""
    effective_caption = (video.caption or '').strip()
    if transcript is None:
        word_count = transcript_duration = words_per_second = None
        semantic_status = 'skipped_no_transcript'
    else:
        word_count = count_transcript_words(transcript.text)
        transcript_duration = transcript.duration
        if transcript_duration is None or transcript_duration <= 0:
            words_per_second = None
        else:
            words_per_second = (
                Decimal(word_count) / Decimal(str(transcript_duration))
            ).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
        semantic_status = 'not_requested'
    canonical_input = _canonical_input(
        video, transcript, audio_assessment, extractor_version, effective_caption
    )
    return DeterministicFeatures(
        deterministic_input_sha256=hashlib.sha256(canonical_input.encode('utf-8')).hexdigest(),
        duration_seconds=video.duration,
        caption_present=bool(effective_caption),
        caption_char_count=len(effective_caption),
        transcript_id=None if transcript is None else transcript.id,
        transcript_word_count=word_count,
        transcript_duration_seconds=transcript_duration,
        words_per_second=words_per_second,
        audio_assessment_id=None if audio_assessment is None else audio_assessment.id,
        semantic_status=semantic_status,
    )


def upsert_viral_dna(
    session: Session,
    video_id: UUID,
    features: DeterministicFeatures,
    *,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ViralDNAUpsertResult:
    """Insert, update, or leave unchanged one (video, extractor version) row.

    The nested insert transaction handles the only meaningful creation race:
    the database's unique constraint rejects a competing insert, after which
    the winning global row is returned unchanged.  The competing caller's
    features were computed against a stale view and must never update it.
    """
    row = session.scalar(select(ViralDNA).where(
        ViralDNA.video_id == video_id,
        ViralDNA.extractor_version == extractor_version,
    ))
    if row is None:
        candidate = ViralDNA(video_id=video_id, extractor_version=extractor_version, **features.values())
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
        except IntegrityError:
            # A concurrent transaction inserted the same constrained identity.
            row = session.scalar(select(ViralDNA).where(
                ViralDNA.video_id == video_id,
                ViralDNA.extractor_version == extractor_version,
            ))
            if row is None:
                raise
            return ViralDNAUpsertResult(row, 'unchanged')
        else:
            return ViralDNAUpsertResult(candidate, 'inserted')
    assert row is not None
    if row.deterministic_input_sha256 == features.deterministic_input_sha256:
        return ViralDNAUpsertResult(row, 'unchanged')
    for field, value in features.values().items():
        setattr(row, field, value)
    session.flush()
    return ViralDNAUpsertResult(row, 'updated')


def extract_viral_dna_for_video(
    session: Session,
    video: Video | UUID,
    *,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ViralDNAUpsertResult:
    """Load global assets for one video and upsert its deterministic DNA."""
    if isinstance(video, UUID):
        video = session.get(Video, video)
        if video is None:
            raise LookupError('video_not_found')
    transcript = session.scalar(select(Transcript).where(Transcript.video_id == video.id))
    assessment = select_global_audio_assessment(session, video.id)
    features = compute_deterministic_features(
        video, transcript, assessment, extractor_version=extractor_version
    )
    return upsert_viral_dna(session, video.id, features, extractor_version=extractor_version)
