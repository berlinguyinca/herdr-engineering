// Renders collab-guide.md for the public site: a small Markdown subset, every character escaped first.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const inline = (raw) => {
  const codes = [];
  let t = esc(raw).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
};
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
const cells = (line) => line.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());

export function mdToHtml(md) {
  const lines = md.replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    if (line.startsWith('```')) {
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith('```')) { body.push(lines[i]); i += 1; }
      i += 1;
      out.push(`<pre><code>${esc(body.join('\n'))}</code></pre>`);
      continue;
    }
    const h = /^(#{1,3}) (.*)$/.exec(line);
    if (h) { out.push(`<h${h[1].length} id="${slug(h[2])}">${inline(h[2])}</h${h[1].length}>`); i += 1; continue; }
    if (line.startsWith('|') && /^\|[\s:|-]+\|$/.test(lines[i + 1] || '')) {
      const head = cells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].startsWith('|')) { rows.push(cells(lines[i])); i += 1; }
      out.push(`<div class="cx-guide-table"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
      continue;
    }
    const li = /^(\s*)([*-]|\d+\.) (.*)$/.exec(line);
    if (li) {
      const ordered = /\d/.test(li[2]);
      const items = [];
      while (i < lines.length) {
        const m = /^(\s*)([*-]|\d+\.) (.*)$/.exec(lines[i]);
        if (m && m[1].length <= li[1].length + 1) items.push(m[3]);
        else if (lines[i].startsWith('  ') && items.length && lines[i].trim()) items[items.length - 1] += ` ${lines[i].trim()}`;
        else if (m) items[items.length - 1] += ` ${m[3]}`;
        else break;
        i += 1;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${items.map((x) => `<li>${inline(x)}</li>`).join('')}</${tag}>`);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,3} |```|\|)/.test(lines[i]) && !/^\s*([*-]|\d+\.) /.test(lines[i])) { para.push(lines[i].trim()); i += 1; }
    out.push(`<p>${inline(para.join(' '))}</p>`);
  }
  return out.join('\n');
}

async function main() {
  const box = document.getElementById('guide');
  try {
    const res = await fetch('collab-guide.md');
    if (!res.ok) throw new Error(String(res.status));
    box.innerHTML = mdToHtml(await res.text());
  } catch {
    box.textContent = 'The guide could not be loaded. Try again in a moment.';
  }
}
if (typeof document !== 'undefined' && document.getElementById('guide')) main();
