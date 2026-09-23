#!/usr/bin/env bash
# Reproducible validation command for the HerdR Engineering integration layer.
# Mirrors the CI unit-test job locally (spec 0010/0180).
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

echo "== ruff =="
ruff check herdr_engineering tests

echo "== spec manifest =="
python -c "import yaml; d=yaml.safe_load(open('SPEC_MANIFEST.yaml')); assert d['version']==1; print('ok')"

echo "== config =="
python -c "from herdr_engineering.config import load_config; load_config(); print('ok')"

echo "== lock =="
python -c "import yaml; d=yaml.safe_load(open('lock/upstreams.yaml')); assert 'dependencies' in d; print('ok')"

echo "== pytest =="
python -m pytest -q

echo "== doctor (json) =="
herdr-eng doctor --json | python -c "import sys,json; d=json.load(sys.stdin); print('overall:', d['overall'])"

echo "VALIDATION PASSED"
