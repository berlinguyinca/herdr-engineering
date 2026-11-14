// The per-session error timeline (H05): H02's timeline split by session and read in the order H02 gave it — chronological by instant with H02's
// positional tiebreak, never re-sorted here. A row shows the step the event carries ("unknown" when it carries none, never one inferred from where
// it sits) and labels a payload's reported input error as an input error. The retry trace becomes a link only when the caller supplied an href, built
// through the existing escape helper so a url carrying a quote or markup cannot leave the attribute; without one the absence is stated instead of an
// href="#" pointing nowhere. Read-only: no controls, scripts, timers, fetch, clock or fs, and nothing added to the events it is handed.
import { escapeHtml } from './herdr-web-components/util.js';
import { describeUnavailable } from './diagnostics-source.js';

const plain = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
const text = (v) => (typeof v === 'string' ? v : '');
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
export const UNKNOWN = 'unknown';              // the only word for a step, type or instant the event itself does not carry
export const NO_SESSION = 'unknown session';   // the bucket for an event that reports no sessionId — never a fabricated session id

const at = (event) => (finite(plain(event) ? event.at : undefined) ? event.at : Infinity), first = (list) => Math.min(...list.map(at));   // Infinity - Infinity is NaN, so the session id settles a session tie
const when = (ms) => (finite(ms) ? new Date(ms).toISOString().slice(0, 16).replace('T', ' ') : UNKNOWN);   // the record's own instant, never a clock reading

/** The events keyed by the session id each one reports, every list kept in the order given — which is H02's timeline order. The caller's records and arrays are not changed. */
export function groupBySession(events) {
  const groups = Object.create(null);   // a null prototype: a session id of "__proto__" stays one session id
  for (const event of Array.isArray(events) ? events : []) (groups[text(plain(event) ? event.sessionId : '') || NO_SESSION] ||= []).push(event);
  return groups;
}

/** The step the event carries, as text, taken only from what the event reports — its own step, else its payload's; an event carrying none is "unknown". */
export function stepOf(event) {
  for (const value of [plain(event) ? event.step : undefined, plain(event?.payload) ? event.payload.step : undefined]) {
    if (finite(value)) return String(value); if (text(value).trim()) return text(value).trim();
  }
  return UNKNOWN;
}

/** The input error an event's payload reports, or '' when it reports none — a payload that says only that input failed still says that much, and no cause is added. */
export function inputErrorOf(event) {
  const payload = plain(event?.payload) ? event.payload : {};
  const value = payload.inputError === undefined ? payload.input_error : payload.inputError;
  if (plain(value)) return text(value.message) || 'input error reported';
  return text(value) || (value === true ? 'input error reported' : '');
}

const row = (event) => { const e = plain(event) ? event : {}, step = stepOf(e), error = inputErrorOf(e);   // event id and step carry H02's values; nothing is numbered from position
  return `<li class="diag-tl-row" data-tl-row data-event="${escapeHtml(text(e.id) || UNKNOWN)}" data-step="${escapeHtml(step)}"${error ? ' data-input-error="true"' : ''}>`
    + `<b>${escapeHtml(text(e.eventType) || UNKNOWN)}</b><span>step ${escapeHtml(step)}</span><time>${escapeHtml(when(e.at))}</time>`
    + `${error ? `<p class="diag-tl-error" role="alert">input error: ${escapeHtml(error)}</p>` : ''}</li>`; };

// Only a real href becomes a link, and only an escaped one; anything else says the trace is unavailable rather than linking to "#" or to itself.
const trace = (href) => (text(href).trim() ? `<a class="diag-tl-trace" data-tl-trace="link" href="${escapeHtml(href)}">Retry trace</a>`
  : `<p class="diag-tl-note" data-tl-trace="unavailable">No retry trace link was provided, so the retry trace for this session is unavailable</p>`);

const section = (sessionId, events, href) => `<section class="diag-tl-session" data-tl-session="${escapeHtml(sessionId)}"><h3>${escapeHtml(sessionId === NO_SESSION ? 'no session reported' : sessionId)}</h3>`
  + `<ul class="diag-tl" role="list">${events.map(row).join('')}</ul>${trace(href)}</section>`;

const STYLE = '<style>.diag-tl{list-style:none;margin:8px 0;padding:0;display:grid;gap:4px}.diag-tl-session h3{margin:8px 0 0;font-size:14px;overflow-wrap:anywhere}.diag-tl-row{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;font-size:13px}.diag-tl-row time{font-size:11px;color:var(--text-secondary)}.diag-tl-error{margin:0;font-size:12px;color:var(--status-failed);overflow-wrap:anywhere}.diag-tl-note,.diag-tl-trace{font-size:13px;margin:8px 0;overflow-wrap:anywhere}</style>';

/** One section per session — sessions ordered by their earliest event instant, then by session id, so the same events always render byte-identical HTML, and rows keep H02's order. */
export function renderSessionTimeline(events, { traceHref, unavailable } = {}) {
  const withheld = describeUnavailable(unavailable);   // H03's vocabulary for a feed that could not be read; naming it here invents nothing
  const note = withheld ? `<p class="diag-tl-note" role="alert" data-tl-unavailable>${escapeHtml(`${withheld}, so a session absent here is not evidence that nothing happened`)}</p>` : '';
  const grouped = Object.entries(groupBySession(events)).sort((a, b) => first(a[1]) - first(b[1]) || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)).map(([id, list]) => section(id, list, traceHref));
  return `${STYLE}${note}${grouped.join('') || (withheld ? '' : '<p class="diag-tl-note" data-tl-empty>No timeline events were reported by the feeds that could be read</p>')}`;
}
