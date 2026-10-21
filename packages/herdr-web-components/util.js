// herdr-web-components — pure utilities (DOM-free, node-testable).

export function shortenId(id) {
  if (!id) return '';
  return id.length > 24 ? `${id.slice(0, 6)}…${id.slice(-6)}` : id;
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// Mission titles often start life as a pasted prompt ("- \"Phase 0 ...", "# Heading").
// Strip leading list/heading/quote markers and wrapping quotes or backticks so the
// text reads as a title; the full original stays available in the record.
export function cleanTitle(value) {
  let t = String(value ?? '').replace(/\s+/g, ' ').trim();
  for (let i = 0; i < 4; i++) {
    const next = t.replace(/^(?:[-*+>]|#{1,6})\s+/, '').replace(/^(["'`])(.*)\1$/, '$2').trim();
    if (next === t) break;
    t = next;
  }
  return t.replace(/^["'`]+(?=\S)/, '').trim();
}
