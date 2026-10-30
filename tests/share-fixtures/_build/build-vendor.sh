#!/usr/bin/env bash
# Rebuild the vendored libraries of the parquet share fixtures from npm. Usage: build-vendor.sh <empty scratch dir>
# Writes html-parquet-hyparquet/vendor/hyparquet.mjs and html-parquet-duckdb/vendor/duckdb.mjs (the JS API only).
# The duckdb-wasm worker + wasm and the parquet extension (~40 MB) are NOT committed: scripts/share-browser-check.mjs
# downloads them at run time (pinned version and hashes there; keep DUCKDB/DUCKDB_CORE here in step with it).
set -euo pipefail
HYPARQUET=1.32.0
DUCKDB=1.33.1-dev57.0
DUCKDB_CORE=v1.5.4        # SELECT version() in this build; names the extension path (used by the harness)
ESBUILD=0.25.10
work=${1:?usage: build-vendor.sh <empty scratch dir>}
here=$(cd "$(dirname "$0")" && pwd)
fixtures=$(dirname "$here")
mkdir -p "$work"
cp "$here/hyparquet-entry.js" "$here/duckdb-entry.js" "$work/"
cd "$work"
[ -f package.json ] || npm init -y >/dev/null
npm install --ignore-scripts --no-audit --no-fund "hyparquet@$HYPARQUET" "@duckdb/duckdb-wasm@$DUCKDB" "esbuild@$ESBUILD" >/dev/null
npx esbuild hyparquet-entry.js --bundle --format=esm --platform=browser --minify --legal-comments=eof \
  --outfile="$fixtures/html-parquet-hyparquet/vendor/hyparquet.mjs"
npx esbuild duckdb-entry.js --bundle --format=esm --platform=browser --minify --legal-comments=eof \
  --outfile="$fixtures/html-parquet-duckdb/vendor/duckdb.mjs"
cp node_modules/hyparquet/LICENSE "$fixtures/html-parquet-hyparquet/vendor/LICENSE.hyparquet"
cp node_modules/apache-arrow/LICENSE.txt "$fixtures/html-parquet-duckdb/vendor/LICENSE.apache-arrow" 2>/dev/null || true
