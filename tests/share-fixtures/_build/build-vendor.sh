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
# Licences of everything inside the two bundles (esbuild --metafile: hyparquet; duckdb-wasm, apache-arrow, flatbuffers,
# tslib). duckdb-wasm's npm package ships no LICENSE file: take it from the v1.33.0 tag's commit (pinned).
cp node_modules/hyparquet/LICENSE "$fixtures/html-parquet-hyparquet/vendor/LICENSE.hyparquet"
duckdb_vendor="$fixtures/html-parquet-duckdb/vendor"
curl -sSfL -o "$duckdb_vendor/LICENSE.duckdb-wasm" \
  "https://raw.githubusercontent.com/duckdb/duckdb-wasm/fa1d47b38ed0821cecab0bdc331c48abd0f2cc65/LICENSE"
cp node_modules/apache-arrow/LICENSE.txt "$duckdb_vendor/LICENSE.apache-arrow"
cp node_modules/flatbuffers/LICENSE "$duckdb_vendor/LICENSE.flatbuffers"
cp node_modules/tslib/LICENSE.txt "$duckdb_vendor/LICENSE.tslib"
# Record what was built (paste into ../README.md, "Vendored files").
for pkg in hyparquet @duckdb/duckdb-wasm apache-arrow flatbuffers tslib; do
  node -e "const p=require('./node_modules/$pkg/package.json');console.log(p.name+'@'+p.version+' '+p.license)"
done
sha256sum "$fixtures/html-parquet-hyparquet/vendor/hyparquet.mjs" "$duckdb_vendor/duckdb.mjs"
