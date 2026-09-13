from functools import lru_cache
from pathlib import Path
from typing import Literal
from pydantic import AliasChoices, Field, SecretStr, model_validator, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

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
    openai_api_key: SecretStr | None = Field(None, validation_alias='OPENAI_API_KEY', repr=False)
    gemini_api_key: SecretStr | None = Field(None, validation_alias='GEMINI_API_KEY', repr=False)
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

    @property
    def initial_enrichment_budget(self):
        return self.acquisition_batch_size

    def resolve_rabbitmq_url(self):
        from urllib.parse import urlsplit, unquote
        try:
            value = (Path(self.rabbitmq_url_file).read_text().strip() if self.rabbitmq_url_file
                     else self.rabbitmq_url.get_secret_value() if self.rabbitmq_url else '')
            url = urlsplit(value)
            if (url.scheme != 'amqp' or url.hostname != 'rabbit_mq' or url.port != 5672
                    or url.username != 'kurukin_tiktok' or not url.password
                    or unquote(url.path) != '//kurukin-tiktok' or url.query or url.fragment):
                raise ValueError()
            return value
        except Exception:
            raise RuntimeError('RabbitMQ configuration missing or invalid') from None

    @model_validator(mode='after')
    def duration_limits(self):
        if not self.min_transcribe_duration_seconds <= self.auto_transcribe_max_duration_seconds <= self.hard_transcribe_max_duration_seconds:
            raise ValueError('Duration limits must satisfy min <= auto <= hard')
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
        key = {
            'openai': self.openai_api_key,
            'google': self.gemini_api_key,
            'moonshot': self.moonshot_api_key,
            'deepseek': self.deepseek_api_key,
        }.get(provider_name)
        if key is None or not key.get_secret_value().strip():
            raise RuntimeError('semantic_provider_api_key_missing')
        return key.get_secret_value()

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
            if (url.drivername != 'postgresql+psycopg' or url.host != 'postgres'
                    or url.port != 5432 or url.database != 'kurukin_tiktok'
                    or url.username != 'kurukin_tiktok' or not url.password
                    or url.query or any(c.isspace() for c in value)):
                raise ValueError()
            return url
        except Exception:
            raise DatabaseConfigurationError('Database configuration missing or invalid') from None

@lru_cache
def get_settings():
    return Settings()
