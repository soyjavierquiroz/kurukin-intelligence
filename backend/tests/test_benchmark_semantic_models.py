from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.llm.semantic import SemanticProviderCapabilities, SemanticProviderExecutionMetadata
from app.models import Channel, Transcript, Video, ViralDNA
from ops.benchmark_semantic_models import benchmark_semantic_models, load_video_ids
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
        'semantic': minimum_output(), 'error_code': None,
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
    assert summary['failed'] == 2 and summary['total_tokens'] == 15


def test_golden_set_ids_file_contains_ids_only_and_loads_read_only(db, tmp_path):
    video = make_video(db)
    file = tmp_path / 'golden-set.json'
    file.write_text('{"video_ids": ["%s", "%s"]}' % (video.id, video.id))
    assert load_video_ids(ids_file=file, session=db) == [video.id]
    bad = tmp_path / 'bad.json'; bad.write_text('{"video_ids": ["not-a-uuid"]}')
    with pytest.raises(ValueError, match='invalid_ids_file'):
        load_video_ids(ids_file=bad, session=db)
