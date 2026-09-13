"""All real provider adapters are exercised against an in-process HTTP boundary."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.llm.semantic import SemanticProviderConfig, get_semantic_provider
from app.llm.providers import register_builtin_semantic_providers
from app.llm.providers._common import SemanticProviderError, gemini_schema
from app.llm.providers.deepseek import DeepSeekSemanticProvider
from app.llm.providers.google import GoogleSemanticProvider
from app.llm.providers.moonshot import MoonshotSemanticProvider
from app.llm.providers.openai import OpenAISemanticProvider
from app.llm.semantic_contract import SEMANTIC_OUTPUT_FIELDS, SEMANTIC_VIRAL_DNA_JSON_SCHEMA
from app.viral_dna import validate_semantic_output


PAYLOAD = {'language': 'es', 'caption': 'CAPTION-PRIVATE-TEST', 'transcript': 'TRANSCRIPT-PRIVATE-TEST'}
FIXTURES = Path(__file__).parent / 'fixtures'
ADAPTERS = (
    ('openai', OpenAISemanticProvider, 'openai_semantic_response.json', 'OPENAI_API_KEY'),
    ('google', GoogleSemanticProvider, 'google_semantic_response.json', 'GEMINI_API_KEY'),
    ('moonshot', MoonshotSemanticProvider, 'moonshot_semantic_response.json', 'MOONSHOT_API_KEY'),
    ('deepseek', DeepSeekSemanticProvider, 'deepseek_semantic_response.json', 'DEEPSEEK_API_KEY'),
)


def minimum_output():
    result = {field: None for field in SEMANTIC_OUTPUT_FIELDS if field in SEMANTIC_VIRAL_DNA_JSON_SCHEMA['properties']
              and field in ('hook_text', 'topic', 'subtopic', 'angle_summary', 'pain', 'desire', 'fear',
                            'audience_identity', 'belief', 'objection', 'promise', 'reframe', 'emotional_arc', 'cta_text')}
    result.update({
        'hook_type': 'none_unclear', 'hook_mechanism': 'none_unclear', 'hook_target': 'none_unclear',
        'audience_specificity': 'unclear', 'angle_type': 'other_unclear', 'emotion_primary': 'neutral_unclear',
        'emotion_secondary': None, 'content_function_primary': 'other_unclear', 'content_function_secondary': None,
        'content_role_primary': 'unclear', 'content_role_secondary': None, 'content_format': 'other_unclear',
        'narrative_structure': 'other_unclear', 'awareness_stage': 'mixed_unclear', 'proof_type': 'none',
        'authority_mechanism': 'none_unclear', 'creator_positioning_signal': 'none_unclear', 'cta_type': 'none',
        'cta_secondary_type': None, 'commercial_intent': 'none', 'offer_integration': 'none', 'offer_type': 'none',
        'monetization_model': 'none',
    })
    return result


def fixture_response(filename: str, semantic: dict[str, object] | None = None) -> dict[str, object]:
    response = json.loads((FIXTURES / filename).read_text())
    text = json.dumps(minimum_output() if semantic is None else semantic)
    if 'output_text' in response:
        response['output_text'] = text
    elif 'candidates' in response:
        response['candidates'][0]['content']['parts'][0]['text'] = text
    else:
        response['choices'][0]['message']['content'] = text
    return response


def client_with(response_data, captured):
    def handler(request):
        captured.append(request)
        if isinstance(response_data, Exception):
            raise response_data
        status, body = response_data if isinstance(response_data, tuple) else (200, response_data)
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('name,adapter,filename,_', ADAPTERS)
def test_provider_request_success_usage_and_safe_logs(name, adapter, filename, _, caplog):
    captured = []
    provider = adapter(SemanticProviderConfig(provider_name=name, model='model-x'), api_key='key-do-not-leak',
                       client=client_with(fixture_response(filename), captured))
    assert validate_semantic_output(provider.extract(PAYLOAD)) == minimum_output()
    request = captured[0]
    body = json.loads(request.content)
    assert request.url.host
    if name == 'google':
        assert request.url.path.endswith('/models/model-x:generateContent')
    else:
        assert body['model'] == 'model-x'
    encoded = json.dumps(body)
    assert 'CAPTION-PRIVATE-TEST' in encoded and 'TRANSCRIPT-PRIVATE-TEST' in encoded
    assert not any(key in encoded for key in ('"views"', '"likes"', '"outlier_score"', '"analysis"'))
    assert provider.last_metadata.usage['input_tokens'] == 11
    assert provider.last_metadata.usage['output_tokens'] == 7
    assert provider.last_metadata.usage['total_tokens'] == 18
    if name in ('openai', 'google', 'deepseek'):
        assert provider.last_metadata.usage['cached_input_tokens'] == 2
        assert provider.last_metadata.usage['reasoning_tokens'] == 1
    assert 'key-do-not-leak' not in caplog.text
    assert 'CAPTION-PRIVATE-TEST' not in caplog.text and 'TRANSCRIPT-PRIVATE-TEST' not in caplog.text


@pytest.mark.parametrize('name,adapter,filename,_', ADAPTERS)
def test_each_provider_normalizes_documented_failures(name, adapter, filename, _):
    config = SemanticProviderConfig(provider_name=name, model='model-x')
    for status, code in ((401, 'authentication_error'), (429, 'rate_limited'), (503, 'provider_unavailable')):
        provider = adapter(config, api_key='safe-key', client=client_with((status, {}), []))
        with pytest.raises(SemanticProviderError, match=code) as error:
            provider.extract(PAYLOAD)
        assert error.value.code == code
        assert error.value.attempts == (2 if status in (429, 503) else 1)
    timeout = adapter(config, api_key='safe-key', client=client_with(httpx.ReadTimeout('timed out'), []))
    with pytest.raises(SemanticProviderError, match='timeout'):
        timeout.extract(PAYLOAD)


@pytest.mark.parametrize('name,adapter,filename,_', ADAPTERS)
def test_each_provider_rejects_malformed_empty_and_invalid_contract(name, adapter, filename, _):
    config = SemanticProviderConfig(provider_name=name, model='model-x')
    malformed = fixture_response(filename)
    if 'output_text' in malformed:
        malformed['output_text'] = '{'
    elif 'candidates' in malformed:
        malformed['candidates'][0]['content']['parts'][0]['text'] = '{'
    else:
        malformed['choices'][0]['message']['content'] = '{'
    with pytest.raises(SemanticProviderError, match='invalid_json'):
        adapter(config, api_key='safe-key', client=client_with(malformed, [])).extract(PAYLOAD)
    empty = fixture_response(filename)
    if 'output_text' in empty:
        empty['output_text'] = ''
    elif 'candidates' in empty:
        empty['candidates'][0]['content']['parts'][0]['text'] = ''
    else:
        empty['choices'][0]['message']['content'] = ''
    with pytest.raises(SemanticProviderError, match='empty_output'):
        adapter(config, api_key='safe-key', client=client_with(empty, [])).extract(PAYLOAD)
    invalid = adapter(config, api_key='safe-key', client=client_with(fixture_response(filename, {'bad': 'contract'}), []))
    with pytest.raises(ValueError):
        validate_semantic_output(invalid.extract(PAYLOAD))


def test_gemini_schema_is_a_deterministic_subset_of_kurukin_contract():
    projected = gemini_schema()
    assert projected['required'] == SEMANTIC_VIRAL_DNA_JSON_SCHEMA['required']
    assert projected['additionalProperties'] is False
    assert all('maxLength' not in property_schema for property_schema in projected['properties'].values())
    assert SEMANTIC_VIRAL_DNA_JSON_SCHEMA['properties']['hook_text']['maxLength'] == 280


def test_native_schema_capability_is_model_aware_and_schema_remains_kurukin_owned():
    captured = []
    openai = OpenAISemanticProvider(SemanticProviderConfig(provider_name='openai', model='gpt-5-mini'),
                                    api_key='safe-key', client=client_with(fixture_response('openai_semantic_response.json'), captured))
    openai.extract(PAYLOAD)
    assert openai.capabilities.native_json_schema is True
    assert json.loads(captured[0].content)['text']['format']['schema'] == SEMANTIC_VIRAL_DNA_JSON_SCHEMA
    unknown_openai = OpenAISemanticProvider(SemanticProviderConfig(provider_name='openai', model='unknown'), api_key='safe-key',
                                             client=client_with(fixture_response('openai_semantic_response.json'), []))
    assert unknown_openai.capabilities.native_json_schema is False and unknown_openai.capabilities.json_mode is True
    gemini = GoogleSemanticProvider(SemanticProviderConfig(provider_name='google', model='gemini-2.5-flash'), api_key='safe-key',
                                    client=client_with(fixture_response('google_semantic_response.json'), []))
    assert gemini.capabilities.native_json_schema is True
    assert GoogleSemanticProvider(SemanticProviderConfig(provider_name='google', model='unknown'), api_key='safe-key',
                                  client=client_with(fixture_response('google_semantic_response.json'), [])).capabilities.native_json_schema is False


@pytest.mark.parametrize('name,_,__,env_name', ADAPTERS)
def test_builtin_registry_selects_only_the_selected_provider_key(monkeypatch, name, _, __, env_name):
    monkeypatch.setenv(env_name, 'selected-key')
    register_builtin_semantic_providers()
    provider = get_semantic_provider(name, SemanticProviderConfig(provider_name=name, model='configured-model'))
    assert provider.config.model == 'configured-model'
    assert provider._api_key == 'selected-key'
