"""Gemini REST generateContent adapter with a deterministic schema projection."""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import quote

from ._common import RestSemanticProvider, SemanticProviderError, gemini_schema, normalize_usage, parse_json_object, semantic_messages
from ..semantic import SemanticProviderCapabilities, SemanticProviderConfig, SemanticProviderExecutionMetadata


DEFAULT_CREDENTIAL_COOLDOWN_SECONDS = 60.0


@dataclass
class _CredentialState:
    """Non-secret, process-local status for one Gemini credential slot."""

    cooldown_until: float | None = None
    disabled_auth: bool = False


class GoogleSemanticProvider(RestSemanticProvider):
    capabilities = SemanticProviderCapabilities(native_json_schema=True, json_mode=True)
    endpoint_prefix = 'https://generativelanguage.googleapis.com/v1beta/models/'

    def __init__(self, config: SemanticProviderConfig, *, api_key: str | None = None,
                 api_keys: Sequence[str] | None = None, cooldown_seconds: float = DEFAULT_CREDENTIAL_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic, **kwargs) -> None:
        credentials = tuple(key.strip() for key in (api_keys or (() if api_key is None else (api_key,))) if key.strip())
        if not credentials:
            raise RuntimeError('semantic_provider_api_key_missing')
        if cooldown_seconds < 0:
            raise ValueError('invalid_credential_cooldown')
        # RestSemanticProvider retains the current key only to construct this
        # request's header. The tuple and all state stay in memory only.
        super().__init__(config, api_key=credentials[0], **kwargs)
        self._api_keys = credentials
        self._credential_states = [_CredentialState() for _ in credentials]
        self._next_credential_index = 0
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        # Google's structured-output documentation lists these model families.
        native = config.model.lower().startswith(('gemini-2.0', 'gemini-2.5', 'gemini-3'))
        self.capabilities = SemanticProviderCapabilities(native_json_schema=native, json_mode=True)

    def extract(self, payload: dict[str, str]) -> dict[str, object]:
        self._begin_invocation()
        messages = semantic_messages(payload, include_schema=not self.capabilities.native_json_schema)
        request = {
            'systemInstruction': {'parts': [{'text': messages[0]['content']}]},
            'contents': [{'role': 'user', 'parts': [{'text': messages[1]['content']}]}],
            'generationConfig': {'responseMimeType': 'application/json'},
        }
        if self.capabilities.native_json_schema:
            request['generationConfig']['responseJsonSchema'] = gemini_schema()
        encoded_model = quote(self.config.model, safe='.-_')
        endpoint = f'{self.endpoint_prefix}{encoded_model}:generateContent'
        attempted: set[int] = set()
        total_attempts = 0
        total_latency_ms = 0
        last_error: SemanticProviderError | None = None

        while (credential_index := self._next_available_credential(attempted)) is not None:
            attempted.add(credential_index)
            self._api_key = self._api_keys[credential_index]
            transport_counted = False
            try:
                response = self._post(endpoint, headers={'x-goog-api-key': self._api_key}, payload=request)
                transport_metadata = self.last_metadata
                total_attempts += transport_metadata.attempts
                total_latency_ms += transport_metadata.latency_ms or 0
                transport_counted = True
                self._set_usage(normalize_usage(response.get('usageMetadata'), input_key='promptTokenCount',
                                                 output_key='candidatesTokenCount', total_key='totalTokenCount',
                                                 cached_input_key='cachedContentTokenCount', reasoning_key='thoughtsTokenCount'))
                candidates = response.get('candidates')
                if not isinstance(candidates, list) or not candidates:
                    if isinstance(response.get('promptFeedback'), Mapping):
                        raise SemanticProviderError('refusal', attempts=transport_metadata.attempts)
                    raise SemanticProviderError('empty_output', attempts=transport_metadata.attempts)
                parts = candidates[0].get('content', {}).get('parts', []) if isinstance(candidates[0], dict) else []
                text = next((part.get('text') for part in parts if isinstance(part, dict) and isinstance(part.get('text'), str)), None)
                result = parse_json_object(text)
                self._set_credential_metadata(credential_index, total_attempts, total_latency_ms)
                return result
            except SemanticProviderError as error:
                metadata = self.last_metadata
                # Parsing/refusal errors happen after successful transport;
                # count that transport once without double-counting it.
                if not transport_counted:
                    total_attempts += metadata.attempts
                    total_latency_ms += metadata.latency_ms or 0
                self._set_credential_metadata(credential_index, total_attempts, total_latency_ms)
                error.attempts = total_attempts
                last_error = error
                if error.code == 'authentication_error':
                    self._credential_states[credential_index].disabled_auth = True
                    continue
                if error.code == 'rate_limited':
                    self._credential_states[credential_index].cooldown_until = self._clock() + self._cooldown_seconds
                    continue
                if error.code == 'provider_unavailable' and error.status_code == 503:
                    continue
                self._safe_log_error(error)
                raise

        if last_error is None:
            # Every credential was already cooling down (or had an auth
            # failure) before this invocation; no network request was made.
            last_error = SemanticProviderError('provider_unavailable', attempts=0)
            self.last_metadata = SemanticProviderExecutionMetadata(attempts=0)
        self._safe_log_error(last_error)
        raise last_error

    def _next_available_credential(self, attempted: set[int]) -> int | None:
        now = self._clock()
        for offset in range(len(self._api_keys)):
            index = (self._next_credential_index + offset) % len(self._api_keys)
            state = self._credential_states[index]
            if index in attempted or state.disabled_auth:
                continue
            if state.cooldown_until is not None and state.cooldown_until > now:
                continue
            if state.cooldown_until is not None:
                state.cooldown_until = None
            self._next_credential_index = (index + 1) % len(self._api_keys)
            return index
        return None

    def _set_credential_metadata(self, credential_index: int, attempts: int, latency_ms: int) -> None:
        self.last_metadata = SemanticProviderExecutionMetadata(
            usage=dict(self.last_metadata.usage), latency_ms=latency_ms, attempts=attempts,
            credential_index=credential_index,
        )
