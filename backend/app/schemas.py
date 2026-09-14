from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SENSITIVE = {'secuid', 'verifyfp', 'device_id', 'odinid', 'cookies', 'tokens',
             'authorization', 'playaddr', 'downloadaddr', 'mediaurl', 'headers'}


def reject_sensitive(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in SENSITIVE:
                raise ValueError(f'Sensitive field forbidden: {key}')
            reject_sensitive(child)
    elif isinstance(value, list):
        for child in value:
            reject_sensitive(child)
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


Username = Annotated[str, Field(pattern=r'^[A-Za-z0-9_.]{1,64}$')]
Count = Annotated[int, Field(ge=0, le=9223372036854775807)]


class Profile(StrictModel):
    username: Username
    nickname: str = Field(max_length=256)
    # Optional for backwards compatibility with the existing extension.  New
    # collectors should send TikTok's stable author/user id when available.
    author_id: str | None = Field(default=None, min_length=1, max_length=64,
                                  pattern=r'^[A-Za-z0-9_-]+$')


class VideoInput(StrictModel):
    id: str = Field(pattern=r'^\d{5,30}$')
    author: Username
    nickname: str = Field(max_length=256)
    caption: str = Field(max_length=10000)
    created_at: int = Field(ge=0, le=253402300799)
    views: Count
    likes: Count
    comments: Count
    shares: Count
    favorites: Count
    duration: float | None = Field(ge=0, le=86400)
    url: str = Field(max_length=2048)
    # TikTok sound IDs remain strings: public IDs exceed safe JavaScript integer precision.
    music_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r'^\d{1,64}$')
    music_title: str | None = Field(default=None, min_length=1, max_length=512)
    music_author: str | None = Field(default=None, min_length=1, max_length=256)
    music_original: bool | None = None

    @field_validator('music_title', 'music_author')
    @classmethod
    def music_text_is_not_a_url(cls, value):
        if value is not None and value.lstrip().lower().startswith(('http://', 'https://')):
            raise ValueError('music metadata must not be a media URL')
        return value

    @model_validator(mode='after')
    def public_url(self):
        if self.url != f'https://www.tiktok.com/@{self.author}/video/{self.id}':
            raise ValueError('url must be the public canonical video URL, without query parameters')
        return self


class AnalysisInput(StrictModel):
    profile: Profile
    videos: list[VideoInput] = Field(min_length=1, max_length=500)

    @model_validator(mode='before')
    @classmethod
    def sensitive_fields(cls, value):
        return reject_sensitive(value)

    @model_validator(mode='after')
    def unique_channel_videos(self):
        if len({v.id for v in self.videos}) != len(self.videos):
            raise ValueError('Duplicate video IDs')
        if any(v.author.lower() != self.profile.username.lower() for v in self.videos):
            raise ValueError('All video authors must match profile.username')
        return self


class AnalysisCheckpointInput(AnalysisInput):
    """A safe, idempotent browser checkpoint for one logical analysis scan."""
    analysis_id: UUID
    scan_id: UUID
    checkpoint_number: Annotated[int, Field(ge=1, le=1_000_000)]
    checkpoint_count: Annotated[int, Field(ge=1, le=1_000_000)]
    discovered_count: Count
    # ``full`` is the product default. Numeric targets remain intentionally
    # available for bounded administrative debugging.
    target: Literal['full'] | Annotated[int, Field(ge=1, le=200)]
    has_more: bool = False

    @field_validator('analysis_id', 'scan_id', mode='before')
    @classmethod
    def wire_uuid_is_canonical(cls, value):
        # JSON has no UUID scalar.  Parse only canonical UUID strings before
        # strict model validation, rather than weakening the whole contract.
        if not isinstance(value, str):
            raise ValueError('UUID string required')
        parsed = UUID(value)
        if str(parsed) != value.lower():
            raise ValueError('canonical UUID required')
        return parsed

    @model_validator(mode='after')
    def checkpoint_is_coherent(self):
        if self.checkpoint_count != self.checkpoint_number:
            raise ValueError('checkpoint_count must equal checkpoint_number')
        if isinstance(self.target, int) and self.discovered_count > self.target:
            raise ValueError('discovered_count must not exceed target')
        return self


class AcquisitionBatchInput(StrictModel):
    """Whether this browser request is the final, full-corpus drain."""
    discovery_complete: bool = False


class BrowserAcquisitionFailure(StrictModel):
    """Safe, browser-originated reason for abandoning a reserved acquisition."""
    code: Literal['FETCH_MP4_VIDEO_NOT_AVAILABLE', 'FETCH_MP4_FAILED', 'DECODE_FAILED',
                  'WAV_FAILED', 'HANDOFF_FAILED', 'HTTP_UPLOAD_FAILED']
