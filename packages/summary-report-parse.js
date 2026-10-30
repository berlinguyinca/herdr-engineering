// Structure of a /summary report, read from its markdown. Pure, so it runs under `node --test`.
// Best effort: a report that does not match the expected shape falls back to plain rendering (sections: []).
const SECTION = /^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(\d{1,2})[.)]\s+(?:\*\*)?([^*\n]+?)(?:\*\*)?\s*(?:[—:-]\s*(.*))?$/;
const COVERAGE = /\*{0,2}Evidence coverage\*{0,2}\s*:?\s*\*{0,2}\s*`?(complete|partial|limited)\b/i;
const STATUS = /\*\*Overall status:\*\*\s*`?([^\n.`;,]+)/i;

export function parseReport(markdown) {
  const text = typeof markdown === 'string' ? markdown : '';
  const lines = text.split('\n');
  const sections = [];
  let current = null, preface = [];
  for (const line of lines) {
    const m = SECTION.exec(line);
    if (m && Number(m[1]) === sections.length + 1 && m[2].trim().length < 80) {
      current = {n: Number(m[1]), title: m[2].trim(), lines: m[3] ? [m[3]] : []};
      sections.push(current);
    } else if (/^\s*(?:#{1,6}\s*)?\*{0,2}Evidence coverage\b/i.test(line) && sections.length) {
      current = {n: 0, title: 'Evidence coverage', lines: [line.replace(/^\s*#{1,6}\s*/, '')]};
      sections.push(current);
    } else (current ? current.lines : preface).push(line);
  }
  const coverage = (COVERAGE.exec(text) || [])[1]?.toLowerCase() || '';
  const status = ((STATUS.exec(text) || [])[1] || '').trim();
  const resume = sections.find(s => /resume point/i.test(s.title));
  return {
    status, coverage,
    resume: resume ? resume.lines.join('\n').trim() : '',
    preface: preface.join('\n').trim(),
    sections: sections.length >= 3 ? sections.map(s => ({n: s.n, title: s.title, body: s.lines.join('\n').trim()})) : [],
  };
}

// full_report.basis_seq is the newest event the run could see; newer loaded events make it stale.
export function staleness(basisSeq, newestSeq, events) {
  if (!Number.isFinite(basisSeq) || !Number.isFinite(newestSeq) || newestSeq <= basisSeq) return {stale: false, count: 0};
  const count = (events || []).filter(e => e.seq > basisSeq && e.kind === 'message').length;
  return {stale: true, count};
}

export function elapsedLabel(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
}

// Closest conversation turn at or before an ISO-ish timestamp; null when nothing is within a day.
export function nearestTurn(ts, turns) {
  const t = Date.parse(String(ts).replace(' ', 'T') + (/[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? '' : 'Z'));
  if (!Number.isFinite(t)) return null;
  let best = null, gap = Infinity;
  for (const e of turns || []) {
    const d = Math.abs(Date.parse(e.ts) - t);
    if (d < gap) { gap = d; best = e; }
  }
  return best && gap <= 86400000 ? best.seq : null;
}

export const TIMESTAMP = /\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?/g;
