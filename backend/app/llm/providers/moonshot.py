"""Moonshot/Kimi's documented OpenAI-compatible Chat Completions JSON mode."""
from __future__ import annotations

from ._common import RestSemanticProvider, SemanticProviderError, normalize_usage, parse_json_object, semantic_messages
from ..semantic import SemanticProviderCapabilities, SemanticProviderConfig


class MoonshotSemanticProvider(RestSemanticProvider):
    # Moonshot documents JSON mode; it does not claim strict JSON Schema here.
    capabilities = SemanticProviderCapabilities(native_json_schema=False, json_mode=True)
    endpoint = 'https://api.moonshot.ai/v1/chat/completions'

    def extract(self, payload: dict[str, str]) -> dict[str, object]:
        self._begin_invocation()
        request = {'model': self.config.model, 'messages': semantic_messages(payload, include_schema=True),
                   'response_format': {'type': 'json_object'}}
        try:
            response = self._post(self.endpoint, headers={'Authorization': f'Bearer {self._api_key}'}, payload=request)
            self._set_usage(normalize_usage(response.get('usage'), input_key='prompt_tokens', output_key='completion_tokens',
                                             total_key='total_tokens'))
            choices = response.get('choices')
            if not isinstance(choices, list) or not choices:
                raise SemanticProviderError('empty_output', attempts=self.last_metadata.attempts)
            choice = choices[0] if isinstance(choices[0], dict) else {}
            if choice.get('finish_reason') == 'content_filter':
                raise SemanticProviderError('refusal', attempts=self.last_metadata.attempts)
            return parse_json_object(choice.get('message', {}).get('content') if isinstance(choice.get('message'), dict) else None)
        except SemanticProviderError as error:
            self._safe_log_error(error)
            raise
