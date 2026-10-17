#!/usr/bin/env bash
# Compatibility entry point: use the same guarded updater as herdr-eng fleet update.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HERDR_ENGINEERING_REPO="$REPO_ROOT"
UPDATE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --host) UPDATE_ARGS+=(--host "${2:?--host needs a value}"); shift 2 ;;
    --no-pull) UPDATE_ARGS+=(--no-pull); shift ;;
    --no-local-reinstall) UPDATE_ARGS+=(--no-reinstall); shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
UPDATE_PYTHON="${HERDR_ENGINEERING_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ -x "$UPDATE_PYTHON" ]; then
  exec "$UPDATE_PYTHON" -m herdr_engineering.cli fleet update "${UPDATE_ARGS[@]}"
fi
exec herdr-eng fleet update "${UPDATE_ARGS[@]}"
