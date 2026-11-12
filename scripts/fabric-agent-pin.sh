#!/usr/bin/env bash
#
# Pin the fabric agent's PYTHONPATH to the release this script runs from.
#
# Why: the unit's services-source.conf drop-in was hand-maintained, so `fleet update` staged
# new releases while the agent kept running an old one (2026-11-10: thousands of HTTP 401s on
# beast and bender). Usage: fabric-agent-pin.sh <drop-in-dir>. A no-op unless this checkout is an
# immutable release under .../herdr/releases/<sha>; developer checkouts must not be pinned.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DROPIN_DIR="${1:?usage: fabric-agent-pin.sh <drop-in-dir>}"

case "$REPO_ROOT" in
  */herdr/releases/*) ;;
  *) echo "  not a managed release; drop-in left alone"; exit 0 ;;
esac

mkdir -p "$DROPIN_DIR"
DROPIN="$DROPIN_DIR/services-source.conf"
printf '[Service]\nEnvironment="PYTHONPATH=%s"\n' "$REPO_ROOT" > "$DROPIN.tmp"
mv "$DROPIN.tmp" "$DROPIN"
echo "  pinned agent to release $(basename "$REPO_ROOT")"
