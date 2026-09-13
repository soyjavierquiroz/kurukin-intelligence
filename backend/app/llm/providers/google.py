"""Gemini REST generateContent adapter with a deterministic schema projection."""
from __future__ import annotations

from urllib.parse import quote

from ._common import RestSemanticProvider, SemanticProviderError, gemini_schema, normalize_usage, parse_json_object, semantic_messages
from ..semantic import SemanticProviderCapabilities, SemanticProviderConfig


class GoogleSemanticProvider(RestSemanticProvider):
    capabilities = SemanticProviderCapabilities(native_json_schema=True, json_mode=True)
    endpoint_prefix = 'https://generativelanguage.googleapis.com/v1beta/models/'

    def __init__(self, config: SemanticProviderConfig, **kwargs) -> None:
        super().__init__(config, **kwargs)
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
        try:
            response = self._post(endpoint, headers={'x-goog-api-key': self._api_key}, payload=request)
            self._set_usage(normalize_usage(response.get('usageMetadata'), input_key='promptTokenCount',
                                             output_key='candidatesTokenCount', total_key='totalTokenCount',
                                             cached_input_key='cachedContentTokenCount', reasoning_key='thoughtsTokenCount'))
            candidates = response.get('candidates')
            if not isinstance(candidates, list) or not candidates:
                if isinstance(response.get('promptFeedback'), Mapping):
                    raise SemanticProviderError('refusal', attempts=self.last_metadata.attempts)
                raise SemanticProviderError('empty_output', attempts=self.last_metadata.attempts)
            parts = candidates[0].get('content', {}).get('parts', []) if isinstance(candidates[0], dict) else []
            text = next((part.get('text') for part in parts if isinstance(part, dict) and isinstance(part.get('text'), str)), None)
            return parse_json_object(text)
        except SemanticProviderError as error:
            self._safe_log_error(error)
            raise
