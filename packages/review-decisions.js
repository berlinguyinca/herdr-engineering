// The Reviews page's first two sections, as pure functions over the review records the server already holds:
//  - "Needs a decision": reviews waiting on the operator, each with the reason it is listed;
//  - a pass/fail trend by UTC day or week, where a review with no known outcome is never counted as a pass or a fail.
// Nothing here guesses: a record without a usable status, decision or time is counted under "unknown" or "undated".
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {stackedDayPlot, chartTable} from './ci-charts.js';

export const MAX_RECORDS = 5000;                       // bound the work and the page; the API pages far below this
export const CHANGES_WINDOW_MS = 14 * 24 * 3600 * 1000; // changes requested longer ago are history, not a to-do list
export const PERIODS = [['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days (by week)']];
const DAY = 24 * 3600 * 1000;

const lower = value => (typeof value === 'string' ? value.toLowerCase().trim().replaceAll('-', '_') : '');
export const reviewTime = value => {
  if (typeof value === 'number') return Number.isFinite(value) && value > 0 ? (value < 1e12 ? value * 1000 : value) : null;
  if (typeof value !== 'string' || !value) return null;
  const t = Date.parse(value);
  return Number.isFinite(t) && t > 0 ? t : null;
};
const objects = list => (Array.isArray(list) ? list.filter(r => r && typeof r === 'object' && !Array.isArray(r)) : []);
const text = value => (typeof value === 'string' ? value : '');

const CHANGES = new Set(['changes_requested', 'request_changes', 'rejected', 'annotated', 'denied']);
const APPROVED = new Set(['approved', 'approve', 'accepted']);
const CLOSED = new Set(['closed', 'dismissed', 'cancelled', 'canceled']);
const FAILED = new Set(['failed', 'error', 'errored']);
const OPEN = new Set(['starting', 'awaiting_review', 'pending', 'running', 'in_progress', 'open', 'waiting']);

/**
 * What the review ended as: 'approved' | 'changes' | 'failed' | 'closed' (ended without a decision) | 'open' | 'unknown'.
 * A decision or verdict wins over the status; a bare "done" with no verdict is 'unknown', never a pass.
 */
export function reviewOutcome(record) {
  const r = record && typeof record === 'object' ? record : {};
  const status = lower(r.status ?? r.state);
  const verdicts = [lower(r.decision), lower(r.verdict)].filter(Boolean);
  if (FAILED.has(status)) return 'failed';
  for (const v of verdicts) {
    if (CHANGES.has(v)) return 'changes';
    if (APPROVED.has(v)) return 'approved';
    if (CLOSED.has(v)) return 'closed';
  }
  if (CHANGES.has(status)) return 'changes';
  if (APPROVED.has(status)) return 'approved';
  if (CLOSED.has(status)) return 'closed';
  if (OPEN.has(status)) return 'open';
  return 'unknown';
}

// The decision time: when the review finished. A finished review without one is undated, not placed on a guessed day.
const decidedAt = r => reviewTime(r.completed_at) ?? reviewTime(r.decided_at) ?? reviewTime(r.finished_at);
const startedAt = r => reviewTime(r.created_at) ?? reviewTime(r.started_at) ?? reviewTime(r.updated_at);
const target = r => {
  const n = Number.isInteger(r.pull_request_number) ? r.pull_request_number : null;
  if (n && text(r.repository)) return `pr:${r.repository.toLowerCase()}#${n}`;
  if (text(r.session_id)) return `session:${r.session_id}`;
  return null;
};
const idOf = r => text(r.review_id) || text(r.id);

/**
 * Reviews that wait on the operator, most urgent first:
 *  awaiting  - ready and waiting for a decision in Plannotator;
 *  stale     - not finished and its launcher stopped reporting (or it never became ready), so nobody can decide it;
 *  unanswered- changes were requested in the last 14 days and no later review exists for the same pull request or agent
 *              ('unverified' when the review names neither, since an answer cannot be looked for).
 * Within a group the longest-waiting review comes first.
 */
export function reviewsNeedingDecision(records, now = Date.now(), {windowMs = CHANGES_WINDOW_MS} = {}) {
  const all = objects(records).slice(0, MAX_RECORDS);
  const newestByTarget = new Map();
  for (const r of all) {
    const key = target(r), at = startedAt(r);
    if (key && at !== null && at > (newestByTarget.get(key) ?? 0)) newestByTarget.set(key, at);
  }
  const out = [];
  for (const r of all) {
    const status = lower(r.status ?? r.state), outcome = reviewOutcome(r), since = startedAt(r);
    const base = {id: idOf(r), record: r, since, ageMs: since === null ? null : Math.max(0, now - since)};
    if (outcome === 'open') {
      if (r.stale === true) out.push({...base, kind: 'stale', rank: 1, reason: status === 'starting' ? 'Never became ready: the launcher stopped reporting.' : 'Waiting for a decision, but the launcher stopped reporting.'});
      else if (status === 'awaiting_review') out.push({...base, kind: 'awaiting', rank: 0, reason: 'Waiting for your decision in Plannotator.'});
      continue;
    }
    if (outcome === 'changes') {
      const at = decidedAt(r) ?? since;
      if (at === null || now - at > windowMs) continue;       // an undated or old request is not a to-do item
      const key = target(r);
      if (key && (newestByTarget.get(key) ?? 0) > startedAt(r)) continue;   // a later review exists for the same target
      out.push({...base, kind: key ? 'unanswered' : 'unverified', rank: 2, since: at, ageMs: Math.max(0, now - at),
        reason: key ? 'Changes were requested and no later review of the same pull request or agent exists.'
          : 'Changes were requested; this review names no pull request or agent, so whether it was answered cannot be checked.'});
    }
  }
  out.sort((a, b) => a.rank - b.rank || (a.since ?? Infinity) - (b.since ?? Infinity) || a.id.localeCompare(b.id));
  return out;
}

const dayKey = t => new Date(t).toISOString().slice(0, 10);
const weekKey = t => { const d = new Date(t); d.setUTCHours(0, 0, 0, 0); d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7)); return d.toISOString().slice(0, 10); };

/**
 * Approved / changes requested / failed per UTC day (or week for 90 days) over the period.
 * `closed`, `open` and `unknown` reviews never reach the chart; they are counted in `undecided`/`unknown`, so a pass rate
 * is never computed from a review whose outcome was not recorded. `undated`: ended but no usable time.
 */
export function reviewTrend(records, {period = 30, now = Date.now()} = {}) {
  const days = [30, 7, 90].includes(Number(period)) ? Number(period) : 30;
  const weekly = days > 30, since = now - days * DAY;
  const all = objects(records), used = all.slice(0, MAX_RECORDS);
  const buckets = new Map();
  const model = {period: days, bucket: weekly ? 'week' : 'day', days: [], plotted: 0, undecided: 0, closed: 0, unknown: 0, undated: 0, outside: 0, omitted: all.length - used.length, total: used.length};
  for (const r of used) {
    const outcome = reviewOutcome(r);
    if (outcome === 'open') { model.undecided++; continue; }
    if (outcome === 'unknown') { model.unknown++; continue; }
    const at = decidedAt(r);
    if (at === null) { model.undated++; continue; }
    if (at < since || at > now + DAY) { model.outside++; continue; }
    if (outcome === 'closed') { model.closed++; continue; }
    const day = weekly ? weekKey(at) : dayKey(at);
    if (!buckets.has(day)) buckets.set(day, {day, approved: 0, changes: 0, failed: 0});
    buckets.get(day)[outcome]++;
    model.plotted++;
  }
  model.days = [...buckets.values()].sort((a, b) => a.day.localeCompare(b.day));
  return model;
}

export const reviewDecisionStyles = `<style>
.rv-section{margin:16px 0;padding:12px 16px;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface)}
.rv-section h2{font-size:15px;margin:0 0 4px;display:flex;align-items:center;gap:8px}.rv-count{font-size:12px;font-weight:600;padding:0 8px;border:1px solid var(--border);border-radius:99px}
.rv-note{font-size:12px;line-height:1.6;color:var(--text-secondary);margin:4px 0}.rv-why{font-size:12px;color:var(--status-attention);margin:4px 0;line-height:1.6}
.rv-need .ig-row{border-top:1px solid var(--border)}.rv-need .ig-row:first-of-type{border-top:0}.rv-empty{font-size:13px;color:var(--text-secondary);margin:8px 0}
.rv-controls{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:8px 0}.rv-controls label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--text-secondary)}.rv-controls select{min-height:44px}
.rv-trend svg{display:block;width:100%;max-height:210px}.rv-trend svg text{font-size:11px;fill:var(--text-secondary);font-family:inherit}.rv-trend svg line{stroke:var(--border)}
.rv-legend{display:flex;flex-wrap:wrap;gap:12px;font-size:11px;color:var(--text-secondary)}.rv-legend span::before{content:'';display:inline-block;width:9px;height:9px;margin-right:5px;background:var(--chart-color);border-radius:2px}
.rv-data summary{min-height:44px;display:flex;align-items:center;cursor:pointer;font-size:13px}
@media(max-width:700px){.rv-section{padding:12px}}
</style>`;

const SERIES = [
  {key: 'approved', label: 'Approved', color: 'var(--status-ok)'},
  {key: 'changes', label: 'Changes requested', color: 'var(--status-attention)'},
  {key: 'failed', label: 'Failed', color: 'var(--status-failed)'},
];
export const ageText = ms => {
  if (ms === null || !Number.isFinite(ms)) return 'age unknown';
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min`;
  if (m < 2880) return `${Math.floor(m / 60)} h`;
  return `${Math.floor(m / 1440)} days`;
};

/** `rowHtml(item)` returns the review row markup (name, repository, links); this adds the count, the reason and the empty state. */
export function renderReviewDecisions(items, rowHtml, {loading = false, unavailable = false, placeholder = ''} = {}) {
  const list = Array.isArray(items) ? items : [];
  const body = unavailable ? '<p class="rv-empty" data-rv-unavailable>Reviews could not be loaded, so it is not known which are waiting. Retrying automatically.</p>' : loading ? placeholder
    : list.length ? list.map(item => `<div data-rv-item="${esc(item.id)}" data-rv-kind="${esc(item.kind)}">${rowHtml(item)}<p class="rv-why">${esc(item.reason)} Waiting ${esc(ageText(item.ageMs))}.</p></div>`).join('')
      : '<p class="rv-empty" data-rv-clear>Nothing is waiting for a decision.</p>';
  return `<section class="rv-section rv-need" aria-label="Reviews that need a decision"><h2>Needs a decision <span class="rv-count" data-rv-count>${loading ? '…' : list.length}</span></h2>
<p class="rv-note">Reviews waiting in Plannotator, reviews whose launcher went quiet, and changes requested in the last 14 days that no later review answered.</p>${body}</section>`;
}

export function renderReviewTrend(model, {loading = false, unavailable = false, placeholder = ''} = {}) {
  const select = `<div class="rv-controls"><label>Period<select data-rv-period>${PERIODS.map(([v, l]) => `<option value="${v}"${Number(v) === model.period ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label></div>`;
  if (loading || unavailable) return `<section class="rv-section rv-trend" aria-label="Review outcomes over time"><h2>Outcomes over time</h2>${select}${unavailable ? '<p class="rv-empty" data-rv-unavailable>Reviews could not be loaded, so no outcomes can be shown yet.</p>' : placeholder}</section>`;
  const unit = model.bucket === 'week' ? 'UTC week starting' : 'UTC date';
  const plot = stackedDayPlot(model.days, SERIES, {id: 'rv-outcomes', title: `Review outcomes by ${model.bucket === 'week' ? 'UTC week' : 'UTC day'}`,
    desc: `${model.bucket === 'week' ? 'weeks' : 'dates'} with decided reviews. Stacked counts distinguish approved, changes requested and failed reviews. Exact counts follow in Chart data.`,
    empty: 'No review with a recorded outcome and decision time falls in this period.'});
  const excluded = [model.undecided && `${model.undecided} not yet decided`, model.closed && `${model.closed} closed without a decision`, model.unknown && `${model.unknown} with no recorded outcome`,
    model.undated && `${model.undated} decided but undated`, model.outside && `${model.outside} outside the period`].filter(Boolean);
  return `<section class="rv-section rv-trend" aria-label="Review outcomes over time"><h2>Outcomes over time</h2>${select}
<p class="rv-note" data-rv-covers>${model.plotted} of ${model.total} loaded reviews are plotted${excluded.length ? `; not plotted: ${excluded.join(', ')}` : ''}.${model.omitted ? ` ${model.omitted} further reviews were not read.` : ''} Outcomes that are not recorded are never counted as approved or failed. This covers the loaded reviews only, not a complete history.</p>
${plot}<div class="rv-legend">${SERIES.map(s => `<span style="--chart-color:${s.color}">${esc(s.label)}</span>`).join('')}</div>
<details class="rv-data"><summary>Chart data</summary>${chartTable([unit, 'Approved', 'Changes requested', 'Failed'], model.days.map(d => [d.day, d.approved, d.changes, d.failed]))}</details></section>`;
}
