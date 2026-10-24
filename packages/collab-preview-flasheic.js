import { plotMarkup } from './collab-preview-msp.js';
import { sandboxMarkup } from './collab-preview-text.js';

// Inject an authenticated service adapter when one exists. Never send a storage
// key or object URL to an external plotting service; it must resolve room access.
export async function loadChromatogram(file, { adapter, signal } = {}) {
  if (typeof adapter !== 'function') throw new Error('Flasheic preview service unavailable. Use Download.');
  signal?.throwIfAborted();
  const result = await adapter({ room_id: file.room_id, file_id: file.file_id }, { signal });
  signal?.throwIfAborted();
  if (result?.room_id !== file.room_id || result?.file_id !== file.file_id || !Array.isArray(result.traces) || !result.traces.length || result.traces.length > 16) throw new Error('Invalid Flasheic response.');
  let count = 0;
  for (const trace of result.traces) {
    if (!trace || typeof trace.name !== 'string' || trace.name.length > 2000 || !Array.isArray(trace.points) || !trace.points.length || trace.points.length > 10000) throw new Error('Invalid Flasheic trace.');
    let last = -Infinity;
    for (const p of trace.points) {
      if (++count > 20000 || !Array.isArray(p) || p.length !== 2 || !p.every(n => Number.isFinite(n) && n >= 0) || p[0] < last) throw new Error('Invalid Flasheic points.');
      last = p[0];
    }
  }
  return result.traces;
}

export async function renderFlasheic(file, options) {
  const traces = await loadChromatogram(file, options), host = document.createElement('div');
  const label = document.createElement('label'), select = document.createElement('select'), display = document.createElement('div');
  label.textContent = 'Chromatogram '; select.setAttribute('aria-label', 'Chromatogram'); label.append(select);
  traces.forEach((trace, i) => { const option = document.createElement('option'); option.value = String(i); option.textContent = trace.name; select.append(option); });
  const draw = () => { const trace = traces[Number(select.value)]; display.replaceChildren(sandboxMarkup(plotMarkup(trace.name, trace.points), 'Chromatogram preview')); };
  select.onchange = draw; host.append(label, display); draw(); return host;
}
