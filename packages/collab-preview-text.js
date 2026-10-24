import { escapeHtml as esc } from './herdr-web-components/util.js';
import { renderMarkdown } from './conversation-markdown.js';
import { renderJson } from './conversation-json.js';

export const TEXT_LIMIT = 200000;
export function boundedText(text) {
  if (typeof text !== 'string' || text.length > TEXT_LIMIT) throw new Error('Preview text limit exceeded.');
  return text;
}

// Generated markup only. Opaque origin, no scripts, navigation, forms or network
// resources; details/summary provide native read-only interactions in the frame.
export function sandboxMarkup(markup, title) {
  const frame = document.createElement('iframe');
  frame.title = title; frame.setAttribute('sandbox', ''); frame.referrerPolicy = 'no-referrer';
  frame.style.cssText = 'width:100%;height:420px;border:1px solid #888;min-width:0';
  frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>body{font:14px system-ui;overflow-wrap:anywhere;margin:12px;color:#222;background:#fff}pre{white-space:pre-wrap}summary,select{cursor:pointer}svg{width:100%;max-height:300px}ul{padding-left:20px}table{border-collapse:collapse}td,th{padding:4px;border:1px solid #ddd}</style></head><body>${markup}</body></html>`;
  return frame;
}

// Strict bounded XML source tree. No DTD, resolver, browser DOM, namespaces
// expansion or active content. Names/attributes are literal display labels.
function xmlMarkup(text) {
  const invalid = () => { throw new Error('Invalid XML or unsupported XML syntax.'); };
  if (/<!\s*(?:DOCTYPE|ENTITY)/i.test(text) || /<\?(?!xml\s[^?]*\?>)/i.test(text)) throw new Error('XML entities and processing instructions are not supported.');
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffe\uffff]/u.test(text)) invalid();
  const decode = value => value.replace(/&(?:#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);|&/g, entity => {
    const named = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'" };
    if (Object.hasOwn(named, entity)) return named[entity];
    if (entity === '&') return invalid();
    const code = entity.startsWith('&#x') ? parseInt(entity.slice(3, -1), 16) : Number(entity.slice(2, -1));
    if (!(code === 9 || code === 10 || code === 13 || (code >= 32 && code <= 0xd7ff) || (code >= 0xe000 && code <= 0xfffd) || (code >= 0x10000 && code <= 0x10ffff))) invalid();
    return String.fromCodePoint(code);
  });
  // A conservative XML name subset; unsupported names fail visibly, not as HTML.
  const name = '[A-Za-z_][A-Za-z0-9_.:-]*';
  const stack = [], out = []; let roots = 0, nodes = 0, offset = 0;
  const addNode = () => { if (++nodes > 500) throw new Error('XML node limit exceeded. Use Download.'); };
  while (offset < text.length) {
    const rest = text.slice(offset);
    if (rest.startsWith('<?xml')) {
      const declaration = /^<\?xml\s+version\s*=\s*(['"])1\.[01]\1(?:\s+encoding\s*=\s*(['"])[A-Za-z][A-Za-z0-9._-]*\2)?(?:\s+standalone\s*=\s*(['"])(?:yes|no)\3)?\s*\?>/.exec(rest);
      if (offset !== 0 || !declaration) invalid();
      offset += declaration[0].length; continue;
    }
    if (rest.startsWith('<!--')) {
      const end = rest.indexOf('-->');
      if (end < 0 || rest.slice(4, end).includes('--') || rest[end - 1] === '-') invalid();
      addNode(); offset += end + 3; continue;
    }
    if (rest.startsWith('<![CDATA[')) {
      const end = rest.indexOf(']]>'); if (!stack.length || end < 0) invalid();
      addNode(); out.push(esc(rest.slice(9, end))); offset += end + 3; continue;
    }
    if (!rest.startsWith('<')) {
      const end = rest.indexOf('<'), raw = end < 0 ? rest : rest.slice(0, end);
      if (raw.includes(']]>') || (!stack.length && raw.trim())) invalid();
      addNode(); out.push(esc(decode(raw))); offset += raw.length; continue;
    }
    const close = new RegExp(`^</(${name})\\s*>`).exec(rest);
    if (close) {
      if (stack.pop() !== close[1]) invalid();
      out.push('</div></details>'); offset += close[0].length; continue;
    }
    const open = new RegExp(`^<(${name})(?=[\\s/>])`).exec(rest); if (!open) invalid();
    let cursor = open[0].length; const attributes = [], seen = new Set();
    while (!/^(?:\s*\/?>)/.test(rest.slice(cursor))) {
      const attr = new RegExp(`^\\s+(${name})\\s*=\\s*(?:"([^"<]*)"|'([^'<]*)')`).exec(rest.slice(cursor));
      if (!attr || seen.has(attr[1])) invalid();
      seen.add(attr[1]); attributes.push(`${attr[1]}="${decode(attr[2] ?? attr[3])}"`); cursor += attr[0].length;
    }
    const end = /^\s*(\/?)>/.exec(rest.slice(cursor)); if (!end) invalid();
    if (!stack.length && ++roots > 1) invalid();
    addNode(); if (stack.length >= 12) throw new Error('XML depth limit exceeded. Use Download.');
    out.push(`<details open><summary>${esc(`<${open[1]}${attributes.length ? ' ' + attributes.join(' ') : ''}>`)}</summary><div style="margin-left:16px">`);
    if (end[1]) out.push('</div></details>'); else stack.push(open[1]);
    offset += cursor + end[0].length;
  }
  if (stack.length || roots !== 1) invalid();
  return out.join('');
}

// An indentation outline, deliberately not a YAML object deserializer: tags,
// aliases, merge keys and complex scalars are displayed literally, never run.
function yamlMarkup(text) {
  const lines = text.split(/\r?\n/), out = ['<p>YAML source outline (tags and aliases are literal).</p>'];
  const levels = [];
  for (const line of lines.slice(0, 1000)) {
    if (!line.trim()) continue;
    const indent = line.match(/^\s*/)[0].length;
    while (levels.length && indent <= levels.at(-1)) { out.push('</details>'); levels.pop(); }
    if (levels.length >= 12) out.push(`<pre>${esc(line)}</pre>`);
    else { out.push(`<details open><summary>${esc(line.trim())}</summary>`); levels.push(indent); }
  }
  out.push('</details>'.repeat(levels.length));
  if (lines.length > 1000) out.push('<p>Display limit reached. Use Source or Download.</p>');
  return out.join('');
}

export function textPreviewMarkup(kind, value) {
  const text = boundedText(value);
  let markup;
  if (kind === 'markdown') markup = renderMarkdown(text);
  else if (kind === 'json') {
    try { JSON.parse(text); } catch { throw new Error('Invalid JSON.'); }
    markup = renderJson(text);
  } else if (kind === 'xml') markup = xmlMarkup(text);
  else if (kind === 'yaml') markup = yamlMarkup(text);
  else throw new Error('Unsupported text preview.');
  return markup + `<details><summary>Source</summary><pre>${esc(text)}</pre></details>`;
}
