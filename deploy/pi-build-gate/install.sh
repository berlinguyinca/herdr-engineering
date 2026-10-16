#!/usr/bin/env bash
# Install the pi build-gate extension into the per-user pi extension directory.
#
# The extension intercepts heavy build commands (`cargo check --workspace ...`,
# `tsc`, ...) issued by pi agents on THIS host and routes them through
# `herdr-eng build-coordinate`, so every pi session contends for a bounded set
# of build slots instead of each launching a full-workspace build at once.
#
# Usage:  ./deploy/pi-build-gate/install.sh
# Uninstall: remove ~/.pi/agent/extensions/build-gate.ts (or set
#            HERDR_BUILD_GATE_DISABLE=1 in the pi process env).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/build-gate.ts"
DEST_DIR="${HERDR_PI_EXT_DIR:-$HOME/.pi/agent/extensions}"
DEST="$DEST_DIR/build-gate.ts"

mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST"
chmod 644 "$DEST"
echo "Installed pi build-gate extension -> $DEST"

# Sanity: the gate binary must be on PATH for the extension to do anything.
if ! command -v herdr-eng >/dev/null 2>&1; then
  echo "WARN: 'herdr-eng' not found on PATH; the gate will no-op until it is."
fi

# Optional tuning (export in the pi process environment before starting pi):
#   HERDR_BUILD_GATE_DISABLE=1   disable gating entirely
#   HERDR_BUILD_GATE_SLOTS=N     max concurrent builds host-wide (default 2)
#   HERDR_BUILD_GATE_MAX_LOAD=F  back off while 1-min load/core exceeds (default 3.0)
#   HERDR_BUILD_GATE_WAIT=S      seconds to wait for a slot before running ungated (default 900)
echo "Done."
