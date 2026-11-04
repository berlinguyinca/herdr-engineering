// WC-002: pure weekly activity model over WorkSummary v1 items (packages/work-summary.js).
// No server aggregation: no existing API supplies cross-entity dated events for a window (see handoffs/WC-002.md).
// Counts come only from real timestamps on items; absent evidence is 'unknown' (count null), never 0.

const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;
const parseTime = v => {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(v) || !HAS_ZONE.test(v.trim())) return null;
  const ms = Date.parse(v);
  return Number.isNaN(ms) ? null : ms;
};
const toMs = v => v instanceof Date ? v.getTime() : typeof v === 'number' ? v : Date.parse(v);

function formatter(timeZone) {
  return new Intl.DateTimeFormat('en-US', {timeZone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', second: 'numeric', weekday: 'short'}); // throws RangeError on a bad zone
}
const WEEKDAYS = {Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6};
function zoned(fmt, ms) {
  const p = Object.fromEntries(fmt.formatToParts(new Date(ms)).map(x => [x.type, x.value]));
  return {y: +p.year, m: +p.month, d: +p.day, h: +p.hour, mi: +p.minute, s: +p.second, wd: WEEKDAYS[p.weekday]};
}
const offsetAt = (fmt, ms) => { const z = zoned(fmt, ms); return Date.UTC(z.y, z.m - 1, z.d, z.h, z.mi, z.s) - Math.floor(ms / 1000) * 1000; };

/** UTC instant of local midnight on calendar date (y,m,d); if midnight does not exist (DST gap) the first instant after the gap. */
function localMidnight(fmt, y, m, d) {
  const wall = Date.UTC(y, m - 1, d);
  let t = wall - offsetAt(fmt, wall);
  t = wall - offsetAt(fmt, t); // re-evaluate at the candidate: correct across both DST directions
  const z = zoned(fmt, t);
  if (Date.UTC(z.y, z.m - 1, z.d, z.h, z.mi, z.s) < wall) t += 3600e3; // landed before a gap; step forward
  return t;
}

/** Half-open [start,end) week containing `now` in an IANA zone. weekStartsOn: 0=Sunday .. 6=Saturday. */
export function weekWindow({now, timeZone, weekStartsOn = 1}) {
  const fmt = formatter(timeZone), ms = toMs(now);
  if (!Number.isFinite(ms)) throw new RangeError('now is not a valid instant');
  if (!Number.isInteger(weekStartsOn) || weekStartsOn < 0 || weekStartsOn > 6) throw new RangeError('weekStartsOn must be 0..6');
  const z = zoned(fmt, ms), back = (z.wd - weekStartsOn + 7) % 7;
  const s = new Date(Date.UTC(z.y, z.m - 1, z.d - back)), e = new Date(Date.UTC(z.y, z.m - 1, z.d - back + 7));
  return {start: localMidnight(fmt, s.getUTCFullYear(), s.getUTCMonth() + 1, s.getUTCDate()),
    end: localMidnight(fmt, e.getUTCFullYear(), e.getUTCMonth() + 1, e.getUTCDate()), timeZone};
}

const hostOf = i => new Set((i.pane_refs || []).map(p => p.host_id));
/** Merge duplicate work_ids; a differing timestamp for the same item is a conflict (earliest kept, coverage degraded). */
function dedupe(items) {
  const byId = new Map();
  for (const it of items) {
    if (!it || typeof it !== 'object' || !it.work_id) continue;
    const prev = byId.get(it.work_id);
    if (!prev) { byId.set(it.work_id, {...it, _conflict: false}); continue; }
    const m = {...prev};
    for (const k of ['started_at', 'completed_at']) {
      const a = parseTime(prev[k]), b = parseTime(it[k]);
      if (a === null && b !== null) m[k] = it[k];
      else if (a !== null && b !== null && a !== b) { m._conflict = true; if (b < a) m[k] = it[k]; }
    }
    m.evidence = [...(prev.evidence || []), ...(it.evidence || [])];
    if (!m.lifecycle || m.lifecycle === 'unconfirmed') m.lifecycle = it.lifecycle;
    byId.set(it.work_id, m);
  }
  return [...byId.values()];
}
/** A standalone session already owned by a mission (same id, no conflicting host) is the mission's, not separate work. */
function dropOwnedSessions(items) {
  const owned = items.filter(i => i.source === 'mission');
  return items.filter(i => {
    if (i.source !== 'session') return true;
    return !(i.session_ids || []).every(id => owned.some(m => (m.session_ids || []).includes(id) && ![...hostOf(i)].some(h => hostOf(m).size && !hostOf(m).has(h))));
  });
}

const OPEN = new Set(['running', 'blocked', 'review', 'paused_quota']);
const quotaEvidenced = i => (i.evidence || []).some(e => e && e.kind === 'quota_status');
const degraded = i => i.coverage !== 'complete' || i.lifecycle === 'unconfirmed' || i._conflict;

function metric(count, coverage, extra = {}) { return {count: coverage === 'unknown' ? null : count, coverage, ...extra}; }
function history(inWindow, withTs, total, items, historyComplete) {
  if (!total || withTs === 0) return metric(0, 'unknown');
  const full = historyComplete && withTs === total && !items.some(degraded);
  return metric(inWindow, full ? 'complete' : 'partial');
}

/**
 * @param {{items: object[], now: number|string|Date, timeZone: string, weekStartsOn?: number, historyComplete?: boolean}} input
 * historyComplete must be asserted by the caller when the items cover every work item that existed in the window
 * (a present snapshot alone never does, so default false caps started/finished at 'partial').
 */
export function weeklyActivity({items = [], now, timeZone, weekStartsOn = 1, historyComplete = false}) {
  const win = weekWindow({now, timeZone, weekStartsOn});
  const work = dropOwnedSessions(dedupe(Array.isArray(items) ? items : []));
  const inWin = ms => ms !== null && ms >= win.start && ms < win.end;
  const asOf = new Date(toMs(now)).toISOString();

  const started = work.map(i => parseTime(i.started_at));
  const startedWithTs = started.filter(t => t !== null).length;
  const finished = work.filter(i => i.lifecycle === 'finished');
  const completed = finished.map(i => parseTime(i.completed_at));
  const completedWithTs = completed.filter(t => t !== null).length;

  const finishedMetric = !finished.length ? (work.length ? metric(0, historyComplete && !work.some(degraded) ? 'complete' : 'partial') : metric(0, 'unknown'))
    : completedWithTs === 0 ? metric(0, 'unknown')
    : metric(completed.filter(inWin).length, historyComplete && completedWithTs === finished.length && !work.some(degraded) ? 'complete' : 'partial');

  const open = work.filter(i => OPEN.has(i.lifecycle));
  const pausedAll = work.filter(i => i.lifecycle === 'paused_quota');
  const paused = pausedAll.filter(quotaEvidenced);
  const attention = work.filter(i => i.lifecycle === 'blocked' || i.lifecycle === 'review' ||
    (i.lifecycle === 'failed' && inWin(parseTime(i.updated_at))));
  const snapCov = work.length ? (work.some(degraded) ? 'partial' : 'complete') : 'unknown';

  return {
    window: {start: new Date(win.start).toISOString(), end: new Date(win.end).toISOString(), timeZone, weekStartsOn, interval: '[start,end)'},
    total_items: work.length,
    started: history(started.filter(inWin).length, startedWithTs, work.length, work, historyComplete),
    finished: finishedMetric,
    open: metric(open.length, snapCov, {basis: 'snapshot', as_of: asOf}),
    paused_quota: metric(paused.length, pausedAll.length > paused.length ? 'partial' : snapCov, {basis: 'snapshot', as_of: asOf, unevidenced: pausedAll.length - paused.length}),
    attention_needed: metric(attention.length, snapCov, {basis: 'snapshot', as_of: asOf}),
  };
}
