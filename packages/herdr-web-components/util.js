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
