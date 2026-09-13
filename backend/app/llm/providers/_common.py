"""Small, content-safe REST primitives shared by semantic providers."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import time
from typing import Any, Mapping

import httpx

from ..semantic import SemanticProviderCapabilities, SemanticProviderConfig, SemanticProviderExecutionMetadata
from ..semantic_contract import SEMANTIC_VIRAL_DNA_JSON_SCHEMA
from ...viral_dna import SEMANTIC_PROMPT


LOGGER = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 45.0


class SemanticProviderError(RuntimeError):
    """Provider-neutral, deliberately content-free error for callers."""

    def __init__(self, code: str, *, exception_class: str | None = None, status_code: int | None = None,
                 attempts: int = 1) -> None:
        super().__init__(code)
        self.code = code
        self.exception_class = exception_class
        self.status_code = status_code
        self.attempts = attempts


def semantic_messages(payload: Mapping[str, str], *, include_schema: bool) -> list[dict[str, str]]:
    """Build the one shared semantic intent; payload has only the three inputs."""
    if set(payload) != {'language', 'caption', 'transcript'}:
        raise ValueError('invalid_semantic_payload')
    system = SEMANTIC_PROMPT
    if include_schema:
        system += (' Return JSON only. The following Kurukin-owned closed JSON Schema is authoritative: '
                   + json.dumps(SEMANTIC_VIRAL_DNA_JSON_SCHEMA, ensure_ascii=False, separators=(',', ':')))
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': json.dumps(dict(payload), ensure_ascii=False, separators=(',', ':'))},
    ]


def parse_json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str) or not value.strip():
        raise SemanticProviderError('empty_output')
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise SemanticProviderError('invalid_json', exception_class=type(error).__name__) from None
    if not isinstance(parsed, dict):
        raise SemanticProviderError('invalid_json')
    return parsed


def normalize_usage(raw: object, *, input_key: str, output_key: str, total_key: str,
                    cached_input_key: str | None = None, reasoning_key: str | None = None) -> dict[str, int]:
    """Keep only actual non-negative integer counters exposed by an API."""
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, int] = {}
    for normalized, source in (
        ('input_tokens', input_key), ('output_tokens', output_key), ('total_tokens', total_key),
        ('cached_input_tokens', cached_input_key), ('reasoning_tokens', reasoning_key),
    ):
        value = raw.get(source) if source else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[normalized] = value
    return result


def gemini_schema() -> dict[str, object]:
    """Return the documented Gemini JSON-Schema subset without a second contract.

    Gemini documents no ``maxLength`` support.  Removing that annotation is a
    deterministic compatibility projection; Kurukin still centrally enforces
    every max length after parsing.
    """
    def project(value: object) -> object:
        if isinstance(value, list):
            return [project(item) for item in value]
        if isinstance(value, dict):
            return {key: project(item) for key, item in value.items() if key != 'maxLength'}
        return value
    return project(deepcopy(SEMANTIC_VIRAL_DNA_JSON_SCHEMA))  # type: ignore[return-value]


def _error_for_status(status_code: int, attempts: int) -> SemanticProviderError:
    if status_code in (401, 403):
        return SemanticProviderError('authentication_error', status_code=status_code, attempts=attempts)
    if status_code == 429:
        return SemanticProviderError('rate_limited', status_code=status_code, attempts=attempts)
    if 500 <= status_code <= 599:
        return SemanticProviderError('provider_unavailable', status_code=status_code, attempts=attempts)
    return SemanticProviderError('provider_http_error', status_code=status_code, attempts=attempts)


class RestSemanticProvider:
    """Minimal synchronous transport with one explicit transient retry."""

    capabilities = SemanticProviderCapabilities()

    def __init__(self, config: SemanticProviderConfig, *, api_key: str, client: httpx.Client | None = None,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        if not api_key:
            raise RuntimeError('semantic_provider_api_key_missing')
        self.config = config
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_seconds))
        self._owns_client = client is None
        self.last_metadata = SemanticProviderExecutionMetadata()

    def _post(self, url: str, *, headers: Mapping[str, str], payload: Mapping[str, object]) -> dict[str, object]:
        started = time.monotonic()
        attempts = 0
        try:
            while True:
                attempts += 1
                try:
                    response = self._client.post(url, headers=dict(headers), json=dict(payload))
                except httpx.TimeoutException as error:
                    if attempts < 2:
                        time.sleep(0.15)
                        continue
                    raise SemanticProviderError('timeout', exception_class=type(error).__name__, attempts=attempts) from None
                except httpx.HTTPError as error:
                    if attempts < 2:
                        time.sleep(0.15)
                        continue
                    raise SemanticProviderError('provider_unavailable', exception_class=type(error).__name__, attempts=attempts) from None
                if response.status_code == 429 or response.status_code >= 500:
                    if attempts < 2:
                        time.sleep(0.15)
                        continue
                if response.status_code >= 400:
                    raise _error_for_status(response.status_code, attempts)
                try:
                    decoded = response.json()
                except (ValueError, json.JSONDecodeError) as error:
                    raise SemanticProviderError('invalid_json', exception_class=type(error).__name__, attempts=attempts) from None
                if not isinstance(decoded, dict):
                    raise SemanticProviderError('invalid_json', attempts=attempts)
                return decoded
        finally:
            latency_ms = round((time.monotonic() - started) * 1000)
            self.last_metadata = SemanticProviderExecutionMetadata(
                usage=self.last_metadata.usage, latency_ms=latency_ms, attempts=attempts or 1
            )

    def _set_usage(self, usage: Mapping[str, int]) -> None:
        self.last_metadata = SemanticProviderExecutionMetadata(
            usage=dict(usage), latency_ms=self.last_metadata.latency_ms, attempts=self.last_metadata.attempts
        )

    def _safe_log_error(self, error: SemanticProviderError) -> None:
        LOGGER.warning('semantic_provider_failed provider=%s model=%s code=%s status=%s exception=%s attempts=%s',
                       self.config.provider_name, self.config.model, error.code, error.status_code,
                       error.exception_class, error.attempts)
