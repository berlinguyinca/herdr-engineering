// Pure helpers of the annotation view: anchor maths, text clean-up, fixed messages. No DOM, no network, no storage, so
// node:test can import this file as it is. The limits mirror herdr_engineering/control_plane/annotation_anchor.py.
export const LIMITS = Object.freeze({ exact: 1000, context: 64, page: 100000, minBox: 0.005, maxRows: 500 });
export const ANNOTATION_ID = /^ann_[A-Za-z0-9_-]{22}$/;
const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/g;
const BAD_CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;
const RAW_HREF = /^(?:\.\.\/)+f\/[^?#\\]*$/;
const SESSION_PATH = /^\/s\/([A-Za-z0-9_-]{43})\/k\/([A-Za-z0-9_.-]{1,768})\/a\/./;

// What the server accepts in a text field: LF only, no control characters but tab, no lone surrogate.
export function cleanText(value) {
  return String(value).replace(/\r\n?/g, '\n').replace(LONE_SURROGATE, '�').replace(BAD_CONTROL, ' ');
}

const isHigh = (code) => code >= 0xd800 && code <= 0xdbff;
const isLow = (code) => code >= 0xdc00 && code <= 0xdfff;

export function clipHead(text, limit) {
  if (text.length <= limit) return text;
  let end = limit;
  if (end > 0 && isHigh(text.charCodeAt(end - 1))) end -= 1;
  return text.slice(0, end);
}

export function clipTail(text, limit) {
  if (text.length <= limit) return text;
  let start = text.length - limit;
  if (start < text.length && isLow(text.charCodeAt(start))) start += 1;
  return text.slice(start);
}

// The anchor of the characters [start, end) of `full`: the selection without its edge whitespace, clipped to 1000
// characters, with 64 characters of context on each side. null when nothing but whitespace is selected.
export function textQuote(full, start, end) {
  let from = Math.max(0, Math.min(start, end));
  let to = Math.min(full.length, Math.max(start, end));
  while (from < to && /\s/.test(full[from])) from += 1;
  while (to > from && /\s/.test(full[to - 1])) to -= 1;
  if (from >= to) return null;
  const exact = clipHead(full.slice(from, to), LIMITS.exact);
  const stop = from + exact.length;
  const text = cleanText(exact);
  if (!text.trim()) return null;
  return {
    exact: text,
    prefix: clipTail(cleanText(full.slice(Math.max(0, from - LIMITS.context), from)), LIMITS.context),
    suffix: clipHead(cleanText(full.slice(stop, stop + LIMITS.context)), LIMITS.context),
  };
}

// The same quote from selected text alone (no range): the first place `selected` occurs in `full`.
export function textQuoteFromString(full, selected) {
  const needle = selected.trim();
  if (!needle) return null;
  const at = full.indexOf(needle);
  return at < 0 ? null : textQuote(full, at, at + needle.length);
}

// Where an anchor's quote sits in `full`: the occurrence whose surroundings match best; -1 when it is gone.
export function findQuote(full, anchor) {
  const exact = anchor && typeof anchor.exact === 'string' ? anchor.exact : '';
  if (!exact) return -1;
  const prefix = typeof anchor.prefix === 'string' ? anchor.prefix : '';
  const suffix = typeof anchor.suffix === 'string' ? anchor.suffix : '';
  let best = -1;
  let bestScore = -1;
  for (let at = full.indexOf(exact), seen = 0; at >= 0 && seen < 200; at = full.indexOf(exact, at + 1), seen += 1) {
    const score = (prefix && full.slice(Math.max(0, at - prefix.length), at) === prefix ? 1 : 0)
      + (suffix && full.slice(at + exact.length, at + exact.length + suffix.length) === suffix ? 1 : 0);
    if (score > bestScore) { best = at; bestScore = score; }
    if (score === 2) break;
  }
  return best;
}

const floor4 = (value) => Math.floor(value * 1e4 + 1e-9) / 1e4;

// A drag from (ax, ay) to (bx, by) inside a box of width x height pixels as a unit box, or null when it is too small.
// x + w and y + h never exceed 1 (the server refuses a region that leaves the page).
export function unitBox(ax, ay, bx, by, width, height) {
  if (!(width > 0 && height > 0) || ![ax, ay, bx, by].every(Number.isFinite)) return null;
  const clamp = (v) => Math.min(1, Math.max(0, v));
  const [x0, x1] = [clamp(ax / width), clamp(bx / width)].sort((p, q) => p - q);
  const [y0, y1] = [clamp(ay / height), clamp(by / height)].sort((p, q) => p - q);
  if (x1 - x0 < LIMITS.minBox || y1 - y0 < LIMITS.minBox) return null;
  const box = { x: floor4(x0), y: floor4(y0), w: floor4(x1 - x0), h: floor4(y1 - y0) };
  while (box.x + box.w > 1 && box.w > 0) box.w = floor4(box.w - 1e-4);
  while (box.y + box.h > 1 && box.h > 0) box.h = floor4(box.h - 1e-4);
  return box.w > 0 && box.h > 0 ? box : null;
}

export function percent(value) {
  return `${Math.round(value * 100)}%`;
}

export function boxText(box) {
  return `x ${percent(box.x)}, y ${percent(box.y)}, ${percent(box.w)} wide, ${percent(box.h)} tall`;
}

const isBox = (b) => !!b && typeof b === 'object' && ['x', 'y', 'w', 'h'].every((k) => typeof b[k] === 'number');

// {title, quote} for the panel: plain strings only (the caller uses textContent), never markup.
export function describeAnchor(anchor, file) {
  if (!anchor || typeof anchor !== 'object') return { title: `Whole file: ${file}`, quote: '' };
  if (anchor.kind === 'text_quote' && typeof anchor.exact === 'string') return { title: 'Selected text', quote: anchor.exact };
  if (anchor.kind === 'image_region' && isBox(anchor)) return { title: `Image region: ${boxText(anchor)}`, quote: '' };
  if (anchor.kind === 'pdf_page' && Number.isInteger(anchor.page)) {
    const region = isBox(anchor.region) ? `, region ${boxText(anchor.region)}` : '';
    return { title: `Page ${anchor.page}${region}`, quote: typeof anchor.exact === 'string' ? anchor.exact : '' };
  }
  return { title: `Whole file: ${file}`, quote: '' };
}

export function statusLabel(row) {
  switch (row.status) {
    case 'open': return row.edited_at ? 'Open, edited' : 'Open';
    case 'addressed': return Number.isInteger(row.addressed_in_version_number) ? `Addressed in v${row.addressed_in_version_number}` : 'Addressed';
    case 'dismissed': return 'Dismissed';
    case 'submitted': return 'Submitted';
    case 'edited': return 'Edited';
    default: return 'Unknown';
  }
}

const MESSAGES = Object.freeze({
  bad_anchor: 'That selection cannot be saved. Pick the text again, or comment on the whole file.',
  bad_file: 'That file is not part of this share.',
  empty: 'Write a comment first.',
  too_long: 'That is too long.',
  bad_chars: 'Remove the unusual control characters and try again.',
  bad_id: 'That annotation cannot be changed.',
});

// A fixed message per outcome; nothing the server sent is ever shown. `pending`: the row is not confirmed by the
// server's list yet, so a 409 only means "not saved yet".
export function errorMessage(status, code, pending = false) {
  if (status === 400) return Object.hasOwn(MESSAGES, code) ? MESSAGES[code] : 'The server did not accept that.';
  if (status === 409) return pending ? 'Still saving — try again in a moment' : 'This annotation can no longer be changed';
  if (status === 429) return 'Too many annotations — wait a minute';
  if (status === 503) return 'Annotations are unavailable right now — try again later';
  if (status === 404) return 'Your session ended — open the link again';
  if (status === 0) return 'Could not reach the server — check your connection and try again';
  return 'Something went wrong — try again later';
}

export const RETRY_DELAYS = Object.freeze([400, 800, 1600, 3200, 5000, 5000]);
export const POLL_DELAYS = Object.freeze([600, 1200, 2000, 3000, 5000, 5000, 5000, 5000, 5000, 5000]);

// Is this one of our own relative raw-file links (../f/<name>)? Anything else is never used as a source or link.
export function isRawHref(href) {
  return typeof href === 'string' && href.length < 4096 && RAW_HREF.test(href);
}

// The annotation endpoints of the session this a/ page belongs to, or null when the path is not an a/ page.
export function annotationUrls(pathname) {
  const match = SESSION_PATH.exec(pathname);
  if (!match) return null;
  const base = `/s/${match[1]}/k/${match[2]}/annotations`;
  return {
    list: base,
    create: base,
    edit: (id) => (ANNOTATION_ID.test(id) ? `${base}/${id}/edit` : null),
    remove: (id) => (ANNOTATION_ID.test(id) ? `${base}/${id}/delete` : null),
  };
}

// One server row, or null when it is not shaped like a projection row.
export function cleanRow(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const { annotation_id: id, body, status, file } = raw;
  if (typeof id !== 'string' || !ANNOTATION_ID.test(id) || typeof body !== 'string' || typeof status !== 'string') return null;
  return {
    annotation_id: id,
    body,
    status,
    file: typeof file === 'string' ? file : '',
    anchor: raw.anchor && typeof raw.anchor === 'object' ? raw.anchor : null,
    edited_at: typeof raw.edited_at === 'string' ? raw.edited_at : null,
    addressed_in_version_number: Number.isInteger(raw.addressed_in_version_number) ? raw.addressed_in_version_number : null,
  };
}
