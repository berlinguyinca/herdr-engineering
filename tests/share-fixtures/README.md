# Share-link browser fixtures

Self-contained artifact bundles for the share-link browser harness (`scripts/share-browser-check.mjs`). Each
directory is uploaded as-is by the harness's launcher (`scripts/share_browser_server.py`) into a temp shares root
and served by the real `make_share_server`. Nothing here loads from the network.

| Bundle | Entry | What it proves |
| --- | --- | --- |
| `pdf-only/` | `report.pdf` | PDF served inline with the PDF CSP and displayed by the browser's viewer |
| `html-parquet-hyparquet/` | `index.html` | hyparquet reads `data/table.parquet` over range requests inside the sandbox |
| `html-parquet-duckdb/` | `index.html` | duckdb-wasm (eh build) queries the same file over range requests inside the sandbox |

Both HTML bundles start with the same inline probe: it records `securitypolicyviolation` events, tries
`sessionStorage`/`localStorage` (must throw `SecurityError`), and loads a script from `cdn.jsdelivr.net` (must be
blocked by the CSP). The page writes `ok` or `error: …` into `#status[data-state]`.

## Run the harness

```
npm ci                                              # playwright (repo devDependency)
node scripts/share-browser-check.mjs --browser chromium   # or firefox, webkit; --out DIR keeps screenshots
```

Chromium runs as the full build in new headless mode (`channel: 'chromium'`) because the headless shell has no PDF
viewer. Playwright's Linux WebKit has no PDF viewer at all: the PDF display check reports `N/V` there.

## How the files were produced

* `*/data/table.parquet` and `pdf-only/report.pdf`: `python3 tests/share-fixtures/_build/make_data.py` (pyarrow 24.0.0;
  40,000 rows, 8 row groups of 5,000, snappy, ~0.95 MB; deterministic). The file is larger than a reader's first
  footer read, so the pages must use range requests; the pages read only the first 20 rows.
* Vendored libraries: `bash tests/share-fixtures/_build/build-vendor.sh <empty scratch dir>`, which pins
  `hyparquet@1.32.0`, `@duckdb/duckdb-wasm@1.33.1-dev57.0` (npm `latest`; DuckDB core v1.5.4) and `esbuild@0.25.10`:
  * `html-parquet-hyparquet/vendor/hyparquet.mjs`: esbuild bundle (ESM, minified) of `_build/hyparquet-entry.js`.
  * `html-parquet-duckdb/vendor/duckdb.mjs`: esbuild bundle of `_build/duckdb-entry.js` (duckdb-wasm's browser API
    with apache-arrow inlined; the npm file imports `apache-arrow` as a bare specifier).
* NOT in git (~40 MB), fetched by the harness at run time into `scripts/.share-browser-cache/` (gitignored) and
  copied into a temp copy of the fixtures for each run; the pins (URL + hash) live in `scripts/share-browser-check.mjs`:
  * `vendor/duckdb-browser-eh.worker.js` and `vendor/duckdb-eh.wasm`: from the npm tarball
    `@duckdb/duckdb-wasm@1.33.1-dev57.0` (checked against its npm `sha512` integrity), `package/dist/`. Only the
    single-threaded "eh" build is used (mvp is 41 MB more; coi needs cross-origin isolation, which a sandboxed
    opaque-origin page cannot have).
  * `vendor/extensions/v1.5.4/wasm_eh/parquet.duckdb_extension.wasm`: the signed parquet extension from
    `https://extensions.duckdb.org/v1.5.4/wasm_eh/` (pinned sha256; the eh wasm has no parquet reader).
  The first run therefore needs network access to registry.npmjs.org and extensions.duckdb.org; later runs use the
  cache. `vendor/duckdb.mjs` (committed) is built from the same duckdb-wasm version: bump both together.

Licenses: hyparquet MIT (`LICENSE.hyparquet`), apache-arrow Apache-2.0 (`LICENSE.apache-arrow`), duckdb-wasm MIT
(its npm package ships no LICENSE file).

Committed fixtures stay under ~3 MB; never commit the duckdb binaries (`.gitignore` refuses them).

## Guidance for artifact authors (what worked in the sandbox)

The share CSP runs every page in an opaque-origin sandbox (`sandbox allow-scripts`, no `allow-same-origin`) with
`connect-src 'self'` and `worker-src 'self' blob:`:

* No browser storage: `sessionStorage`, `localStorage`, IndexedDB, cookies and OPFS all throw. Keep state in memory.
* Bundle everything; external URLs are blocked. Use relative URLs, and build absolute ones from `location.href` when a
  library needs them (a `blob:` worker has no useful base URL).
* Workers: start them from a `blob:` URL (fetch the worker script, then `new Worker(URL.createObjectURL(blob))`);
  `duckdb.createWorker(url)` does exactly that.
* hyparquet: `asyncBufferFromUrl({ url })` + `parquetReadObjects` works as-is (HEAD, then range reads).
* duckdb-wasm: vendor every extension your queries need and run
  `SET custom_extension_repository = '<absolute URL of vendor/extensions>'` before the first query. The default
  repository is blocked by the CSP, and in Chromium the blocked autoload crashed the renderer instead of failing the
  query. Open the database with
  `db.open({ filesystem: { forceFullHTTPReads: false, allowFullHTTPReads: false, reliableHeadRequests: true } })`;
  without it this build downloaded the whole parquet file with one GET.
* WebKit/Safari: see the harness report; in Playwright WebKit 27.2 `connect-src 'self'` did not match the share's own
  URL from the sandboxed page, so every `fetch`/XHR to the bundle was refused (scripts, styles and images loaded).
