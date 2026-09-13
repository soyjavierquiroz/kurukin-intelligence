# Semantic provider benchmark

Semantic Viral DNA v1 is Kurukin-owned and frozen.  Providers receive only
`language`, `caption`, and `transcript`, then return the same 37-field contract.
Kurukin centrally validates every response, including responses made with a
provider's structured-output feature.  No model is preferred by architecture:
the selection is made from benchmark evidence.

## Providers and configuration

The canonical provider IDs are `openai`, `google`, `moonshot`, and `deepseek`.
`moonshot` is the provider ID; Kimi is its model family/product, never the
provenance provider prefix.  Provenance is always `provider:model`.

Set the neutral pair for normal application routing:

```bash
export VIRAL_DNA_SEMANTIC_PROVIDER=openai
export VIRAL_DNA_SEMANTIC_MODEL='your-model-id'
```

Only the chosen provider needs its credential: `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `MOONSHOT_API_KEY`, or `DEEPSEEK_API_KEY`.  There are no
model defaults and no credentials, input content, raw responses, or
authorization values in logs.

The REST protocols are deliberately lightweight: OpenAI `POST /v1/responses`,
Gemini `POST /v1beta/models/{model}:generateContent`, and OpenAI-compatible
Chat Completions endpoints for Moonshot and DeepSeek.  `httpx`, already pinned
by the backend, is the sole HTTP dependency.

| Provider | Capability used | Schema handling | Usage mapping |
| --- | --- | --- | --- |
| OpenAI | native JSON Schema via Responses `text.format` | frozen Kurukin schema passed directly, strict | `input_tokens`, `output_tokens`, `total_tokens` |
| Google | native JSON Schema via `responseJsonSchema` | deterministic Gemini subset removes only unsupported `maxLength`; central validation remains strict | `promptTokenCount`, `candidatesTokenCount`, `totalTokenCount`, cache/thought fields when returned |
| Moonshot | documented JSON mode | shared prompt embeds the frozen schema; central validation | `prompt_tokens`, `completion_tokens`, `total_tokens` |
| DeepSeek | documented JSON mode | shared prompt embeds the frozen schema; central validation | `prompt_tokens`, `completion_tokens`, `total_tokens`, cache field when returned |

Moonshot and DeepSeek do not claim native JSON Schema capability here.  No
tools, search, files, streaming, conversation state, or automatic semantic
retry is used.

## Benchmark

The harness is read-only: it selects `Video`/`Transcript`, does not create or
modify `ViralDNA`, never flushes or commits, and makes one provider call per
video.  It emits safe JSONL: video ID, provider/model, validated semantic
object, latency, attempts, normalized usage, and normalized error code.  It
never emits captions, transcripts, raw provider bodies, or keys.

```bash
cd backend
.venv/bin/python ops/benchmark_semantic_models.py \
  --provider openai --model 'configured-model-id' --video-id UUID

.venv/bin/python ops/benchmark_semantic_models.py \
  --provider google --model 'configured-model-id' --ids-file ops/golden-set.json

.venv/bin/python ops/benchmark_semantic_models.py \
  --provider openai --model 'configured-model-id' \
  --input-file /tmp/semantic-golden.json --video-id UUID

.venv/bin/python ops/benchmark_semantic_models.py \
  --provider deepseek --model 'configured-model-id' --limit 20 \
  --input-cost-per-million 0.50 --output-cost-per-million 2.00
```

`--input-file` accepts a JSON array whose records contain `video_id`,
`language`, `caption`, and `transcript`. It needs no database connection or
`DATABASE_URL`, and ignores any other metadata fields. It can be combined with
one or more `--video-id` selectors; a requested ID absent from the file fails
before any provider call. The normal DB-backed path is unchanged when this
option is omitted.

The optional cost parameters are CLI values, never provider-core prices.  The
summary reports counts, validation rate, token totals, mean/p50/p95 latency,
and optional estimated cost.  At most one retry is made for a timeout, 429, or
5xx; semantic failures are never retried.

`ops/golden-set.json` is intentionally an empty IDs-only starter file.  Future
human-curated Golden Sets must contain video IDs only, never copied text.

## Offline Golden Reference evaluation

`ops/evaluate_semantic_benchmark.py` is a separate, fully offline evaluator.
It imports the frozen Semantic DNA v1 contract directly, reads a Golden
Reference and one or more benchmark JSONL files, and has no provider, network,
database, or migration dependency.

```bash
cd backend
.venv/bin/python ops/evaluate_semantic_benchmark.py \
  --reference /path/golden-reference-v0.json \
  --result /tmp/openai-results.jsonl \
  --result /tmp/gemini-results.jsonl \
  --json-out /tmp/semantic-evaluation.json \
  --markdown-out /tmp/semantic-evaluation.md \
  --adjudication-template-out /tmp/semantic-adjudication.jsonl
```

The evaluator reports coverage and failures before comparing values, so missing
or invalid records cannot make a provider appear more accurate. Closed enums
are exact-only comparisons with per-field/video accuracy and confusion pairs.
Text uses nullability and normalized token diagnostics only; **TEXT SIMILARITY
IS DIAGNOSTIC ONLY** and is never folded into a winner score.

The template contains only `video_id`, field, provider/model, reference value,
candidate value, and a decision. Exact matches are prefilled `exact`; every
difference is `pending`. Reviewers may supply it later through
`--adjudication`, choosing `exact`, `acceptable`, `wrong`, or `pending`.
`exact` and `acceptable` receive full credit, `wrong` receives zero, and
`pending` is excluded. Until no decisions are pending, the adjudicated score is
explicitly not final. No automatic winner is declared.

The dimension table in the evaluator groups the frozen fields for presentation
only; it is checked at import time to cover every v1 field exactly once and
does not alter the contract or create weighting rules.

### Live execution plan (not executed here)

Phase 1 smoke: run 3 Golden Set videos with OpenAI and 3 with Gemini/Google.

Phase 2: if both smoke runs work, run all 18 videos with OpenAI and all 18 with
Gemini/Google. Run Gemini sequentially to remain within free-tier quotas and
rate limits.

Moonshot/Kimi and DeepSeek remain implemented but are not selected until a key
and credit are available. They require no key while unselected and can later be
evaluated against this exact same Golden Reference.

## Errors and adding a provider

Errors normalize to `authentication_error`, `rate_limited`, `timeout`,
`provider_unavailable`, `provider_http_error`, `refusal`, `empty_output`,
`invalid_json`, `schema_validation_failed`, and where applicable
`structured_output_unsupported`.  Observability records only provider, model,
latency, status, error code, exception class, attempts, and token counters.

To add a provider, implement the tiny `SemanticProvider.extract(payload)`
boundary under `app/llm/providers`, use `_common.RestSemanticProvider` for
timeout/retry/error safety, build the shared `semantic_messages`, normalize
only documented usage fields, register a canonical provider ID, and add
zero-network fixtures/tests.  Do not add a second semantic schema or relax
central `validate_semantic_output`.
