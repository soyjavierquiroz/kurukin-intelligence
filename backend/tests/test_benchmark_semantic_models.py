from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.llm.semantic import SemanticProviderCapabilities, SemanticProviderExecutionMetadata
from app.llm.providers._common import SemanticProviderError
from app.models import Channel, Transcript, Video, ViralDNA
import ops.benchmark_semantic_models as benchmark_module
from ops.benchmark_semantic_models import (
    benchmark_semantic_input_records,
    benchmark_semantic_models,
    load_input_records,
    load_video_ids,
)
from tests.test_semantic_providers import minimum_output


class BenchmarkProvider:
    capabilities = SemanticProviderCapabilities(native_json_schema=True)

    def __init__(self, output=None):
        self.output = minimum_output() if output is None else output
        self.last_metadata = SemanticProviderExecutionMetadata(
            usage={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}, latency_ms=123, attempts=1
        )
        self.calls = []

    def extract(self, payload):
        self.calls.append(payload)
        return self.output


class SequentialBenchmarkProvider:
    """Offline provider double whose metadata is explicitly call-scoped."""

    capabilities = SemanticProviderCapabilities(native_json_schema=True)

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.last_metadata = SemanticProviderExecutionMetadata()

    def extract(self, payload):
        output, metadata = next(self.outcomes)
        self.last_metadata = metadata
        if isinstance(output, Exception):
            raise output
        return output


def make_video(db, *, transcript=True):
    channel = Channel(platform='tiktok', username=f'benchmark-{uuid4()}', nickname='Benchmark')
    db.add(channel); db.flush()
    video = Video(channel_id=channel.id, tiktok_id=str(uuid4().int)[:30], author='benchmark', nickname='Benchmark',
                  caption='Benchmark caption', published_at=datetime(2026, 1, 1, tzinfo=timezone.utc), duration=12,
                  url=f'https://www.tiktok.com/@benchmark/video/{uuid4().int}', enrichment_status='missing')
    db.add(video); db.flush()
    if transcript:
        db.add(Transcript(video_id=video.id, text='Benchmark transcript', language='es', duration=2, model='small'))
        db.flush()
    return video


def test_benchmark_is_read_only_and_emits_safe_per_video_records(db):
    video = make_video(db)
    provider = BenchmarkProvider()
    records, summary = benchmark_semantic_models(db, provider, provider_name='openai', model='model-x', video_ids=[video.id])
    assert records == [{
        'video_id': str(video.id), 'provider': 'openai', 'model': 'model-x', 'valid': True, 'latency_ms': 123,
        'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}, 'attempts': 1,
        'semantic': minimum_output(), 'error_code': None, 'validation_errors': None,
    }]
    assert summary == {
        'processed': 1, 'valid': 1, 'failed': 0, 'valid_rate': 1.0, 'total_input_tokens': 10,
        'total_output_tokens': 5, 'total_tokens': 15, 'latency_ms_avg': 123, 'latency_ms_p50': 123,
        'latency_ms_p95': 123,
    }
    assert provider.calls == [{'language': 'es', 'caption': 'Benchmark caption', 'transcript': 'Benchmark transcript'}]
    assert db.query(ViralDNA).count() == 0
    assert not db.new and not db.dirty


def test_benchmark_reports_missing_transcript_and_contract_failure_without_content(db):
    missing = make_video(db, transcript=False)
    invalid = make_video(db)
    records, summary = benchmark_semantic_models(db, BenchmarkProvider({'not': 'contract'}), provider_name='deepseek',
                                                  model='model-y', video_ids=[missing.id, invalid.id])
    assert [record['error_code'] for record in records] == ['missing_transcript', 'schema_validation_failed']
    assert all(record['semantic'] is None for record in records)
    assert records[0]['validation_errors'] is None
    assert {'field': '$', 'code': 'extra'} in records[1]['validation_errors']
    assert {'field': 'topic', 'code': 'missing'} in records[1]['validation_errors']
    assert summary['failed'] == 2 and summary['total_tokens'] == 15


def test_benchmark_records_keep_invocation_telemetry_and_results_isolated():
    """Failures, retries, invalid outputs, and successes cannot share state."""
    first_id, failed_id, invalid_id, final_id = (uuid4() for _ in range(4))
    first_usage = {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
    final_usage = {'input_tokens': 31, 'output_tokens': 17, 'total_tokens': 48}
    provider = SequentialBenchmarkProvider([
        (minimum_output(), SemanticProviderExecutionMetadata(usage=first_usage, latency_ms=10, attempts=1)),
        (SemanticProviderError('provider_unavailable', attempts=2),
         SemanticProviderExecutionMetadata(usage={}, latency_ms=20, attempts=2)),
        ({'invalid': 'response'}, SemanticProviderExecutionMetadata(usage={'total_tokens': 7}, latency_ms=30, attempts=1)),
        (minimum_output(), SemanticProviderExecutionMetadata(usage=final_usage, latency_ms=40, attempts=2)),
    ])
    inputs = [
        benchmark_module.BenchmarkInputRecord(video_id=video_id, language='es', caption='caption', transcript='text')
        for video_id in (first_id, failed_id, invalid_id, final_id)
    ]

    records, summary = benchmark_semantic_input_records(
        inputs, provider, provider_name='google', model='gemini-test',
    )

    # A prior successful invocation cannot supply usage or semantic output to
    # a final 503; a schema failure keeps only its own consumed-token usage.
    assert records[0]['usage'] == first_usage and records[0]['semantic'] == minimum_output()
    assert records[1] == {
        'video_id': str(failed_id), 'provider': 'google', 'model': 'gemini-test',
        'valid': False, 'latency_ms': 20, 'usage': {}, 'attempts': 2,
        'semantic': None, 'error_code': 'provider_unavailable', 'validation_errors': None,
    }
    assert records[2]['usage'] == {'total_tokens': 7}
    assert records[2]['error_code'] == 'schema_validation_failed'
    assert records[2]['semantic'] is None and records[2]['validation_errors']
    assert records[3]['usage'] == final_usage and records[3]['attempts'] == 2
    assert records[3]['semantic'] == minimum_output() and records[3]['error_code'] is None
    assert summary['total_tokens'] == 70


@pytest.mark.parametrize(('output', 'expected'), [
    ({**minimum_output(), 'angle_type': 'not-an-enum'}, {'field': 'angle_type', 'code': 'invalid_enum'}),
    ({**minimum_output(), 'hook_text': 'x' * 281}, {
        'field': 'hook_text', 'code': 'max_length', 'maximum_length': 280, 'received_length': 281,
    }),
    ({field: value for field, value in minimum_output().items() if field != 'topic'}, {'field': 'topic', 'code': 'missing'}),
    ({**minimum_output(), 'unexpected_field': 'ignored'}, {'field': '$', 'code': 'extra'}),
    ({**minimum_output(), 'content_role_secondary': 'unclear'}, {
        'field': 'content_role_secondary', 'code': 'same_as_primary',
    }),
    ({**minimum_output(), 'angle_type': None}, {'field': 'angle_type', 'code': 'not_nullable'}),
])
def test_benchmark_reports_safe_schema_validation_diagnostics(output, expected):
    records, _ = benchmark_semantic_input_records(
        [benchmark_module.BenchmarkInputRecord(
            video_id=uuid4(), language='es', caption='CAPTION-PRIVATE-TEST', transcript='TRANSCRIPT-PRIVATE-TEST',
        )],
        BenchmarkProvider(output), provider_name='openai', model='model-x',
    )
    record = records[0]
    assert record['valid'] is False
    assert record['error_code'] == 'schema_validation_failed'
    assert record['validation_errors'] == [expected]


def test_benchmark_validation_diagnostic_never_leaks_input_raw_response_or_api_key():
    private_caption = 'CAPTION-PRIVATE-TEST'
    private_transcript = 'TRANSCRIPT-PRIVATE-TEST'
    private_raw_response = 'RAW-RESPONSE-PRIVATE'
    private_api_key = 'API-KEY-PRIVATE'
    output = {**minimum_output(), 'unexpected_field': f'{private_raw_response} {private_api_key}'}
    records, _ = benchmark_semantic_input_records(
        [benchmark_module.BenchmarkInputRecord(
            video_id=uuid4(), language='es', caption=private_caption, transcript=private_transcript,
        )],
        BenchmarkProvider(output), provider_name='openai', model='model-x',
    )
    encoded = json.dumps(records)
    for private_value in (private_caption, private_transcript, private_raw_response, private_api_key):
        assert private_value not in encoded


def test_benchmark_emits_only_credential_index_and_usage_never_a_key():
    private_key = 'GEMINI-PRIVATE-KEY'
    provider = BenchmarkProvider()
    provider.last_metadata = SemanticProviderExecutionMetadata(
        usage={'total_tokens': 15}, latency_ms=123, attempts=1, credential_index=1,
    )
    records, summary = benchmark_semantic_input_records(
        [benchmark_module.BenchmarkInputRecord(video_id=uuid4(), language='es', caption='caption', transcript='text')],
        provider, provider_name='google', model='gemini-3.7-flash',
    )
    assert records[0]['credential_index'] == 1
    assert summary['credential_usage'] == {'1': 1}
    assert private_key not in json.dumps({'records': records, 'summary': summary})


def test_benchmark_delay_is_applied_only_between_input_records(monkeypatch):
    sleeps = []
    monkeypatch.setattr(benchmark_module.time, 'sleep', sleeps.append)
    inputs = [
        benchmark_module.BenchmarkInputRecord(video_id=uuid4(), language='es', caption='caption', transcript='text')
        for _ in range(3)
    ]
    records, _ = benchmark_semantic_input_records(
        inputs, BenchmarkProvider(), provider_name='openai', model='model-x', delay_seconds=2.5,
    )
    assert len(records) == 3
    assert sleeps == [2.5, 2.5]


def test_golden_set_ids_file_contains_ids_only_and_loads_read_only(db, tmp_path):
    video = make_video(db)
    file = tmp_path / 'golden-set.json'
    file.write_text('{"video_ids": ["%s", "%s"]}' % (video.id, video.id))
    assert load_video_ids(ids_file=file, session=db) == [video.id]
    bad = tmp_path / 'bad.json'; bad.write_text('{"video_ids": ["not-a-uuid"]}')
    with pytest.raises(ValueError, match='invalid_ids_file'):
        load_video_ids(ids_file=bad, session=db)


def write_input_file(tmp_path, records):
    path = tmp_path / 'benchmark-input.json'
    path.write_text(json.dumps(records), encoding='utf-8')
    return path


def input_record(video_id, **extra):
    return {
        'video_id': str(video_id), 'language': ' es ', 'caption': ' Caption-private ',
        'transcript': 'Transcript-private', **extra,
    }


def test_input_file_uses_only_required_text_fields_and_emits_safe_records(tmp_path):
    video_id = uuid4()
    path = write_input_file(tmp_path, [input_record(video_id, platform_video_id='platform-private', author='private',
                                                    duration_seconds=33, reference_semantic={'private': 'metadata'})])
    inputs = load_input_records(input_file=path)
    assert inputs[0].video_id == video_id
    assert vars(inputs[0]) == {
        'video_id': video_id, 'language': ' es ', 'caption': ' Caption-private ', 'transcript': 'Transcript-private',
    }
    provider = BenchmarkProvider()
    records, _ = benchmark_semantic_input_records(inputs, provider, provider_name='openai', model='model-x')
    assert provider.calls == [{'language': 'es', 'caption': 'Caption-private', 'transcript': 'Transcript-private'}]
    encoded = json.dumps(records)
    assert 'Caption-private' not in encoded and 'Transcript-private' not in encoded
    assert 'platform-private' not in encoded and 'private' not in encoded


def test_input_file_video_id_selection_and_missing_id_are_explicit(tmp_path):
    selected, unselected, missing = uuid4(), uuid4(), uuid4()
    path = write_input_file(tmp_path, [input_record(selected), input_record(unselected)])
    assert [item.video_id for item in load_input_records(input_file=path, video_ids=[selected])] == [selected]
    with pytest.raises(ValueError, match=f'missing_video_id_in_input_file: {missing}'):
        load_input_records(input_file=path, video_ids=[missing])


def test_input_file_main_needs_no_database_or_session(monkeypatch, tmp_path, capsys):
    video_id = uuid4()
    path = write_input_file(tmp_path, [input_record(video_id)])
    provider = BenchmarkProvider()
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setattr(benchmark_module, 'register_builtin_semantic_providers', lambda: None)
    monkeypatch.setattr(benchmark_module, 'get_semantic_provider', lambda *args: provider)
    monkeypatch.setattr(benchmark_module, 'get_engine', lambda: pytest.fail('database engine created'))
    monkeypatch.setattr(benchmark_module, 'Session', lambda *args, **kwargs: pytest.fail('database session created'))
    monkeypatch.setattr('sys.argv', [
        'benchmark_semantic_models.py', '--provider', 'openai', '--model', 'model-x',
        '--input-file', str(path), '--video-id', str(video_id),
    ])

    assert benchmark_module.main() == 0
    output = capsys.readouterr().out
    assert 'Caption-private' not in output and 'Transcript-private' not in output
    assert str(video_id) in output


def test_input_file_main_parses_delay_seconds(monkeypatch, tmp_path):
    video_id = uuid4()
    path = write_input_file(tmp_path, [input_record(video_id)])
    provider = BenchmarkProvider()
    captured = {}
    monkeypatch.setattr(benchmark_module, 'register_builtin_semantic_providers', lambda: None)
    monkeypatch.setattr(benchmark_module, 'get_semantic_provider', lambda *args: provider)

    def run(inputs, supplied_provider, **kwargs):
        captured.update(kwargs)
        return [], {}

    monkeypatch.setattr(benchmark_module, 'benchmark_semantic_input_records', run)
    monkeypatch.setattr('sys.argv', [
        'benchmark_semantic_models.py', '--provider', 'google', '--model', 'gemini-3.7-flash',
        '--input-file', str(path), '--delay-seconds', '3',
    ])
    assert benchmark_module.main() == 0
    assert captured['delay_seconds'] == 3.0
