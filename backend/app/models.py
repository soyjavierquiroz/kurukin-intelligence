import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB


def now():
    return datetime.now(timezone.utc)


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
