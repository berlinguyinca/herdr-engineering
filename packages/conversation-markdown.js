// Bounded Markdown/GFM subset: raw HTML, images and arbitrary URL schemes
// are never rendered. Every untrusted fragment passes through escapeHtml.
import { escapeHtml as esc } from './herdr-web-components/util.js';

function safeLink(value) {
  if (/[\s\u0000-\u001f\u007f]/.test(value)) return false;
  if (/^#/.test(value) || /^\/(?!\/)/.test(value)) return !value.includes('\\');
  try { return ['https:', 'http:', 'mailto:'].includes(new URL(value).protocol); } catch { return false; }
}

function inline(text, depth = 0) {
  if (depth > 4) return esc(text);
  const token = /`([^`\n]+)`|\[([^\]\n]+)\]\(([^\s)]+)\)|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|~~([^~\n]+)~~/g;
  let out = '', end = 0;
  for (const m of text.matchAll(token)) {
    out += esc(text.slice(end, m.index));
    if (m[1] !== undefined) out += `<code>${esc(m[1])}</code>`;
    else if (m[2] !== undefined) out += safeLink(m[3])
      ? `<a href="${esc(m[3])}" target="_blank" rel="noopener noreferrer">${inline(m[2], depth + 1)}</a>` : esc(m[0]);
    else if (m[4] !== undefined) out += `<strong>${inline(m[4], depth + 1)}</strong>`;
    else if (m[5] !== undefined) out += `<em>${inline(m[5], depth + 1)}</em>`;
    else out += `<del>${inline(m[6], depth + 1)}</del>`;
    end = m.index + m[0].length;
  }
  return out + esc(text.slice(end));
}

export function renderMarkdown(value) {
  const text = String(value ?? '');
  const lines = text.slice(0, 200000).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let paragraph = [], list = [], listType = '', fence = null, code = [];
  const flushParagraph = () => { if (paragraph.length) out.push(`<p>${paragraph.map((s) => inline(s)).join('<br>')}</p>`); paragraph = []; };
  const flushList = () => { if (list.length) out.push(`<${listType}>${list.map((s) => `<li>${/^\[[ xX]\] /.test(s) ? `<input type="checkbox" disabled${/^\[[xX]\]/.test(s) ? ' checked' : ''}> ${inline(s.slice(4))}` : inline(s)}</li>`).join('')}</${listType}>`); list = []; listType = ''; };
  const cells = (line) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map((s) => s.trim().replace(/\\\|/g, '|'));
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (fence !== null) {
      if (new RegExp(`^\\s{0,3}${fence}{3,}\\s*$`).test(line)) { out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); fence = null; code = []; }
      else code.push(line);
      continue;
    }
    const open = /^\s{0,3}(`{3,}|~{3,})[^`]*$/.exec(line);
    if (open) { flushParagraph(); flushList(); fence = open[1][0]; continue; }
    if (!line.trim()) { flushParagraph(); flushList(); continue; }
    if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushParagraph(); flushList(); out.push('<hr>'); continue; }
    const separators = index + 1 < lines.length && line.includes('|') ? cells(lines[index + 1]) : [];
    if (separators.length && separators.length <= 64 && separators.every((cell) => /^:?-{3,}:?$/.test(cell)) && cells(line).length === separators.length) {
      flushParagraph(); flushList();
      const align = separators.map((cell) => cell.startsWith(':') && cell.endsWith(':') ? 'center' : cell.endsWith(':') ? 'right' : 'left');
      const row = (values, tag) => '<tr>' + separators.map((_, i) => `<${tag} style="text-align:${align[i]}">${inline(values[i] || '')}</${tag}>`).join('') + '</tr>';
      const rows = []; index++;
      while (rows.length < 500 && index + 1 < lines.length && lines[index + 1].includes('|') && lines[index + 1].trim()) rows.push(row(cells(lines[++index]), 'td'));
      out.push(`<div class="conversation-table"><table><thead>${row(cells(line), 'th')}</thead><tbody>${rows.join('')}</tbody></table></div>`); continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) { flushParagraph(); flushList(); out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); continue; }
    const item = /^\s{0,3}(?:([-+*])|\d+[.)])\s+(.+)$/.exec(line);
    if (item) { flushParagraph(); const type = item[1] ? 'ul' : 'ol'; if (type !== listType) flushList(); listType = type; list.push(item[2]); continue; }
    flushList();
    if (/^>\s?/.test(line)) { flushParagraph(); out.push(`<blockquote>${inline(line.replace(/^>\s?/, ''))}</blockquote>`); }
    else paragraph.push(line);
  }
  if (fence !== null) out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
  flushParagraph(); flushList();
  if (text.length > 200000) out.push('<p class="conversation-truncated">Message truncated for display.</p>');
  return out.join('');
}
