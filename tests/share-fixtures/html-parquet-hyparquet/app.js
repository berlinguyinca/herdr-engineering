// Reads a few rows of data/table.parquet with hyparquet over HTTP range requests and renders them.
// Pattern for artifact authors: build absolute URLs from location.href (the page has an opaque origin, but its URL is
// the share path), let hyparquet's asyncBufferFromUrl issue range reads, and never touch browser storage.
import { asyncBufferFromUrl, parquetMetadataAsync, parquetReadObjects } from './vendor/hyparquet.mjs'

const status = document.getElementById('status')

async function suffixProbe(url) {
  // A suffix range (the last 8 bytes: footer length + "PAR1"). Not CORS-safelisted, so it also needs the preflight.
  const r = await fetch(url, { headers: { Range: 'bytes=-8' } })
  const bytes = new Uint8Array(await r.arrayBuffer())
  return { status: r.status, magic: new TextDecoder().decode(bytes.slice(4)), contentRange: r.headers.get('Content-Range') }
}

async function main() {
  const url = new URL('data/table.parquet', location.href).href
  const file = await asyncBufferFromUrl({ url })
  const metadata = await parquetMetadataAsync(file)
  const rows = await parquetReadObjects({ file, metadata, rowStart: 0, rowEnd: 20 })
  const table = document.getElementById('data')
  const head = table.createTHead().insertRow()
  for (const name of Object.keys(rows[0])) head.insertCell().textContent = name
  const body = table.createTBody()
  for (const row of rows) {
    const tr = body.insertRow()
    for (const value of Object.values(row)) tr.insertCell().textContent = String(value)
  }
  window.__probe.suffix = await suffixProbe(url)
  window.__probe.rowsTotal = Number(metadata.num_rows)
  status.textContent = 'ok'
  status.dataset.state = 'ok'
}

main().catch((e) => {
  status.textContent = 'error: ' + (e && e.stack || e)
  status.dataset.state = 'error'
})
