// herdr-web-components — formatting helpers (pure, node-testable).
// Monospace is reserved for terminal/code/paths/IDs/hashes/logs; these
// helpers only produce plain text and byte/duration/timestamp strings.

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

export function formatBytes(n) {
  if (n == null || Number.isNaN(n)) return '—';
  if (n === 0) return '0 B';
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const str = Number.isInteger(value)
    ? String(value)
    : value.toFixed(1).replace(/\.0$/, '');
  return `${str} ${UNITS[unit]}`;
}

// Duration in ms -> compact human string (e.g. '2m 5s', '1.2h').
export function formatDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`;
  const s = ms / 1000;
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const remS = Math.round(s % 60);
  if (m < 60) return `${m}m ${remS}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  if (h < 24) return `${h}h ${remM}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

// ISO timestamp -> localized string, or '—' when absent/invalid.
export function formatTimestamp(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString();
}

// Token count with thousands separators; '—' when null (never fabricate).
export function formatTokens(n) {
  if (n == null || Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US');
}
