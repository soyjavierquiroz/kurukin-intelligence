from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit
from pydantic import AliasChoices, Field, SecretStr, model_validator, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

# Adaptive enrichment policy.  These names are deliberately shared by the
# selection helpers below rather than being duplicated through request paths.
MIN_VIEWS = 10_000
MIN_OUTLIER = 2.0
INCREMENTAL_MEDIAN_MULTIPLIER = 3.0
ENRICHMENT_TARGET_RATIO = 0.15
ENRICHMENT_MIN_NEW = 20
ENRICHMENT_MAX_NEW = 100
MIN_DISCOVERED_FOR_ADAPTIVE_INCREMENTAL = 100

class DatabaseConfigurationError(RuntimeError):
    pass

class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra='ignore', hide_input_in_errors=True)
    database_url_file: str | None = Field(None, repr=False)
    database_url: SecretStr | None = Field(None, repr=False)
    acquisition_batch_size: int = Field(10, ge=1, le=500, validation_alias=AliasChoices('ACQUISITION_BATCH_SIZE', 'acquisition_batch_size', 'INITIAL_ENRICHMENT_BUDGET', 'TOP_TRANSCRIPTS', 'initial_enrichment_budget'))
    min_transcribe_duration_seconds: float = Field(8, ge=0, allow_inf_nan=False)
    auto_transcribe_max_duration_seconds: float = Field(180, gt=0, allow_inf_nan=False)
    hard_transcribe_max_duration_seconds: float = Field(300, gt=0, allow_inf_nan=False)
    high_value_outlier_threshold: float = Field(2.0, ge=0, allow_inf_nan=False)
    enrichment_min_views: int = Field(MIN_VIEWS, ge=0, validation_alias=AliasChoices('ENRICHMENT_MIN_VIEWS', 'MIN_VIEWS', 'enrichment_min_views'))
    enrichment_min_outlier_score: float = Field(MIN_OUTLIER, ge=0, allow_inf_nan=False,
                                                validation_alias=AliasChoices('ENRICHMENT_MIN_OUTLIER_SCORE', 'MIN_OUTLIER', 'MIN_OUTLIER_SCORE', 'enrichment_min_outlier_score'))
    incremental_median_multiplier: float = Field(INCREMENTAL_MEDIAN_MULTIPLIER, gt=0, allow_inf_nan=False,
                                                   validation_alias=AliasChoices('INCREMENTAL_MEDIAN_MULTIPLIER', 'incremental_median_multiplier'))
    enrichment_target_ratio: float = Field(ENRICHMENT_TARGET_RATIO, gt=0, le=1, allow_inf_nan=False,
                                            validation_alias=AliasChoices('ENRICHMENT_TARGET_RATIO', 'enrichment_target_ratio'))
    enrichment_min_new: int = Field(ENRICHMENT_MIN_NEW, ge=0,
                                    validation_alias=AliasChoices('ENRICHMENT_MIN_NEW', 'enrichment_min_new'))
    enrichment_max_new: int = Field(ENRICHMENT_MAX_NEW, ge=1,
                                    validation_alias=AliasChoices('ENRICHMENT_MAX_NEW', 'enrichment_max_new'))
    enrichment_lease_seconds: int = Field(1800, ge=60, le=86400)

    # Provider-neutral Semantic Viral DNA routing configuration.  No provider
    # SDK, credential, or automatic fallback is configured at this layer.
    viral_dna_semantic_provider: str | None = Field(
        None, validation_alias='VIRAL_DNA_SEMANTIC_PROVIDER'
    )
    viral_dna_semantic_model: str | None = Field(
        None, validation_alias='VIRAL_DNA_SEMANTIC_MODEL'
    )
    viral_dna_semantic_fallback_provider: str | None = Field(
        None, validation_alias='VIRAL_DNA_SEMANTIC_FALLBACK_PROVIDER'
    )
    viral_dna_semantic_fallback_model: str | None = Field(
        None, validation_alias='VIRAL_DNA_SEMANTIC_FALLBACK_MODEL'
    )
    # Credentials are intentionally provider-specific and optional.  The
    # selected adapter alone resolves its own key at construction time.
    openai_api_key_file: str | None = Field(
        None, validation_alias='OPENAI_API_KEY_FILE', repr=False
    )
    openai_api_key: SecretStr | None = Field(None, validation_alias='OPENAI_API_KEY', repr=False)
    gemini_api_key: SecretStr | None = Field(None, validation_alias='GEMINI_API_KEY', repr=False)
    gemini_api_keys: SecretStr | None = Field(None, validation_alias='GEMINI_API_KEYS', repr=False)
    moonshot_api_key: SecretStr | None = Field(None, validation_alias='MOONSHOT_API_KEY', repr=False)
    deepseek_api_key: SecretStr | None = Field(None, validation_alias='DEEPSEEK_API_KEY', repr=False)

    audio_queue_dir: str = '/data/audio-queue'
    audio_queue_max_bytes: int = Field(3 * 1024**3, gt=0)
    audio_queue_min_free_bytes: int = Field(5 * 1024**3, ge=0)
    audio_queue_max_jobs: int = Field(2000, gt=0)
    failed_audio_retention_hours: int = Field(24, ge=1)
    reconciliation_interval_seconds: int = Field(60, ge=10)
    transcription_max_attempts: int = Field(3, ge=1)
    rabbitmq_url_file: str | None = Field(None, repr=False)
    rabbitmq_url: SecretStr | None = Field(None, repr=False)
    audio_storage_backend: Literal['local', 'minio'] = 'local'
    minio_endpoint: str | None = 'http://minio:9000'
    minio_bucket: str | None = None
    minio_access_key_file: str | None = Field(None, repr=False)
    minio_access_key: SecretStr | None = Field(None, repr=False)
    minio_secret_key_file: str | None = Field(None, repr=False)
    minio_secret_key: SecretStr | None = Field(None, repr=False)
    audio_scratch_dir: str = '/tmp/kurukin-audio'

    @property
    def initial_enrichment_budget(self):
        return self.acquisition_batch_size

    @model_validator(mode='after')
    def enrichment_budget_limits(self):
        if self.enrichment_min_new > self.enrichment_max_new:
            raise ValueError('ENRICHMENT_MIN_NEW must not exceed ENRICHMENT_MAX_NEW')
        return self

    def resolve_rabbitmq_url(self):
        try:
            value = (Path(self.rabbitmq_url_file).read_text().strip() if self.rabbitmq_url_file
                     else self.rabbitmq_url.get_secret_value() if self.rabbitmq_url else '')
            url = urlsplit(value)
            if (url.scheme not in ('amqp', 'amqps') or not url.hostname or
                    url.username != 'kurukin_tiktok' or not url.password or
                    unquote(url.path) != '//kurukin-tiktok'):
                raise ValueError()
            return value
        except Exception:
            raise RuntimeError('RabbitMQ configuration missing or invalid') from None

    def _resolve_secret(self, file_name, secret, error_code):
        try:
            value = (Path(file_name).read_text().strip() if file_name else
                     secret.get_secret_value().strip() if secret else '')
            if not value:
                raise ValueError()
            return value
        except Exception:
            raise RuntimeError(error_code) from None

    def resolve_minio_credentials(self):
        return (self._resolve_secret(self.minio_access_key_file, self.minio_access_key,
                                     'MinIO access key missing'),
                self._resolve_secret(self.minio_secret_key_file, self.minio_secret_key,
                                     'MinIO secret key missing'))

    @model_validator(mode='after')
    def duration_limits(self):
        if not self.min_transcribe_duration_seconds <= self.auto_transcribe_max_duration_seconds <= self.hard_transcribe_max_duration_seconds:
            raise ValueError('Duration limits must satisfy min <= auto <= hard')
        return self

    @model_validator(mode='after')
    def audio_storage_configuration(self):
        if self.audio_storage_backend != 'minio':
            return self
        try:
            endpoint = urlsplit(self.minio_endpoint or '')
            bucket = self.minio_bucket or ''
            if (endpoint.scheme not in ('http', 'https') or not endpoint.hostname or
                    endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or
                    not 3 <= len(bucket) <= 63 or bucket[0] == '-' or bucket[-1] == '-' or
                    any(not (c.islower() or c.isdigit() or c == '-') for c in bucket)):
                raise ValueError()
        except Exception:
            raise ValueError('MinIO configuration missing or invalid') from None
        return self

    @model_validator(mode='after')
    def semantic_routing_pairs(self):
        if bool(self.viral_dna_semantic_provider) != bool(self.viral_dna_semantic_model):
            raise ValueError('Semantic Viral DNA primary provider and model must be configured together')
        if bool(self.viral_dna_semantic_fallback_provider) != bool(self.viral_dna_semantic_fallback_model):
            raise ValueError('Semantic Viral DNA fallback provider and model must be configured together')
        if self.viral_dna_semantic_fallback_provider and not self.viral_dna_semantic_provider:
            raise ValueError('Semantic Viral DNA fallback requires a primary provider')
        return self

    @property
    def semantic_viral_dna_routing_policy(self):
        """Return declared primary/fallback policy; this performs no routing."""
        if self.viral_dna_semantic_provider is None:
            return None
        from .llm.semantic import SemanticProviderConfig, SemanticRoutingPolicy
        primary = SemanticProviderConfig(
            provider_name=self.viral_dna_semantic_provider,
            model=self.viral_dna_semantic_model,
        )
        fallback = None
        if self.viral_dna_semantic_fallback_provider is not None:
            fallback = SemanticProviderConfig(
                provider_name=self.viral_dna_semantic_fallback_provider,
                model=self.viral_dna_semantic_fallback_model,
            )
        return SemanticRoutingPolicy(primary=primary, fallback=fallback)

    def semantic_provider_api_key(self, provider_name: str) -> str:
        """Resolve only the selected provider credential without exposing it."""
        if provider_name == 'openai' and self.openai_api_key_file is not None:
            try:
                value = Path(self.openai_api_key_file).read_text().strip()
            except OSError:
                value = ''
            if value:
                return value
            raise RuntimeError('semantic_provider_api_key_missing')
        key = {
            'openai': self.openai_api_key,
            'google': self.gemini_api_key,
            'moonshot': self.moonshot_api_key,
            'deepseek': self.deepseek_api_key,
        }.get(provider_name)
        if key is None or not key.get_secret_value().strip():
            raise RuntimeError('semantic_provider_api_key_missing')
        return key.get_secret_value()

    def semantic_provider_api_keys(self, provider_name: str) -> tuple[str, ...]:
        """Resolve provider credentials without recording their values.

        Gemini alone currently supports a development credential pool. A
        non-empty comma-separated ``GEMINI_API_KEYS`` takes precedence over
        the legacy single-key setting; empty entries are ignored so an empty
        pool setting remains backward compatible with ``GEMINI_API_KEY``.
        """
        if provider_name == 'google' and self.gemini_api_keys is not None:
            keys = tuple(
                value.strip() for value in self.gemini_api_keys.get_secret_value().split(',')
                if value.strip()
            )
            if keys:
                return keys
        return (self.semantic_provider_api_key(provider_name),)

    max_audio_mb: int = Field(10, ge=1, le=100)
    whisper_model: Literal['base', 'small', 'medium'] = 'small'
    whisper_device: Literal['cpu'] = 'cpu'
    whisper_compute_type: Literal['int8'] = 'int8'
    whisper_concurrency: Literal[1] = 1
    @field_validator('whisper_concurrency', mode='before')
    @classmethod
    def strict_single_concurrency(cls, value):
        if type(value) is str and value == '1':
            return 1
        if type(value) is int and value == 1:
            return value
        raise ValueError('Whisper concurrency must be exactly 1')

    whisper_timeout_seconds: int = Field(300, ge=1, le=3600)
    whisper_cpu_threads: int = Field(2, ge=1, le=16)
    whisper_cache_dir: str = '/models/huggingface'
    audio_gate_mode: Literal['observe', 'enforce'] = 'observe'

    def resolve_database_url(self):
        try:
            if self.database_url_file is not None:
                value = Path(self.database_url_file).read_text().strip()
            else:
                value = self.database_url.get_secret_value() if self.database_url else ''
            url = make_url(value)
            if (url.drivername != 'postgresql+psycopg' or not url.host or
                    url.database != 'kurukin_tiktok' or url.username != 'kurukin_tiktok' or
                    not url.password or any(c.isspace() for c in value)):
                raise ValueError()
            return url
        except Exception:
            raise DatabaseConfigurationError('Database configuration missing or invalid') from None

@lru_cache
def get_settings():
    return Settings()
