import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { skeleton } from './motion.js';
import { morph } from './patch.js';
import { pollWhileVisible } from './refresh-bus.js';

const text = (value, fallback = '—') => value == null || value === '' ? fallback : String(value);
const number = value => typeof value === 'number' && Number.isFinite(value)
  ? value.toLocaleString(undefined, { maximumFractionDigits: 3 }) : 'unknown';
const reasonLabels = Object.freeze({
  HOST_MANUAL_ONLY: 'Requires explicit manual placement', HOST_DISABLED: 'Host is disabled',
  HOST_STALE: 'Resource telemetry is stale', HOST_UNTRUSTED: 'Host trust is not established',
  HOST_NOT_OWNED: 'Host is outside the owned fleet', HOST_UNREACHABLE: 'Host is unreachable',
  RESERVATION_CAPACITY: 'Insufficient unreserved capacity', WORKSPACE_LOCK: 'Workspace already has a writer',
});
const reasonText = reason => (Object.hasOwn(reasonLabels, reason)
  ? reasonLabels[reason] : reason.toLowerCase().replaceAll('_', ' ')) + ` (${reason})`;
const capturePolicy = host => host && ['allow_tags', 'deny_tags'].every(key =>
  Array.isArray(host.policy?.[key]) && host.policy[key].every(tag => typeof tag === 'string'))
  ? { host_uid: host.host_uid, allow_tags: [...host.policy.allow_tags], deny_tags: [...host.policy.deny_tags] } : null;

export function mountScheduling(container, client, { freshness = () => null } = {}) {
  let disposed = false, controller = null, planController = null, policyController = null, loaded = false;
  let reading = null, epoch = 0, latest = { hosts: [] }, reloadBusy = false, requiresReload = false, policyBase = null;
  container.innerHTML = '<p data-scheduler-status role="status"></p><div data-scheduler-content></div><button type="button" data-scheduler-retry hidden>Retry</button>';
  const content = container.querySelector('[data-scheduler-content]');
  const status = container.querySelector('[data-scheduler-status]');
  const retry = container.querySelector('[data-scheduler-retry]');
  const policyForm = () => content.querySelector('[data-scheduler-policy]');
  const selectedHost = () => latest.hosts.find(host => host.host_uid === policyForm()?.elements.host_uid.value);
  const enablePolicy = () => {
    const form = policyForm(); if (!form) return;
    const host = selectedHost(), busy = Boolean(policyController) || reloadBusy;
    const macOverride = host && ['darwin', 'macos', 'mac'].includes(String(host.platform).toLowerCase())
      && form.elements.mode.value === 'automatic';
    form.querySelector('[data-policy-warning]').hidden = !macOverride;
    form.querySelector('[data-policy-confirm-label]').textContent = macOverride
      ? 'I explicitly allow automatic placements on this Mac' : 'Confirm this policy change';
    form.querySelectorAll('input,select,button').forEach(element => { element.disabled = busy; });
    const stale = host && String(host.policy?.revision) !== form.dataset.revision;
    form.querySelector('[type=submit]').disabled = busy || !host || !policyBase || policyBase.host_uid !== host.host_uid
      || stale || requiresReload || !form.elements.confirm.checked;
    if (host && !policyBase) form.querySelector('output').textContent = 'Policy editing requires complete policy data from the server. Reload after the backend is updated.';
  };
  const syncPolicy = (reset = false) => {
    const form = policyForm(); if (!form || policyController || reloadBusy) return;
    const uid = form.elements.host_uid.value;
    if (reset || (form.dataset.dirty !== 'true' && !form.contains(container.ownerDocument.activeElement))) {
      form.elements.host_uid.innerHTML = '<option value="">Choose a host</option>' + latest.hosts.map(host =>
        `<option value="${esc(host.host_uid)}">${esc(text(host.host_name, host.host_uid))}</option>`).join('');
      form.elements.host_uid.value = latest.hosts.some(host => host.host_uid === uid) ? uid : '';
      const host = selectedHost();
      policyBase = capturePolicy(host);
      form.elements.mode.value = host?.policy?.mode || 'manual_only';
      form.dataset.revision = host ? String(host.policy.revision) : '';
      form.elements.confirm.checked = false;
      if (reset) { form.elements.reason.value = ''; form.dataset.dirty = 'false'; requiresReload = false; }
    }
    const host = selectedHost();
    form.querySelector('[data-policy-revision]').textContent = host
      ? `Editing revision ${form.dataset.revision}. Latest observed revision ${host.policy.revision}.` : 'Choose an available host.';
    if (host && String(host.policy.revision) !== form.dataset.revision) {
      form.querySelector('output').textContent = 'Policy changed while you were editing. Reload policy, review, and confirm again.';
    } else if (uid && !host) {
      form.querySelector('output').textContent = 'Host unavailable. Reload the host list before editing.';
    }
    enablePolicy();
  };
  const refresh = () => {
    if (disposed || policyController) return Promise.resolve(null);
    if (reading) return reading;
    const generation = epoch;
    const request = new AbortController(); controller = request;
    if (!loaded) content.innerHTML = skeleton('scheduling', { rows: 8, height: 64 });
    retry.hidden = true;
    reading = (async () => {
    try {
      const data = await client.schedulerOverview({ signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]) });
      if (disposed || generation !== epoch) return null;
      if (!Array.isArray(data.hosts)) throw new Error('Invalid scheduler response');
      if (!loaded) content.innerHTML = renderSchedulingView(data);
      else {
        const template = container.ownerDocument.createElement('template');
        template.innerHTML = renderSchedulingView(data);
        for (const name of ['metrics', 'hosts', 'history', 'queue', 'admission']) {
          const selector = `[data-scheduler-${name}]`;
          morph(content.querySelector(selector), template.content.querySelector(selector).innerHTML);
        }
      }
      latest = data; loaded = true; syncPolicy(); status.textContent = ''; freshness()?.touch(true);
      return data;
    } catch (error) {
      if (!disposed && !request.signal.aborted) {
        if (!loaded) content.innerHTML = '';
        status.textContent = `Scheduling could not load: ${error.message}`;
        retry.hidden = false; freshness()?.touch(false, error.message);
      }
      return null;
    } finally { if (controller === request) controller = null; }
    })().finally(() => { reading = null; });
    return reading;
  };
  const submit = async event => {
    const policy = event.target.closest('[data-scheduler-policy]');
    if (policy) {
      event.preventDefault();
      if (disposed || policyController || reloadBusy || policy.querySelector('[type=submit]').disabled) return;
      const request = new AbortController(); policyController = request;
      epoch++; controller?.abort();
      const values = new FormData(policy);
      const output = policy.querySelector('output');
      const payload = { host_uid: values.get('host_uid'), mode: values.get('mode'),
        expected_revision: Number(policy.dataset.revision), reason: String(values.get('reason') || '').trim(),
        allow_tags: policyBase.allow_tags, deny_tags: policyBase.deny_tags };
      enablePolicy(); output.textContent = 'Saving policy…';
      try {
        const saved = await client.schedulerSetHostPolicy(payload, { signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]) });
        if (disposed) return;
        const host = selectedHost(); if (host) { host.policy = saved; policyBase = capturePolicy(host); }
        policy.dataset.dirty = 'false'; policy.dataset.revision = String(saved.revision);
        policy.elements.confirm.checked = false; requiresReload = false;
        output.textContent = `Saved policy revision ${saved.revision}.`;
      } catch (error) {
        if (!disposed) {
          requiresReload = error.status === 409;
          output.textContent = requiresReload ? 'Policy conflict: reload policy, review, and confirm again.'
            : `Policy could not be saved: ${error.message}. You can retry.`;
        }
      } finally {
        if (reading) await reading;
        policyController = null;
        if (!disposed) { await refresh(); syncPolicy(); }
      }
      return;
    }
    const form = event.target.closest('[data-scheduler-plan]');
    if (!form) return;
    event.preventDefault();
    if (disposed || planController) return;
    const request = new AbortController(); planController = request;
    const values = new FormData(form);
    const result = form.querySelector('[data-scheduler-plan-result]');
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true; result.textContent = 'Planning…';
    try {
      const decision = await client.schedulerPlan({ request_id: values.get('request_id'), agent_family: values.get('agent_family'),
        placement_mode: 'automatic', resource_profile: { class: 'coding', cpu_cores: Number(values.get('cpu_cores')), memory_gib: Number(values.get('memory_gib')) },
      }, { signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]) });
      if (disposed) return;
      const reasons = Object.entries(decision.explanations || {}).map(([host, explanation]) => `${host}: ${(explanation.reason_codes || []).join(', ') || `score ${explanation.score}`}`).join(' · ');
      result.textContent = `Selected: ${decision.selected_host_uid || 'queued'}. ${reasons}`;
    } catch (error) { if (!disposed) result.textContent = `Placement preview failed: ${error.message}. Try again.`; }
    finally { if (planController === request) planController = null; if (!disposed) button.disabled = false; }
  };
  const edit = event => {
    const form = event.target.closest('[data-scheduler-policy]'); if (!form || policyController || reloadBusy) return;
    if (event.target.name === 'host_uid') {
      if (event.type === 'change') { form.dataset.dirty = 'false'; form.querySelector('output').textContent = ''; syncPolicy(true); }
    } else {
      if (event.target.name !== 'confirm') form.elements.confirm.checked = false;
      form.dataset.dirty = 'true'; enablePolicy();
    }
  };
  const click = async event => {
    if (!event.target.closest('[data-policy-reload]') || disposed || policyController || reloadBusy) return;
    reloadBusy = true; enablePolicy();
    epoch++; controller?.abort();
    if (reading) await reading;
    if (disposed) return;
    const data = await refresh(); reloadBusy = false;
    if (disposed) return;
    if (data) { syncPolicy(true); policyForm().querySelector('output').textContent = 'Latest policy loaded. Review and confirm the change.'; }
    else { policyForm().querySelector('output').textContent = 'Could not reload policy. Your draft is preserved; try again.'; enablePolicy(); }
  };
  container.addEventListener('submit', submit);
  container.addEventListener('input', edit);
  container.addEventListener('change', edit);
  container.addEventListener('click', click);
  retry.addEventListener('click', refresh);
  const stop = pollWhileVisible(refresh, 30000);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); planController?.abort(); policyController?.abort();
    container.removeEventListener('submit', submit); container.removeEventListener('input', edit);
    container.removeEventListener('change', edit); container.removeEventListener('click', click); } };
}

function explanations(record) {
  return `<details><summary>Why this placement — ${esc(text(record.request_id))}</summary><p>Recorded decision: ${esc(formatTimestamp(record.decision_at || record.created_at))}. Resource figures describe that decision.</p><ul>${Object.entries(record.explanations || {}).map(([host, detail]) =>
    `<li><strong>${esc(detail.host_name || host)}</strong>: ${esc((detail.reason_codes || []).map(reasonText).join(', ') || 'No rejection reasons recorded')}; score ${esc(number(detail.score))}
      ${detail.measured?.available_cpu != null ? ` · ${esc(number(detail.measured.available_cpu))} CPU cores available` : ''}
      ${detail.measured?.available_memory_gib != null ? ` · ${esc(number(detail.measured.available_memory_gib))} GiB RAM available` : ''}</li>`).join('') || '<li>No recorded explanation available.</li>'}</ul></details>`;
}

function historyRows(history) {
  return history.map(row => `<tr data-key="${esc(row.reservation_id || row.request_id)}">
    <th scope="row">${esc(text(row.request_id))}</th><td>${esc(text(row.agent_family))}</td>
    <td>${esc(text(row.state))}${row.execution_claimed ? ' · launch claimed' : ''}</td>
    <td>${esc(text(row.host_uid, 'Unassigned'))}</td><td>${esc(formatTimestamp(row.created_at || row.queued_at))}</td>
    <td>${explanations(row)}</td></tr>`).join('') || '<tr><td colspan="6">No placement requests recorded.</td></tr>';
}

export function renderSchedulingView(payload, now = Date.now()) {
  const hosts = payload?.hosts ?? [];
  const eligible = hosts.filter((h) => h.policy?.mode === 'automatic' && h.trusted && h.owned && h.reachable
    && Number.isFinite(h.telemetry?.freshness_seconds) && h.telemetry.freshness_seconds <= 60).length;
  const unavailable = hosts.length - eligible;
  const cards = hosts.map((host) => {
    const stale = host.telemetry?.freshness_seconds == null || Number(host.telemetry.freshness_seconds) > 60;
    const mode = text(host.policy?.mode, 'unknown');
    const label = mode === 'manual_only' ? 'Manual only' : mode;
    return `<article class="hd-card" data-scheduler-host="${esc(host.host_uid)}">
      <div class="hd-card__title">${esc(text(host.host_name, host.host_uid))}</div>
      <div class="hd-card__sub"><strong>${esc(label)}</strong> · ${stale ? 'stale telemetry' : 'telemetry current'}</div>
      <div class="hd-card__sub">${esc(text(host.platform))} · ${host.trusted ? 'trusted' : 'untrusted'} · ${host.reachable ? 'reachable' : 'unreachable'}</div>
      <div class="hd-card__sub">CPU ${esc(text(host.capacity?.total_cpu, 'unknown'))} total · RAM ${esc(text(host.capacity?.total_memory_gib, 'unknown'))} GiB</div>
      <div class="hd-card__sub">Policy revision ${esc(text(host.policy?.revision, '0'))}</div>
    </article>`;
  }).join('');
  return `<section aria-label="Scheduling overview" data-scheduler-overview>
    <div class="hd-grid" data-scheduler-metrics>
      <herdr-metric label="Eligible hosts" value="${eligible}"></herdr-metric>
      <herdr-metric label="Unavailable" value="${unavailable}"></herdr-metric>
      <herdr-metric label="Queued agents" value="${esc(text(payload?.queued_agents, '0'))}"></herdr-metric>
      <herdr-metric label="Claimed executions" value="${esc(text(payload?.claimed_executions, 'unknown'))}"></herdr-metric>
    </div>
    <p data-scheduler-admission role="status">${payload?.admission_enabled === false ? 'Scheduler admission is disabled. Previews and policy editing remain available.'
      : payload?.admission_enabled === true ? 'Scheduler admission is enabled.' : 'Admission status unavailable.'}</p>
    <p class="hd-card__sub">Placement decisions are dry-run explainable; stale telemetry is not treated as zero load.</p>
    <form class="hd-card" data-scheduler-plan aria-label="Preview agent placement">
      <h2>Preview placement</h2>
      <div class="hd-grid">
        <label>Request ID <input name="request_id" required value="preview-1"></label>
        <label>Agent family <select name="agent_family"><option>codex</option><option>pi</option><option>claude-code</option><option>agent</option></select></label>
        <label>CPU cores <input name="cpu_cores" type="number" min="0" step="0.5" value="1"></label>
        <label>Memory GiB <input name="memory_gib" type="number" min="0" step="0.5" value="1"></label>
      </div>
      <button class="hd-btn" type="submit">Preview automatic placement</button>
      <output data-scheduler-plan-result aria-live="polite"></output>
    </form>
    <div class="hd-grid" data-scheduler-hosts>${cards || '<herdr-empty message="No scheduler host telemetry"></herdr-empty>'}</div>
    <form class="hd-card" data-scheduler-policy data-revision="" aria-label="Scheduler host policy">
      <h2>Scheduler host policy</h2>
      <p>Operator access is required. These policies govern scheduler placements; other launch paths have independent controls. Existing agents continue. Tag restrictions are preserved.</p>
      <div class="hd-grid">
        <label>Host <select name="host_uid" aria-label="Host"><option value="">Choose a host</option>${hosts.map(host => `<option value="${esc(host.host_uid)}">${esc(text(host.host_name, host.host_uid))}</option>`).join('')}</select></label>
        <label>Placement policy <select name="mode" aria-label="Placement policy">${['automatic','manual_only','draining','disabled'].map(mode => `<option value="${mode}">${mode.replaceAll('_', ' ')}</option>`).join('')}</select></label>
        <label>Reason for change <input name="reason" maxlength="300"></label>
      </div>
      <p data-policy-revision>Choose an available host.</p>
      <p data-policy-warning role="alert" hidden>Automatic mode permits background and child-agent placement on this Mac. Keep manual only unless you intend this broader eligibility.</p>
      <label class="scheduler-confirm"><input type="checkbox" name="confirm" required> <span data-policy-confirm-label>Confirm this policy change</span></label>
      <div class="scheduler-actions"><button class="hd-btn" type="submit" disabled>Save policy</button> <button class="hd-btn hd-btn--ghost" type="button" data-policy-reload>Reload policy</button></div>
      <output aria-live="polite"></output>
    </form>
    <section class="hd-card" aria-label="Placement queue"><h2>Waiting for placement</h2>
      <p>Listed by request time. Scheduling also considers priority, age, and capacity.${(payload?.queued_agents || 0) > (payload?.queue || []).length ? ` Showing ${(payload?.queue || []).length} of ${esc(payload.queued_agents)} queued requests.` : ''}</p>
      <div data-scheduler-queue>${(payload?.queue || []).map(row => `<article data-key="${esc(row.reservation_id || row.request_id)}"><strong>${esc(text(row.request_id))}</strong> · ${esc(text(row.state))}${explanations(row)}</article>`).join('') || '<p>No queued requests in the available history.</p>'}</div>
    </section>
    <section class="hd-card" aria-label="Placement history"><h2>Recent placement requests</h2>
      <p>Current state of the latest 200 requests, newest first. A launch claim does not confirm a running process.</p>
      <div class="hd-table-wrap" tabindex="0" aria-label="Placement history table"><table class="hd-table">
        <thead><tr><th scope="col">Request</th><th scope="col">Family</th><th scope="col">State</th><th scope="col">Host</th><th scope="col">Requested</th><th scope="col">Explanation</th></tr></thead>
        <tbody data-scheduler-history>${historyRows(payload?.history || [])}</tbody></table></div>
    </section>
  </section>`;
}

