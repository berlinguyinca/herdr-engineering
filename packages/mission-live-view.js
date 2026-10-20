// Read-only Pi mission telemetry. Clocks redraw locally; polling never creates
// activity events, attention notifications, or session control operations.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const list = (value) => Array.isArray(value) ? value : [];
const records = (value) => list(value).filter(item => item && typeof item === 'object' && !Array.isArray(item));
const text = (value) => typeof value === 'string' ? value : '';
const human = (value) => text(value).replaceAll('_', ' ');
const bounded = (value, max = 8192) => text(value).slice(-max);
const duration = (seconds) => {
  if (!Number.isFinite(seconds)) return 'unknown';
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
};
const age = (timestamp, now) => timestamp && Number.isFinite(Date.parse(timestamp)) ? duration((now - Date.parse(timestamp)) / 1000) : 'unknown';
const clock = (label, at, now, end) => `<span><span data-clock-label="${esc(label)}">${esc(label)}</span> <span data-clock="${esc(at || '')}"${end ? ` data-end="${esc(end)}"` : ''}>${age(at, end ? Date.parse(end) : now)}</span></span>`;
const row = (label, value) => value == null || value === '' ? '' : `<div class="ml-row"><span class="muted">${esc(label)}</span><span>${esc(value)}</span></div>`;

export function freshnessAt(p, received, now = Date.now()) {
  const f = p.freshness || {};
  const seconds = Number.isFinite(f.age_seconds) ? f.age_seconds + Math.max(0, now - received) / 1000 : Infinity;
  let state = seconds >= (f.disconnect_after_seconds ?? 120) ? 'disconnected' : seconds >= (f.stale_after_seconds ?? 30) ? 'stale' : 'live';
  if (p.connection === 'disconnected' || f.state === 'disconnected') state = 'disconnected';
  else if (f.state === 'stale' && state === 'live') state = 'stale';
  if (state === 'live' && (p.connection === 'updating' || f.state === 'updating')) state = 'updating';
  return { state, age: seconds };
}
function worstHealth(a, mission) {
  // Lifecycle is authoritative after completion or a pause. Failed attempts
  // remain inspectable, but cannot override a running replacement worker.
  const status = text(mission.status).toUpperCase();
  if (status === 'COMPLETE') return 'complete';
  if (status === 'FAILED') return 'failed';
  if (status === 'CANCELED') return 'canceled';
  if (status === 'PAUSED' || status.startsWith('PAUSED_')) {
    return status === 'PAUSED' ? 'paused' : `paused: ${human(status.slice(7).toLowerCase())}`;
  }
  const ranks = ['failed', 'stalled', 'blocked', 'slow', 'waiting', 'active', 'complete'];
  const currentWorkers = records(a.workerDetails).filter(w =>
    !w.endedAt && ['running', 'waiting', 'stalled', 'blocked'].includes(w.state));
  const health = [a.health, ...currentWorkers.map(w => w.health || (w.state === 'running' ? 'active' : w.state))];
  return ranks.find(h => health.includes(h)) || 'unknown';
}
function progress(a) {
  const c = a.acceptanceCoverage;
  if (c && Number.isFinite(c.completed) && Number.isFinite(c.total) && c.total > 0 && c.completed >= 0 && c.completed <= c.total) {
    const percent = Math.floor(c.completed / c.total * 100);
    return `<div class="ml-progress"><label>${c.completed} / ${c.total} acceptance criteria verified · ${percent}%<progress max="${c.total}" value="${c.completed}" aria-label="Acceptance criteria verified"></progress></label></div>`;
  }
  return '<div class="muted">Progress unknown · no acceptance measurements reported</div>';
}
function model(w) { return [w.route, w.model].filter(Boolean).join(' → ') || 'Model not reported'; }
function freshnessLabel(f, health) {
  if (f.state === 'updating') return `Updating telemetry · current details unavailable · last reported ${health}`;
  return f.state === 'live' ? `Live telemetry · ${health}` : `${f.state === 'stale' ? 'Stale' : 'Disconnected'} telemetry · current state unknown · last reported ${health}`;
}
export function missionCard(p, now = Date.now(), detail = false) {
  const a = p.activity || {}, m = p.mission || {};
  const health = worstHealth(a, m), f = freshnessAt(p, now, now);
  return `<article class="hd-card ml-card" data-mission-key="${esc(p.mission_key)}">
    <div class="hd-card__title">${detail ? esc(m.title || m.id) : `<a href="#/missions/${encodeURIComponent(p.mission_key)}">${esc(m.title || m.id)}</a>`}</div>
    <div class="ml-freshness" data-freshness data-health="${esc(health)}" data-state="${f.state}">${esc(freshnessLabel(f, health))}</div>
    <div class="muted">Pi · ${esc(p.host_id || 'Host unknown')} · ${esc(p.mission_key)} · ${esc(m.status || 'State unknown')}</div>
    <p><strong data-activity-label>${f.state === 'live' ? 'Activity' : 'Last known activity'}:</strong> ${esc(a.currentActivity?.summary || 'No current activity reported')}</p>
    ${a.currentObjective ? row('Objective', a.currentObjective) : ''}
    <div class="muted" data-evidence-label>${f.state === 'live' ? 'Current reported measurements' : 'Last recorded measurements'}</div>
    ${progress(a)}
    ${a.workflowProgress?.basis === 'weighted_dag' && Number.isFinite(a.workflowProgress.completed) && Number.isFinite(a.workflowProgress.total) && a.workflowProgress.total > 0 ? row('Workflow tasks', `${a.workflowProgress.completed} / ${a.workflowProgress.total} completed`) : ''}
    <div class="ml-clocks">${clock('Activity elapsed', a.currentActivityStartedAt, now)}${clock('Last output', a.lastOutputAt, now)}${clock('Last progress', a.lastMeaningfulProgressAt, now)}${clock('Heartbeat age', a.lastHeartbeatAt, now)}<span>Report age <span data-report-age>${duration(f.age)}</span></span></div>
    ${row('Waiting', human(a.waitingReason))}${a.waitingSince ? `<div class="ml-clocks">${clock('Waiting for', a.waitingSince, now)}</div>` : ''}
    <div class="muted" data-workers-label>${f.state === 'live' ? 'Workers' : 'Last recorded workers'}</div>
    ${records(a.workerDetails).length ? `<div class="ml-workers-summary">${records(a.workerDetails).slice(0, 100).map(w => `<div>${esc(w.workerId)} · ${esc(w.health || w.state || 'unknown')} · ${esc(model(w))}</div>`).join('')}</div>` : '<div class="muted">Worker detail not reported</div>'}
    ${row('Next action', a.nextAction)}
  </article>`;
}
function section(title, items, render) {
  return `<section class="hd-card"><h3>${esc(title)}</h3>${items.length ? items.map(render).join('') : '<p class="muted">No data reported</p>'}</section>`;
}
export function missionDetail(p, now = Date.now()) {
  const a = p.activity || {};
  const current = freshnessAt(p, now, now).state === 'live';
  const workers = records(a.workerDetails).slice(0, 100).map(w => `<details class="hd-card ml-worker" data-worker="${esc(w.workerId)}">
    <summary>${esc(w.workerId)} · ${esc(w.health || w.state || 'unknown')} · ${esc(w.currentActivity || 'No current activity reported')}</summary>
    ${row('Task', w.taskId)}${row('Model / route', model(w))}${row('Serving node', w.node)}${row('Host', w.host)}
    ${row('Waiting reason', human(w.waitingReason))}
    <div class="ml-clocks">${clock('Worker elapsed', w.startedAt, now, w.endedAt)}${clock('Activity elapsed', w.currentActivityStartedAt, now, w.endedAt)}${clock('Last output', w.lastOutputAt, now)}${clock('Last progress', w.lastMeaningfulProgressAt, now)}${clock('Heartbeat age', w.lastHeartbeatAt, now)}</div>
    ${w.inferenceWaitMs != null ? row('Inference wait', duration(w.inferenceWaitMs / 1000)) : ''}
    ${row('Route fallbacks', w.fallbacks)}${row('Repeated errors', w.repeatedErrors)}${row('Repeated cycles', w.repeatedCycles)}
    <h4>Captured tool output</h4><pre>${esc(bounded(w.lastOutputPreview) || 'No captured output reported')}</pre>
  </details>`).join('');
  const tests = a.tests, review = a.review;
  return `${missionCard(p, now, true)}
    <div class="hd-card">${row('Native Pi session', p.session_id)}${row('Pi instance', p.instance_id)}${row('Activity snapshot time', p.activity_snapshot_at || p.snapshot_at)}${row('Mission revision', p.mission_revision)}${row('Activity revision', p.activity_revision)}</div>
    <h2 data-detail-workers>${current ? 'Workers' : 'Last recorded workers'}</h2>${workers || '<p class="muted">No worker detail reported</p>'}
    <section class="hd-card"><h3>Tests and review</h3>${tests ? row('Tests', `${tests.running ? 'Running' : 'Last result'} · ${tests.passed ?? 0} passed · ${tests.failed ?? 0} failed · ${tests.completed ?? 0} / ${tests.total ?? 0} completed`) : '<p class="muted">No tests reported</p>'}${review ? row('Review', `${human(review.status)} · ${review.blockingOpen ?? 0} open blockers`) : ''}${row('Completion', human(a.completionStatus))}${list(tests?.failures).slice(0, 50).map(f => `<pre>${esc(bounded(f, 2048))}</pre>`).join('')}${records(review?.findings).slice(0, 50).map(f => row(`${f.severity} · ${f.status}`, f.summary)).join('')}</section>
    ${section('Changed files', list(a.changes?.changedFiles).slice(0, 100), file => `<div><code>${esc(file)}</code></div>`)}
    ${section('Retries and recovery', records(a.recovery).slice(-30), r => row(`Attempt ${r.attempt} · ${r.status}`, r.summary || r.action))}
    ${section('Errors and blockers', records(a.errors).slice(-30), e => row(`${e.count ?? 1} occurrences`, e.example))}
    ${section('Recent activity', records(a.activity).slice(-50).reverse(), e => `<div class="ml-log">${row(e.at, `${e.workerId ? `${e.workerId} · ` : ''}${e.summary || human(e.type)}`)}${e.command ? `<code>${esc(bounded(e.command, 2048))}</code>` : ''}</div>`)}
    ${row('Reason', a.reason)}${row('Recovery', a.action)}`;
}
const styles = `<style>
.ml-card{overflow-wrap:anywhere}.ml-card p{margin:10px 0}.ml-freshness{font-weight:600;margin:6px 0;color:var(--status-active)}
.ml-freshness[data-state=updating],.ml-freshness[data-state=stale],.ml-freshness[data-state=disconnected]{color:var(--status-blocked)}
.ml-freshness[data-health=stalled],.ml-freshness[data-health=failed]{color:var(--status-failed,var(--text))}
.ml-clocks{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:.8rem;color:var(--text-muted);margin:10px 0}
.ml-progress progress{display:block;width:100%;height:9px;margin:6px 0;accent-color:var(--accent)}
.ml-row{display:flex;gap:12px;flex-wrap:wrap;margin:6px 0;overflow-wrap:anywhere}.ml-row>span:first-child{min-width:110px}
.ml-workers-summary{font-size:.8rem;margin:10px 0;overflow-wrap:anywhere}.ml-worker summary{cursor:pointer;font-weight:600;overflow-wrap:anywhere}
.ml-worker pre, .ml-content pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto;background:var(--surface-2);padding:12px}
.ml-log{border-bottom:1px solid var(--border-subtle);padding:6px 0}.ml-content h3{margin-top:0}.ml-content{min-width:0}.ml-content .hd-grid{align-items:start}
</style>`;

export function mountMissionActivity(container, client, missionKey) {
  let disposed = false, controller = null, pollTimer = null, clockTimer = null, failures = 0;
  let projections = [], received = Date.now(), lastJson = '', fetchError = false;
  container.innerHTML = `${styles}<div class="ml-connection muted"></div><div class="ml-content"><p>Loading mission activity…</p></div>`;
  const content = container.querySelector('.ml-content'), connection = container.querySelector('.ml-connection');
  const tick = () => {
    if (disposed || document.hidden) return;
    const now = Date.now();
    content.querySelectorAll('[data-clock]').forEach(el => {
      const p = projections.find(p => p.mission_key === el.closest('[data-mission-key]')?.dataset.missionKey) || (missionKey ? projections[0] : null);
      const label = el.parentElement.querySelector('[data-clock-label]');
      const frozen = label?.dataset.clockLabel.includes('elapsed') && p && freshnessAt(p, received, now).state !== 'live';
      const reference = frozen ? Date.parse(p.activity_snapshot_at || p.snapshot_at) : now;
      el.textContent = age(el.dataset.clock, el.dataset.end ? Date.parse(el.dataset.end) : reference);
      if (label) label.textContent = label.dataset.clockLabel + (frozen ? ' at last snapshot' : '');
    });
    content.querySelectorAll('[data-mission-key]').forEach(el => {
      const p = projections.find(p => p.mission_key === el.dataset.missionKey);
      if (!p) return;
      const f = freshnessAt(p, received, now), badge = el.querySelector('[data-freshness]');
      badge.dataset.state = f.state; badge.textContent = freshnessLabel(f, badge.dataset.health);
      el.querySelector('[data-report-age]').textContent = duration(f.age);
      el.querySelector('[data-activity-label]').textContent = f.state === 'live' ? 'Activity:' : 'Last known activity:';
      el.querySelector('[data-evidence-label]').textContent = f.state === 'live' ? 'Current reported measurements' : 'Last recorded measurements';
      el.querySelector('[data-workers-label]').textContent = f.state === 'live' ? 'Workers' : 'Last recorded workers';
      const workerHeading = content.querySelector('[data-detail-workers]');
      if (workerHeading) workerHeading.textContent = f.state === 'live' ? 'Workers' : 'Last recorded workers';
    });
    connection.textContent = fetchError ? 'Activity connection interrupted · showing last known data; retrying automatically.' : '';
  };
  const redraw = () => {
    const open = new Set([...content.querySelectorAll('details[open]')].map(el => el.dataset.worker));
    content.innerHTML = projections.length
      ? missionKey ? missionDetail(projections[0], Date.now()) : `<div class="hd-grid">${projections.map(p => missionCard(p)).join('')}</div>`
      : `<p class="muted">${missionKey ? 'No live telemetry reported for this Pi mission.' : 'No Pi mission activity reported yet.'}</p>`;
    content.querySelectorAll('details').forEach(el => { el.open = open.has(el.dataset.worker); });
    tick();
  };
  const poll = async () => {
    if (disposed || document.hidden || controller) return;
    controller = new AbortController();
    const activeController = controller;
    let timedOut = false;
    const deadline = setTimeout(() => { timedOut = true; activeController.abort(); }, 15000);
    try {
      const response = missionKey ? await client.missionActivity(missionKey, {signal: controller.signal}) : await client.listMissionActivity({signal: controller.signal});
      if (disposed || activeController.signal.aborted) return;
      projections = missionKey ? (response.activity ? [response.activity] : []) : records(response.activities);
      received = Date.now(); failures = 0; fetchError = false;
      const json = JSON.stringify(projections);
      if (json !== lastJson) { lastJson = json; redraw(); } else tick();
    } catch (error) {
      if (disposed || (activeController.signal.aborted && !timedOut)) return;
      failures++; fetchError = true;
      if (!projections.length) content.innerHTML = '<p class="muted">Mission activity unavailable. Retrying automatically.</p>';
      tick();
    } finally {
      clearTimeout(deadline);
      controller = null;
      if (!disposed && !document.hidden) pollTimer = setTimeout(poll, Math.min(30000, 5000 * 2 ** Math.min(failures, 3)));
    }
  };
  const visibility = () => {
    clearTimeout(pollTimer); clearInterval(clockTimer);
    if (document.hidden) { controller?.abort(); return; }
    tick(); clockTimer = setInterval(tick, 1000); poll();
  };
  document.addEventListener('visibilitychange', visibility);
  visibility();
  return () => { disposed = true; controller?.abort(); clearTimeout(pollTimer); clearInterval(clockTimer); document.removeEventListener('visibilitychange', visibility); };
}
