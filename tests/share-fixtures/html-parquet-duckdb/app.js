// Reads a few rows of data/table.parquet with duckdb-wasm (the single-threaded "eh" build) over HTTP range requests.
// Pattern for artifact authors (the page runs in an opaque-origin sandbox):
//  * build every URL absolute from location.href -- a blob: worker has no useful base URL;
//  * start the worker with duckdb.createWorker(url), which fetches the worker script and starts it from a blob: URL
//    (a worker from the bundle's own URL is cross-origin to the page's opaque origin);
//  * vendor every DuckDB extension the queries need (here: parquet) and point custom_extension_repository at the
//    bundle -- the default repository (extensions.duckdb.org) is blocked by the CSP, and in Chromium the blocked
//    autoload crashed the renderer instead of failing the query;
//  * register the parquet file by URL with DuckDBDataProtocol.HTTP and open the database with
//    filesystem {forceFullHTTPReads: false, allowFullHTTPReads: false, reliableHeadRequests: true}, so DuckDB reads it
//    with range requests (without it this build fetched the whole file with one GET);
//  * never use OPFS, IndexedDB or any other browser storage.
import * as duckdb from './vendor/duckdb.mjs'

const status = document.getElementById('status')

async function suffixProbe(url) {
  const r = await fetch(url, { headers: { Range: 'bytes=-8' } })
  const bytes = new Uint8Array(await r.arrayBuffer())
  return { status: r.status, magic: new TextDecoder().decode(bytes.slice(4)), contentRange: r.headers.get('Content-Range') }
}

async function main() {
  const workerUrl = new URL('vendor/duckdb-browser-eh.worker.js', location.href).href
  const wasmUrl = new URL('vendor/duckdb-eh.wasm', location.href).href
  const url = new URL('data/table.parquet', location.href).href
  const worker = await duckdb.createWorker(workerUrl)
  const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker)
  await db.instantiate(wasmUrl)
  // This duckdb-wasm build downloads a registered HTTP file whole unless told otherwise: ask for range reads only.
  await db.open({ filesystem: { forceFullHTTPReads: false, allowFullHTTPReads: false, reliableHeadRequests: true } })
  await db.registerFileURL('table.parquet', url, duckdb.DuckDBDataProtocol.HTTP, false)
  const conn = await db.connect()
  const repository = new URL('vendor/extensions', location.href).href
  await conn.query(`SET custom_extension_repository = '${repository.replaceAll("'", "''")}'`)
  const result = await conn.query("SELECT id, name, mz, intensity FROM read_parquet('table.parquet') WHERE id < 20 ORDER BY id")
  const count = await conn.query("SELECT count(*) AS n FROM read_parquet('table.parquet')")
  const rows = result.toArray().map((r) => r.toJSON())
  const table = document.getElementById('data')
  const head = table.createTHead().insertRow()
  for (const name of Object.keys(rows[0])) head.insertCell().textContent = name
  const body = table.createTBody()
  for (const row of rows) {
    const tr = body.insertRow()
    for (const value of Object.values(row)) tr.insertCell().textContent = String(value)
  }
  window.__probe.suffix = await suffixProbe(url)
  window.__probe.rowsTotal = Number(count.toArray()[0].toJSON().n)
  await conn.close()
  status.textContent = 'ok'
  status.dataset.state = 'ok'
}

main().catch((e) => {
  status.textContent = 'error: ' + (e && e.stack || e)
  status.dataset.state = 'error'
})
