"""OpenAI Responses API adapter; no SDK or tools are used."""
from __future__ import annotations

from typing import Mapping

from ._common import RestSemanticProvider, SemanticProviderError, normalize_usage, parse_json_object, semantic_messages
from ..semantic import SemanticProviderCapabilities, SemanticProviderConfig
from ..semantic_contract import SEMANTIC_VIRAL_DNA_JSON_SCHEMA


class OpenAISemanticProvider(RestSemanticProvider):
    capabilities = SemanticProviderCapabilities(native_json_schema=True, json_mode=True)
    endpoint = 'https://api.openai.com/v1/responses'

    def __init__(self, config: SemanticProviderConfig, **kwargs) -> None:
        super().__init__(config, **kwargs)
        # Structured Outputs are documented for the modern model families.
        # Unknown configured IDs conservatively use JSON mode rather than
        # falsely advertising strict native-schema support.
        native = config.model.lower().startswith(('gpt-4o', 'gpt-4.1', 'gpt-5', 'o1', 'o3', 'o4'))
        self.capabilities = SemanticProviderCapabilities(native_json_schema=native, json_mode=True)

    def extract(self, payload: dict[str, str]) -> dict[str, object]:
        request = {
            'model': self.config.model,
            'input': [
                {'role': message['role'], 'content': [{'type': 'input_text', 'text': message['content']}]}
                for message in semantic_messages(payload, include_schema=not self.capabilities.native_json_schema)
            ],
        }
        if self.capabilities.native_json_schema:
            request['text'] = {'format': {'type': 'json_schema', 'name': 'viral_dna_semantic_v1',
                                          'strict': True, 'schema': SEMANTIC_VIRAL_DNA_JSON_SCHEMA}}
        else:
            request['text'] = {'format': {'type': 'json_object'}}
        try:
            response = self._post(self.endpoint, headers={'Authorization': f'Bearer {self._api_key}'}, payload=request)
            usage = normalize_usage(response.get('usage'), input_key='input_tokens', output_key='output_tokens',
                                    total_key='total_tokens')
            raw_usage = response.get('usage')
            if isinstance(raw_usage, Mapping):
                input_details = raw_usage.get('input_tokens_details')
                output_details = raw_usage.get('output_tokens_details')
                cached = input_details.get('cached_tokens') if isinstance(input_details, Mapping) else None
                reasoning = output_details.get('reasoning_tokens') if isinstance(output_details, Mapping) else None
                if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
                    usage['cached_input_tokens'] = cached
                if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
                    usage['reasoning_tokens'] = reasoning
            self._set_usage(usage)
            text = response.get('output_text')
            if not isinstance(text, str):
                for item in response.get('output', []):
                    if isinstance(item, Mapping):
                        for content in item.get('content', []):
                            if isinstance(content, Mapping) and content.get('type') == 'refusal':
                                raise SemanticProviderError('refusal', attempts=self.last_metadata.attempts)
                            if isinstance(content, Mapping) and content.get('type') == 'output_text':
                                text = content.get('text')
                                break
            return parse_json_object(text)
        except SemanticProviderError as error:
            self._safe_log_error(error)
            raise
