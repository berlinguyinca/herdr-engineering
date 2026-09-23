#!/usr/bin/env bash
# Dogfood automatic workspace naming (spec 0010).
# Derives a canonical semantic slug from a prompt/spec/issue/mission.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

PROMPT="${1:-}"
if [ -z "$PROMPT" ]; then
  echo "usage: $0 \"<initial prompt>\" [--repo owner/repo]" >&2
  exit 2
fi
REPO="${2:-}"
if [ -n "$REPO" ]; then
  herdr-eng name --prompt "$PROMPT" --repo "${REPO#--repo }"
else
  herdr-eng name --prompt "$PROMPT"
fi
