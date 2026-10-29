// Deployments page (deploy-4 Task 4): read-only view of GET /api/v1/deployments. One row per component, grouped by stage,
// with the coordinator's plan per release track and a drill-down event timeline. Every string from the server (evidence,
// messages, ids, commits, file names in definition errors) is host- or file-supplied and untrusted: it is escaped with esc()
// or set with textContent, never interpolated as markup.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { morph } from './patch.js';
import { pollWhileVisible } from './refresh-bus.js';

export const POLL_MS = 15000;
const short = (commit) => (typeof commit === 'string' && commit ? commit.slice(0, 12) : '');
const commitCell = (commit) => commit ? `<code title="${esc(commit)}">${esc(short(commit))}</code>` : '—';
const text = (value, fallback = '—') => (value == null || value === '' ? fallback : String(value));
const isObject = (v) => v != null && typeof v === 'object' && !Array.isArray(v);
const list = (v) => (Array.isArray(v) ? v : []);
const NO_ROW = Object.freeze(Object.create(null));

function seconds(s) {
  if (s == null || !Number.isFinite(Number(s))) return '—';
  const n = Math.max(0, Math.round(Number(s)));
  return n >= 60 ? `${Math.floor(n / 60)}m ${n % 60}s` : `${n}s`;
}

function describeAction(action) {
  if (!isObject(action)) return 'not decided yet';
  const stage = action.stage == null ? '' : ` stage ${action.stage}`;
  switch (action.type) {
    case 'Start': return `Start${stage} (order ${text(action.order)}): ${list(action.components).join(', ')}`;
    case 'Hold': return `Hold${stage}: ${text(action.reason, 'waiting')}${action.remaining_s != null ? ` · ${seconds(action.remaining_s)} left` : ''}`;
    case 'Halt': return `Halt${stage}: ${text(action.reason, 'halted')}`;
    case 'Idle': return `Idle: ${text(action.reason, 'nothing to do')}`;
    case 'Replan': return `Replan to ${short(action.new_target)} (generation ${text(action.generation)})`;
    default: return text(action.type, 'unknown action');
  }
}

// Stage → [component] from the definitions' groups (roll order); components with events but no definition go last.
function stageRows(view) {
  const stages = new Map();
  for (const group of list(view.definitions?.groups)) {
    if (!isObject(group)) continue;
    const key = `Stage ${text(group.stage, '?')}`;
    if (!stages.has(key)) stages.set(key, []);
    stages.get(key).push(...list(group.components).map((name) => ({ name: String(name), order: group.order })));
  }
  const known = new Set([...stages.values()].flat().map((r) => r.name));
  const extra = Object.keys(view.components).filter((name) => !known.has(name)).sort();
  if (extra.length) stages.set(stages.size ? 'Not in any stage' : 'Reported components', extra.map((name) => ({ name, order: null })));
  return stages;
}

function planFor(view, name) {
  return view.plans.find((p) => isObject(p?.components) && Object.hasOwn(p.components, name)) || null;
}

function timeline(row) {
  const events = list(row?.timeline);
  if (!events.length) return '<p class="hd-muted">No events reported yet.</p>';
  return `<ol class="dp-timeline" data-deploy-events>${events.slice().reverse().map((e) => `<li data-deploy-event>
    <div><strong>${esc(text(e.outcome))}</strong> · ${esc(text(e.step))} · ${commitCell(e.from)} → ${commitCell(e.to)}</div>
    <div class="hd-card__sub">received ${esc(text(e.received_at))} · host time ${esc(text(e.host_ts))} · reported by ${esc(text(e.reported_by, 'unknown'))} · host ${esc(text(e.host))}${e.instance ? ` · instance ${esc(e.instance)}` : ''}</div>
    ${e.evidence ? `<pre class="dp-evidence" data-deploy-evidence>${esc(e.evidence)}</pre>` : ''}
  </li>`).join('')}</ol>`;
}

// Only a GitHub issue URL of the plain shape is ever a link; anything else is shown as text (the server validates it too).
const ISSUE_URL = /^https:\/\/github\.com\/[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*\/issues\/[1-9][0-9]*$/;

function issueCell(issue) {
  if (!isObject(issue)) return 'issue: none';
  const label = `issue ${issue.number != null ? `#${esc(text(issue.number))}` : ''} ${esc(text(issue.state))}`.replace(/\s+/g, ' ');
  if (typeof issue.url === 'string' && ISSUE_URL.test(issue.url)) {
    return `<a href="${esc(issue.url)}" target="_blank" rel="noopener noreferrer" data-deploy-issue>${label}</a>`;
  }
  return `<span data-deploy-issue>${label}${issue.reason ? ` · ${esc(issue.reason)}` : ''}</span>`;
}

// Drill-down (spec 8): the repair attempts, and links to the issue / PR / report of each escalated release of a component.
function escalations(view, name) {
  const recs = view.escalations.filter((e) => isObject(e) && e.component === name);
  if (!recs.length) return '';
  return recs.map((rec, n) => {
    const rounds = list(rec.rounds).filter(isObject);
    const report = isObject(rec.report) ? rec.report : null;
    const rid = `dp-report-${encodeURIComponent(name).replace(/%/g, '_')}-${n}`;
    return `<section class="dp-escalation" data-deploy-escalation>
    <div><strong>Escalation</strong> · release ${commitCell(rec.release)}${rec.open ? ' · open' : ' · closed'} · ${issueCell(rec.issue)} · PR: ${rec.pr ? esc(text(rec.pr)) : 'none'}${report ? ` · <a href="#${esc(rid)}" data-deploy-report-link>report</a>` : ''}${isObject(rec.assistant) ? ` · assistant ${esc(text(rec.assistant.state))}` : ' · assistant: not notified (herdr notification)'}</div>
    <ol class="dp-repairs" data-deploy-repairs>${rounds.map((r) => {
      const f = isObject(r.finding) ? r.finding : {}, rep = isObject(r.repair) ? r.repair : {};
      return `<li data-deploy-repair>round ${esc(text(r.index))}: ${esc(text(f.kind))} (${esc(text(f.source))})${f.source === 'model' ? ' <em>model output</em>' : ''} → <strong>${esc(text(rep.action))}</strong>${r.attempt != null ? ` · attempt ${esc(text(r.attempt))}` : ''}${rep.rollback_to ? ` · to ${commitCell(rep.rollback_to)}` : ''} · ${esc(text(rep.reason))}<div class="hd-card__sub">${esc(text(f.summary))}</div></li>`;
    }).join('')}</ol>
    ${report ? `<dl id="${esc(rid)}" class="dp-report" data-deploy-report>
      <dt>What changed</dt><dd>${esc(text(report.what_changed))}</dd>
      <dt>Current state</dt><dd>${esc(text(report.current_state))}</dd>
      <dt>Tried</dt><dd>${list(report.tried).map((t) => esc(text(t))).join('<br>') || '—'}</dd>
      <dt>Diagnosis source</dt><dd data-deploy-report-source>${esc(text(report.finding_source))}${report.finding_source === 'model' ? ' <em>model output (AI-generated, unverified)</em>' : ''}</dd>
      <dt>Suggested next step</dt><dd>${report.finding_source === 'model' ? '<em>model output:</em> ' : ''}${esc(text(report.next_step))}</dd>
      ${report.evidence ? `<dt>Evidence (redacted)</dt><dd><pre class="dp-evidence" data-deploy-report-evidence>${esc(report.evidence)}</pre></dd>` : ''}
    </dl>` : ''}
  </section>`;
  }).join('');
}

function componentRow(view, item, open) {
  // Own keys only, and a prototype-free empty row: a name like 'constructor' never reads the prototype chain.
  const row = Object.hasOwn(view.components, item.name) && isObject(view.components[item.name]) ? view.components[item.name] : NO_ROW;
  const plan = planFor(view, item.name);
  const coord = plan ? plan.components[item.name] : null;
  const last = isObject(row.last_result) ? row.last_result : null;
  const health = coord ? coord.state : text(row.health, 'unknown');
  const hold = coord ? coord.hold_remaining_s : null;
  const permitted = plan ? list(plan.permitted).includes(item.name) : false;
  const id = `dp-tl-${encodeURIComponent(item.name).replace(/%/g, '_')}`;
  return `<tr data-deploy-row="${esc(item.name)}" data-health="${esc(health)}">
    <th scope="row"><button type="button" class="hd-linkbtn" data-deploy-toggle="${esc(item.name)}" aria-expanded="${open}" aria-controls="${esc(id)}">${esc(item.name)}</button></th>
    <td>${commitCell(row.running)}</td>
    <td>${commitCell(plan?.target_commit || row.target)}</td>
    <td>${esc(text(row.step))}</td>
    <td data-deploy-health>${esc(health)}${coord?.outcome ? ` (${esc(coord.outcome)})` : ''}${permitted ? ' · permitted' : ''}</td>
    <td>${last ? `${esc(text(last.outcome))} · ${esc(text(last.received_at || last.ts))}` : '—'}</td>
    <td data-deploy-hold>${hold != null ? seconds(hold) : '—'}</td>
    <td data-deploy-repairs-count title="${coord ? `${esc(text(coord.attempts, '0'))} start(s) toward this release` : ''}">${coord ? esc(text(coord.repair_attempts, '0')) : '—'}</td>
  </tr>
  <tr id="${esc(id)}" data-deploy-timeline="${esc(item.name)}"${open ? '' : ' hidden'}><td colspan="8">${escalations(view, item.name)}${timeline(row)}</td></tr>`;
}

function planCard(plan, coordinator) {
  const track = isObject(plan.track) ? plan.track : {};
  const missing = list(plan.missing);
  return `<article class="hd-card" data-deploy-plan>
    <div class="hd-card__title">${esc(text(track.repo))} · ${esc(text(track.branch))}</div>
    <div class="hd-card__sub">target ${commitCell(plan.target_commit)} · generation ${esc(text(plan.generation, '0'))}${plan.plan_commit ? ` · plan ${commitCell(plan.plan_commit)}` : ''}</div>
    <div data-deploy-current><strong>${esc(describeAction(plan.current))}</strong></div>
    <div class="hd-card__sub">Permitted now: ${list(plan.permitted).length ? esc(list(plan.permitted).join(', ')) : 'nothing'}</div>
    ${missing.length ? `<div class="hd-card__sub" data-deploy-missing>Not reporting for this release: ${esc(missing.join(', '))}</div>` : ''}
    ${coordinator.evidence === 'unknown' ? '<div class="hd-card__sub">Release evidence is not wired: the coordinator holds until a release is confirmed.</div>' : ''}
  </article>`;
}

export function renderDeployments(raw) {
  const view = {
    components: isObject(raw?.components) ? raw.components : {},
    definitions: isObject(raw?.definitions) ? raw.definitions : {},
    plans: list(raw?.plans).filter(isObject),
    coordinator: isObject(raw?.coordinator) ? raw.coordinator : {},
    escalations: list(raw?.escalations).filter(isObject),
  };
  return (open) => {
    const defs = view.definitions;
    const notes = [];
    if (defs.warning) notes.push(`<p role="note" data-deploy-warning>${esc(defs.warning)}</p>`);
    for (const err of list(defs.errors).slice(0, 20)) notes.push(`<p role="note" class="hd-card__sub" data-deploy-error>${esc(err)}</p>`);
    if (view.coordinator.error) notes.push(`<p role="note" data-deploy-tick-error>Coordinator: ${esc(view.coordinator.error)}${view.coordinator.stale ? ' (showing the last good plan)' : ''}</p>`);
    const stages = stageRows(view);
    if (!defs.configured && !stages.size) {
      return `${notes.join('')}<herdr-empty data-deploy-empty message="No deployment pipelines are configured"></herdr-empty>
        <p class="hd-muted">Set <code>control_plane.deployment_definitions</code> in the fleet config to a checkout of InferWeave <code>deploy/pipelines</code>.</p>`;
    }
    const plans = view.plans.map((p) => planCard(p, view.coordinator)).join('');
    const sections = [...stages].map(([label, items]) => `<section data-deploy-stage="${esc(label)}" aria-label="${esc(label)}">
      <h2>${esc(label)}</h2>
      <div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Component</th><th>Running</th><th>Target</th><th>Step</th><th>Health</th><th>Last result</th><th>Hold left</th><th>Repairs</th></tr></thead>
      <tbody>${items.map((item) => componentRow(view, item, open.has(item.name))).join('')}</tbody></table></div>
    </section>`).join('');
    return `${notes.join('')}<div class="hd-grid">${plans}</div>${sections || '<herdr-empty message="No components defined"></herdr-empty>'}`;
  };
}

export function mountDeployments(container, client, { freshness = () => null } = {}) {
  let disposed = false, controller = null, loaded = false, signature = null, draw = null;
  const open = new Set();
  container.innerHTML = '<p class="hd-muted">Staged rollouts: each component\'s running and target commit, the coordinator\'s current step and the events hosts reported. Read-only; host text is escaped.</p><p data-deploy-status role="status"></p><div data-deploy-content></div><button type="button" data-deploy-retry hidden>Retry</button>';
  const content = container.querySelector('[data-deploy-content]');
  const status = container.querySelector('[data-deploy-status]');
  const retry = container.querySelector('[data-deploy-retry]');
  // morph keeps focus and scroll on a redraw (the hold timers change the payload on every poll).
  const paint = () => { if (draw) morph(content, draw(open)); };
  const onClick = (event) => {
    const button = event.target.closest('[data-deploy-toggle]');
    if (!button || !container.contains(button)) return;
    const name = button.getAttribute('data-deploy-toggle');
    if (open.has(name)) open.delete(name); else open.add(name);
    const row = [...content.querySelectorAll('[data-deploy-timeline]')].find((r) => r.getAttribute('data-deploy-timeline') === name);
    if (row) row.hidden = !open.has(name);
    button.setAttribute('aria-expanded', String(open.has(name)));
  };
  const refresh = async () => {
    if (disposed || controller) return;
    const request = new AbortController(); controller = request;
    if (!loaded) content.innerHTML = skeleton('deployments', { rows: 2, height: 64 });
    retry.hidden = true;
    try {
      const data = await client.getDeployments({ signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]) });
      if (disposed) return;
      if (!isObject(data)) throw new Error('Invalid deployments response');
      const next = JSON.stringify(data);
      if (next !== signature) { signature = next; draw = renderDeployments(data); paint(); }
      loaded = true; status.textContent = ''; freshness()?.touch(true);
    } catch (error) {
      if (!disposed && !request.signal.aborted) {
        if (!loaded) content.innerHTML = '';
        status.textContent = `Deployments could not load: ${error.message}`;
        retry.hidden = false; freshness()?.touch(false, error.message);
      }
    } finally { if (controller === request) controller = null; }
  };
  container.addEventListener('click', onClick);
  retry.addEventListener('click', refresh);
  const stop = pollWhileVisible(refresh, POLL_MS);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); container.removeEventListener('click', onClick); } };
}
