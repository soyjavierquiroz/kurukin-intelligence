"""Global, deterministic Phase A Viral DNA extraction.

This module deliberately reads only a Video and its globally-owned Transcript
and AudioAssessment rows.  It has no Analysis, user, scan, or acquisition
inputs, so a video's Viral DNA is reusable across every scan that references it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import logging
import re
from typing import Literal, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AudioAssessment, Transcript, Video, ViralDNA
from .llm.semantic import SemanticProvider, semantic_provider_model_identifier
from .llm.semantic_contract import (
    SEMANTIC_CONTRACT_VERSION,
    SEMANTIC_ENUM_FIELDS,
    SEMANTIC_NULLABLE_FIELDS,
    SEMANTIC_OUTPUT_FIELDS,
    SEMANTIC_SECONDARY_PRIMARY_PAIRS,
    SEMANTIC_TEXT_FIELDS,
)
from .yamnet import CLASSIFIER as CURRENT_CLASSIFIER, VERSION as CURRENT_CLASSIFIER_VERSION


EXTRACTOR_VERSION = 'viral-dna-v1'
SEMANTIC_PROMPT_VERSION = 'viral-dna-semantic-v1'
SEMANTIC_PROMPT = """Analyze only caption and transcript. Return exactly one JSON object matching the supplied closed schema, with no explanation or reasoning. Do not use popularity, performance, truth evaluation, visuals, channel-wide niche, Analysis, user, or private-business context. Describe only observable communication strategy. When transcript is nonempty, derive hook_text, hook_type, hook_mechanism, and hook_target from its opening spoken words; never replace that hook with the caption. If no clear spoken hook exists, use null for hook_text and none_unclear for its hook enums. Write every free-text field in the supplied language; use exact canonical English enum tokens without translating them. For every primary/secondary pair, set secondary to null unless a distinct clear second signal exists, and never repeat primary. For nullable free text, write only what has reasonable caption or transcript evidence; otherwise use null. Never infer merely plausible objections, fears, audience identities, promises, or unobserved proof such as demonstration or personal_experience. Do not invent intent: use null, unclear, unknown, or none_unclear as defined. Normalize free text to short plain phrases; no markdown or lists. Never infer visual format."""
WORD_PATTERN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
LOGGER = logging.getLogger(__name__)


class SemanticOutputValidationError(ValueError):
    """A content-free validation error carrying only approved diagnostics."""

    def __init__(self, validation_errors: tuple[dict[str, object], ...]) -> None:
        # Do not put any provider value in the exception message: some callers
        # deliberately log only ``str(error)``.
        super().__init__('semantic_output_invalid')
        self.validation_errors = validation_errors


def _semantic_validation_error(field: str, code: str, /, **details: object) -> SemanticOutputValidationError:
    """Build a diagnostic from contract metadata, never provider-supplied text."""
    return SemanticOutputValidationError(({'field': field, 'code': code, **details},))


@dataclass(frozen=True)
class SemanticViralDNAResult:
    row: ViralDNA
    status: Literal['completed', 'failed', 'skipped_no_transcript', 'unchanged']
    error_code: str | None = None

    @property
    def action(self) -> str:
        """Alias matching the deterministic extractor result naming."""
        return self.status


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


def semantic_input_sha256(
    *,
    language: str,
    caption: str,
    transcript: str,
    semantic_provider: str,
    semantic_model: str,
    semantic_prompt_version: str = SEMANTIC_PROMPT_VERSION,
) -> str:
    """Hash the versioned Phase B input with stable UTF-8 JSON.

    This deliberately has no overlap with Phase A's deterministic source hash:
    it represents only the semantic contract, provider policy, and text assets.
    """
    payload = {
        'caption': caption,
        'language': language,
        'semantic_contract_version': SEMANTIC_CONTRACT_VERSION,
        'semantic_provider_model': semantic_provider_model_identifier(
            semantic_provider, semantic_model
        ),
        'semantic_prompt_version': semantic_prompt_version,
        'transcript': transcript,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def semantic_payload(video: Video, transcript: Transcript) -> dict[str, str]:
    """Build the sole provider input from global text assets."""
    return {
        'language': (transcript.language or '').strip(),
        'caption': (video.caption or '').strip(),
        'transcript': transcript.text,
    }


_MARKDOWN_OR_LIST = re.compile(r'(?:^|\n)\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+)|[`*_]{2,}')


def _normalize_semantic_text(value: object, *, maximum_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _semantic_validation_error('$', 'invalid_type')
    if _MARKDOWN_OR_LIST.search(value):
        raise _semantic_validation_error('$', 'invalid_text')
    normalized = ' '.join(value.split())
    if not normalized:
        raise _semantic_validation_error('$', 'invalid_text')
    if len(normalized) > maximum_length:
        raise _semantic_validation_error(
            '$', 'max_length', maximum_length=maximum_length, received_length=len(normalized)
        )
    return normalized


def validate_semantic_output(output: object) -> dict[str, str | None]:
    """Strictly validate and normalize the one Kurukin-owned response shape."""
    if not isinstance(output, Mapping):
        raise _semantic_validation_error('$', 'invalid_object')
    output_fields = set(output)
    contract_fields = set(SEMANTIC_OUTPUT_FIELDS)
    missing_fields = contract_fields - output_fields
    extra_fields = output_fields - contract_fields
    if missing_fields or extra_fields:
        errors = tuple(
            [{'field': field, 'code': 'missing'} for field in sorted(missing_fields)]
            # An unexpected key is provider-controlled, so even its name is
            # not safe to expose. The root path identifies this schema rule.
            + [{'field': '$', 'code': 'extra'} for _ in extra_fields]
        )
        raise SemanticOutputValidationError(errors)

    normalized: dict[str, str | None] = {}
    for field in SEMANTIC_OUTPUT_FIELDS:
        value = output[field]
        if field in SEMANTIC_TEXT_FIELDS:
            try:
                normalized[field] = _normalize_semantic_text(
                    value, maximum_length=SEMANTIC_TEXT_FIELDS[field]
                )
            except SemanticOutputValidationError as error:
                diagnostic = dict(error.validation_errors[0])
                diagnostic['field'] = field
                raise SemanticOutputValidationError((diagnostic,)) from None
            continue
        if value is None:
            if field not in SEMANTIC_NULLABLE_FIELDS:
                raise _semantic_validation_error(field, 'not_nullable')
            normalized[field] = None
            continue
        if not isinstance(value, str):
            raise _semantic_validation_error(field, 'invalid_type')
        value = value.strip()
        if value not in SEMANTIC_ENUM_FIELDS[field]:
            raise _semantic_validation_error(field, 'invalid_enum')
        normalized[field] = value

    for primary, secondary in SEMANTIC_SECONDARY_PRIMARY_PAIRS:
        if normalized[secondary] is not None and normalized[primary] == normalized[secondary]:
            raise _semantic_validation_error(secondary, 'same_as_primary')
    return normalized


def _clear_semantic_values(row: ViralDNA, *, status: str) -> None:
    """Leave deterministic Phase A values untouched after a Phase B non-success."""
    for field in SEMANTIC_OUTPUT_FIELDS:
        setattr(row, field, None)
    row.semantic_input_sha256 = None
    row.semantic_model = None
    row.semantic_prompt_version = None
    row.semantic_extracted_at = None
    row.semantic_status = status


def _provider_extract(provider: SemanticProvider, payload: dict[str, str]) -> object:
    # Keep provider integration out of the core: this invokes only the tiny
    # injected interface and imports no SDK or transport.
    return provider.extract(payload)


def extract_semantic_viral_dna_for_video(
    session: Session,
    video: Video | UUID,
    provider: SemanticProvider,
    *,
    semantic_provider: str,
    semantic_model: str,
    semantic_prompt_version: str = SEMANTIC_PROMPT_VERSION,
    extractor_version: str = EXTRACTOR_VERSION,
) -> SemanticViralDNAResult:
    """Extract global semantic Viral DNA for one video using an injected provider.

    The deterministic row is ensured first only to obtain the established
    global (video, extractor_version) identity.  The provider receives exactly
    language, caption, and transcript, all from Video/Transcript.
    """
    if isinstance(video, UUID):
        video = session.get(Video, video)
        if video is None:
            raise LookupError('video_not_found')
    semantic_provider_model = semantic_provider_model_identifier(
        semantic_provider, semantic_model
    )
    if len(semantic_provider_model) > 128:
        raise ValueError('invalid_semantic_provider_model')
    if not semantic_prompt_version or len(semantic_prompt_version) > 64:
        raise ValueError('invalid_semantic_prompt_version')

    row = extract_viral_dna_for_video(
        session, video, extractor_version=extractor_version
    ).row
    transcript = session.scalar(select(Transcript).where(Transcript.video_id == video.id))
    if transcript is None:
        _clear_semantic_values(row, status='skipped_no_transcript')
        session.flush()
        return SemanticViralDNAResult(row, 'skipped_no_transcript')

    payload = semantic_payload(video, transcript)
    input_hash = semantic_input_sha256(
        language=payload['language'],
        caption=payload['caption'],
        transcript=payload['transcript'],
        semantic_provider=semantic_provider,
        semantic_model=semantic_model,
        semantic_prompt_version=semantic_prompt_version,
    )
    if (
        row.semantic_status == 'completed'
        and row.semantic_input_sha256 == input_hash
        and row.semantic_model == semantic_provider_model
        and row.semantic_prompt_version == semantic_prompt_version
    ):
        return SemanticViralDNAResult(row, 'unchanged')

    try:
        output = validate_semantic_output(_provider_extract(provider, payload))
    except SemanticOutputValidationError:
        _clear_semantic_values(row, status='failed')
        session.flush()
        LOGGER.warning('semantic_viral_dna_failed code=semantic_output_invalid')
        return SemanticViralDNAResult(row, 'failed', 'semantic_output_invalid')
    except Exception:
        _clear_semantic_values(row, status='failed')
        session.flush()
        LOGGER.warning('semantic_viral_dna_failed code=semantic_provider_exception')
        return SemanticViralDNAResult(row, 'failed', 'semantic_provider_exception')

    for field in SEMANTIC_OUTPUT_FIELDS:
        setattr(row, field, output[field])
    row.semantic_input_sha256 = input_hash
    row.semantic_model = semantic_provider_model
    row.semantic_prompt_version = semantic_prompt_version
    row.semantic_extracted_at = datetime.now(timezone.utc)
    row.semantic_status = 'completed'
    session.flush()
    return SemanticViralDNAResult(row, 'completed')
