import json

import pytest

from app.llm.semantic_contract import SEMANTIC_CONTRACT_VERSION, SEMANTIC_ENUM_FIELDS, SEMANTIC_OUTPUT_FIELDS
from ops.evaluate_semantic_benchmark import EvaluationInputError, evaluate, load_reference
from tests.test_semantic_viral_dna import VALID_OUTPUT


def write_reference(tmp_path, records):
    path = tmp_path / 'reference.json'
    path.write_text(json.dumps({
        'reference_set': 'test-v0', 'contract': SEMANTIC_CONTRACT_VERSION,
        'record_count': len(records), 'records': records,
    }), encoding='utf-8')
    return path


def write_results(tmp_path, rows, name='results.jsonl'):
    path = tmp_path / name
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    return path


def reference_record(video_id='video-1', semantic=None):
    return {'video_id': video_id, 'reference_semantic': dict(VALID_OUTPUT if semantic is None else semantic)}


def result_record(video_id='video-1', semantic=None, **extra):
    return {
        'video_id': video_id, 'provider': 'openai', 'model': 'model-a', 'valid': True,
        'semantic': dict(VALID_OUTPUT if semantic is None else semantic), 'error_code': None,
        'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}, 'latency_ms': 100,
        **extra,
    }


def one_result(tmp_path, rows, reference=None, adjudication_path=None):
    reference = write_reference(tmp_path, reference or [reference_record()])
    report, template = evaluate(reference, [write_results(tmp_path, rows)], adjudication_path=adjudication_path)
    return report['results'][0], template


def test_valid_reference_needs_only_id_and_semantic(tmp_path):
    path = write_reference(tmp_path, [reference_record()])
    records, meta = load_reference(path)
    assert set(records) == {'video-1'} and meta['record_count'] == 1
    assert 'transcript' not in json.loads(path.read_text())['records'][0]


def test_invalid_reference_fails_immediately(tmp_path):
    bad = dict(VALID_OUTPUT); bad['hook_type'] = 'not-an-enum'
    with pytest.raises(EvaluationInputError, match='invalid reference semantic'):
        load_reference(write_reference(tmp_path, [reference_record(semantic=bad)]))


def test_exact_and_mismatched_enums_include_confusion_and_video_accuracy(tmp_path):
    candidate = dict(VALID_OUTPUT); candidate['content_role_primary'] = 'conversion'
    result, _ = one_result(tmp_path, [result_record(semantic=candidate)])
    assert result['enum']['exact'] == len(SEMANTIC_ENUM_FIELDS) - 1
    assert result['enum']['mismatch'] == 1
    assert result['enum']['accuracy_by_video']['video-1']['mismatch'] == 1
    assert result['enum']['confusion_pairs'] == [{
        'field': 'content_role_primary', 'reference': 'nurture', 'candidate': 'conversion', 'count': 1,
    }]


def test_null_text_and_paraphrase_are_diagnostics_not_adjudicated_score(tmp_path):
    reference = dict(VALID_OUTPUT); reference['pain'] = None; reference['topic'] = 'aprende rápido'
    candidate = dict(reference); candidate['topic'] = 'rápido aprende'; candidate['pain'] = 'not null'
    result, _ = one_result(tmp_path, [result_record(semantic=candidate)], [reference_record(semantic=reference)])
    diagnostics = result['text_similarity_diagnostics']
    assert diagnostics['warning'] == 'TEXT SIMILARITY IS DIAGNOSTIC ONLY.'
    assert diagnostics['by_field']['pain']['nullability_accuracy'] == 0
    assert diagnostics['by_field']['topic']['normalized_token_f1_avg'] == 1
    assert result['adjudication']['pending'] == 2


def test_invalid_candidate_and_valid_false_are_real_failures(tmp_path):
    invalid = dict(VALID_OUTPUT); invalid['topic'] = 'x' * 121
    result, template = one_result(tmp_path, [
        result_record('video-1', invalid),
        {**result_record('video-2'), 'valid': False, 'semantic': None, 'error_code': 'rate_limited'},
    ], [reference_record('video-1'), reference_record('video-2')])
    assert result['failures']['processed'] == 2 and result['failures']['failed'] == 2
    assert result['failures']['error_code_counts'] == {'rate_limited': 1, 'schema_validation_failed': 1}
    assert result['schema_valid_rate'] == 0
    assert len(template) == 2 * len(SEMANTIC_OUTPUT_FIELDS)


def test_missing_extra_and_duplicate_are_reported_and_missing_does_not_raise_accuracy(tmp_path):
    result, _ = one_result(tmp_path, [
        result_record('video-1'), result_record('video-1'), result_record('unknown'),
    ], [reference_record('video-1'), reference_record('video-2')])
    assert result['coverage']['expected'] == 2 and result['coverage']['received'] == 1
    assert result['coverage']['missing'] == ['video-2']
    assert result['coverage']['extra'] == ['unknown'] and result['coverage']['duplicate_count'] == 1
    assert result['enum_exact_accuracy'] == .5


def test_groups_multiple_providers_and_models_independently(tmp_path):
    ref = write_reference(tmp_path, [reference_record()])
    rows = [
        result_record(provider='openai', model='a'), result_record(provider='openai', model='b'),
        result_record(provider='google', model='a'), result_record(provider='moonshot', model='m'),
    ]
    report, _ = evaluate(ref, [write_results(tmp_path, rows)])
    assert [(item['provider'], item['model']) for item in report['results']] == [
        ('google', 'a'), ('moonshot', 'm'), ('openai', 'a'), ('openai', 'b'),
    ]


@pytest.mark.parametrize('decision, expected', [
    ('exact', 1), ('acceptable', 1), ('wrong', 36 / len(SEMANTIC_OUTPUT_FIELDS)), ('pending', 1),
])
def test_adjudication_decisions_and_pending_exclusion(tmp_path, decision, expected):
    candidate = dict(VALID_OUTPUT); candidate['topic'] = 'paráfrasis'
    adjudication = tmp_path / 'adjudication.json'
    adjudication.write_text(json.dumps([{
        'video_id': 'video-1', 'field': 'topic', 'provider': 'openai', 'model': 'model-a',
        'reference': VALID_OUTPUT['topic'], 'candidate': 'paráfrasis', 'decision': decision,
    }]), encoding='utf-8')
    result, _ = one_result(tmp_path, [result_record(semantic=candidate)], adjudication_path=adjudication)
    assert result['adjudicated_accuracy'] == pytest.approx(expected)
    assert result['adjudicated_coverage'] == pytest.approx(
        1 if decision != 'pending' else 36 / len(SEMANTIC_OUTPUT_FIELDS)
    )  # all matching cells are automatic
    assert result['adjudication']['is_final'] is (decision != 'pending')


def test_adjudication_template_has_only_safe_requested_fields(tmp_path):
    result, template = one_result(tmp_path, [result_record()])
    assert result['adjudication']['pending'] == 0
    assert set(template[0]) == {'video_id', 'field', 'provider', 'model', 'reference', 'candidate', 'decision'}
    assert template[0]['decision'] == 'exact'


def test_coverage_token_latency_and_cost_aggregation(tmp_path):
    first = result_record('video-1', latency_ms=10, estimated_cost_usd=.01)
    second = result_record('video-2', latency_ms=90, usage={'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30}, estimated_cost_usd=.02)
    result, _ = one_result(tmp_path, [first, second], [reference_record('video-1'), reference_record('video-2')])
    assert result['performance'] == {
        'total_input_tokens': 30.0, 'total_output_tokens': 15.0, 'total_tokens': 45.0,
        'avg_tokens_per_video': 22.5, 'latency_ms_avg': 50.0, 'latency_ms_p50': 10.0,
        'latency_ms_p95': 90.0, 'total_estimated_cost_usd': .03, 'estimated_cost_available_for_records': 2,
    }


def test_missing_failure_metadata_never_leaks_from_a_previous_result(tmp_path):
    failed = {**result_record('video-1'), 'valid': False, 'semantic': None, 'error_code': 'timeout',
              'usage': {}, 'latency_ms': None}
    valid = result_record('video-2', latency_ms=200)
    result, _ = one_result(tmp_path, [failed, valid], [reference_record('video-1'), reference_record('video-2')])
    assert result['performance']['total_tokens'] == 15
    assert result['performance']['latency_ms_avg'] == 200


def test_evaluator_uses_no_network_or_database(monkeypatch, tmp_path):
    import socket
    monkeypatch.setattr(socket, 'create_connection', lambda *args, **kwargs: pytest.fail('network used'))
    result, _ = one_result(tmp_path, [result_record()])
    assert result['schema_valid_rate'] == 1
