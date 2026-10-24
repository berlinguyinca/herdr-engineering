import { boundedText, sandboxMarkup, textPreviewMarkup, TEXT_LIMIT } from './collab-preview-text.js';
import { renderMsp } from './collab-preview-msp.js';
import { renderPdf, PDF_LIMIT } from './collab-preview-pdf.js';
import { renderFlasheic } from './collab-preview-flasheic.js';
import { openAuthorizedFile } from './collab-files.js';

const renderers = Object.fromEntries(['msp', 'flasheic', 'pdf', 'markdown', 'json', 'xml', 'yaml', 'unsupported'].map(id => [id, Object.freeze({ id })]));
const extensions = { msp: 'msp', mzml: 'flasheic', mzxml: 'flasheic', cdf: 'flasheic', netcdf: 'flasheic', pdf: 'pdf', md: 'markdown', markdown: 'markdown', json: 'json', xml: 'xml', yaml: 'yaml', yml: 'yaml' };
const mimes = { 'application/pdf': 'pdf', 'text/markdown': 'markdown', 'application/json': 'json', 'application/xml': 'xml', 'text/xml': 'xml', 'application/yaml': 'yaml', 'text/yaml': 'yaml' };
export function previewRendererFor(file) {
  const name = String(file.relative_path || '').split('/').at(-1), ext = name.includes('.') ? name.split('.').at(-1).toLowerCase() : null;
  const mime = String(file.content_type || '').split(';')[0].toLowerCase();
  const kind = ext !== null ? (Object.hasOwn(extensions, ext) ? extensions[ext] : null) : (Object.hasOwn(mimes, mime) ? mimes[mime] : null);
  return renderers[kind || 'unsupported'];
}

function validateSource(file, source) {
  if (file.status !== 'available' || !file.room_id || !file.file_id) throw new Error('Only finalized, available room files can be previewed.');
  let url;
  try { url = new URL(source?.url); } catch { throw new Error('Invalid authorized file source.'); }
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('A secure authorized file source is required.');
  if (!Number.isFinite(source.expires_at) || source.expires_at * 1000 <= Date.now()) throw new Error('Authorized file source expired. Try again.');
}

export async function readPreviewBytes(file, source, { fetcher = globalThis.fetch, signal = AbortSignal.timeout(15000), limit = TEXT_LIMIT } = {}) {
  validateSource(file, source); signal?.throwIfAborted();
  if (file.size_bytes > limit) throw new Error('Preview size limit exceeded.');
  const response = await fetcher(source.url, { signal, credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', cache: 'no-store' });
  if (!response.ok) { await response.body?.cancel(); throw new Error(`Preview content unavailable (${response.status}).`); }
  if (Number(response.headers.get('content-length')) > limit) { await response.body?.cancel(); throw new Error('Preview size limit exceeded.'); }
  if (!response.body?.getReader) throw new Error('Streaming preview content unavailable.');
  const reader = response.body.getReader(), chunks = []; let length = 0;
  try {
    while (true) {
      signal?.throwIfAborted();
      const { value, done } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > limit) throw new Error('Preview size limit exceeded.');
      chunks.push(value);
    }
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

const reference = file => ({ room_id: file.room_id, file_id: file.file_id });
function validateJob(file, job, jobId) {
  if (!job || job.room_id !== file.room_id || job.file_id !== file.file_id || typeof job.job_id !== 'string' || !job.job_id || (jobId && job.job_id !== jobId) || !['pending', 'running', 'ready', 'failed'].includes(job.status)) throw new Error('Invalid preview job.');
  if (job.status === 'ready' && (!Array.isArray(job.artifacts) || job.artifacts.length > 50 || job.artifacts.some(a => !a || a.room_id !== file.room_id || typeof a.file_id !== 'string' || !a.file_id || a.status !== 'available' || typeof a.relative_path !== 'string'))) throw new Error('Invalid preview artifacts.');
  // Omit arbitrary service URLs/errors from state and display.
  return { ...reference(file), job_id: job.job_id, status: job.status, artifacts: (job.artifacts || []).map(a => ({ room_id: a.room_id, file_id: a.file_id, relative_path: a.relative_path, size_bytes: a.size_bytes, content_type: a.content_type, status: a.status })) };
}

// Local adapter seam, not a proposed HTTP contract. HEAD has no job endpoints.
// Providers must authorize each request/status lookup and store derived files in
// the existing room file service; the browser owns neither caches nor storage.
export async function requestAsyncPreview(file, { jobs, signal } = {}) {
  if (typeof jobs?.request !== 'function' || typeof jobs?.status !== 'function') return { ...reference(file), status: 'unavailable' };
  signal?.throwIfAborted();
  const job = await jobs.request(reference(file), { signal });
  signal?.throwIfAborted(); return validateJob(file, job);
}
function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    const finish = () => { signal?.removeEventListener('abort', abort); resolve(); };
    const timer = setTimeout(finish, ms);
    const abort = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(signal.reason); };
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
  });
}
export async function pollPreviewJob(file, initial, { jobs, signal, interval = 1000, maxPolls = 30, onStatus = () => {} } = {}) {
  let job = validateJob(file, initial);
  for (let i = 0; ['pending', 'running'].includes(job.status); i++) {
    signal?.throwIfAborted();
    if (i >= maxPolls) throw new Error('Preview still pending. Try again later.');
    onStatus(job.status); await delay(interval, signal);
    job = validateJob(file, await jobs.status({ ...reference(file), job_id: job.job_id }, { signal }), job.job_id);
    signal?.throwIfAborted();
  }
  return job;
}

export function renderPreview(file, authorizedSource, options = {}) {
  const root = document.createElement('section'), heading = document.createElement('h3'), body = document.createElement('div');
  heading.textContent = `Preview authorized: ${file.relative_path}`;
  body.setAttribute('role', 'status'); body.textContent = 'Loading preview…'; root.append(heading, body);
  const controller = new AbortController(), signal = controller.signal;
  let disposed = false, child = null;
  const deadline = setTimeout(() => controller.abort(new DOMException('Preview timed out. Try again.', 'TimeoutError')), 60000);
  const current = () => !disposed && !signal.aborted;
  signal.addEventListener('abort', () => {
    if (!disposed) { child?.dispose?.(); body.setAttribute('role', 'alert'); body.textContent = 'Preview timed out. Try again.'; }
  }, { once: true });
  root.dispose = () => { disposed = true; clearTimeout(deadline); controller.abort(); child?.dispose?.(); };
  const show = element => {
    if (!current()) { element.dispose?.(); return; }
    child?.dispose?.(); child = element; body.removeAttribute('role'); body.replaceChildren(element);
  };
  const asyncPreview = async () => {
    let job = await requestAsyncPreview(file, { ...options, signal });
    if (!current()) return;
    if (job.status === 'unavailable') { body.textContent = 'Preview exceeds the local size limit or needs processing; asynchronous previews are unavailable. Use Download.'; return; }
    job = await pollPreviewJob(file, job, { ...options, signal, onStatus: status => { if (current()) body.textContent = `Preview ${status}…`; } });
    if (!current()) return;
    if (job.status === 'failed') throw new Error('Preview processing failed. Try again or use Download.');
    const artifacts = document.createElement('div');
    artifacts.textContent = job.artifacts.length ? 'Derived artifacts' : 'No derived artifacts were produced.';
    for (const artifact of job.artifacts) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = `Preview ${artifact.relative_path}`;
      button.onclick = async () => {
        if (!current() || button.disabled) return;
        button.disabled = true;
        try {
          const source = await openAuthorizedFile(artifact, { api: options.api, purpose: 'preview' });
          if (current()) show(renderPreview(artifact, source, options));
        } catch { if (current()) { body.setAttribute('role', 'alert'); body.textContent = 'Derived artifact access unavailable. Try again.'; } }
        finally { if (current()) button.disabled = false; }
      };
      artifacts.append(button);
    }
    show(artifacts);
  };
  root.ready = (async () => {
    try {
      validateSource(file, authorizedSource);
      const renderer = previewRendererFor(file), limit = renderer.id === 'pdf' ? PDF_LIMIT : TEXT_LIMIT;
      if (renderer.id === 'unsupported') { body.textContent = `No preview renderer for this file (${file.size_bytes ?? 'unknown'} bytes). Use Download.`; return; }
      if (renderer.id === 'flasheic') {
        if (!options.flasheic && options.jobs) await asyncPreview();
        else show(await renderFlasheic(file, { adapter: options.flasheic, signal }));
        return;
      }
      if (file.size_bytes > limit) { await asyncPreview(); return; }
      let bytes;
      try { bytes = await readPreviewBytes(file, authorizedSource, { signal, limit }); }
      catch (error) { if (/size limit/.test(error.message) && current()) { await asyncPreview(); return; } throw error; }
      if (!current()) return;
      if (renderer.id === 'pdf') show(renderPdf(bytes));
      else {
        const text = boundedText(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
        show(renderer.id === 'msp' ? renderMsp(text) : sandboxMarkup(textPreviewMarkup(renderer.id, text), `${renderer.id} preview`));
      }
    } catch (error) {
      if (!disposed) { body.setAttribute('role', 'alert'); body.textContent = signal.aborted ? 'Preview timed out. Try again.' : `Preview unavailable: ${error.message}`; }
    } finally { clearTimeout(deadline); }
  })();
  return root;
}
