#!/usr/bin/env bash
# Report whether the pi build gate is wired up and how busy it is. Read-only.
ok(){ printf '  [ ok ] %s\n' "$*"; }; bad(){ printf '  [FAIL] %s\n' "$*"; fail=1; }; note(){ printf '  [info] %s\n' "$*"; }
fail=0; PATH="$HOME/.local/bin:$PATH"
echo "build gate on $(hostname)"
if he=$(command -v herdr-eng); then ok "herdr-eng: $he"; else bad "herdr-eng not on PATH (gating is OFF)"; fi
ext="$HOME/.pi/agent/extensions/build-gate.ts"
[ -f "$ext" ] && ok "extension installed: $ext" || bad "extension missing: $ext"
if [ -n "${he:-}" ]; then
  echo "slots:"; "$he" build-coordinate --status 2>&1 | sed 's/^/    /'
fi
cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu); load=$(uptime | sed 's/.*averages*: *//' | awk -F'[ ,]+' '{print $1}')
note "cores=$cores 1-min load=$load (back-off above ${HERDR_BUILD_GATE_MAX_LOAD:-3.0} per core)"
env | grep '^HERDR_BUILD_GATE_' | sed 's/^/  [info] /'
[ "$fail" = 0 ] && echo "verdict: gating active for NEW pi sessions" || echo "verdict: gating NOT active (see FAIL lines)"
exit $fail
