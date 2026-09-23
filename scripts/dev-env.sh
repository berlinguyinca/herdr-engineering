#!/usr/bin/env bash
# Set up a local Python dev environment for HerdR Engineering.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
echo "Dev environment ready. Activate with: source .venv/bin/activate"
