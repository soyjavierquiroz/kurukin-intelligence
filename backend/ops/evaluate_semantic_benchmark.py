#!/usr/bin/env python3
"""Offline, provider-neutral evaluator for Semantic Viral DNA benchmark JSONL.

This module deliberately has no database, provider, or network imports.  It
reads the frozen Kurukin contract directly and evaluates already-produced
benchmark records against a human-curated Golden Reference.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Iterable


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The frozen contract is the authority for names, values, nullability, and
# maximum lengths.  Do not duplicate any of those definitions here.
from app.llm.semantic_contract import (  # noqa: E402
    SEMANTIC_CONTRACT_VERSION,
    SEMANTIC_ENUM_FIELDS,
    SEMANTIC_NULLABLE_FIELDS,
    SEMANTIC_OUTPUT_FIELDS,
    SEMANTIC_SECONDARY_PRIMARY_PAIRS,
    SEMANTIC_TEXT_FIELDS,
)


TOKEN_PATTERN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
ADJUDICATION_DECISIONS = frozenset({'exact', 'acceptable', 'wrong', 'pending'})

# This is intentionally only a presentation map, versioned next to the
# evaluator.  Field existence is checked against the frozen contract below;
# it does not define semantic fields, enums, nullability, or weights.
DIMENSION_FIELDS: dict[str, tuple[str, ...]] = {
    'HOOK': ('hook_text', 'hook_type', 'hook_mechanism', 'hook_target'),
    'SUBJECT/ANGLE': ('topic', 'subtopic', 'angle_summary', 'angle_type'),
    'AUDIENCE/PSYCHOLOGY': (
        'audience_specificity', 'pain', 'desire', 'fear', 'audience_identity',
        'belief', 'objection', 'promise', 'reframe',
    ),
    'EMOTION': ('emotion_primary', 'emotion_secondary', 'emotional_arc'),
    'CONTENT FUNCTION': ('content_function_primary', 'content_function_secondary'),
    'CONTENT ROLE': ('content_role_primary', 'content_role_secondary'),
    'FORMAT': ('content_format',),
    'NARRATIVE': ('narrative_structure',),
    'AWARENESS': ('awareness_stage',),
    'POSITIONING/PROOF': ('proof_type', 'authority_mechanism', 'creator_positioning_signal'),
    'CTA': ('cta_text', 'cta_type', 'cta_secondary_type'),
    'COMMERCIAL': ('commercial_intent', 'offer_integration', 'offer_type', 'monetization_model'),
}


class EvaluationInputError(ValueError):
    """A safe, user-actionable error for malformed offline artifacts."""


def _validate_dimension_map() -> None:
    mapped = [field for fields in DIMENSION_FIELDS.values() for field in fields]
    if set(mapped) != set(SEMANTIC_OUTPUT_FIELDS) or len(mapped) != len(set(mapped)):
        raise RuntimeError('dimension map must cover every contract field exactly once')


_validate_dimension_map()


def validate_semantic(value: object) -> list[str]:
    """Return contract-validation error codes without exposing semantic text."""
    if not isinstance(value, dict):
        return ['not_object']
    actual = set(value)
    expected = set(SEMANTIC_OUTPUT_FIELDS)
    errors: list[str] = []
    if actual - expected:
        errors.append('unexpected_fields')
    if expected - actual:
        errors.append('missing_fields')
    for field in SEMANTIC_OUTPUT_FIELDS:
        if field not in value:
            continue
        item = value[field]
        if field in SEMANTIC_TEXT_FIELDS:
            if item is not None and (not isinstance(item, str) or len(item) > SEMANTIC_TEXT_FIELDS[field]):
                errors.append(f'invalid_{field}')
        elif item is None:
            if field not in SEMANTIC_NULLABLE_FIELDS:
                errors.append(f'invalid_{field}')
        elif not isinstance(item, str) or item not in SEMANTIC_ENUM_FIELDS[field]:
            errors.append(f'invalid_{field}')
    if not errors:
        for primary, secondary in SEMANTIC_SECONDARY_PRIMARY_PAIRS:
            if value[secondary] is not None and value[primary] == value[secondary]:
                errors.append(f'invalid_{secondary}')
    return errors


def _load_json(path: Path, *, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationInputError(f'invalid_{label}: {path}') from error


def load_reference(path: Path) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Load and immediately reject a Golden Reference that violates v1."""
    document = _load_json(path, label='reference')
    if not isinstance(document, dict) or not isinstance(document.get('records'), list):
        raise EvaluationInputError('reference must be an object containing records')
    if document.get('contract') not in (None, SEMANTIC_CONTRACT_VERSION):
        raise EvaluationInputError('reference contract does not match Semantic DNA v1')
    records: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(document['records']):
        if not isinstance(raw, dict) or not isinstance(raw.get('video_id'), str):
            raise EvaluationInputError(f'invalid reference record at index {index}')
        video_id = raw['video_id']
        if video_id in records:
            raise EvaluationInputError(f'duplicate reference video_id: {video_id}')
        errors = validate_semantic(raw.get('reference_semantic'))
        if errors:
            raise EvaluationInputError(f'invalid reference semantic for video_id {video_id}: {errors[0]}')
        records[video_id] = raw['reference_semantic']
    declared_count = document.get('record_count')
    if declared_count is not None and declared_count != len(records):
        raise EvaluationInputError('reference record_count does not match records')
    return records, {'reference_set': document.get('reference_set'), 'contract': document.get('contract'),
                     'record_count': len(records)}


def load_jsonl_results(paths: Iterable[Path]) -> list[dict[str, object]]:
    """Load benchmark records; summary lines are deliberately ignored."""
    records: list[dict[str, object]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError as error:
            raise EvaluationInputError(f'invalid result: {path}') from error
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise EvaluationInputError(f'invalid JSONL result at {path}:{line_number}') from error
            if isinstance(raw, dict) and set(raw) == {'summary'}:
                continue
            if not isinstance(raw, dict):
                raise EvaluationInputError(f'invalid result record at {path}:{line_number}')
            required = ('video_id', 'provider', 'model', 'valid')
            if any(not isinstance(raw.get(key), str) for key in required[:3]) or not isinstance(raw.get('valid'), bool):
                raise EvaluationInputError(f'invalid result identity at {path}:{line_number}')
            raw['_source'] = str(path)
            raw['_line'] = line_number
            records.append(raw)
    return records


def _normalised_tokens(value: str) -> list[str]:
    return TOKEN_PATTERN.findall(value.casefold())


def _normalised_text(value: str) -> str:
    """Use the same deterministic Unicode token normalization as diagnostics."""
    return ' '.join(_normalised_tokens(value))


def _text_diagnostic(reference: str | None, candidate: str | None) -> dict[str, object]:
    nullability_match = (reference is None) == (candidate is None)
    if reference is None or candidate is None:
        return {'nullability_match': nullability_match, 'normalized_exact_match': reference == candidate,
                'token_precision': None, 'token_recall': None, 'token_f1': None}
    reference_counts, candidate_counts = Counter(_normalised_tokens(reference)), Counter(_normalised_tokens(candidate))
    overlap = sum((reference_counts & candidate_counts).values())
    candidate_total, reference_total = sum(candidate_counts.values()), sum(reference_counts.values())
    precision = 1.0 if candidate_total == 0 and reference_total == 0 else (overlap / candidate_total if candidate_total else 0.0)
    recall = 1.0 if reference_total == 0 and candidate_total == 0 else (overlap / reference_total if reference_total else 0.0)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {'nullability_match': nullability_match, 'normalized_exact_match': _normalised_text(reference) == _normalised_text(candidate),
            'token_precision': precision, 'token_recall': recall, 'token_f1': f1}


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))]


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _usage(record: dict[str, object], key: str) -> float:
    usage = record.get('usage')
    value = _number(usage.get(key)) if isinstance(usage, dict) else None
    if value is None:
        value = _number(record.get(key))
    return value or 0.0


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _identity(record: dict[str, object]) -> tuple[str, str]:
    return record['provider'], record['model']  # asserted by loader


def _adjudication_key(provider: str, model: str, video_id: str, field: str) -> tuple[str, str, str, str]:
    return provider, model, video_id, field


def load_adjudications(path: Path | None) -> dict[tuple[str, str, str, str], str]:
    if path is None:
        return {}
    text = path.read_text(encoding='utf-8')
    try:
        raw = json.loads(text)
        rows = raw if isinstance(raw, list) else (
            raw.get('records') if isinstance(raw, dict) and 'records' in raw else [raw] if isinstance(raw, dict) else None
        )
    except json.JSONDecodeError:
        try:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as error:
            raise EvaluationInputError(f'invalid adjudication: {path}') from error
    if not isinstance(rows, list):
        raise EvaluationInputError('adjudication must be a JSON array/object records or JSONL')
    decisions: dict[tuple[str, str, str, str], str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in ('provider', 'model', 'video_id', 'field', 'decision')):
            raise EvaluationInputError(f'invalid adjudication record at index {index}')
        if row['decision'] not in ADJUDICATION_DECISIONS:
            raise EvaluationInputError(f'invalid adjudication decision at index {index}')
        key = _adjudication_key(row['provider'], row['model'], row['video_id'], row['field'])
        if key in decisions:
            raise EvaluationInputError(f'duplicate adjudication for {row["video_id"]}/{row["field"]}')
        decisions[key] = row['decision']
    return decisions


def _summary_for_group(
    provider: str, model: str, records: list[dict[str, object]], reference: dict[str, dict[str, object]],
    adjudications: dict[tuple[str, str, str, str], str],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    expected_ids = set(reference)
    by_video: dict[str, dict[str, object]] = {}
    duplicates: list[str] = []
    extras: list[str] = []
    schema_valid: dict[str, bool] = {}
    error_codes: Counter[str] = Counter()
    for record in records:
        video_id = record['video_id']
        if video_id in by_video:
            duplicates.append(video_id)
            error_codes['duplicate_video_id'] += 1
            continue
        by_video[video_id] = record
        if video_id not in expected_ids:
            extras.append(video_id)
        semantic_errors = validate_semantic(record.get('semantic')) if record.get('semantic') is not None else ['missing_semantic']
        record_ok = record['valid'] is True and not record.get('error_code') and not semantic_errors
        schema_valid[video_id] = record_ok
        if not record_ok:
            if record['valid'] is not True:
                code = str(record.get('error_code') or 'benchmark_valid_false')
            elif record.get('error_code'):
                code = str(record['error_code'])
            else:
                code = 'schema_validation_failed'
            error_codes[code] += 1

    received_ids = set(by_video) & expected_ids
    missing = sorted(expected_ids - received_ids)
    enum_totals: dict[str, dict[str, int]] = {field: {'exact': 0, 'mismatch': 0} for field in SEMANTIC_ENUM_FIELDS}
    enum_confusion: Counter[tuple[str, str, str]] = Counter()
    text_totals: dict[str, dict[str, float]] = {
        field: {'nullability_exact': 0, 'normalized_exact': 0, 'token_compared': 0,
                'precision_sum': 0.0, 'recall_sum': 0.0, 'f1_sum': 0.0}
        for field in SEMANTIC_TEXT_FIELDS
    }
    dimensions: dict[str, dict[str, int]] = {name: {'exact': 0, 'mismatch': 0} for name in DIMENSION_FIELDS}
    template: list[dict[str, object]] = []
    adjudicated = {'exact': 0, 'acceptable': 0, 'wrong': 0, 'pending': 0}

    for video_id, ref in reference.items():
        record = by_video.get(video_id)
        candidate = record.get('semantic') if record is not None and schema_valid.get(video_id) else None
        for field in SEMANTIC_OUTPUT_FIELDS:
            reference_value = ref[field]
            candidate_value = candidate[field] if isinstance(candidate, dict) else None
            same = isinstance(candidate, dict) and reference_value == candidate_value
            dimension = next(name for name, fields in DIMENSION_FIELDS.items() if field in fields)
            if field in SEMANTIC_ENUM_FIELDS:
                if same:
                    enum_totals[field]['exact'] += 1; dimensions[dimension]['exact'] += 1
                else:
                    enum_totals[field]['mismatch'] += 1; dimensions[dimension]['mismatch'] += 1
                    if isinstance(candidate, dict):
                        enum_confusion[(field, str(reference_value), str(candidate_value))] += 1
            else:
                diagnostic = _text_diagnostic(reference_value, candidate_value if isinstance(candidate, dict) else None)
                text_totals[field]['nullability_exact'] += int(diagnostic['nullability_match'])
                text_totals[field]['normalized_exact'] += int(diagnostic['normalized_exact_match'])
                if diagnostic['token_f1'] is not None:
                    text_totals[field]['token_compared'] += 1
                    text_totals[field]['precision_sum'] += diagnostic['token_precision']
                    text_totals[field]['recall_sum'] += diagnostic['token_recall']
                    text_totals[field]['f1_sum'] += diagnostic['token_f1']
                if same:
                    dimensions[dimension]['exact'] += 1
                else:
                    dimensions[dimension]['mismatch'] += 1
            automatic = 'exact' if same else 'pending'
            decision = adjudications.get(_adjudication_key(provider, model, video_id, field), automatic)
            adjudicated[decision] += 1
            template.append({'video_id': video_id, 'field': field, 'provider': provider, 'model': model,
                             'reference': reference_value, 'candidate': candidate_value, 'decision': automatic})

    expected = len(reference)
    enum_cells = expected * len(SEMANTIC_ENUM_FIELDS)
    text_cells = expected * len(SEMANTIC_TEXT_FIELDS)
    enum_exact = sum(row['exact'] for row in enum_totals.values())
    text_nullability_exact = sum(int(row['nullability_exact']) for row in text_totals.values())
    per_field_enum = {
        field: {**counts, 'accuracy': _round(counts['exact'] / expected) if expected else None}
        for field, counts in enum_totals.items()
    }
    per_video_enum = {}
    for video_id, ref in reference.items():
        record = by_video.get(video_id)
        semantic = record.get('semantic') if record is not None and schema_valid.get(video_id) else None
        exact_count = sum(isinstance(semantic, dict) and ref[field] == semantic[field] for field in SEMANTIC_ENUM_FIELDS)
        per_video_enum[video_id] = {'exact': exact_count, 'mismatch': len(SEMANTIC_ENUM_FIELDS) - exact_count,
                                    'accuracy': _round(exact_count / len(SEMANTIC_ENUM_FIELDS))}
    text_diagnostics = {
        field: {
            'nullability_accuracy': _round(row['nullability_exact'] / expected) if expected else None,
            'normalized_exact_accuracy': _round(row['normalized_exact'] / expected) if expected else None,
            'token_compared': int(row['token_compared']),
            'normalized_token_precision_avg': _round(row['precision_sum'] / row['token_compared']) if row['token_compared'] else None,
            'normalized_token_recall_avg': _round(row['recall_sum'] / row['token_compared']) if row['token_compared'] else None,
            'normalized_token_f1_avg': _round(row['f1_sum'] / row['token_compared']) if row['token_compared'] else None,
        } for field, row in text_totals.items()
    }
    valid = sum(schema_valid.values())
    latencies = [value for record in records if (value := _number(record.get('latency_ms'))) is not None]
    total_input = sum(_usage(record, 'input_tokens') for record in records)
    total_output = sum(_usage(record, 'output_tokens') for record in records)
    total_tokens = sum(_usage(record, 'total_tokens') for record in records)
    costs = [_number(record.get('estimated_cost_usd')) for record in records]
    available_costs = [cost for cost in costs if cost is not None]
    decided = adjudicated['exact'] + adjudicated['acceptable'] + adjudicated['wrong']
    score_denominator = decided
    return {
        'provider': provider,
        'model': model,
        'coverage': {'expected': expected, 'received': len(received_ids), 'missing': missing, 'extra': sorted(set(extras)),
                     'duplicates': sorted(set(duplicates)), 'duplicate_count': len(duplicates),
                     'coverage_rate': _round(len(received_ids) / expected) if expected else None},
        'failures': {'processed': len(records), 'valid': valid, 'failed': len(records) - valid,
                     'failure_rate': _round((len(records) - valid) / len(records)) if records else None,
                     'error_code_counts': dict(sorted(error_codes.items()))},
        'schema_valid_rate': _round(valid / len(records)) if records else None,
        'enum_exact_accuracy': _round(enum_exact / enum_cells) if enum_cells else None,
        'enum': {'exact': enum_exact, 'mismatch': enum_cells - enum_exact, 'accuracy_by_field': per_field_enum,
                 'accuracy_by_video': per_video_enum,
                 'confusion_pairs': [{'field': field, 'reference': ref_value, 'candidate': candidate_value, 'count': count}
                                     for (field, ref_value, candidate_value), count in sorted(enum_confusion.items())]},
        'text_nullability_accuracy': _round(text_nullability_exact / text_cells) if text_cells else None,
        'text_similarity_diagnostics': {'warning': 'TEXT SIMILARITY IS DIAGNOSTIC ONLY.', 'by_field': text_diagnostics},
        'adjudicated_accuracy': _round((adjudicated['exact'] + adjudicated['acceptable']) / score_denominator)
            if score_denominator else None,
        'adjudicated_coverage': _round(decided / (expected * len(SEMANTIC_OUTPUT_FIELDS)) if expected else None),
        'adjudication': {**adjudicated, 'is_final': adjudicated['pending'] == 0,
                          'message': None if adjudicated['pending'] == 0 else 'Overall adjudicated score is NOT final: pending decisions are excluded.'},
        'dimensions': {name: {**counts, 'accuracy': _round(counts['exact'] / (counts['exact'] + counts['mismatch']))
                              if counts['exact'] + counts['mismatch'] else None} for name, counts in dimensions.items()},
        'performance': {'total_input_tokens': total_input, 'total_output_tokens': total_output, 'total_tokens': total_tokens,
                        'avg_tokens_per_video': _round(total_tokens / len(records)) if records else None,
                        'latency_ms_avg': _round(statistics.mean(latencies)) if latencies else None,
                        'latency_ms_p50': _percentile(latencies, .50), 'latency_ms_p95': _percentile(latencies, .95),
                        'total_estimated_cost_usd': _round(sum(available_costs)) if available_costs else None,
                        'estimated_cost_available_for_records': len(available_costs)},
    }, template


def evaluate(reference_path: Path, result_paths: Iterable[Path], *, adjudication_path: Path | None = None) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Evaluate result JSONL files without any network or database activity."""
    reference, reference_meta = load_reference(reference_path)
    adjudications = load_adjudications(adjudication_path)
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for record in load_jsonl_results(result_paths):
        groups[_identity(record)].append(record)
    valid_adjudication_keys = {
        _adjudication_key(provider, model, video_id, field)
        for provider, model in groups for video_id in reference for field in SEMANTIC_OUTPUT_FIELDS
    }
    unknown_adjudications = set(adjudications) - valid_adjudication_keys
    if unknown_adjudications:
        raise EvaluationInputError('adjudication does not match an evaluated provider/model/video/field')
    results, templates = [], []
    for provider, model in sorted(groups):
        summary, template = _summary_for_group(provider, model, groups[(provider, model)], reference, adjudications)
        results.append(summary); templates.extend(template)
    return {'reference': reference_meta, 'contract': SEMANTIC_CONTRACT_VERSION, 'results': results}, templates


def _markdown(report: dict[str, object]) -> str:
    lines = [
        '# Semantic benchmark evaluation', '',
        '**TEXT SIMILARITY IS DIAGNOSTIC ONLY.** It is not included in adjudicated accuracy.', '',
        '| provider | model | coverage | valid rate | enum exact | adjudicated accuracy | adjudicated coverage | tokens | latency | estimated cost |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for result in report['results']:
        coverage, failures, performance = result['coverage'], result['failures'], result['performance']
        cost = performance['total_estimated_cost_usd']
        lines.append('| {provider} | {model} | {received}/{expected} ({coverage_rate:.1%}) | {valid_rate:.1%} | {enum_exact:.1%} | {adj} | {adj_cov:.1%} | {tokens:g} | {latency} ms | {cost} |'.format(
            provider=result['provider'], model=result['model'], received=coverage['received'], expected=coverage['expected'],
            coverage_rate=coverage['coverage_rate'] or 0, valid_rate=result['schema_valid_rate'] or 0,
            enum_exact=result['enum_exact_accuracy'] or 0,
            adj='—' if result['adjudicated_accuracy'] is None else f"{result['adjudicated_accuracy']:.1%}",
            adj_cov=result['adjudicated_coverage'] or 0, tokens=performance['total_tokens'],
            latency='—' if performance['latency_ms_avg'] is None else f"{performance['latency_ms_avg']:g}",
            cost='—' if cost is None else f'${cost:.6f}',
        ))
        if result['adjudication']['pending']:
            lines.extend(['', f"- `{result['provider']}:{result['model']}`: overall adjudicated score is **NOT final**; {result['adjudication']['pending']} pending decisions are excluded."])
        if coverage['missing'] or coverage['extra'] or coverage['duplicate_count']:
            lines.extend(['', f"- `{result['provider']}:{result['model']}` coverage: missing {len(coverage['missing'])}, extra {len(coverage['extra'])}, duplicates {coverage['duplicate_count']}."])
    lines.extend(['', 'No winner is declared automatically.'])
    return '\n'.join(lines) + '\n'


def _write_json_or_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    if path.suffix.lower() == '.jsonl':
        path.write_text(''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in rows), encoding='utf-8')
    else:
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', required=True, type=Path)
    parser.add_argument('--result', required=True, action='append', type=Path)
    parser.add_argument('--adjudication', type=Path)
    parser.add_argument('--adjudication-template-out', type=Path)
    parser.add_argument('--json-out', type=Path)
    parser.add_argument('--markdown-out', type=Path)
    args = parser.parse_args()
    try:
        report, template = evaluate(args.reference, args.result, adjudication_path=args.adjudication)
    except (EvaluationInputError, OSError) as error:
        parser.error(str(error))
    if args.adjudication_template_out:
        _write_json_or_jsonl(args.adjudication_template_out, template)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    if args.json_out:
        args.json_out.write_text(rendered, encoding='utf-8')
    if args.markdown_out:
        args.markdown_out.write_text(_markdown(report), encoding='utf-8')
    if not args.json_out and not args.markdown_out:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
