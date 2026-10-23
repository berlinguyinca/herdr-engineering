// Bounded Markdown/GFM subset: raw HTML, images and arbitrary URL schemes
// are never rendered. Every untrusted fragment passes through escapeHtml.
import { escapeHtml as esc } from './herdr-web-components/util.js';

// ---- Shared safe linkifier -------------------------------------------------
// Output is an escaped HTML string; no untrusted text is ever concatenated raw.
const MAX_URL = 2048;
const MAX_DISPLAY = 80;
const MAX_LINK_TEXT = 200;
const REL = 'noopener noreferrer nofollow';

const cap = (text, max) => (text.length > max ? `${text.slice(0, max - 1)}\u2026` : text);

// Returns a parsed URL only for plain http(s) without userinfo, else null.
export function parseWebUrl(value) {
  if (typeof value !== 'string' || value.length > MAX_URL || /[\s\u0000-\u001f\u007f<>"`\\]/.test(value)) return null;
  if (!/^https?:\/\//i.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    if (url.username || url.password || !url.hostname) return null;
    return url;
  } catch { return null; }
}

// Removes trailing sentence punctuation and unbalanced closing brackets.
// Returns [url, stripped]. Linear: bracket balance is counted once.
export function splitTrailingPunctuation(raw) {
  let end = raw.length;
  let open = 0, close = 0, openSq = 0, closeSq = 0;
  for (let i = 0; i < end; i++) {
    const c = raw[i];
    if (c === '(') open++; else if (c === ')') close++;
    else if (c === '[') openSq++; else if (c === ']') closeSq++;
  }
  while (end > 0) {
    const c = raw[end - 1];
    if ('.,;:!?\'"}>'.includes(c)) end--;
    else if (c === ')' && close > open) { close--; end--; }
    else if (c === ']' && closeSq > openSq) { closeSq--; end--; }
    else break;
  }
  return [raw.slice(0, end), raw.slice(end)];
}

const hostLabel = (url) => url.host.toLowerCase();
const isLookalikeHost = (url, original) => url.hostname.includes('xn--') || /[^\x00-\x7f]/.test(original);

function anchor(url, innerHtml, title) {
  return `<a href="${esc(url.href)}" target="_blank" rel="${REL}" title="${esc(title)}">${innerHtml}</a>`;
}

// Bare URL in text -> anchor showing the (capped) URL; IDN hosts also show punycode.
export function linkifyBare(raw) {
  const [candidate, tail] = splitTrailingPunctuation(raw);
  const url = parseWebUrl(candidate);
  if (!url) return null;
  const shown = cap(candidate, MAX_DISPLAY);
  const puny = isLookalikeHost(url, candidate) ? ` <span class="md-link-host">(${esc(hostLabel(url))})</span>` : '';
  return anchor(url, esc(shown), url.href) + puny + esc(tail);
}

function safeLink(value) {
  if (/[\s\u0000-\u001f\u007f]/.test(value)) return false;
  if (/^#/.test(value) || /^\/(?!\/)/.test(value)) return !value.includes('\\');
  return parseWebUrl(value) !== null || /^mailto:[^\s@]+@[^\s@]+$/i.test(value) && value.length <= MAX_URL;
}

function markdownLink(label, target, depth) {
  const web = parseWebUrl(target);
  const labelHtml = inline(cap(label, MAX_LINK_TEXT), depth + 1, true);
  if (!web) {
    // Internal route, root path or mailto: keep the existing behaviour.
    const internal = target.startsWith('#') || target.startsWith('/');
    return `<a href="${esc(target)}" target="_blank" rel="${REL}"${internal ? '' : ` title="${esc(target)}"`}>${labelHtml}</a>`;
  }
  // The destination host is always visible so lookalike link text cannot hide it.
  const sameAsHost = label.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/$/, '') === hostLabel(web);
  const suffix = sameAsHost ? '' : ` <span class="md-link-host">(${esc(hostLabel(web))})</span>`;
  return anchor(web, labelHtml, web.href) + suffix;
}

const LINK_SYNTAX_MAX = 30000;
const TOKEN_WITH_LINKS = /`([^`\n]+)`|\[([^\]\n]{1,500})\]\(([^\s)]{1,2100})\)|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|~~([^~\n]+)~~|(?<![A-Za-z0-9])(https?:\/\/[^\s<>`*"\\]{1,2100})/g;
// same groups, the link alternative can never match (groups 2 and 3 stay undefined)
const TOKEN_NO_LINKS = /`([^`\n]+)`|(\b\B)()|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|~~([^~\n]+)~~|(?<![A-Za-z0-9])(https?:\/\/[^\s<>`*"\\]{1,2100})/g;

function inline(text, depth = 0, noLinks = false) {
  if (depth > 4) return esc(text);
  // The markdown-link alternative scans up to ~2600 characters per '[': only try it when a link can exist and the
  // text is of chat size, so a 100k-character "[[[[" message cannot make the page stall.
  const token = text.length <= LINK_SYNTAX_MAX && text.includes('](') && text.includes(')') ? TOKEN_WITH_LINKS : TOKEN_NO_LINKS;
  let out = '', end = 0;
  for (const m of text.matchAll(token)) {
    out += esc(text.slice(end, m.index));
    if (m[1] !== undefined) out += `<code>${esc(m[1])}</code>`;
    else if (m[2] !== undefined) out += !noLinks && safeLink(m[3]) ? markdownLink(m[2], m[3], depth) : esc(m[0]);
    else if (m[4] !== undefined) out += `<strong>${inline(m[4], depth + 1, noLinks)}</strong>`;
    else if (m[5] !== undefined) out += `<em>${inline(m[5], depth + 1, noLinks)}</em>`;
    else if (m[6] !== undefined) out += `<del>${inline(m[6], depth + 1, noLinks)}</del>`;
    else out += (noLinks ? null : linkifyBare(m[7])) ?? esc(m[0]);
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
