import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { skeleton } from './motion.js';
import { morph } from './patch.js';
import { pollWhileVisible } from './refresh-bus.js';

const text = (value, fallback = '—') => value == null || value === '' ? fallback : String(value);

export function mountScheduling(container, client, { freshness = () => null } = {}) {
  let disposed = false, controller = null, planController = null, loaded = false;
  container.innerHTML = '<p data-scheduler-status role="status"></p><div data-scheduler-content></div><button type="button" data-scheduler-retry hidden>Retry</button>';
  const content = container.querySelector('[data-scheduler-content]');
  const status = container.querySelector('[data-scheduler-status]');
  const retry = container.querySelector('[data-scheduler-retry]');
  const refresh = async () => {
    if (disposed || controller) return;
    const request = new AbortController(); controller = request;
    if (!loaded) content.innerHTML = skeleton('scheduling', { rows: 6, height: 64 });
    retry.hidden = true;
    try {
      const data = await client.schedulerOverview({ signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]) });
      if (disposed) return;
      if (!Array.isArray(data.hosts)) throw new Error('Invalid scheduler response');
      if (!loaded) content.innerHTML = renderSchedulingView(data);
      else {
        // Keep the edited preview form and result while metrics and host telemetry refresh.
        const template = container.ownerDocument.createElement('template');
        template.innerHTML = renderSchedulingView(data);
        const grids = content.querySelectorAll('section > .hd-grid');
        template.content.querySelectorAll('section > .hd-grid').forEach((grid, i) => morph(grids[i], grid.innerHTML));
      }
      loaded = true; status.textContent = ''; freshness()?.touch(true);
    } catch (error) {
      if (!disposed && !request.signal.aborted) {
        if (!loaded) content.innerHTML = '';
        status.textContent = `Scheduling could not load: ${error.message}`;
        retry.hidden = false; freshness()?.touch(false, error.message);
      }
    } finally { if (controller === request) controller = null; }
  };
  const submit = async event => {
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
  container.addEventListener('submit', submit);
  retry.addEventListener('click', refresh);
  const stop = pollWhileVisible(refresh, 30000);
  void refresh();
  return { refresh, dispose() { disposed = true; stop(); controller?.abort(); planController?.abort(); container.removeEventListener('submit', submit); } };
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
  return `<section aria-label="Scheduling overview">
    <div class="hd-grid">
      <herdr-metric label="Eligible hosts" value="${eligible}"></herdr-metric>
      <herdr-metric label="Unavailable" value="${unavailable}"></herdr-metric>
      <herdr-metric label="Queued agents" value="${esc(text(payload?.queued_agents, '0'))}"></herdr-metric>
      <herdr-metric label="Claimed executions" value="${esc(text(payload?.claimed_executions, 'unknown'))}"></herdr-metric>
    </div>
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
    <div class="hd-grid">${cards || '<herdr-empty message="No scheduler host telemetry"></herdr-empty>'}</div>
  </section>`;
}

