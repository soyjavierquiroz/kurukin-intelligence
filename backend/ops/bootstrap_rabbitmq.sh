#!/usr/bin/env bash
# Explicit opt-in only. Never emit a credential or alter unrelated namespaces.
set +x
set -euo pipefail
case "${1:-}" in --check|--create) mode="$1";; *) echo 'Usage: bootstrap_rabbitmq.sh --check|--create' >&2; exit 2;; esac
mapfile -t containers < <(docker ps --filter label=com.docker.swarm.service.name=rabbitmq_rabbit_mq --format '{{.ID}}')
[[ ${#containers[@]} == 1 && -n "${containers[0]}" ]] || { echo 'Expected one local running RabbitMQ container' >&2; exit 1; }
container="${containers[0]}"
docker exec "$container" rabbitmq-diagnostics ping
vhosts=$(docker exec "$container" rabbitmqctl list_vhosts name --no-table-headers)
users=$(docker exec "$container" rabbitmqctl list_users --no-table-headers)
has_vhost=false; has_user=false
if awk '$0 == "/kurukin-tiktok" {found=1} END {exit !found}' <<< "$vhosts"; then has_vhost=true; fi
if awk '$1 == "kurukin_tiktok" {found=1} END {exit !found}' <<< "$users"; then has_user=true; fi
printf 'vhost /kurukin-tiktok exists: %s\nuser kurukin_tiktok exists: %s\n' "$has_vhost" "$has_user"
[[ "$mode" == --create ]] || exit 0
[[ $EUID == 0 ]] || { echo 'Creation requires root' >&2; exit 1; }
if $has_user; then
    echo 'Dedicated user already exists; password and permissions unchanged. Inspect manually.'
    exit 0
fi
# Persist credential BEFORE mutations for recovery from partial bootstrap.
# Python sends password over stdin, never argv, stdout, env or exception text.
python3 - "$container" "$has_vhost" <<'PY'
import os, secrets, subprocess, sys
path = '/root/.kurukin-tiktok-rabbitmq-url'
container, has_vhost = sys.argv[1:]
try:
    password = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('amqp://kurukin_tiktok:' + password + '@rabbit_mq:5672/%2Fkurukin-tiktok\n')
        f.flush()
        os.fsync(f.fileno())
    def ctl(*args, data=None):
        subprocess.run(['docker', 'exec', '-i', container, 'rabbitmqctl', *args],
                       input=data, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    if has_vhost != 'true':
        ctl('add_vhost', '/kurukin-tiktok')
    # RabbitMQ prompts on stdin when password is omitted.
    ctl('add_user', 'kurukin_tiktok', data=password + '\n')
    ctl('set_permissions', '-p', '/kurukin-tiktok', 'kurukin_tiktok', '.*', '.*', '.*')
except Exception:
    print('Bootstrap incomplete; no credential output. Preserve root credential file and inspect dedicated namespace manually.', file=sys.stderr)
    sys.exit(1)
print('Dedicated namespace created; credential stored root:0600. No Docker secret created.')
PY
