#!/usr/bin/env bash
#
# monitor-fabric-gateway.sh — report the health of the dev-fabric gateway and
# the per-host build-gate. Run on demand or on a schedule (systemd timer /
# cron) to confirm the two build-storm fixes are holding:
#
#   1. Gateway lease store stays small (the 2026-10-16 fix pruned 98% expired
#      leases and batched persistence; the store was 3.8MB/6.5k leases and the
#      gateway CPU was ~222%, now ~165KB/~400 leases and near-idle).
#   2. Build-gate slots exist and are being used by pi agents (bounded
#      concurrent builds instead of a 27-agent "build storm").
#
# Usage:
#   scripts/monitor-fabric-gateway.sh            # human-readable report
#   scripts/monitor-fabric-gateway.sh --json     # one-line JSON (for cron/log)
#   scripts/monitor-fabric-gateway.sh --alerts   # exit nonzero on a degraded state
#
# Degraded states (--alerts): gateway container not healthy, lease store > 1MB,
# > 90% of leases expired, or 1-min load/core > 3.0.
set -u

JSON=0; ALERTS=0
for a in "$@"; do
  case "$a" in --json) JSON=1;; --alerts) ALERTS=1;; esac
done

STATE_DIR="${HERDR_STATE_DIR:-$HOME/.local/share/herdr-engineering/state}"
CONTAINER="${HERDR_GATEWAY_CONTAINER:-herdr-fabric-gateway}"
STORE_IN_CONTAINER="/state/leases.json"

# --- gather raw facts ------------------------------------------------------
CONTAINER_STATUS="n/a"; HEALTHY=0; STORE_BYTES=0; TOTAL=0; ACTIVE=0; EXPIRED=0
GATEWAY_CPU="n/a"; LOADCORE="n/a"; GATE_SLOTS="n/a"; GATE_USED=0

if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
  CONTAINER_STATUS=$(docker inspect -f '{{.State.Status}} ({{.State.Health.Status}})' "$CONTAINER" 2>/dev/null)
  case "$CONTAINER_STATUS" in *'(healthy)'*) HEALTHY=1;; esac   # not *healthy*: that also matches '(unhealthy)'
  # CPU% = average over 1s of the container's cpu.usage deltas / host cores.
  GATEWAY_CPU=$(docker stats --no-stream --format '{{.CPUPerc}}' "$CONTAINER" 2>/dev/null)
  if docker exec "$CONTAINER" sh -c "test -f $STORE_IN_CONTAINER" >/dev/null 2>&1; then
    STORE_BYTES=$(docker exec "$CONTAINER" sh -c "wc -c < $STORE_IN_CONTAINER" 2>/dev/null | tr -d '[:space:]')
    read TOTAL ACTIVE EXPIRED <<< "$(docker exec "$CONTAINER" python3 -c "
import json,sys
d=json.load(open('$STORE_IN_CONTAINER'))
items = d.get('leases', d) if isinstance(d,dict) else d
if isinstance(items,dict): items=list(items.values())
now=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)
tot=len(items); act=0; exp=0
for it in items:
    e=it.get('expires_at') or it.get('expire_at') or it.get('expires')
    try:
        if __import__('datetime').datetime.fromisoformat(str(e).replace('Z','+00:00'))>now: act+=1
        else: exp+=1
    except Exception: act+=1
print(tot, act, exp)
" 2>/dev/null)"
  fi
fi

# host load (1-min) normalized per core
NCPU=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
if [ -r /proc/loadavg ]; then
  L1=$(awk '{print $1}' /proc/loadavg)
  LOADCORE=$(awk -v l="$L1" -v n="$NCPU" 'BEGIN{printf "%.2f", l/n}')
fi

# build-gate slots
GATE_SLOTS=$(ls "$STATE_DIR/build-gates"/gate.* 2>/dev/null | wc -l)
GATE_USED=$(find "$STATE_DIR/build-gates" -name 'gate.*' -mmin -5 2>/dev/null | wc -l)
# build-gate contention telemetry (recorded by herdr-eng build-coordinate)
TELE="$STATE_DIR/build-gates/telemetry.jsonl"
GATE_CONT_1H=0; GATE_CONT_24H=0
if [ -f "$TELE" ]; then
  # count only contention events whose "ts" (UTC ISO, to the second) falls inside each window
  CUTOFF_1H=$(date -u -d '-1 hour' +%FT%T 2>/dev/null)
  CUTOFF_24H=$(date -u -d '-24 hours' +%FT%T 2>/dev/null)
  if [ -n "$CUTOFF_1H" ] && [ -n "$CUTOFF_24H" ]; then
    read -r GATE_CONT_1H GATE_CONT_24H <<< "$(awk -v c1="$CUTOFF_1H" -v c24="$CUTOFF_24H" '
      /"event": *"contention"/ && match($0, /"ts": *"[^"]+"/) {
        ts = substr($0, RSTART, RLENGTH); sub(/^"ts": *"/, "", ts); ts = substr(ts, 1, 19)
        if (ts > c24) n24++
        if (ts > c1) n1++
      }
      END { print n1 + 0, n24 + 0 }' "$TELE" 2>/dev/null)"
    GATE_CONT_1H=${GATE_CONT_1H:-0}; GATE_CONT_24H=${GATE_CONT_24H:-0}
  fi
fi

# --- evaluate --------------------------------------------------------------
DEGRADED=0; REASONS=()
[ "$HEALTHY" -eq 0 ] && { DEGRADED=1; REASONS+=("gateway-not-healthy"); }
[ "${STORE_BYTES:-0}" -gt 1048576 ] && { DEGRADED=1; REASONS+=("store>1MB"); }
if [ "${TOTAL:-0}" -gt 0 ]; then
  P_EXPIRED=$(awk -v e="${EXPIRED:-0}" -v t="$TOTAL" 'BEGIN{printf "%.0f", e*100/t}')
  [ "$P_EXPIRED" -gt 90 ] && { DEGRADED=1; REASONS+=("expired>90%"); }
fi
if [ -n "$LOADCORE" ] && [ "$LOADCORE" != "n/a" ]; then
  awk -v l="$LOADCORE" 'BEGIN{exit !(l>3.0)}' && { DEGRADED=1; REASONS+=("load/core>3.0"); }
fi

# --- output ----------------------------------------------------------------
if [ "$JSON" -eq 1 ]; then
  python3 -c "import json;print(json.dumps({
    'ts':'$(date -u +%FT%TZ)','container_status':'$CONTAINER_STATUS',
    'gateway_cpu':'$GATEWAY_CPU','store_bytes':${STORE_BYTES:-0},
    'leases_total':${TOTAL:-0},'leases_active':${ACTIVE:-0},'leases_expired':${EXPIRED:-0},
    'load_core':'$LOADCORE','gate_slots':${GATE_SLOTS:-0},'gate_used_5m':${GATE_USED:-0},
    'gate_contention_1h':${GATE_CONT_1H:-0},'gate_contention_24h':${GATE_CONT_24H:-0},
    'degraded':$DEGRADED,'reasons':[$(printf '%s,' "${REASONS[@]}" | sed 's/,$//;s/[^,]*/\"&\"/g')]
  }))"
else
  echo "dev-fabric gateway monitor  (ts=$(date -u +%FT%TZ))"
  echo "  container          : $CONTAINER_STATUS"
  echo "  gateway cpu (1s)   : $GATEWAY_CPU"
  echo "  lease store        : ${STORE_BYTES} bytes ($TOTAL leases: $ACTIVE active, $EXPIRED expired)"
  echo "  host load/core     : $LOADCORE  (nproc=$NCPU)"
  echo "  build-gate         : $GATE_SLOTS slot file(s), $GATE_USED used in last 5m,"
  echo "                       $GATE_CONT_1H contentions in last 1h ($GATE_CONT_24H in 24h)"
  if [ "$DEGRADED" -eq 1 ]; then
    echo "  STATUS             : DEGRADED (${REASONS[*]})"
  else
    echo "  STATUS             : OK"
  fi
fi
exit "$DEGRADED"
