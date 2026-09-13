"""Stable, provider-neutral boundary for Semantic Viral DNA.

This module intentionally contains neither an SDK import nor a network client.
Provider adapters belong under :mod:`app.llm.providers` and implement the
small ``SemanticProvider`` protocol below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Callable, Mapping, Protocol


_PROVIDER_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}$')


class SemanticResponseMode(StrEnum):
    """Transport strategies an adapter may select without weakening the contract."""

    NATIVE_JSON_SCHEMA = 'native_json_schema'
    JSON_MODE = 'json_mode'
    PROMPT_VALIDATED_JSON = 'prompt_validated_json'


@dataclass(frozen=True)
class SemanticProviderCapabilities:
    """Features advertised by a provider adapter, not guarantees of validity."""

    native_json_schema: bool = False
    json_mode: bool = False

    @property
    def response_mode(self) -> SemanticResponseMode:
        if self.native_json_schema:
            return SemanticResponseMode.NATIVE_JSON_SCHEMA
        if self.json_mode:
            return SemanticResponseMode.JSON_MODE
        return SemanticResponseMode.PROMPT_VALIDATED_JSON


@dataclass(frozen=True)
class SemanticProviderConfig:
    """Non-secret configuration passed to one provider adapter.

    Adapter-specific options are deliberately isolated here.  The core only
    uses the provider name and model to establish reproducible provenance.
    """

    provider_name: str
    model: str
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _PROVIDER_NAME.fullmatch(self.provider_name):
            raise ValueError('invalid_semantic_provider')
        if not self.model or len(self.model) > 128 or self.model != self.model.strip():
            raise ValueError('invalid_semantic_model')

    @property
    def identifier(self) -> str:
        return semantic_provider_model_identifier(self.provider_name, self.model)


@dataclass(frozen=True)
class SemanticRoutingPolicy:
    """Declared routing policy.  Selection/fallback execution is intentionally absent."""

    primary: SemanticProviderConfig
    fallback: SemanticProviderConfig | None = None


class SemanticProvider(Protocol):
    """The only LLM interface consumed by Semantic Viral DNA core."""

    capabilities: SemanticProviderCapabilities

    def extract(self, payload: dict[str, str]) -> dict[str, object]:
        """Return the Kurukin-owned Semantic Viral DNA JSON contract."""


SemanticProviderFactory = Callable[[SemanticProviderConfig], SemanticProvider]
_SEMANTIC_PROVIDER_FACTORIES: dict[str, SemanticProviderFactory] = {}


def semantic_provider_model_identifier(provider_name: str, model: str) -> str:
    """Return the stable provider-qualified model identifier used for provenance."""
    config = SemanticProviderConfig(provider_name=provider_name, model=model)
    return f'{config.provider_name}:{config.model}'


def register_semantic_provider(
    provider_name: str,
    factory: SemanticProviderFactory,
    *,
    replace: bool = False,
) -> None:
    """Register an isolated adapter factory (normally at application bootstrap)."""
    # Validate the name through the same policy as runtime configuration.
    SemanticProviderConfig(provider_name=provider_name, model='validation-only')
    if provider_name in _SEMANTIC_PROVIDER_FACTORIES and not replace:
        raise ValueError('semantic_provider_already_registered')
    _SEMANTIC_PROVIDER_FACTORIES[provider_name] = factory


def get_semantic_provider(
    provider_name: str,
    config: SemanticProviderConfig,
) -> SemanticProvider:
    """Build a registered adapter without exposing provider details to the core."""
    if provider_name != config.provider_name:
        raise ValueError('semantic_provider_config_mismatch')
    try:
        factory = _SEMANTIC_PROVIDER_FACTORIES[provider_name]
    except KeyError:
        raise ValueError('semantic_provider_not_configured') from None
    return factory(config)
