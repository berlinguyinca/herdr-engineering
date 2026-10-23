import { escapeHtml as esc } from './herdr-web-components/util.js';

const MAX_TEXT = 200000;
export function parseJson(value) {
  try {
    if (typeof value === 'string') {
      if (value.length > MAX_TEXT) return { ok: false, error: 'JSON exceeds the 200,000 character display limit.' };
      return { ok: true, value: JSON.parse(value) };
    }
    return { ok: true, value };
  } catch (error) { return { ok: false, error: `Invalid JSON: ${error.message}` }; }
}

// Bound depth, node count and scalar text independently, including object input
// from activity events. Keys are data, never property paths or HTML attributes.
export function renderJson(value, options = {}) {
  const parsed = parseJson(value);
  if (!parsed.ok) return `<p class="conversation-json-error">${esc(parsed.error)}</p>`;
  let nodes = 0, characters = 0;
  const seen = new WeakSet();
  const limit = '<span class="conversation-json-limit">Display limit reached</span>';
  const scalar = (value) => {
    let text = JSON.stringify(value) ?? String(value);
    const remaining = Math.max(0, 40000 - characters);
    const truncated = text.length > Math.min(remaining, 4000);
    text = text.slice(0, Math.min(remaining, 4000)); characters += text.length;
    return `${esc(text)}${truncated ? '…' : ''}`;
  };
  const walk = (item, depth) => {
    if (++nodes > 500 || depth > 12 || characters >= 40000) return limit;
    if (item === null || typeof item !== 'object') return `<span class="conversation-json-${item === null ? 'null' : typeof item}">${scalar(item)}</span>`;
    if (seen.has(item)) return '<span class="conversation-json-limit">Circular reference</span>';
    seen.add(item);
    const array = Array.isArray(item), keys = Object.keys(item);
    const rows = [];
    for (const key of keys) {
      if (nodes >= 500 || characters >= 40000) { rows.push(`<li>${limit}</li>`); break; }
      rows.push(`<li><span class="conversation-json-key">${scalar(key)}</span>: ${walk(item[key], depth + 1)}</li>`);
    }
    seen.delete(item);
    return `<details${depth < (options.openDepth ?? 2) ? ' open' : ''}><summary>${array ? 'Array' : 'Object'} · ${keys.length} ${array ? 'items' : 'keys'}</summary><ul>${rows.join('')}</ul></details>`;
  };
  return `<div class="conversation-json">${walk(parsed.value, 0)}</div>`;
}

export function mountJsonViewer(host, value, options = {}) {
  const parsed = parseJson(value);
  let raw = '', copyAvailable = true;
  try {
    let nodes = 0, characters = 0;
    const depths = new WeakMap();
    raw = typeof value === 'string' ? value : JSON.stringify(value, function (key, item) {
      characters += key.length + (typeof item === 'string' ? item.length : 0);
      if (++nodes > 5000 || characters > MAX_TEXT) throw new Error('display limit');
      if (item && typeof item === 'object') {
        const depth = depths.get(this) || 0;
        if (depth >= 12) throw new Error('depth limit');
        depths.set(item, depth + 1);
      }
      return item;
    }, 2);
    if (String(raw ?? '').length > MAX_TEXT) throw new Error('display limit');
  } catch { raw = 'Raw JSON exceeds the display limit or cannot be serialized. Use the bounded tree view.'; copyAvailable = false; }
  raw = String(raw ?? '');
  host.innerHTML = `<div class="conversation-json-toolbar"><button type="button" data-json-view aria-pressed="false">Raw JSON</button><button type="button" data-json-copy>Copy JSON</button><span role="status"></span></div><div data-json-tree>${renderJson(value, options)}</div><pre data-json-raw hidden></pre>`;
  const toggle = host.querySelector('[data-json-view]'), copy = host.querySelector('[data-json-copy]');
  const tree = host.querySelector('[data-json-tree]'), pre = host.querySelector('[data-json-raw]'), status = host.querySelector('[role=status]');
  pre.textContent = raw; copy.disabled = !copyAvailable;
  const change = () => { pre.hidden = !pre.hidden; tree.hidden = !pre.hidden; toggle.textContent = pre.hidden ? 'Raw JSON' : 'Tree view'; toggle.setAttribute('aria-pressed', String(!pre.hidden)); };
  let disposed = false;
  const copyText = async () => {
    try {
      if (!host.ownerDocument.defaultView.navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await host.ownerDocument.defaultView.navigator.clipboard.writeText(raw);
      if (!disposed) status.textContent = 'Copied';
    } catch { if (!disposed) status.textContent = 'Copy unavailable. Select and copy the raw JSON.'; }
  };
  toggle.addEventListener('click', change); copy.addEventListener('click', copyText);
  if (!parsed.ok) status.textContent = parsed.error;
  return { dispose() { disposed = true; toggle.removeEventListener('click', change); copy.removeEventListener('click', copyText); } };
}
