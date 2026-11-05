// The error issue list (H04): what the host diagnostics feeds reported, as read-only cards — fingerprint, severity, occurrences,
// sessions, last seen and recovery state. Nothing is fetched, clicked, timed or counted here, and nothing is invented: a figure the record
// does not carry reads "unknown" (never zero, never "resolved"), a record H02 refuses is shown as a rejected record instead of a half-rendered
// card, and a section H03 could not read says so instead of an empty list that looks like "no issues". An issue record is either an H02 incident
// or { incident, occurrences?, sessions?, lastSeen?, recoveryState? } — the grouping fields are what the feed reported, never supplied here.
import { normalizeIncident, SEVERITIES } from './diagnostics-model.js';
import { describeUnavailable } from './diagnostics-source.js';
import { escapeHtml } from './herdr-web-components/util.js';

export { escapeHtml };

const plain = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
const text = (v) => (typeof v === 'string' ? v : '');
const UNKNOWN = 'unknown';
const count = (v) => (Array.isArray(v) ? new Set(v.map(text).filter(Boolean)).size : Number.isInteger(v) && v >= 0 ? v : null);
const seen = (ms) => (finite(ms) ? new Date(ms).toISOString().slice(0, 16).replace('T', ' ') : UNKNOWN);   // the record's own instant, never a clock reading
const rank = (severity) => SEVERITIES.length - 1 - SEVERITIES.indexOf(severity);   // critical > error > warning > info

/** One issue as the view sees it: validated through H02, carrying only the fields the record itself reported. */
export function issueView(raw) {
  const report = plain(raw) ? raw : {}, inner = plain(report.incident) ? report.incident : raw;
  const result = normalizeIncident(inner);
  if (!result.ok) return { ok: false, fingerprint: text(report.fingerprint) || text(inner?.id) || UNKNOWN, errors: result.errors };
  const i = result.incident, lastSeen = finite(report.lastSeen) ? report.lastSeen : finite(i.at) ? i.at : null;
  return {
    ok: true, fingerprint: i.id, severity: i.severity, message: text(i.message), lastSeen, when: seen(lastSeen),
    occurrences: count(report.occurrences), recovery: text(report.recoveryState) || text(report.recovery) || UNKNOWN,
    sessions: count(report.sessions ?? (text(i.sessionId) ? [i.sessionId] : null)),   // one reported session id is one session; none reported is unknown
  };
}

/** Severity rank first, then last seen descending, then fingerprint ascending; equal on all three keeps input order (sort is stable). */
export function compareIssues(a, b) {
  return rank(a.severity) - rank(b.severity) || (b.lastSeen ?? -Infinity) - (a.lastSeen ?? -Infinity) || (a.fingerprint < b.fingerprint ? -1 : a.fingerprint > b.fingerprint ? 1 : 0);
}

const STYLE = '<style>.diag-list{list-style:none;margin:12px 0;padding:0;display:grid;gap:8px}.diag-card{border:1px solid var(--border);border-radius:8px;padding:8px 12px}.diag-card h3{margin:0;font-size:14px;overflow-wrap:anywhere}.diag-card p{margin:4px 0 0;font-size:13px;color:var(--text-secondary);overflow-wrap:anywhere}.diag-fields{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 0}.diag-field{border:1px solid var(--border-subtle);border-radius:4px;padding:0 8px}.diag-field b{font-size:13px}.diag-field span{font-size:11px;color:var(--text-secondary)}.diag-card[data-severity=critical],.diag-card[data-severity=error]{border-left:4px solid var(--status-failed)}.diag-card[data-severity=warning]{border-left:4px solid var(--status-attention)}.diag-card[data-recovery=unknown] b{color:var(--text-muted)}.diag-note{font-size:13px;color:var(--text-secondary);margin:8px 0;overflow-wrap:anywhere}.diag-rejected{border-left:4px solid var(--status-failed);padding-left:8px;font-size:13px;color:var(--text-secondary);overflow-wrap:anywhere}</style>';

const field = (label, value) => `<div class="diag-field"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`;
const card = (v) => `<li class="diag-card" data-diag-card data-severity="${escapeHtml(v.severity)}" data-recovery="${escapeHtml(v.recovery)}">`
  + `<h3>${escapeHtml(v.fingerprint)}</h3><div class="diag-fields">${field('severity', v.severity)}${field('occurrences', v.occurrences === null ? UNKNOWN : String(v.occurrences))}`
  + `${field('sessions', v.sessions === null ? UNKNOWN : String(v.sessions))}${field('last seen', v.when)}${field('recovery state', v.recovery)}</div>`
  + `${v.message ? `<p>${escapeHtml(v.message)}</p>` : ''}</li>`;

/** The issue list, read-only: escaped cards in a fixed order, rejected records shown as such, and H03's unavailable feeds as a banner. */
export function renderIssueList(incidents, { state } = {}) {
  const views = (Array.isArray(incidents) ? incidents : []).map(issueView);
  const cards = views.filter((v) => v.ok).sort(compareIssues).map(card).join('');
  const rejected = views.filter((v) => !v.ok).map((v) => `<li class="diag-rejected" data-diag-rejected>rejected record ${escapeHtml(v.fingerprint)}: ${escapeHtml(v.errors.join('; '))}</li>`).join('');
  const note = (key, msg) => `<p class="diag-note" role="alert" data-diag-${key}>${escapeHtml(msg)}</p>`;
  const withheld = describeUnavailable(state?.unavailable);
  const banner = withheld ? note('unavailable', `${withheld}, so what is listed here — or not listed — is not evidence about the part that could not be read`)
    : plain(state) && state.partial ? note('partial', 'the feeds reported a partial page, so issues beyond what arrived are not listed') : '';
  return `${STYLE}${banner}${!views.length && !banner ? note('empty', 'nothing was reported by the feeds that could be read') : ''}<ul class="diag-list" role="list" aria-label="Error issues">${cards}${rejected}</ul>`;
}
