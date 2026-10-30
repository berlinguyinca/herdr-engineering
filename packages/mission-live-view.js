// Read-only Pi mission telemetry. Clocks redraw locally; polling never creates
// activity events, attention notifications, or session control operations.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';

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
export function missionHealth(a = {}, mission = {}) {
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
const count = value => Number.isInteger(value) ? String(value) : String(Number(value.toFixed(2)));
const terminalMission = m => ['COMPLETE', 'COMPLETED', 'FAILED', 'CANCELED', 'CANCELLED'].includes(text(m.status).toUpperCase());
const finishedWorker = (w, m = {}) => terminalMission(m) || Boolean(w.endedAt) || ['completed', 'complete', 'failed', 'canceled', 'cancelled', 'stopped', 'aborted'].includes(text(w.state).toLowerCase());
const shortTitle = value => value.length > 180 ? `${value.slice(0, 177).trimEnd()}…` : value;
function disclosure(key, title, body, open = false) {
  return `<details class="hd-card ml-disclosure" data-disclosure="${esc(key)}"${open ? ' open' : ''}><summary>${esc(title)}</summary>${body}</details>`;
}
// Which model a mission is pinned to and which agents ran elsewhere (Pi `modelUse`); absent on older Pi.
function modelUseLine(u) {
  if (!u || typeof u !== 'object' || Array.isArray(u)) return '';
  const away = records(u.agents).filter(x => x.onPin === false).length;
  const head = u.pin ? `Pinned to ${text(u.pin)}` : 'Not pinned · models chosen by role';
  return away ? `${head} · ${away} agent${away === 1 ? '' : 's'} on another model` : head;
}
function modelUseAgents(u) {
  return records(u?.agents).slice(0, 100).map(x => row(
    [x.role, x.workerId].filter(Boolean).join(' · ') || 'Agent',
    `${x.model || 'Model not reported'}${x.onPin === true ? ' · on pin' : x.onPin === false ? ' · other model' : ''}${x.state ? ` · ${human(x.state)}` : ''}`,
  )).join('');
}
function model(w) { return [w.route, w.model].filter(Boolean).join(' → ') || 'Model not reported'; }
function freshnessLabel(f, health) {
  if (f.state === 'updating') return `Updating telemetry · current details unavailable · last reported ${health}`;
  return f.state === 'live' ? `Live telemetry · ${health}` : `${f.state === 'stale' ? 'Stale' : 'Disconnected'} telemetry · current state unknown · last reported ${health}`;
}
export function missionCard(p, now = Date.now(), detail = false) {
  const a = p.activity || {}, m = p.mission || {};
  const health = missionHealth(a, m), f = freshnessAt(p, now, now);
  const workers = records(a.workerDetails), finished = workers.filter(w => finishedWorker(w, m)).length;
  return `<article class="hd-card ml-card" data-mission-key="${esc(p.mission_key)}">
    <div class="hd-card__title${detail ? ' ml-detail-title' : ''}">${detail ? esc(shortTitle(m.title || m.id || 'Untitled mission')) : `<a href="#/missions/${encodeURIComponent(p.mission_key)}">${esc(m.title || m.id)}</a>`}</div>
    <div class="ml-freshness" data-freshness data-health="${esc(health)}" data-state="${f.state}">${esc(freshnessLabel(f, health))}</div>
    <div class="muted">Pi · ${esc(p.host_id || 'Host unknown')} · ${detail ? '' : `${esc(p.mission_key)} · `}${esc(m.status || 'State unknown')}</div>
    <p><strong data-activity-label>${f.state === 'live' ? 'Activity' : 'Last known activity'}:</strong> ${esc(a.currentActivity?.summary || 'No current activity reported')}</p>
    ${!detail && a.currentObjective ? row('Objective', a.currentObjective) : ''}
    <div class="muted" data-evidence-label>${f.state === 'live' ? 'Current reported measurements' : 'Last recorded measurements'}</div>
    ${progress(a)}
    ${a.workflowProgress?.basis === 'weighted_dag' && Number.isFinite(a.workflowProgress.completed) && Number.isFinite(a.workflowProgress.total) && a.workflowProgress.total > 0 ? row('Workflow tasks', `${count(a.workflowProgress.completed)} / ${count(a.workflowProgress.total)} completed`) : ''}
    <div class="ml-clocks">${detail ? a.lastOutputAt ? clock('Last output', a.lastOutputAt, now) : '<span>No output reported</span>' : `${clock('Activity elapsed', a.currentActivityStartedAt, now)}${clock('Last output', a.lastOutputAt, now)}${clock('Last progress', a.lastMeaningfulProgressAt, now)}${clock('Heartbeat age', a.lastHeartbeatAt, now)}`}<span>Report age <span data-report-age>${duration(f.age)}</span></span></div>
    ${row('Waiting', human(a.waitingReason))}${a.waitingSince ? `<div class="ml-clocks">${clock('Waiting for', a.waitingSince, now)}</div>` : ''}
    <div class="muted" data-workers-label>${f.state === 'live' ? 'Workers' : 'Last recorded workers'}</div>
    ${detail ? `<div>${workers.length - finished} current · ${finished} ${terminalMission(m) ? 'recorded' : 'finished'}</div>` : records(a.workerDetails).length ? `<div class="ml-workers-summary">${records(a.workerDetails).slice(0, 100).map(w => `<div>${esc(w.workerId)} · ${esc(w.health || w.state || 'unknown')} · ${esc(model(w))}</div>`).join('')}</div>` : '<div class="muted">Worker detail not reported</div>'}
    ${row('Models', modelUseLine(a.modelUse))}
    ${row('Next action', a.nextAction)}
  </article>`;
}
export function missionDetail(p, now = Date.now()) {
  const a = p.activity || {}, m = p.mission || {};
  const current = freshnessAt(p, now, now).state === 'live' && !terminalMission(m);
  const health = missionHealth(a, m);
  const workers = records(a.workerDetails).slice(0, 100);
  const currentWorkers = workers.filter(w => !finishedWorker(w, m));
  const history = workers.filter(w => finishedWorker(w, m));
  // Attempt identity stays stable if a worker moves from current work into history.
  const keys = new Map();
  const renderWorker = w => {
    const identity = JSON.stringify([w.workerId, w.taskId, w.startedAt]);
    const occurrence = keys.get(identity) || 0;
    keys.set(identity, occurrence + 1);
    return `<details class="hd-card ml-worker" data-worker="${esc(w.workerId)}" data-disclosure="${esc(`worker:${identity}:${occurrence}`)}">
      <summary><span>${esc(w.workerId)} · ${esc(w.health || w.state || 'unknown')}</span><span class="ml-worker-activity">${esc(w.currentActivity || 'No current activity reported')}</span></summary>
      ${row('Task', w.taskId)}${row('Model / route', model(w))}${row('Serving node', w.node)}${row('Host', w.host)}
      ${row('Waiting reason', human(w.waitingReason))}
      <div class="ml-clocks">${clock('Worker elapsed', w.startedAt, now, w.endedAt)}${clock('Activity elapsed', w.currentActivityStartedAt, now, w.endedAt)}${clock('Last output', w.lastOutputAt, now)}${clock('Last progress', w.lastMeaningfulProgressAt, now)}${clock('Heartbeat age', w.lastHeartbeatAt, now)}</div>
      ${w.inferenceWaitMs != null ? row('Inference wait', duration(w.inferenceWaitMs / 1000)) : ''}
      ${row('Route fallbacks', w.fallbacks)}${row('Repeated errors', w.repeatedErrors)}${row('Repeated cycles', w.repeatedCycles)}
      <h4>Captured tool output</h4><pre>${esc(bounded(w.lastOutputPreview) || 'No captured output reported')}</pre>
    </details>`;
  };
  const tests = a.tests, review = a.review;
  const changes = list(a.changes?.changedFiles).slice(0, 100);
  const recovery = records(a.recovery).slice(-30);
  const errors = records(a.errors).slice(-30);
  const events = records(a.activity).slice(-50).reverse();
  const blocked = ['blocked', 'stalled', 'failed'].includes(health) || health.startsWith('paused:');
  return `${missionCard(p, now, true)}
    ${errors.length || a.reason ? disclosure('blockers', 'Errors and blockers', `${row('Reason', a.reason)}${errors.map(e => row(`${e.count ?? 1} occurrences`, e.example)).join('')}${row('Recovery', a.action)}`, blocked) : ''}
    ${a.modelUse && records(a.modelUse.agents).length ? disclosure('models', `Models and agents · ${records(a.modelUse.agents).length}`, modelUseAgents(a.modelUse)) : ''}
    ${disclosure('brief', 'Mission brief', `<div class="ml-brief">${row('Full title', m.title || m.id)}${row('Objective', a.currentObjective)}</div>`)}
    <h2 data-detail-workers>${current ? 'Workers' : 'Last recorded workers'}</h2>
    ${currentWorkers.length ? currentWorkers.map(renderWorker).join('') : '<p class="muted">No current worker activity reported.</p>'}
    ${history.length ? disclosure('worker-history', `Worker history · ${history.length}`, history.map(renderWorker).join('')) : ''}
    ${tests || review || a.completionStatus ? disclosure('tests', 'Tests and review', `${tests ? row('Tests', `${tests.running ? 'Running' : 'Last result'} · ${tests.passed ?? 0} passed · ${tests.failed ?? 0} failed · ${tests.completed ?? 0} / ${tests.total ?? 0} completed`) : ''}${review ? row('Review', `${human(review.status)} · ${review.blockingOpen ?? 0} open blockers`) : ''}${row('Completion', human(a.completionStatus))}${list(tests?.failures).slice(0, 50).map(f => `<pre>${esc(bounded(f, 2048))}</pre>`).join('')}${records(review?.findings).slice(0, 50).map(f => row(`${f.severity} · ${f.status}`, f.summary)).join('')}`, (tests?.failed > 0 || review?.blockingOpen > 0)) : ''}
    ${changes.length ? disclosure('changes', `Changed files · ${changes.length}`, changes.map(file => `<div><code>${esc(file)}</code></div>`).join('')) : ''}
    ${recovery.length || a.action && !a.reason && !errors.length ? disclosure('recovery', 'Retries and recovery', `${recovery.map(r => row(`Attempt ${r.attempt} · ${r.status}`, r.summary || r.action)).join('')}${row('Recovery', a.action)}`) : ''}
    ${events.length ? disclosure('activity', `Recent activity · ${events.length}`, events.map(e => `<div class="ml-log">${row(e.at, `${e.workerId ? `${e.workerId} · ` : ''}${e.summary || human(e.type)}`)}${e.command ? `<code>${esc(bounded(e.command, 2048))}</code>` : ''}</div>`).join('')) : ''}
    ${disclosure('diagnostics', 'Telemetry details', `<div class="ml-clocks">${clock('Activity elapsed', a.currentActivityStartedAt, now)}${clock('Last progress', a.lastMeaningfulProgressAt, now)}${clock('Heartbeat age', a.lastHeartbeatAt, now)}</div>${row('Mission ID', p.mission_key)}${row('Native Pi session', p.session_id)}${row('Pi instance', p.instance_id)}${row('Activity snapshot time', p.activity_snapshot_at || p.snapshot_at)}${row('Mission revision', p.mission_revision)}${row('Activity revision', p.activity_revision)}`)}`;
}
const styles = `<style>
.ml-card .hd-card__title a{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.ml-detail-title{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ml-disclosure>summary{cursor:pointer;font-weight:600}.ml-disclosure[open]>summary{margin-bottom:12px}
.ml-brief{white-space:pre-wrap}.ml-worker-activity{display:block;font-size:.85rem;font-weight:400;margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ml-worker[open] .ml-worker-activity{-webkit-line-clamp:unset}
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

// onStatus(ok, message) reports each poll to the page's freshness chip (the page owns the chip).
export function mountMissionActivity(container, client, missionKey, { onStatus = () => {} } = {}) {
  let disposed = false, controller = null, pollTimer = null, clockTimer = null, failures = 0;
  let projections = [], received = Date.now(), lastJson = '', fetchError = false;
  const disclosureStates = new Map();
  const explicitlyClosed = new Set();
  container.innerHTML = `${styles}<div class="ml-connection muted"></div><div class="ml-content">${skeleton('mission activity', { rows: 2, height: 64 })}</div>`;
  const content = container.querySelector('.ml-content'), connection = container.querySelector('.ml-connection');
  const rememberDisclosureChoice = event => {
    const summary = event.target.closest('summary');
    const disclosure = summary?.parentElement;
    if (!disclosure?.matches('details[data-disclosure]')) return;
    if (disclosure.open) explicitlyClosed.add(disclosure.dataset.disclosure);
    else explicitlyClosed.delete(disclosure.dataset.disclosure);
  };
  content.addEventListener('click', rememberDisclosureChoice);
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
      if (workerHeading) workerHeading.textContent = f.state === 'live' && !terminalMission(p.mission || {}) ? 'Workers' : 'Last recorded workers';
    });
    connection.textContent = fetchError ? 'Activity connection interrupted · showing last known data; retrying automatically.' : '';
  };
  const redraw = () => {
    const inspectedCurrentWorkers = new Set([...content.querySelectorAll('.ml-worker[open]')]
      .filter(el => !el.closest('[data-disclosure="worker-history"]'))
      .map(el => el.dataset.disclosure));
    content.querySelectorAll('details[data-disclosure]').forEach(el => disclosureStates.set(el.dataset.disclosure, el.open));
    const focused = document.activeElement?.closest('details[data-disclosure]');
    const focusKey = content.contains(focused) ? focused.dataset.disclosure : null;
    const focusOffset = focusKey ? focused.getBoundingClientRect().top : null;
    const scrollY = window.scrollY;
    content.innerHTML = projections.length
      ? missionKey ? missionDetail(projections[0], Date.now()) : `<div class="hd-grid">${projections.map(p => missionCard(p)).join('')}</div>`
      : `<p class="muted">${missionKey ? 'No live telemetry reported for this Pi mission.' : 'No Pi mission activity reported yet.'}</p>`;
    let restoreFocus;
    content.querySelectorAll('details[data-disclosure]').forEach(el => {
      if (disclosureStates.has(el.dataset.disclosure)) el.open = disclosureStates.get(el.dataset.disclosure);
      if (el.dataset.disclosure === focusKey) restoreFocus = el;
    });
    if (!explicitlyClosed.has('worker-history')) {
      content.querySelectorAll('[data-disclosure="worker-history"] .ml-worker').forEach(el => {
        if (inspectedCurrentWorkers.has(el.dataset.disclosure)) el.closest('[data-disclosure="worker-history"]').open = true;
      });
    }
    if (restoreFocus) {
      restoreFocus.querySelector('summary')?.focus({preventScroll: true});
      window.scrollBy(0, restoreFocus.getBoundingClientRect().top - focusOffset);
    } else window.scrollTo(window.scrollX, scrollY);
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
      onStatus(true);
      const json = JSON.stringify(projections);
      if (json !== lastJson) { lastJson = json; redraw(); } else tick();
    } catch (error) {
      if (disposed || (activeController.signal.aborted && !timedOut)) return;
      failures++; fetchError = true;
      onStatus(false, timedOut ? 'timed out' : error?.message || 'request failed');
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
  return () => { disposed = true; controller?.abort(); clearTimeout(pollTimer); clearInterval(clockTimer); document.removeEventListener('visibilitychange', visibility); content.removeEventListener('click', rememberDisclosureChoice); };
}
