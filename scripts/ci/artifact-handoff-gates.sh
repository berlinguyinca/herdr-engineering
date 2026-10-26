#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 -m venv .venv-artifact-ci
.venv-artifact-ci/bin/python -m pip install -q -e '.[dev]'
.venv-artifact-ci/bin/python -m pytest -q \
  tests/control_plane/test_handoff.py tests/test_handoff_dependency.py \
  tests/control_plane/test_artifacts_api.py tests/control_plane/test_artifact_versions.py \
  tests/control_plane/test_artifact_registry_migration.py \
  tests/control_plane/test_publication_migration.py tests/test_publication_gateway.py \
  tests/control_plane/test_scheduler_persistence.py tests/control_plane/test_rustfs_streaming.py
npm ci --no-audit --no-fund
node --check packages/app.js
node --test packages/handoffs-view.test.js packages/scheduler-view.test.js
