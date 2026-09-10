#!/usr/bin/env bash
# Never enable tracing: stdin contains the new credential.
set +x
set -euo pipefail
umask 077
fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ $# == 1 && ( $1 == --check || $1 == --create ) ]] || fail 'Usage: bootstrap_database.sh --check|--create'
[[ $EUID == 0 ]] || fail 'Run as root on the PostgreSQL task node.'
mapfile -t containers < <(docker ps --filter label=com.docker.swarm.service.name=postgres_postgres --filter status=running --format '{{.ID}}')
[[ ${#containers[@]} == 1 ]] || fail 'Expected exactly one running local postgres_postgres container.'
container=${containers[0]}
psql_admin() {
    docker exec -i "$container" sh -c '
        export PGPASSWORD="$POSTGRES_PASSWORD"
        exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"
    ' 2>/dev/null
}
state() {
    psql_admin <<'SQL'
SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='kurukin_tiktok');
SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname='kurukin_tiktok');
SELECT COALESCE((SELECT pg_get_userbyid(datdba)='kurukin_tiktok' FROM pg_database WHERE datname='kurukin_tiktok'), false);
SQL
}
if [[ $1 == --create ]]; then
    # Serialize invocations on this single-node host; do not modify PostgreSQL service.
    exec 9>/root/.kurukin-tiktok-bootstrap.lock
    flock -x 9
fi
result=$(state) || fail 'Could not inspect PostgreSQL state.'
mapfile -t status <<< "$result"
[[ ${#status[@]} == 3 ]] || fail 'Unexpected PostgreSQL state.'
if [[ $1 == --check ]]; then
    [[ ${status[0]} == t ]] && echo 'role kurukin_tiktok: yes' || echo 'role kurukin_tiktok: no'
    [[ ${status[1]} == t ]] && echo 'database kurukin_tiktok: yes' || echo 'database kurukin_tiktok: no'
    [[ ${status[2]} == t ]] && echo 'database owner kurukin_tiktok: yes' || echo 'database owner kurukin_tiktok: no'
    exit 0
fi
credential=/root/.kurukin-tiktok-db-url
password=''
if [[ ${status[0]} != t ]]; then
    if [[ -e $credential || -L $credential ]]; then
        # Recover a previous interrupted creation, without overwriting its password.
        [[ -f $credential && ! -L $credential && $(stat -c '%u:%a:%h' "$credential") == 0:600:1 ]] || fail 'Unsafe credential file.'
        value=$(<"$credential")
        [[ $value =~ ^postgresql\+psycopg://kurukin_tiktok:([a-f0-9]{64})@postgres:5432/kurukin_tiktok$ ]] || fail 'Invalid existing credential file.'
        password=${BASH_REMATCH[1]}
    else
        password=$(openssl rand -hex 32) || fail 'Password generation failed.'
        # noclobber prevents accidental overwrite; umask enforces 0600.
        (set -o noclobber; printf 'postgresql+psycopg://kurukin_tiktok:%s@postgres:5432/kurukin_tiktok\n' "$password" > "$credential") || fail 'Cannot create credential file.'
    fi
fi
# Do not rotate or ALTER an existing role. Only the dedicated DB may change owner.
# Session-local logging controls prevent SQL/password disclosure in server logs.
# Built-in PostgreSQL password statement masking also applies. External audit plugins
# require separate review before this script is authorized for execution.
{
    cat <<'SQL'
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SET log_min_messages = 'panic';
SET log_duration = off;
SET log_min_duration_statement = -1;
SET log_min_duration_sample = -1;
SELECT pg_advisory_lock(742819502);
SQL
    if [[ -n $password ]]; then
        printf "SELECT format('CREATE ROLE kurukin_tiktok LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %%L', '%s') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='kurukin_tiktok')\n\\gexec\n" "$password"
    fi
    cat <<'SQL'
SELECT 'CREATE DATABASE kurukin_tiktok OWNER kurukin_tiktok' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='kurukin_tiktok')
\gexec
SELECT 'ALTER DATABASE kurukin_tiktok OWNER TO kurukin_tiktok' WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname='kurukin_tiktok' AND pg_get_userbyid(datdba)<>'kurukin_tiktok')
\gexec
SQL
} | psql_admin >/dev/null || fail 'Bootstrap failed; preserve any credential file and rerun --check before retry.'
unset password
[[ $(state) == $'t\nt\nt' ]] || fail 'Bootstrap verification failed.'
echo 'role/database kurukin_tiktok: ready'
if [[ -f $credential ]]; then
    printf 'Credential written to:\n%s\n' "$credential"
else
    echo 'Existing role preserved; password not changed. Use the existing Docker secret.'
fi
