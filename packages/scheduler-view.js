import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatTimestamp } from './herdr-web-components/format.js';

const text = (value, fallback = '—') => value == null || value === '' ? fallback : String(value);

export function renderSchedulingView(payload, now = Date.now()) {
  const hosts = payload?.hosts ?? [];
  const eligible = hosts.filter((h) => h.policy?.mode === 'automatic' && h.trusted && h.owned && h.reachable).length;
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
      <herdr-metric label="Running agents" value="${esc(text(payload?.running_agents, '0'))}"></herdr-metric>
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

