#!/usr/bin/env bash
set +x
set -euo pipefail
[[ $# == 0 ]] || { echo 'Usage: run_migrations.sh' >&2; exit 1; }
# Never print raw Docker/Alembic output. Accept only exact diagnostic tokens.
if containers_output=$(docker ps --filter label=com.docker.swarm.service.name=kurukin-tiktok_api --filter status=running --format '{{.ID}}' 2>/dev/null); then
    mapfile -t containers <<< "$containers_output"
else
    status=$?
    echo 'API container lookup failed.' >&2
    exit "$status"
fi
[[ ${#containers[@]} == 1 && -n ${containers[0]} ]] || { echo 'Expected exactly one running local kurukin-tiktok_api container.' >&2; exit 1; }
if output=$(docker exec -w /app "${containers[0]}" alembic upgrade head 2>&1); then
    echo 'Migration completed.'
else
    status=$?
    while IFS= read -r line; do
        case "$line" in
            'Migration error: '*)
                case "${line#Migration error: }" in
                    ValidationError|DatabaseConfigurationError|OperationalError|ProgrammingError|IntegrityError|DataError|InterfaceError|ImportError|ModuleNotFoundError|CommandError|RuntimeError|ValueError|TypeError|UnknownError) printf '%s\n' "$line" >&2 ;;
                esac ;;
            'Configuration field: '*)
                case "${line#Configuration field: }" in
                    database_url_file|database_url|whisper_concurrency|whisper_model|whisper_device|whisper_compute_type|whisper_cpu_threads|whisper_timeout_seconds|whisper_cache_dir|initial_enrichment_budget|min_transcribe_duration_seconds|auto_transcribe_max_duration_seconds|hard_transcribe_max_duration_seconds|high_value_outlier_threshold|enrichment_lease_seconds|max_audio_mb) printf '%s\n' "$line" >&2 ;;
                esac ;;
            'SQLSTATE: '*)
                case "${line#SQLSTATE: }" in
                    28P01|28000|3D000|42501|42P01|42P07|42710|23505|23503|08001|08006|42601) printf '%s\n' "$line" >&2 ;;
                esac ;;
        esac
    done <<< "$output"
    printf 'Migration failed (exit code %s); raw output withheld.\n' "$status" >&2
    exit "$status"
fi
