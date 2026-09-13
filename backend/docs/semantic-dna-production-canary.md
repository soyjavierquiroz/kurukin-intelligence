# Semantic DNA Phase B — production canary (prepared, not deployed)

## Decision and scope

Semantic DNA v1 is frozen. The MVP decision is to use OpenAI `gpt-5.6-luna`
as the reliable production fallback: the completed 18-video benchmark recorded
18/18 valid responses, 100% schema validity, 94.4% exact
`commercial_intent`, 88.9% CTA, and 83.3% `offer_integration`. This is
sufficient for the Semantic DNA MVP.

`content_role`, secondary roles/functions/emotions, and similarly weak
secondary dimensions remain experimental. They must not block the MVP or be
treated as production-quality ground truth. Gemini remains the lower-cost
development option when quota is available; it is not configured as an
automatic production fallback for this canary.

The OpenAI adapter uses the Responses API with strict JSON Schema for the
selected GPT-5 model family, followed by the repository's own contract
validation. The exact requested `gpt-5.6-luna` model ID is preserved. Official
OpenAI model documentation lists [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
as a cost-sensitive, high-volume model available through the Responses API.

## Production configuration prepared in `stack.yml`

Only the `api` task receives the Semantic DNA configuration because the canary
executes inside that task. The Whisper worker neither invokes Semantic DNA nor
needs the OpenAI secret.

```text
VIRAL_DNA_SEMANTIC_PROVIDER=openai
VIRAL_DNA_SEMANTIC_MODEL=gpt-5.6-luna
OPENAI_API_KEY_FILE=/run/secrets/kurukin_tiktok_openai_api_key
```

The required external Swarm secret is named
`kurukin_tiktok_openai_api_key_v1`. It is mounted read-only as
`/run/secrets/kurukin_tiktok_openai_api_key`. Its contents are the raw OpenAI
API key only, with no `OPENAI_API_KEY=` prefix and no JSON. `OPENAI_API_KEY`
is still accepted locally for development, but production must use the mounted
file; the key is never committed to Git or written in `stack.yml`.

After authorization, create the secret without placing the value on the
command line or in shell history:

```bash
read -r -s -p 'OpenAI API key: ' openai_key; printf '\n'
printf '%s' "$openai_key" | docker secret create kurukin_tiktok_openai_api_key_v1 -
unset openai_key
```

Do not run that now. If the secret name already exists, `docker secret create`
fails safely; inspect its name/version and obtain an explicit rotation plan
instead of deleting or overwriting it.

## Closed production canary command

After the checklist below has succeeded, run from the deployed API task:

```bash
api_container=$(docker ps --filter label=com.docker.swarm.service.name=kurukin-tiktok_api --filter status=running --format '{{.ID}}')
docker exec "$api_container" python /app/ops/semantic_viral_dna_canary.py --execute
```

The script has no `--video-id`, `--limit`, batch, discovery, ingestion, or
transcript-reprocessing option. Its immutable set is exactly:

```text
3073174b-3919-457c-9dab-74d1e0316225
87f2fd92-b2e6-434b-8a25-9cdbcf275849
f141413b-7423-4d76-b309-04c9c44b9e5f
```

Before an OpenAI extraction can occur, it requires Alembic revision
`0007_semantic_viral_dna`, the exact OpenAI/model route with no fallback, and
an existing global `Video` plus global `Transcript` for each UUID. It reads
only `Video` and `Transcript`; it does not query `Analysis`, snapshots,
performance, acquisition, ingestion, queues, or Whisper.

The core hashes the contract/prompt/provider/model/language/caption/transcript
input. Repeating the command after successful completion reports `unchanged`
for the same records and makes zero provider calls. The explicit `--execute`
guard prevents an accidental invocation while still leaving normal help and
argument validation side-effect-free.

## Pre-canary checklist — do not execute yet

1. Apply Alembic migration `0007_semantic_viral_dna`.
2. Configure `VIRAL_DNA_SEMANTIC_PROVIDER=openai` and `VIRAL_DNA_SEMANTIC_MODEL=gpt-5.6-luna`.
3. Create and attach the `kurukin_tiktok_openai_api_key_v1` OpenAI secret.
4. Build and deploy the backend API image/task containing this script and configuration; deploy the Whisper worker only if its image also needs updating, not for Semantic DNA execution.
5. Run the closed three-video canary command above.
6. Inspect the three `viral_dna` rows: `semantic_status=completed`, `semantic_model=openai:gpt-5.6-luna`, a 64-character `semantic_input_sha256`, prompt version, extraction timestamp, and valid semantic columns.
7. Repeat the exact canary command and confirm three `unchanged` results (idempotence, zero provider calls).
8. Only if all checks pass, run a separately reviewed incremental remainder process.

None of these production steps, secret creation, migrations, deployment, or
provider calls has been performed by this preparation change.
