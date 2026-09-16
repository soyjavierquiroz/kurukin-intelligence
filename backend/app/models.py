import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from .llm.semantic_contract import (
    SEMANTIC_ENUM_FIELDS,
    SEMANTIC_SECONDARY_PRIMARY_PAIRS,
    SEMANTIC_TEXT_FIELDS,
    semantic_secondary_constraint_name,
)


def now():
    return datetime.now(timezone.utc)


def _semantic_enum_constraint(field: str) -> CheckConstraint:
    values = ','.join(repr(value) for value in SEMANTIC_ENUM_FIELDS[field])
    return CheckConstraint(
        f'{field} IN ({values})', name=f'ck_viral_dna_{field}',
    )


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Channel(Identity, Base):
    __tablename__ = 'channels'
    __table_args__ = (UniqueConstraint('platform', 'tiktok_user_id', name='uq_channels_platform_tiktok_user_id'),)
    platform: Mapped[str] = mapped_column(String(32), default='tiktok')
    # TikTok's immutable account identifier.  username is intentionally retained
    # as display/public-URL metadata for legacy clients and changed handles.
    tiktok_user_id: Mapped[str | None] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(64))
    nickname: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Analysis(Identity, Base):
    __tablename__ = 'analyses'
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('channels.id'), index=True)
    status: Mapped[str] = mapped_column(String(32), default='awaiting_audio')
    video_count: Mapped[int] = mapped_column(Integer)
    median_views: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    requested_transcripts: Mapped[int] = mapped_column(Integer)
    completed_transcripts: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    videos_new: Mapped[int] = mapped_column(Integer, default=0)
    videos_refreshed: Mapped[int] = mapped_column(Integer, default=0)


class Video(Identity, Base):
    __tablename__ = 'videos'
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('channels.id'), index=True)
    tiktok_id: Mapped[str] = mapped_column(String(30), unique=True)
    author: Mapped[str] = mapped_column(String(64))
    nickname: Mapped[str] = mapped_column(String(256))
    caption: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration: Mapped[float | None]
    url: Mapped[str] = mapped_column(String(2048))
    # Global TikTok sound metadata.  It is deliberately not analysis- or user-scoped.
    music_id: Mapped[str | None] = mapped_column(String(64))
    music_title: Mapped[str | None] = mapped_column(String(512))
    music_author: Mapped[str | None] = mapped_column(String(256))
    music_original: Mapped[bool | None] = mapped_column(Boolean)
    enrichment_status: Mapped[str] = mapped_column(String(16), default='missing')
    enrichment_analysis_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('analyses.id'))
    enrichment_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class VideoSnapshot(Identity, Base):
    __tablename__ = 'video_snapshots'
    __table_args__ = (UniqueConstraint('analysis_id', 'video_id'),)
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('analyses.id'), index=True)
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('videos.id'), index=True)
    views: Mapped[int] = mapped_column(BigInteger)
    likes: Mapped[int] = mapped_column(BigInteger)
    comments: Mapped[int] = mapped_column(BigInteger)
    shares: Mapped[int] = mapped_column(BigInteger)
    favorites: Mapped[int] = mapped_column(BigInteger)
    like_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    comment_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    share_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    favorite_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    engagement_rate: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    outlier_score: Mapped[Decimal] = mapped_column(Numeric(30, 10))
    overall_rank: Mapped[int] = mapped_column(Integer)
    transcription_rank: Mapped[int | None] = mapped_column(Integer)
    transcript_eligible: Mapped[bool] = mapped_column(Boolean)
    transcript_skip_reason: Mapped[str | None] = mapped_column(String(32))


class Transcript(Identity, Base):
    __tablename__ = 'transcripts'
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('videos.id'), unique=True)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(32))
    duration: Mapped[float | None]
    model: Mapped[str] = mapped_column(String(32))


class AnalysisAcquisition(Identity, Base):
    """A session's reference to a globally-owned video selected for audio work."""
    __tablename__ = 'analysis_acquisitions'
    __table_args__ = (UniqueConstraint('analysis_id', 'video_id', name='uq_analysis_acquisition_video'),)
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('analyses.id'), index=True)
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('videos.id'), index=True)
    transcription_rank: Mapped[int] = mapped_column(Integer)


class AudioAssessment(Identity, Base):
    __tablename__ = 'audio_assessments'
    __table_args__ = (UniqueConstraint('video_id', 'classifier', 'classifier_version', name='uq_audio_assessment_video_classifier_version'),
                      CheckConstraint("classification IN ('speech','music','singing','mixed','ambiguous')", name='ck_audio_assessment_classification'),
                      CheckConstraint('speech_score >= 0 AND speech_score <= 1 AND music_score >= 0 AND music_score <= 1 AND singing_score >= 0 AND singing_score <= 1 AND speech_patch_ratio >= 0 AND speech_patch_ratio <= 1 AND music_patch_ratio >= 0 AND music_patch_ratio <= 1 AND singing_patch_ratio >= 0 AND singing_patch_ratio <= 1', name='ck_audio_assessment_scores'))
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('videos.id'), index=True)
    classifier: Mapped[str] = mapped_column(String(64))
    classifier_version: Mapped[str] = mapped_column(String(64))
    model_sha256: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(32))
    speech_score: Mapped[float]
    music_score: Mapped[float]
    singing_score: Mapped[float]
    speech_patch_ratio: Mapped[float]
    music_patch_ratio: Mapped[float]
    singing_patch_ratio: Mapped[float]
    top_classes: Mapped[list | None] = mapped_column(JSON().with_variant(JSONB, 'postgresql'))
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ViralDNA(Identity, Base):
    """Versioned deterministic features owned globally by a video."""
    __tablename__ = 'viral_dna'
    __table_args__ = (
        UniqueConstraint('video_id', 'extractor_version', name='uq_viral_dna_video_extractor_version'),
        CheckConstraint(
            "semantic_status IN ('not_requested','pending','completed','skipped_no_transcript','failed')",
            name='ck_viral_dna_semantic_status',
        ),
        *(_semantic_enum_constraint(field) for field in SEMANTIC_ENUM_FIELDS),
        *(CheckConstraint(
            f'{secondary} IS NULL OR {primary} != {secondary}',
            name=semantic_secondary_constraint_name(primary, secondary),
        ) for primary, secondary in SEMANTIC_SECONDARY_PRIMARY_PAIRS),
    )
    # The composite unique constraint indexes video_id already, so a separate
    # single-column index would be redundant.
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('videos.id'))
    extractor_version: Mapped[str] = mapped_column(String(64))
    deterministic_input_sha256: Mapped[str] = mapped_column(String(64))
    duration_seconds: Mapped[float | None]
    caption_present: Mapped[bool] = mapped_column(Boolean)
    caption_char_count: Mapped[int] = mapped_column(Integer)
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('transcripts.id'))
    transcript_word_count: Mapped[int | None] = mapped_column(Integer)
    transcript_duration_seconds: Mapped[float | None]
    words_per_second: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    audio_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey('audio_assessments.id'), index=True
    )
    semantic_status: Mapped[str] = mapped_column(String(32))
    # Phase B semantic fields are global to this (video, extractor_version)
    # identity.  They intentionally have no Analysis, user, or snapshot link.
    hook_text: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['hook_text']))
    hook_type: Mapped[str | None] = mapped_column(String(32))
    hook_mechanism: Mapped[str | None] = mapped_column(String(32))
    hook_target: Mapped[str | None] = mapped_column(String(32))
    audience_specificity: Mapped[str | None] = mapped_column(String(32))
    topic: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['topic']))
    subtopic: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['subtopic']))
    angle_type: Mapped[str | None] = mapped_column(String(32))
    angle_summary: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['angle_summary']))
    pain: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['pain']))
    desire: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['desire']))
    fear: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['fear']))
    audience_identity: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['audience_identity']))
    belief: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['belief']))
    objection: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['objection']))
    promise: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['promise']))
    reframe: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['reframe']))
    emotion_primary: Mapped[str | None] = mapped_column(String(32))
    emotion_secondary: Mapped[str | None] = mapped_column(String(32))
    emotional_arc: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['emotional_arc']))
    content_function_primary: Mapped[str | None] = mapped_column(String(32))
    content_function_secondary: Mapped[str | None] = mapped_column(String(32))
    content_role_primary: Mapped[str | None] = mapped_column(String(32))
    content_role_secondary: Mapped[str | None] = mapped_column(String(32))
    content_format: Mapped[str | None] = mapped_column(String(32))
    narrative_structure: Mapped[str | None] = mapped_column(String(32))
    awareness_stage: Mapped[str | None] = mapped_column(String(32))
    proof_type: Mapped[str | None] = mapped_column(String(32))
    authority_mechanism: Mapped[str | None] = mapped_column(String(32))
    creator_positioning_signal: Mapped[str | None] = mapped_column(String(32))
    cta_type: Mapped[str | None] = mapped_column(String(32))
    cta_secondary_type: Mapped[str | None] = mapped_column(String(32))
    cta_text: Mapped[str | None] = mapped_column(String(SEMANTIC_TEXT_FIELDS['cta_text']))
    commercial_intent: Mapped[str | None] = mapped_column(String(32))
    offer_integration: Mapped[str | None] = mapped_column(String(32))
    offer_type: Mapped[str | None] = mapped_column(String(32))
    monetization_model: Mapped[str | None] = mapped_column(String(32))
    semantic_input_sha256: Mapped[str | None] = mapped_column(String(64))
    # Provider-qualified model provenance, e.g. ``openai:gpt-x``.  Keeping
    # this in the existing column avoids a schema change while preserving the
    # exact adapter/model identity used for extraction.
    semantic_model: Mapped[str | None] = mapped_column(String(128))
    semantic_prompt_version: Mapped[str | None] = mapped_column(String(64))
    semantic_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ChannelIntelligenceAnalysis(Identity, Base):
    """One external structured analysis of one immutable Research Pack."""
    __tablename__ = 'channel_intelligence_analyses'
    __table_args__ = (
        UniqueConstraint('channel_id', 'research_pack_hash', 'schema_version',
                         name='uq_channel_intelligence_pack_schema'),
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('channels.id'), index=True)
    research_pack_hash: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    selection_mode: Mapped[str] = mapped_column(String(32))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    channel_intelligence: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, 'postgresql'))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ChannelVideoIntelligence(Identity, Base):
    """The mandatory per-video half of a channel intelligence analysis."""
    __tablename__ = 'channel_video_intelligence'
    __table_args__ = (
        UniqueConstraint('analysis_id', 'video_id', name='uq_channel_video_intelligence_analysis_video'),
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('channel_intelligence_analyses.id'), index=True)
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('videos.id'), index=True)
    intelligence: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, 'postgresql'))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TranscriptionJob(Identity, Base):
    __tablename__ = 'transcription_jobs'
    __table_args__ = (
        CheckConstraint("status IN ('reserved','audio_received','queued','processing','completed','failed','expired','skipped')", name='ck_job_status'),
    )
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('videos.id'), unique=True)
    status: Mapped[str] = mapped_column(String(32), default='reserved', index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50, server_default='50')
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    audio_path: Mapped[str | None] = mapped_column(String(1024))
    audio_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    audio_duration: Mapped[float | None]
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audio_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('audio_assessments.id', name='fk_transcription_jobs_assessment_id'))
    classifier_error_code: Mapped[str | None] = mapped_column(String(64))
    skip_reason: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
