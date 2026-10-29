#!/usr/bin/env bash
# Isolated dev stack for the unified-workspace program: Postgres + MinIO (S3) + control-plane
# web with HCN collab enabled (static-token auth). Nothing here touches the production
# compose project (herdr-control-plane): own container names (herdr-uw-*), own loopback
# ports, own state dir, throwaway credentials.
#
#   scripts/dev/uw-stack.sh up       start pg + s3 + web, migrate, wait healthy
#   scripts/dev/uw-stack.sh down     stop web + containers (data volumes kept; --purge removes them)
#   scripts/dev/uw-stack.sh status   what is running, ports, health
#   scripts/dev/uw-stack.sh env      shell exports (eval "$(scripts/dev/uw-stack.sh env)")
#
# Ports are below the kernel ephemeral range (32768+) so outbound sockets cannot collide (127.0.0.1 only; override with UW_PG_PORT UW_S3_PORT UW_S3_CONSOLE_PORT UW_WEB_PORT):
#   pg 31433   s3 31900   s3-console 31901   web+collab 31080
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STATE="${UW_STACK_DIR:-$HOME/.local/state/herdr-uw-stack}"
PG_PORT="${UW_PG_PORT:-31433}"
S3_PORT="${UW_S3_PORT:-31900}"
CON_PORT="${UW_S3_CONSOLE_PORT:-31901}"
WEB_PORT="${UW_WEB_PORT:-31080}"
PG_C=herdr-uw-pg
S3_C=herdr-uw-s3
PG_IMG=postgres:16-alpine
S3_IMG="${UW_S3_IMAGE:-quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z}"
PY="${UW_PYTHON:-python3}"
LABEL=herdr.uw-stack=1

secret() { # create-once random secret file (0600)
  local f="$STATE/$1"
  [ -s "$f" ] || { umask 077; head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$f"; }
  cat "$f"
}

port_free() { ! ss -ltn "sport = :$1" | grep -q LISTEN; }

owned() { docker inspect -f '{{index .Config.Labels "herdr.uw-stack"}}' "$1" 2>/dev/null | grep -qx 1; }

dsn() { echo "postgresql://uw:$(secret pg-password)@127.0.0.1:$PG_PORT/uw"; }

write_config() {
  mkdir -p "$STATE/fleet"; chmod 700 "$STATE" "$STATE/fleet"
  local tok; tok="$(secret collab-token)"
  printf '{"uw-dev-user": "%s"}\n' "$(printf %s "$tok" | sha256sum | cut -d' ' -f1)" > "$STATE/collab-users.json"
  chmod 600 "$STATE/collab-users.json"
  DSN="$(dsn)" S3PW="$(secret s3-password)" S3PORT="$S3_PORT" OUT="$STATE/fleet/fleet-config.yaml" "$PY" - <<'PYEOF'
import os, yaml
cfg = {"version": 1, "hosts": {}, "sections": {"control_plane": {"roles": ["control-plane"], "values": {
    "pg_dsn": os.environ["DSN"], "host_aliases": {}, "api_keys": {},
    "rustfs_endpoint": f"http://127.0.0.1:{os.environ['S3PORT']}",
    "minio_user": "uwdev", "minio_password": os.environ["S3PW"],
    "rustfs_bucket": "uw-artifacts", "ci_actions": False}}}}
fd = os.open(os.environ["OUT"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(cfg, f)
PYEOF
}

up() {
  mkdir -p "$STATE"; chmod 700 "$STATE"
  if ! owned "$PG_C" && ! port_free "$PG_PORT"; then echo "port $PG_PORT busy" >&2; exit 1; fi
  if ! owned "$S3_C" && ! port_free "$S3_PORT"; then echo "port $S3_PORT busy" >&2; exit 1; fi
  if ! pid_alive && ! port_free "$WEB_PORT"; then echo "port $WEB_PORT busy" >&2; exit 1; fi
  if ! owned "$PG_C"; then
    docker rm -f "$PG_C" >/dev/null 2>&1 || true
    docker run -d --name "$PG_C" --label "$LABEL" --restart no -p "127.0.0.1:$PG_PORT:5432" \
      -e POSTGRES_USER=uw -e POSTGRES_DB=uw -e "POSTGRES_PASSWORD=$(secret pg-password)" \
      -v herdr-uw-pg-data:/var/lib/postgresql/data "$PG_IMG" >/dev/null
  else docker start "$PG_C" >/dev/null; fi
  if ! owned "$S3_C"; then
    docker rm -f "$S3_C" >/dev/null 2>&1 || true
    docker run -d --name "$S3_C" --label "$LABEL" --restart no \
      -p "127.0.0.1:$S3_PORT:9000" -p "127.0.0.1:$CON_PORT:9001" \
      -e MINIO_ROOT_USER=uwdev -e "MINIO_ROOT_PASSWORD=$(secret s3-password)" \
      -v herdr-uw-s3-data:/data "$S3_IMG" server /data --console-address :9001 >/dev/null
  else docker start "$S3_C" >/dev/null; fi
  for _ in $(seq 60); do docker exec "$PG_C" pg_isready -U uw -d uw >/dev/null 2>&1 && break; sleep 1; done
  docker exec "$PG_C" pg_isready -U uw -d uw >/dev/null || { echo "postgres not ready" >&2; exit 1; }
  write_config
  (cd "$ROOT" && "$PY" -m herdr_engineering.cli control-plane migrate --fleet-config "$STATE/fleet/fleet-config.yaml" >"$STATE/migrate.log" 2>&1) \
    || { echo "migrate failed; see $STATE/migrate.log" >&2; exit 1; }
  if ! pid_alive; then
    cd "$ROOT"
    nohup "$PY" -m herdr_engineering.cli control-plane web --host 127.0.0.1 --port "$WEB_PORT" \
      --fleet-config "$STATE/fleet/fleet-config.yaml" --collab-users-file "$STATE/collab-users.json" \
      --web-root "$ROOT/packages" --system-graph >"$STATE/web.log" 2>&1 < /dev/null &
    echo $! >"$STATE/web.pid"
  fi
  for _ in $(seq 60); do curl -fsS -m 3 "http://127.0.0.1:$WEB_PORT/api/v1/health" >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS -m 3 "http://127.0.0.1:$WEB_PORT/api/v1/health" >/dev/null || { echo "web not healthy; see $STATE/web.log" >&2; exit 1; }
  status
}

pid_alive() { [ -s "$STATE/web.pid" ] && kill -0 "$(cat "$STATE/web.pid")" 2>/dev/null \
  && tr '\0' ' ' < "/proc/$(cat "$STATE/web.pid")/cmdline" | grep -q "control-plane web"; }

down() {
  if pid_alive; then kill "$(cat "$STATE/web.pid")"; sleep 1; fi
  rm -f "$STATE/web.pid"
  for c in "$S3_C" "$PG_C"; do owned "$c" && docker rm -f "$c" >/dev/null; done
  if [ "${1:-}" = "--purge" ]; then
    docker volume rm herdr-uw-pg-data herdr-uw-s3-data >/dev/null 2>&1 || true
    echo "volumes purged"
  fi
  echo "down (containers removed; volumes herdr-uw-pg-data / herdr-uw-s3-data kept unless --purge)"
}

status() {
  echo "state dir: $STATE"
  for c in "$PG_C" "$S3_C"; do
    printf '%-14s %s\n' "$c" "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo absent)"
  done
  if pid_alive; then echo "web pid $(cat "$STATE/web.pid") on 127.0.0.1:$WEB_PORT"; else echo "web not running"; fi
  curl -fsS -m 3 "http://127.0.0.1:$WEB_PORT/api/v1/health" 2>/dev/null \
    | "$PY" -c 'import json,sys;d=json.load(sys.stdin);print("health",d["status"],"source="+d["source"],"rustfs="+d["rustfs"],"caps="+",".join(d["capabilities"]),"commit="+str(d["commit"]))' \
    2>/dev/null || echo "health: unreachable"
  echo "ports: pg $PG_PORT, s3 $S3_PORT, s3-console $CON_PORT, web $WEB_PORT (127.0.0.1)"
}

envp() {
  echo "export HERDR_E2E_PG_DSN='$(dsn)'"
  echo "export UW_CONTROL_PLANE_URL='http://127.0.0.1:$WEB_PORT'"
  echo "export UW_COLLAB_TOKEN='$(secret collab-token)'"
  echo "export UW_S3_ENDPOINT='http://127.0.0.1:$S3_PORT'"
}

case "${1:-}" in
  up) up ;;
  down) shift; down "$@" ;;
  status) status ;;
  env) mkdir -p "$STATE"; chmod 700 "$STATE"; envp ;;
  *) sed -n 2,13p "$0"; exit 2 ;;
esac
