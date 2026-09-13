"""Concrete REST adapters and the small default registry bootstrap."""
from __future__ import annotations

from ..semantic import SemanticProviderConfig, register_semantic_provider
from ...config import get_settings
from .deepseek import DeepSeekSemanticProvider
from .google import GoogleSemanticProvider
from .moonshot import MoonshotSemanticProvider
from .openai import OpenAISemanticProvider


def _factory(adapter_type):
    def create(config: SemanticProviderConfig):
        if adapter_type is GoogleSemanticProvider:
            return adapter_type(
                config, api_keys=get_settings().semantic_provider_api_keys(config.provider_name),
            )
        return adapter_type(config, api_key=get_settings().semantic_provider_api_key(config.provider_name))
    return create


def register_builtin_semantic_providers() -> None:
    """Register canonical IDs. Calling repeatedly is harmless for bootstrap/tests."""
    for name, adapter_type in {
        'openai': OpenAISemanticProvider,
        'google': GoogleSemanticProvider,
        'moonshot': MoonshotSemanticProvider,
        'deepseek': DeepSeekSemanticProvider,
    }.items():
        register_semantic_provider(name, _factory(adapter_type), replace=True)


__all__ = [
    'DeepSeekSemanticProvider', 'GoogleSemanticProvider', 'MoonshotSemanticProvider',
    'OpenAISemanticProvider', 'register_builtin_semantic_providers',
]
