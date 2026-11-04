// The Reviews page's first two sections, as pure functions over the review records the server already holds:
//  - "Needs a decision": reviews waiting on the operator, each with the reason it is listed;
//  - a pass/fail trend by UTC day or week, where a review with no known outcome is never counted as a pass or a fail.
// Nothing here guesses: a record without a usable status, decision or time is counted under "unknown" or "undated".
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {stackedDayPlot, chartTable} from './ci-charts.js';

export const MAX_RECORDS = 5000;                       // bound the work and the page; the API pages far below this
export const CHANGES_WINDOW_MS = 14 * 24 * 3600 * 1000; // changes requested longer ago are history, not a to-do list
export const PERIODS = [['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 13 weeks (by week)']];
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
const idOf = r => text(r.review_id) || text(r.id);


// Which reviews can answer which: the same kind (plan or code) and the same pull request; only when NEITHER names a pull
// request does the agent session stand in. A review of one pull request never answers another, and a session-only review
// never answers a pull-request review.
const sameTarget = (a, b) => {
  if (text(a.kind) !== text(b.kind)) return false;
  const pa = prKey(a), pb = prKey(b);
  if (pa || pb) return pa === pb;
  return !!text(a.session_id) && a.session_id === b.session_id;
};
const prKey = r => (Number.isInteger(r.pull_request_number) && text(r.repository) ? `${r.repository.toLowerCase()}#${r.pull_request_number}` : null);
const hasTarget = r => !!prKey(r) || !!text(r.session_id);

/**
 * Reviews that wait on the operator, most urgent first:
 *  awaiting  - ready and waiting for a decision in Plannotator;
 *  stale     - not finished and its launcher stopped reporting (or it never became ready), so nobody can decide it;
 *  unanswered- changes were requested in the last 14 days and no later COMPLETED review (approved or changes requested) of
 *              the same kind and pull request exists. A later review still in progress keeps it listed, labelled as such;
 *              a later failed or closed review does not answer it. 'unverified' when the review names neither a pull
 *              request nor an agent, since an answer cannot be looked for.
 * Within a group the longest-waiting review comes first.
 */
export function reviewsNeedingDecision(records, now = Date.now(), {windowMs = CHANGES_WINDOW_MS} = {}) {
  const all = objects(records).slice(0, MAX_RECORDS);
  const out = [];
  for (const r of all) {
    const status = lower(r.status ?? r.state), outcome = reviewOutcome(r), since = startedAt(r);
    const base = {id: idOf(r), record: r, since, ageMs: since === null ? null : Math.max(0, now - since)};
    if (outcome === 'open') {
      if (r.stale === true) out.push({...base, kind: 'stale', rank: 1, reason: status === 'starting' ? 'Never became ready: the launcher stopped reporting.' : 'Waiting for a decision, but the launcher stopped reporting.'});
      else if (status === 'awaiting_review') out.push({...base, kind: 'awaiting', rank: 0, reason: 'Waiting for your decision in Plannotator.'});
      continue;
    }
    if (outcome !== 'changes') continue;
    const at = decidedAt(r) ?? since;
    if (at === null || now - at > windowMs) continue;       // an undated or old request is not a to-do item
    let answered = false, inProgress = false;
    if (hasTarget(r) && since !== null) {
      for (const other of all) {
        if (other === r || !sameTarget(r, other)) continue;
        const later = startedAt(other);
        if (later === null || later <= since) continue;
        const o = reviewOutcome(other);
        if (o === 'approved' || o === 'changes') { answered = true; break; }
        if (o === 'open') inProgress = true;
      }
    }
    if (answered) continue;
    const item = {...base, since: at, ageMs: Math.max(0, now - at)};
    if (!hasTarget(r)) out.push({...item, kind: 'unverified', rank: 2, reason: 'Changes were requested; this review names no pull request or agent, so whether it was answered cannot be checked.'});
    else out.push({...item, kind: inProgress ? 'rereview' : 'unanswered', rank: 2,
      reason: inProgress ? 'Changes were requested and a later review of the same pull request is in progress.' : 'Changes were requested and no later completed review of the same pull request or agent exists.'});
  }
  out.sort((a, b) => a.rank - b.rank || (a.since ?? Infinity) - (b.since ?? Infinity) || a.id.localeCompare(b.id));
  return out;
}

/** What the needs-decision list could not look at: records past the bound, and change requests with no usable time. */
export function decisionGaps(records) {
  const all = objects(records), used = all.slice(0, MAX_RECORDS);
  let undated = 0;
  for (const r of used) if (reviewOutcome(r) === 'changes' && (decidedAt(r) ?? startedAt(r)) === null) undated++;
  return {omitted: all.length - used.length, undated};
}

const dayStart = t => { const d = new Date(t); d.setUTCHours(0, 0, 0, 0); return d.getTime(); };
const weekStart = t => { const d = dayStart(t); return d - ((new Date(d).getUTCDay() + 6) % 7) * DAY; };
const dayKey = t => new Date(t).toISOString().slice(0, 10);

/**
 * Approved / changes requested / failed per UTC day over the last 7 or 30 days, or per Monday-start UTC week over the last
 * 13 weeks (period 90). The window starts on a day or week boundary, so it holds exactly 7, 30 or 13 buckets, every one
 * present even when empty. `closed`, `open` and `unknown` reviews never reach the chart; they are counted apart, so a pass
 * rate is never computed from a review whose outcome was not recorded. `undated`: ended but no usable time; `future`: dated
 * more than a day ahead of now (a clock fault), not plotted.
 */
export function reviewTrend(records, {period = 30, now = Date.now()} = {}) {
  const days = [30, 7, 90].includes(Number(period)) ? Number(period) : 30;
  const weekly = days > 30, step = weekly ? 7 * DAY : DAY, count = weekly ? 13 : days;
  const first = (weekly ? weekStart(now) : dayStart(now)) - (count - 1) * step;
  const all = objects(records), used = all.slice(0, MAX_RECORDS);
  const buckets = new Map();
  for (let i = 0; i < count; i++) buckets.set(dayKey(first + i * step), {day: dayKey(first + i * step), approved: 0, changes: 0, failed: 0});
  const model = {period: days, bucket: weekly ? 'week' : 'day', days: [], plotted: 0, undecided: 0, closed: 0, unknown: 0, undated: 0, outside: 0, future: 0, omitted: all.length - used.length, total: used.length};
  for (const r of used) {
    const outcome = reviewOutcome(r);
    if (outcome === 'open') { model.undecided++; continue; }
    if (outcome === 'unknown') { model.unknown++; continue; }
    const at = decidedAt(r);
    if (at === null) { model.undated++; continue; }
    if (at > now + DAY) { model.future++; continue; }
    if (at < first) { model.outside++; continue; }
    if (outcome === 'closed') { model.closed++; continue; }
    const slot = Math.min(at, now);   // up to a day of clock skew counts as now
    buckets.get(dayKey(weekly ? weekStart(slot) : dayStart(slot)))[outcome]++;
    model.plotted++;
  }
  model.days = model.plotted ? [...buckets.values()] : [];   // an all-empty window is said in words, not drawn
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

/**
 * `rowHtml(item)` returns the review row markup (name, repository, links); this adds the count, the reason, the empty state
 * and what could not be checked. `coverage`: {hasMore, loaded, total} describe the review list the page holds,
 * {waitingKnown, waitingHasMore, waitingTotal} the separate complete list of open reviews, `gaps` from decisionGaps().
 * "Nothing is waiting" is said only when every review was looked at; otherwise the empty state says what was not.
 */
export function renderReviewDecisions(items, rowHtml, {loading = false, unavailable = false, placeholder = '', coverage = {}} = {}) {
  const list = Array.isArray(items) ? items : [];
  const {hasMore = false, loaded = 0, total = null, waitingKnown = false, waitingHasMore = false, waitingTotal = null, gaps = {}} = coverage;
  const of = total === null || total === undefined ? `${loaded}` : `${loaded} of ${total}`;
  const caveats = [
    hasMore && `${of} reviews are loaded; older reviews may have changes requested that nobody answered.`,
    hasMore && !waitingKnown && 'The list of reviews waiting in Plannotator could not be read, so older waiting reviews may be missing.',
    waitingHasMore && `Only ${list.filter(i => i.kind === 'awaiting' || i.kind === 'stale').length} of ${waitingTotal} open reviews are shown.`,
    gaps.omitted && `${gaps.omitted} further reviews were not examined.`,
    gaps.undated && `${gaps.undated} reviews with requested changes have no usable time and are not listed.`,
  ].filter(Boolean);
  const complete = !caveats.length;
  const notes = caveats.length ? `<p class="rv-note" data-rv-caveat>${caveats.map(esc).join(' ')}</p>` : '';
  const body = unavailable ? '<p class="rv-empty" data-rv-unavailable>Reviews could not be loaded, so it is not known which are waiting. Retrying automatically.</p>'
    : loading ? placeholder
      : list.length ? list.map(item => `<div data-rv-item="${esc(item.id)}" data-rv-kind="${esc(item.kind)}">${rowHtml(item)}<p class="rv-why">${esc(item.reason)} Waiting ${esc(ageText(item.ageMs))}.</p></div>`).join('') + notes
        : complete ? '<p class="rv-empty" data-rv-clear>Nothing is waiting for a decision.</p>'
          : `<p class="rv-empty" data-rv-partial>No waiting review was found in the ${esc(of)} reviews checked, but that is not everything.</p>${notes}`;
  return `<section class="rv-section rv-need" aria-label="Reviews that need a decision"><h2>Needs a decision <span class="rv-count" data-rv-count role="status">${loading || unavailable ? '…' : list.length}</span></h2>
<p class="rv-note">Reviews waiting in Plannotator, reviews whose launcher went quiet, and changes requested in the last 14 days that no later completed review answered.</p>${body}</section>`;
}

export function renderReviewTrend(model, {loading = false, unavailable = false, placeholder = '', coverage = {}} = {}) {
  const select = `<div class="rv-controls"><label>Period<select data-rv-period>${PERIODS.map(([v, l]) => `<option value="${v}"${Number(v) === model.period ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label></div>`;
  if (loading || unavailable) return `<section class="rv-section rv-trend" aria-label="Review outcomes over time"><h2>Outcomes over time</h2>${select}${unavailable ? '<p class="rv-empty" data-rv-unavailable>Reviews could not be loaded, so no outcomes can be shown yet.</p>' : placeholder}</section>`;
  const {hasMore = false, total = null} = coverage;
  const unit = model.bucket === 'week' ? 'UTC week starting' : 'UTC date';
  const plot = stackedDayPlot(model.days, SERIES, {id: 'rv-outcomes', title: `Review outcomes by ${model.bucket === 'week' ? 'UTC week' : 'UTC day'}`,
    desc: `${model.bucket === 'week' ? 'weeks' : 'dates'} in the period. Stacked counts distinguish approved, changes requested and failed reviews. Exact counts follow in Chart data.`,
    empty: 'No review with a recorded outcome and decision time falls in this period.'});
  const excluded = [model.undecided && `${model.undecided} not yet decided`, model.closed && `${model.closed} closed without a decision`, model.unknown && `${model.unknown} with no recorded outcome`,
    model.undated && `${model.undated} decided but undated`, model.future && `${model.future} future-dated`, model.outside && `${model.outside} outside the period`].filter(Boolean);
  const partial = hasMore ? ` ${model.total} of ${total ?? 'more'} reviews are loaded; older outcomes are missing, so earlier days and weeks can look emptier than they were.` : '';
  return `<section class="rv-section rv-trend" aria-label="Review outcomes over time"><h2>Outcomes over time</h2>${select}
<p class="rv-note" data-rv-covers>${model.plotted} of ${model.total} loaded reviews are plotted${excluded.length ? `; not plotted: ${excluded.join(', ')}` : ''}.${model.omitted ? ` ${model.omitted} further reviews were not read.` : ''}${partial} Outcomes that are not recorded are never counted as approved or failed.${hasMore || model.omitted ? '' : ' This covers every review the server returned.'}</p>
${plot}<div class="rv-legend">${SERIES.map(s => `<span style="--chart-color:${s.color}">${esc(s.label)}</span>`).join('')}</div>
<details class="rv-data"><summary>Chart data</summary>${chartTable([unit, 'Approved', 'Changes requested', 'Failed'], model.days.map(d => [d.day, d.approved, d.changes, d.failed]))}</details></section>`;
}
