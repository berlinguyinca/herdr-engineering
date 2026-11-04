import { escapeHtml as esc } from './herdr-web-components/util.js';

const text = (value, fallback = '—') => value == null || value === '' ? fallback : String(value);

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
      <form data-host-policy="${esc(host.host_uid)}" data-revision="${esc(text(host.policy?.revision, '0'))}">
        <label>Host policy <select name="mode" aria-label="Policy for ${esc(text(host.host_name))}">
          ${['automatic','manual_only','draining','disabled'].map(value => `<option value="${value}"${value === mode ? ' selected' : ''}>${value.replaceAll('_',' ')}</option>`).join('')}
        </select></label>
        <label><input type="checkbox" name="confirm" required> Confirm this policy change</label>
        <button class="hd-btn" type="submit">Save policy</button>
        <output aria-live="polite"></output>
      </form>
    </article>`;
  }).join('');
  return `<section aria-label="Scheduling overview">
    <div class="hd-grid">
      <herdr-metric label="Eligible hosts" value="${eligible}"></herdr-metric>
      <herdr-metric label="Unavailable" value="${unavailable}"></herdr-metric>
      <herdr-metric label="Queued agents" value="${esc(text(payload?.queued_agents, '0'))}"></herdr-metric>
      <herdr-metric label="Claimed executions" value="${esc(text(payload?.claimed_executions, '0'))}"></herdr-metric>
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
    <section class="hd-card" aria-label="Placement history"><h2>Placement history</h2>
      <p>Claimed executions retain capacity until their process state is reconciled. A claim does not confirm a running process.</p>
      ${(payload?.history || []).map(row => `<article><strong>${esc(text(row.request_id))}</strong>
        <span>${esc(text(row.state))} · ${esc(text(row.host_uid))}${row.execution_claimed ? ' · execution claimed' : ''}</span>
        ${row.state === 'queued' ? `<button class="hd-btn" type="button" data-retry-placement="${esc(row.reservation_id)}">Retry queued placement</button><output aria-live="polite"></output>` : ''}</article>`).join('') || '<p>No placement history yet.</p>'}
    </section>
    <form class="hd-card" data-scheduler-simulator aria-label="Historical placement simulation">
      <h2>Historical simulation</h2>
      <label>History window <select name="days"><option value="1">24 hours</option><option value="7">7 days</option><option value="30">30 days</option></select></label>
      <label>CPU weight <input type="number" name="cpu_weight" min="0" max="1" step="0.1" value="0.6"></label>
      <label>Memory weight <input type="number" name="memory_weight" min="0" max="1" step="0.1" value="0.4"></label>
      <button type="submit" class="hd-btn">Preview historical placement</button>
      <p>Counterfactual preview of recorded snapshots. Missing metrics remain unknown; results do not predict performance or change policy.</p>
      <output data-simulator-result aria-live="polite"></output>
    </form>
  </section>`;
}

