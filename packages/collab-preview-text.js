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

function xmlMarkup(text) {
  // Do not feed DTDs/entities to any parser, even a normally inert DOMParser.
  if (/<!\s*(?:DOCTYPE|ENTITY)/i.test(text) || /<\?(?!xml\s[^?]*\?>)/i.test(text)) throw new Error('XML entities and processing instructions are not supported.');
  const doc = new DOMParser().parseFromString(text, 'application/xml');
  if (doc.querySelector('parsererror')) throw new Error('Invalid XML.');
  let nodes = 0;
  const walk = (node, depth) => {
    if (++nodes > 500 || depth > 12) return '<p>Display limit reached. Use Source or Download.</p>';
    if (node.nodeType !== 1) return esc(node.textContent || '');
    const attrs = [...node.attributes].map(a => `${a.name}="${a.value}"`).join(' ');
    const children = [];
    for (const child of node.childNodes) {
      if (nodes >= 500) { children.push('<p>Display limit reached.</p>'); break; }
      children.push(walk(child, depth + 1));
    }
    return `<details${depth < 2 ? ' open' : ''}><summary>${esc(`<${node.nodeName}${attrs ? ' ' + attrs : ''}>`)}</summary><div style="margin-left:16px">${children.join('')}</div></details>`;
  };
  return walk(doc.documentElement, 0);
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
