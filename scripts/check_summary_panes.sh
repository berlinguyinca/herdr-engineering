#!/usr/bin/env bash
# Real-pane check of the /summary panes. CI has no Herdr, so this is run by hand from inside a Herdr pane:
#
#   scripts/check_summary_panes.sh
#
# It publishes a fixture report (with a drift-graph block, from a fixture Claude session in a throwaway HOME)
# next to the pane it runs in, resizes the panes through three sizes, and fails if a line the viewers drew is
# wider than its pane. Then it sends q to the summary pane and checks that both panes are gone. Nothing is
# posted to the dashboard and the caller's own pane is never typed into.
set -euo pipefail

[ -n "${HERDR_PANE_ID:-}" ] || { echo "run this inside a Herdr pane (HERDR_PANE_ID is not set)" >&2; exit 2; }
root=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
summary="" graph=""

cleanup() {
  for p in "$summary" "$graph"; do
    [ -n "$p" ] && herdr pane close "$p" >/dev/null 2>&1 || true
  done
  rm -rf -- "$tmp"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
gone() { ! herdr pane get "$1" >/dev/null 2>&1; }

# -- precondition: the server must really resize pane terminals ---------------------------------------
# A Herdr server with no attached client can report a split layout while every pane's terminal keeps the full
# size; the viewers would then (correctly) draw at that size and the checks below would measure nothing.
probe=$(herdr pane split --pane "$HERDR_PANE_ID" --direction right --no-focus | json "d['result']['pane']['pane_id']")
herdr pane run "$probe" "stty size > $tmp/probe.size" >/dev/null
for _ in $(seq 1 20); do [ -s "$tmp/probe.size" ] && break; sleep 0.25; done
probe_cols=$(awk '{print $2}' "$tmp/probe.size" 2>/dev/null || true)
probe_width=$(herdr pane layout --pane "$probe" | json "next(p['rect']['width'] for p in d['result']['layout']['panes'] if p['pane_id'] == '$probe')")
herdr pane close "$probe" >/dev/null 2>&1 || true
if [ "$probe_cols" != "$probe_width" ]; then
  echo "SKIP: Herdr lays a new pane out $probe_width columns wide but its terminal has ${probe_cols:-?}; panes are not" >&2
  echo "      resized here (is a client attached?). Run this from an attached Herdr client." >&2
  exit 3
fi

# -- fixture: eight prompts an hour apart, two topics -------------------------------------------------
home="$tmp/home"
session="$home/.claude/projects/-fixture/fixture-1.jsonl"
mkdir -p "$(dirname "$session")"
python3 - "$session" <<'PY'
import json, sys
topics = ["make a usb stick"] * 4 + ["design a web page"] * 4
with open(sys.argv[1], "w") as f:
    for i, topic in enumerate(topics, 1):
        row = {"type": "user", "timestamp": f"2026-01-01T{8 + i:02d}:00:00Z", "cwd": "/tmp",
               "message": {"content": f"{topic}, step {i}"}}
        f.write(json.dumps(row) + "\n")
PY

report="$tmp/report.md"
cat > "$report" <<'MD'
# Session summary
**Session:** fixture · **Repository:** unknown · **Branch/worktree:** unknown
**Overall status:** in progress

1. **Original objective** — make a USB stick, with enough words on this line that it has to wrap when the pane is narrow.

Evidence coverage: complete

```drift-graph
{"segments": [{"label": "Make a USB stick", "from_prompt": 1, "to_prompt": 4},
              {"label": "Design a web page", "from_prompt": 5, "to_prompt": 8}],
 "drift_at": 5, "drift_note": "A web page is its own project and should have been a new session."}
```
MD

# -- publish from this pane ---------------------------------------------------------------------------
out=$(HOME="$home" PYTHONPATH="$root" python3 -m herdr_engineering.cli summary publish \
        --agent claude --session fixture-1 --no-dashboard < "$report")
summary=$(printf '%s' "$out" | json "d['pane'].get('pane_id', '')")
graph=$(printf '%s' "$out" | json "d['graph'].get('pane_id', '')")
[ -n "$summary" ] || fail "no summary pane: $out"
[ -n "$graph" ] || fail "no graph pane: $out"
echo "summary pane $summary, graph pane $graph"

wait_for() {  # pane, text
  herdr pane wait-output "$1" --match "$2" --source visible --timeout 20000 >/dev/null 2>&1 \
    || fail "pane $1 never showed '$2'"
}
wait_for "$summary" "q closes this pane"
wait_for "$graph" "q closes this pane"
wait_for "$graph" "Make a USB"

width_of() { herdr pane layout --pane "$1" | json "next(p['rect']['width'] for p in d['result']['layout']['panes'] if p['pane_id'] == '$1')"; }
height_of() { herdr pane layout --pane "$1" | json "next(p['rect']['height'] for p in d['result']['layout']['panes'] if p['pane_id'] == '$1')"; }

check_fits() {  # pane, label
  local width text
  width=$(width_of "$1")
  text=$(herdr pane read "$1" --source visible)
  printf '%s\n' "$text" | python3 -c '
import sys
width, label = int(sys.argv[1]), sys.argv[2]
bad = [l for l in sys.stdin.read().split("\n") if len(l.rstrip()) > width]
if bad:
    sys.exit(f"FAIL: {label}: {len(bad)} line(s) wider than {width} columns, e.g. {bad[0]!r}")
print(f"ok: {label} fits {width} columns")
' "$width" "$2" || exit 1
}

# the graph pane must be the smaller one (the split ratio is a guess about Herdr's meaning of --ratio)
sh=$(height_of "$summary"); gh=$(height_of "$graph")
[ "$gh" -lt "$sh" ] || fail "graph pane ($gh rows) is not shorter than the summary pane ($sh rows): flip GRAPH_RATIO"
echo "ok: graph pane $gh rows under summary pane $sh rows"

prev=$(width_of "$summary")
check_fits "$summary" "summary at $prev"; check_fits "$graph" "graph at $prev"

resize_and_check() {  # direction, amount
  herdr pane resize --pane "$summary" --direction "$1" --amount "$2" >/dev/null
  sleep 1.5                                      # the viewer redraws on SIGWINCH
  local now; now=$(width_of "$summary")
  [ "$now" != "$prev" ] || fail "resize $1 $2 did not change the width ($now)"
  prev=$now
  check_fits "$summary" "summary at $now"; check_fits "$graph" "graph at $now"
}
resize_and_check left 0.25
resize_and_check right 0.45
resize_and_check right 0.2

# -- quitting one closes both ---------------------------------------------------------------------------
herdr pane send-keys "$summary" q >/dev/null
for _ in $(seq 1 20); do
  gone "$summary" && gone "$graph" && { echo "both panes closed"; summary="" graph=""; exit 0; }
  sleep 0.5
done
fail "after q, summary gone=$(gone "$summary" && echo yes || echo no) graph gone=$(gone "$graph" && echo yes || echo no)"
