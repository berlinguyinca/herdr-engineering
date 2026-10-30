// Herdr Personal Assistant view (Rakazo integration, P2 settings + chat MVP).
// Built on the existing vanilla JS / Web Components stack and design tokens.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { requestErrorDetail } from './dashboard-load-state.js';

function statusClass(status) {
  return { ready: 'pa-status--ready', running: 'pa-status--running', disabled: 'pa-status--disabled',
           needs_attention: 'pa-status--needs' }[status] || 'pa-status--disabled';
}

function statusLabel(status) {
  return { ready: 'Ready', running: 'Running', disabled: 'Disabled', needs_attention: 'Needs attention' }[status] || status;
}

export function mountPersonalAssistant(container, client, { onFreshness = () => {} } = {}) {
  let disposed = false;
  let state = { assistants: [], selectedId: null, tab: 'General' };

  const render = async () => {
    container.innerHTML = skeleton('Personal Assistant');
    try {
      const { assistants = [] } = await client.listAssistants();
      if (disposed) return;
      state.assistants = assistants;
      if (!state.selectedId && assistants.length) state.selectedId = assistants[0].assistant_id;
      draw();
      onFreshness(true);
    } catch (error) {
      if (!disposed) {
        const detail = requestErrorDetail(error);
        container.innerHTML = `<herdr-error message="${esc(detail)}"></herdr-error>`;
        onFreshness(false, `Personal Assistant: ${detail}`);
      }
    }
  };

  // Only assistants that are on and not archived are offered; of those the three most recently changed lead, the rest wait
  // behind "Show all". Nothing is archived or removed by this: it only decides what is listed.
  const stamp = a => Date.parse(a.updated_at || a.created_at || '') || 0;
  const isActive = a => a.enabled !== false && !/archiv|disabled|deleted/i.test(String(a.status || ''));
  let showAll = false, configOpen = false, pending = [];
  const offered = () => {
    const active = state.assistants.filter(isActive).sort((x, y) => stamp(y) - stamp(x));
    const list = showAll ? active : active.slice(0, 3);
    const sel = state.assistants.find(a => a.assistant_id === state.selectedId);
    return { list: sel && !list.includes(sel) ? [sel, ...list] : list, active: active.length, hidden: state.assistants.length - active.length };
  };

  const draw = () => {
    const selected = state.assistants.find(a => a.assistant_id === state.selectedId);
    const { list, active, hidden } = offered();
    const more = Math.max(0, active - list.filter(isActive).length);
    container.innerHTML = `
      <div class="pa-bar">
        <label class="pa-pick">Assistant
          <select data-select aria-label="Assistant">${list.map(a => `<option value="${esc(a.assistant_id)}" ${a.assistant_id === state.selectedId ? 'selected' : ''}>${esc(a.name)} · ${esc(statusLabel(a.status))}</option>`).join('')}</select></label>
        ${more > 0 || showAll && active > 3 ? `<button class="hd-linkbtn" data-showall>${showAll ? 'Show recent only' : `Show all ${active}`}</button>` : ''}
        <span class="pa-grow"></span>
        <button class="hd-btn hd-btn--ghost" data-config aria-expanded="${configOpen}">Configure</button>
        <button class="hd-btn" data-new>+ New</button>
      </div>
      ${hidden ? `<p class="pa-muted pa-hidden-note">${hidden} inactive assistant${hidden === 1 ? '' : 's'} not listed.</p>` : ''}
      ${selected ? `<div id="pa-strip" class="pa-strip" role="status" ${pending.some(ap => ap.assistant_id === selected.assistant_id) ? '' : 'hidden'}></div>` : ''}
      ${selected ? renderChat(selected) : `<div class="hd-card"><div class="pa-empty">${state.assistants.length ? 'No active assistant. Use Show all, or create one.' : 'No assistants yet. Create one to begin.'}</div></div>`}
      ${selected && configOpen ? `<section class="hd-card pa-config" aria-label="Configure ${esc(selected.name)}">
        <div class="pa-row"><div class="pa-status ${statusClass(selected.status)}"><span class="hd-status__dot"></span>${esc(statusLabel(selected.status))}</div>
          <span class="pa-muted">${esc(selected.assistant_id)}</span><span class="pa-badge">${selected.enabled ? 'enabled' : 'disabled'}</span></div>
        <div class="pa-tabs">${['General', 'Permissions', 'Memory', 'Routines', 'Connectors', 'Delegations', 'Approvals', 'Proposals', 'Notifications', 'Diagnostics'].map(t =>
          `<button class="pa-tab ${state.tab === t ? 'pa-tab--active' : ''}" data-tab="${t}">${t}</button>`).join('')}</div>
        <div id="pa-panel">${renderPanel(selected)}</div></section>` : ''}`;
    container.querySelector('[data-select]')?.addEventListener('change', e => { state.selectedId = e.target.value; draw(); });
    container.querySelector('[data-showall]')?.addEventListener('click', () => { showAll = !showAll; draw(); });
    container.querySelector('[data-config]')?.addEventListener('click', () => { configOpen = !configOpen; draw(); });
    if (selected) drawApprovalStrip(selected);
    container.querySelectorAll('[data-tab]').forEach(el => el.addEventListener('click', () => {
      state.tab = el.dataset.tab; draw();
    }));
    container.querySelectorAll('[data-new]').forEach(el => el.addEventListener('click', async () => {
      const name = prompt('Assistant name', 'Personal Assistant');
      if (!name) return;
      await client.createAssistant({ name });
      await render();
    }));
    const panel = container.querySelector('#pa-panel');
    if (panel) bindPanel(panel, selected);
    bindChat(container, selected);
  };

  // Waiting approvals are the one thing a person must act on here, so they sit above the conversation rather than in a tab.
  const drawApprovalStrip = async (a) => {
    try {
      const { approvals = [] } = await client.listApprovals();
      if (state.selectedId !== a.assistant_id) return;     // the picker moved on while this was in flight
      pending = approvals.filter(ap => ap.decision === 'pending');
      const mine = pending.filter(ap => ap.assistant_id === a.assistant_id);
      const strip = container.querySelector('#pa-strip');
      if (!strip) return;
      strip.hidden = !mine.length;
      strip.innerHTML = mine.map(ap => `<div class="pa-row"><span><strong>Approval needed</strong> · ${esc(ap.policy_snapshot)}</span><span class="pa-grow"></span>
        <button class="hd-btn hd-btn--sm" data-strip-approve="${esc(ap.approval_id)}">Approve</button>
        <button class="hd-btn hd-btn--danger hd-btn--sm" data-strip-deny="${esc(ap.approval_id)}">Deny</button></div>`).join('');
      strip.querySelectorAll('[data-strip-approve],[data-strip-deny]').forEach(b => b.addEventListener('click', async () => {
        const id = b.dataset.stripApprove || b.dataset.stripDeny;
        try { await client.decideApproval(id, b.dataset.stripApprove ? 'approved' : 'denied'); } catch { /* the strip refreshes below */ }
        drawApprovalStrip(a);
      }));
    } catch { /* approvals unavailable: the Approvals tab reports it */ }
  };

  const renderPanel = (a) => {
    if (state.tab === 'General') return renderGeneral(a);
    if (state.tab === 'Permissions') return `<div id="pa-perms">${skeleton('permissions')}</div>`;
    if (state.tab === 'Memory') return `<div id="pa-memory">${skeleton('memory')}</div>`;
    if (state.tab === 'Routines') return `<div id="pa-routines">${skeleton('routines')}</div>`;
    if (state.tab === 'Connectors') return `<div id="pa-connectors">${skeleton('connectors')}</div>`;
    if (state.tab === 'Delegations') return `<div id="pa-delegations">${skeleton('delegations')}</div>`;
    if (state.tab === 'Approvals') return `<div id="pa-approvals">${skeleton('approvals')}</div>`;
    if (state.tab === 'Proposals') return `<div id="pa-proposals">${skeleton('proposals')}</div>`;
    if (state.tab === 'Notifications') return `<div id="pa-notifications">${skeleton('notifications')}</div>`;
    return `<div id="pa-diagnostics">${skeleton('diagnostics')}</div>`;
  };

  const renderGeneral = (a) => `
    <div class="pa-field"><label>Name</label><input data-f="name" value="${esc(a.name)}"></div>
    <div class="pa-field"><label>Persona</label>
      <select data-f="persona">
        ${['helpful', 'concise', 'technical', 'analytical'].map(p =>
          `<option value="${p}" ${a.persona === p ? 'selected' : ''}>${p}</option>`).join('')}
      </select>
    </div>
    <div class="pa-field"><label>Instructions</label><textarea data-f="instructions">${esc(a.instructions)}</textarea></div>
    <div class="pa-field"><label>Model policy</label>
      <select data-f="model_policy">
        <option value="auto" ${a.model_policy === 'auto' ? 'selected' : ''}>Auto (InferWeave routing)</option>
        <option value="manual" ${a.model_policy === 'manual' ? 'selected' : ''}>Manual</option>
      </select>
    </div>
    <div class="pa-row">
      <label class="pa-muted"><input type="checkbox" data-f="enabled" ${a.enabled ? 'checked' : ''}> Enabled</label>
      <button class="hd-btn" data-save>Save</button>
      <button class="hd-btn hd-btn--danger" data-archive>Archive</button>
    </div>`;

  const bindPanel = (panel, a) => {
    const save = panel.querySelector('[data-save]');
    if (save) save.addEventListener('click', async () => {
      const patch = {};
      panel.querySelectorAll('[data-f]').forEach(el => {
        const key = el.dataset.f;
        patch[key] = el.type === 'checkbox' ? el.checked : el.value;
      });
      await client.updateAssistant(a.assistant_id, patch);
      await render();
    });
    const archive = panel.querySelector('[data-archive]');
    if (archive) archive.addEventListener('click', async () => {
      if (confirm(`Archive ${a.name}?`)) { await client.archiveAssistant(a.assistant_id); await render(); }
    });
    const perms = panel.querySelector('#pa-perms');
    if (perms) loadPerms(perms, a);
    const memory = panel.querySelector('#pa-memory');
    if (memory) loadMemory(memory, a);
    const routines = panel.querySelector('#pa-routines');
    if (routines) loadRoutines(routines, a);
    const connectors = panel.querySelector('#pa-connectors');
    if (connectors) loadConnectors(connectors, a);
    const delegations = panel.querySelector('#pa-delegations');
    if (delegations) loadDelegations(delegations, a);
    const approvals = panel.querySelector('#pa-approvals');
    if (approvals) loadApprovals(approvals, a);
    const proposals = panel.querySelector('#pa-proposals');
    if (proposals) loadProposals(proposals, a);
    const notifications = panel.querySelector('#pa-notifications');
    if (notifications) loadNotifications(notifications, a);
    const diag = panel.querySelector('#pa-diagnostics');
    if (diag) loadDiagnostics(diag, a);
  };

  const loadPerms = async (el, a) => {
    try {
      const { tools = [], grants = [] } = await client.getAssistantPermissions(a.assistant_id);
      el.innerHTML = `
        <div class="hd-card__sub">Tools are deny-by-default. A tool only acts when the assistant holds a grant; sensitive tools additionally require an approval unless pre-authorized.</div>
        <div class="hd-table-wrap"><table class="hd-table"><thead><tr><th>Tool</th><th>Sensitive</th><th>Granted</th><th></th></tr></thead><tbody>
        ${tools.map(t => `<tr><td>${esc(t.tool_id)}</td><td>${t.sensitive ? '<span class="pa-badge pa-badge--sensitive">sensitive</span>' : 'no'}</td>
          <td>${t.granted ? '<span class="pa-badge pa-badge--granted">granted</span>' : 'no'}</td>
          <td>${t.granted ? '' : `<button class="hd-btn hd-btn--sm" data-grant="${esc(t.tool_id)}">Grant</button>`}</td></tr>`).join('')}
        </tbody></table></div>
        <div class="hd-card__sub" style="margin-top:8px">Active grants</div>
        <div>${grants.length ? grants.map(g =>
          `<div class="pa-msg pa-msg--meta">${esc(g.tool_id)} · ${esc(g.scope)} · expires ${esc(g.expires_at)} ${g.constraints?.preauthorized ? '· pre-authorized' : ''}</div>`).join('') : '<div class="pa-muted">No grants</div>'}</div>`;
      el.querySelectorAll('[data-grant]').forEach(btn => btn.addEventListener('click', async () => {
        const preauth = confirm('Pre-authorize sensitive operations (skip approval)?\nOK = yes, Cancel = no');
        await client.grantTool(a.assistant_id, { tool_id: btn.dataset.grant, scope: '*', preauthorized: preauth });
        await loadPerms(el, a);
      }));
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const loadMemory = async (el, a) => {
    try {
      const { memory = [] } = await client.searchMemory(a.assistant_id, '');
      el.innerHTML = `
        <div class="pa-row">
          <input style="flex:1" placeholder="Search memory…" data-mq>
          <button class="hd-btn" data-msearch>Search</button>
          <button class="hd-btn hd-btn--ghost" data-mexport>Export</button>
        </div>
        <div class="pa-field" style="margin-top:8px"><textarea data-mcontent placeholder="Store a corrected fact (provenance tracked)"></textarea>
          <button class="hd-btn hd-btn--sm" data-mstore>Store</button></div>
        <div id="pa-memory-results">${memory.map(m => `
          <div class="pa-msg pa-msg--meta">${esc(m.content)} <button class="hd-btn hd-btn--danger hd-btn--sm" data-mforget="${esc(m.memory_id)}">Forget</button></div>`).join('') || '<div class="pa-muted">No memory stored.</div>'}</div>`;
      el.querySelector('[data-msearch]').addEventListener('click', async () => {
        const q = el.querySelector('[data-mq]').value;
        const r = await client.searchMemory(a.assistant_id, q);
        el.querySelector('#pa-memory-results').innerHTML = r.memory.map(m =>
          `<div class="pa-msg pa-msg--meta">${esc(m.content)} <button class="hd-btn hd-btn--danger hd-btn--sm" data-mforget="${esc(m.memory_id)}">Forget</button></div>`).join('') || '<div class="pa-muted">No results.</div>';
        bindForget(el.querySelector('#pa-memory-results'), a, el);
      });
      el.querySelector('[data-mstore]').addEventListener('click', async () => {
        const content = el.querySelector('[data-mcontent]').value;
        if (content) { await client.storeMemory(a.assistant_id, { content, source_ref: 'ui' }); el.querySelector('[data-mcontent]').value = ''; await loadMemory(el, a); }
      });
      el.querySelector('[data-mexport]').addEventListener('click', async () => {
        const r = await client.exportMemory(a.assistant_id);
        const blob = new Blob([JSON.stringify(r, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob); const a2 = document.createElement('a');
        a2.href = url; a2.download = `assistant-${a.assistant_id}-memory.json`; a2.click(); URL.revokeObjectURL(url);
      });
      bindForget(el.querySelector('#pa-memory-results'), a, el);
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const bindForget = (resultsEl, a, el) => {
    resultsEl.querySelectorAll('[data-mforget]').forEach(btn => btn.addEventListener('click', async () => {
      await client.forgetMemory(btn.dataset.mforget); await loadMemory(el, a);
    }));
  };

  const loadRoutines = async (el, a) => {
    try {
      const { routines = [] } = await client.listRoutines();
      const mine = routines.filter(r => r.assistant_id === a.assistant_id);
      el.innerHTML = `
        <div class="pa-row">
          <input data-rname placeholder="Routine name" style="flex:1">
          <select data-rtype><option value="interval">Interval</option><option value="daily">Daily</option></select>
          <input data-rspec placeholder='{"every":"hours","amount":1}' style="flex:1">
          <input data-rtz placeholder="Timezone" value="UTC" style="width:110px">
          <input data-rquiet placeholder="Quiet 22:00-07:00" style="width:150px">
          <button class="hd-btn hd-btn--sm" data-rcreate>Create</button>
        </div>
        <div style="margin-top:8px">${mine.length ? mine.map(r => `
          <div class="pa-msg pa-msg--meta">
            <strong>${esc(r.name)}</strong> · ${esc(r.trigger_type)} · ${esc(r.timezone)}
            · next ${esc(r.next_at || '—')} · ${esc(r.last_result || 'never run')}
            ${r.enabled ? `<button class="hd-btn hd-btn--sm" data-rtrigger="${esc(r.routine_id)}">Run</button>` : ''}
            <button class="hd-btn hd-btn--danger hd-btn--sm" data-rarchive="${esc(r.routine_id)}">Archive</button>
          </div>`).join('') : '<div class="pa-muted">No routines yet.</div>'}</div>`;
      el.querySelector('[data-rcreate]').addEventListener('click', async () => {
        let spec = {}; try { spec = JSON.parse(el.querySelector('[data-rspec]').value || '{}'); } catch { /* ignore */ }
        let quiet = null; const q = el.querySelector('[data-rquiet]').value;
        if (q) { const [start, end] = q.split('-'); quiet = { start: start.trim(), end: end.trim() }; }
        await client.createRoutine({ assistant_id: a.assistant_id, name: el.querySelector('[data-rname]').value || 'Routine', trigger_type: el.querySelector('[data-rtype]').value, trigger_spec: spec, timezone: el.querySelector('[data-rtz]').value || 'UTC', quiet_hours: quiet });
        await loadRoutines(el, a);
      });
      el.querySelectorAll('[data-rtrigger]').forEach(b => b.addEventListener('click', async () => { await client.triggerRoutine(b.dataset.rtrigger); await loadRoutines(el, a); }));
      el.querySelectorAll('[data-rarchive]').forEach(b => b.addEventListener('click', async () => { await client.archiveRoutine(b.dataset.rarchive); await loadRoutines(el, a); }));
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const loadConnectors = async (el, a) => {
    try {
      const { connectors = [] } = await client.listConnectors();
      el.innerHTML = `
        <div class="pa-row">
          <input data-cname placeholder="Name" style="flex:1">
          <select data-ckind><option value="http">http</option><option value="openapi">openapi</option><option value="mcp">mcp</option></select>
          <input data-curl placeholder="https://host" style="flex:1.4">
          <input data-callow placeholder="Allowed host(s), comma-sep" style="flex:1.4">
          <button class="hd-btn hd-btn--sm" data-ccreate>Register</button>
        </div>
        <div style="margin-top:8px">${connectors.length ? connectors.map(c => `
          <div class="pa-msg pa-msg--meta">
            <strong>${esc(c.name)}</strong> · ${esc(c.kind)} · ${esc(c.base_url)}
            · auth ${c.auth_present ? 'yes' : 'no'} · ${c.enabled ? 'enabled' : 'disabled'}
            <button class="hd-btn hd-btn--sm" data-creq="${esc(c.connector_id)}" aria-label="Test connector ${esc(c.name)}">Test</button>
            <button class="hd-btn hd-btn--danger hd-btn--sm" data-carc="${esc(c.connector_id)}">Archive</button>
            <div data-cstatus="${esc(c.connector_id)}" role="status" aria-live="polite"></div>
          </div>`).join('') : '<div class="pa-muted">No connectors registered.</div>'}</div>`;
      el.querySelector('[data-ccreate]').addEventListener('click', async () => {
        const hosts = el.querySelector('[data-callow]').value.split(',').map(s => s.trim()).filter(Boolean);
        await client.registerConnector({ name: el.querySelector('[data-cname]').value || 'Connector', kind: el.querySelector('[data-ckind]').value, base_url: el.querySelector('[data-curl]').value, auth: { _allowed_origins: hosts } });
        await loadConnectors(el, a);
      });
      // The check goes through the same gate as every tool call: it may be refused (no grant), answer, or wait for an
      // approval, and the row says which. Nothing is called until a person approves.
      el.querySelectorAll('[data-creq]').forEach(b => b.addEventListener('click', async () => {
        const out = [...el.querySelectorAll('[data-cstatus]')].find(n => n.dataset.cstatus === b.dataset.creq);
        b.disabled = true;
        out.className = 'pa-msg pa-msg--meta'; out.textContent = 'Checking…';
        try {
          const res = await client.testConnector(b.dataset.creq, { assistant_id: a.assistant_id });
          out.textContent = res.replayed ? 'Connector test: already ran.' : `Connector test: ${res.ok ? 'ok' : (res.reason || res.error || 'denied')}`;
        } catch (e) {
          const body = (e && e.body) || {};
          if (e && e.status === 403 && body.code === 'approval_needed') {
            out.className = 'pa-msg pa-approval';
            out.textContent = `Approval pending (${body.approval_id}). ${body.dry_run || ''} Nothing was called. `;
            const go = document.createElement('button');
            go.className = 'hd-linkbtn'; go.type = 'button'; go.textContent = 'Open approvals';
            go.addEventListener('click', () => { state.tab = 'Approvals'; draw(); });
            out.appendChild(go);
          } else {
            out.className = 'pa-msg pa-msg--meta';
            out.textContent = `Connector test: ${body.error || (e && e.message) || 'failed'}`;
          }
        } finally { b.disabled = false; }
      }));
      el.querySelectorAll('[data-carc]').forEach(b => b.addEventListener('click', async () => { await client.archiveConnector(b.dataset.carc); await loadConnectors(el, a); }));
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const loadDelegations = async (el, a) => {
    try {
      const { delegations = [] } = await client.listDelegations();
      const mine = delegations.filter(d => d.assistant_id === a.assistant_id);
      el.innerHTML = `
        <div class="pa-row">
          <input data-dtask placeholder="Task to delegate to a Pi mission" style="flex:1">
          <button class="hd-btn hd-btn--sm" data-dcreate>Delegate</button>
        </div>
        <div style="margin-top:8px">${mine.length ? mine.map(d => `
          <div class="pa-msg pa-msg--meta">
            <strong>${esc(d.status)}</strong> · ${esc(d.task)}
            ${d.artifact_refs.length ? ' · <code>'+esc(d.artifact_refs.join(', '))+'</code>' : ''}
            ${(d.status === 'queued' || d.status === 'running') ? `<button class="hd-btn hd-btn--danger hd-btn--sm" data-dcancel="${esc(d.delegation_id)}">Cancel</button>` : ''}
          </div>`).join('') : '<div class="pa-muted">No delegations yet.</div>'}</div>`;
      el.querySelector('[data-dcreate]').addEventListener('click', async () => {
        await client.delegate({ assistant_id: a.assistant_id, task: el.querySelector('[data-dtask]').value || 'Task', target_system: 'pi' });
        await loadDelegations(el, a);
      });
      el.querySelectorAll('[data-dcancel]').forEach(b => b.addEventListener('click', async () => { await client.cancelDelegation(b.dataset.dcancel); await loadDelegations(el, a); }));
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const errText = (e) => ((e && e.body && e.body.error) || (e && e.message) || 'failed');
  const when = (iso) => { const t = Date.parse(iso || ''); return t ? new Date(t).toLocaleString() : ''; };
  const flashHtml = (flash) => `<div class="pa-flash" role="${flash && flash.error ? 'alert' : 'status'}" aria-live="polite">${flash ? esc(flash.text) : ''}</div>`;

  const loadApprovals = async (el, a, flash = null) => {
    try {
      const { approvals = [] } = await client.listApprovals();
      const mine = approvals.filter(ap => ap.assistant_id === a.assistant_id);
      el.innerHTML = flashHtml(flash) + (mine.length ? mine.map(ap => `
        <article class="pa-approval pa-msg" aria-label="Approval for ${esc(ap.policy_snapshot)}">
          <div><strong>${esc(ap.policy_snapshot)}</strong> · <span class="pa-badge">${esc(ap.decision)}</span>
            ${ap.action_status ? `<span class="pa-badge">action ${esc(ap.action_status)}</span>` : ''}</div>
          ${ap.dry_run ? `<div class="pa-muted" data-dryrun>Approving allows: ${esc(ap.dry_run)}</div>` : ''}
          <div class="pa-muted">Requested by ${esc(ap.requested_by || 'unknown')} · expires ${esc(when(ap.expires_at))} · ${esc(ap.approval_id)}</div>
          ${ap.decision === 'pending' ? `<div class="pa-muted">The approver must be a different person from the requester.</div>` : ''}
          ${ap.result && ap.result.summary ? `<div class="pa-muted" data-result>Result: ${esc(ap.result.summary)}</div>` : ''}
          ${ap.result && ap.result.error ? `<div class="pa-muted" data-result>Failed: ${esc(ap.result.error)}</div>` : ''}
          ${ap.decision === 'pending' ? `<div class="pa-row" style="margin-top:6px">
            <button class="hd-btn hd-btn--sm" data-approve="${esc(ap.approval_id)}" aria-label="Approve ${esc(ap.policy_snapshot)}">Approve</button>
            <button class="hd-btn hd-btn--danger hd-btn--sm" data-deny="${esc(ap.approval_id)}" aria-label="Deny ${esc(ap.policy_snapshot)}">Deny</button></div>` : ''}
          ${ap.decision === 'approved' && ap.action_status === 'approved' && ap.executable ? `<div class="pa-row" style="margin-top:6px">
            <button class="hd-btn hd-btn--sm" data-run="${esc(ap.approval_id)}" aria-label="Run approved ${esc(ap.policy_snapshot)} now">Run now</button></div>` : ''}
        </article>`).join('') : '<div class="pa-muted">No approvals.</div>');
      el.querySelectorAll('[data-approve],[data-deny]').forEach(btn => btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
          const out = await client.decideApproval(btn.dataset.approve || btn.dataset.deny, btn.dataset.approve ? 'approved' : 'denied');
          const ex = out && out.execution;
          await loadApprovals(el, a, { text: btn.dataset.approve ? `Approved${ex ? `; execution ${ex.status}${ex.error ? `: ${ex.error}` : ''}` : ''}.` : 'Denied.' });
        } catch (e) { await loadApprovals(el, a, { error: true, text: errText(e) }); }
      }));
      el.querySelectorAll('[data-run]').forEach(btn => btn.addEventListener('click', async () => {
        btn.disabled = true;
        try { const out = await client.executeApproval(btn.dataset.run); await loadApprovals(el, a, { text: `Execution ${out.status}.` }); }
        catch (e) { await loadApprovals(el, a, { error: true, text: errText(e) }); }
      }));
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  // Proposals the assistant raised for a person to approve. Pending, approved (waiting to run), executed, then the rest.
  const loadProposals = async (el, a, flash = null) => {
    try {
      const { proposals = [] } = await client.listProposals();
      const mine = proposals.filter(x => !x.assistant_id || x.assistant_id === a.assistant_id);
      const group = (x) => x.execution === 'executed' ? 'executed'
        : x.status === 'awaiting_approval' ? 'pending' : x.status === 'approved' ? 'approved' : 'closed';
      const titles = { pending: 'Waiting for approval', approved: 'Approved, not yet run', executed: 'Executed', closed: 'Rejected, expired or failed' };
      const card = (x) => `
        <article class="pa-msg ${x.status === 'awaiting_approval' ? 'pa-approval' : ''}" aria-label="Proposal ${esc(x.title)}">
          <div><strong>${esc(x.title)}</strong> · <span class="pa-badge">${esc(x.kind)}</span> <span class="pa-badge">${esc(x.status)}</span>
            <span class="pa-badge">execution ${esc(x.execution)}</span></div>
          <div class="pa-muted">Requested by ${esc(x.requested_by || 'unknown')}${x.decided_by ? ` · decided by ${esc(x.decided_by)}` : ''} · ${esc(x.proposal_id)}</div>
          ${x.result_ref ? `<div class="pa-muted">Handed to ${esc(x.result_ref)}</div>` : ''}
          ${x.execution_error ? `<div class="pa-muted">Failed: ${esc(x.execution_error)}</div>` : ''}
          <div class="pa-muted" data-preview="${esc(x.proposal_id)}" role="status" aria-live="polite"></div>
          <div class="pa-row" style="margin-top:6px">
            ${x.kind !== 'ci_repair' ? `<button class="hd-btn hd-btn--ghost hd-btn--sm" data-ppreview="${esc(x.proposal_id)}" aria-label="Preview ${esc(x.title)}">Preview</button>` : ''}
            ${x.status === 'awaiting_approval' && x.kind !== 'ci_repair' ? `
              <button class="hd-btn hd-btn--sm" data-papprove="${esc(x.proposal_id)}" aria-label="Approve ${esc(x.title)}">Approve</button>
              <button class="hd-btn hd-btn--danger hd-btn--sm" data-preject="${esc(x.proposal_id)}" aria-label="Reject ${esc(x.title)}">Reject</button>
              <span class="pa-muted">The approver must differ from the requester.</span>` : ''}
            ${x.status === 'awaiting_approval' && x.kind === 'ci_repair' ? '<span class="pa-muted">CI repairs are decided in the attention queue.</span>' : ''}
            ${x.status === 'approved' && x.execution === 'not_started' ? `<button class="hd-btn hd-btn--sm" data-prun="${esc(x.proposal_id)}" aria-label="Run approved ${esc(x.title)} now">Run now</button>` : ''}
          </div>
        </article>`;
      const groups = ['pending', 'approved', 'executed', 'closed'].map(g => [g, mine.filter(x => group(x) === g)]);
      el.innerHTML = flashHtml(flash) + (mine.length ? groups.filter(([, xs]) => xs.length).map(([g, xs]) => `
        <section aria-labelledby="pa-prop-${g}"><h3 id="pa-prop-${g}" class="pa-h3">${titles[g]} (${xs.length})</h3>${xs.map(card).join('')}</section>`).join('')
        : '<div class="pa-muted">No proposals.</div>');
      const reload = (f) => loadProposals(el, a, f);
      const act = (sel, fn) => el.querySelectorAll(sel).forEach(btn => btn.addEventListener('click', async () => {
        btn.disabled = true;
        try { await fn(btn); } catch (e) { await reload({ error: true, text: errText(e) }); }
      }));
      act('[data-ppreview]', async (btn) => {
        const out = await client.previewProposal(btn.dataset.ppreview);
        const slot = [...el.querySelectorAll('[data-preview]')].find(n => n.dataset.preview === btn.dataset.ppreview);
        slot.textContent = `Dry run: ${out.description}${out.payload_intact ? '' : ' (payload no longer matches what was proposed)'}`;
        btn.disabled = false;
      });
      act('[data-papprove]', async (btn) => {
        const out = await client.decideProposal(btn.dataset.papprove, 'approved');
        await reload({ text: out.execution === 'executed' ? 'Approved and handed off.' : 'Approved.' });
      });
      act('[data-preject]', async (btn) => { await client.decideProposal(btn.dataset.preject, 'rejected'); await reload({ text: 'Rejected.' }); });
      act('[data-prun]', async (btn) => { const out = await client.executeProposal(btn.dataset.prun); await reload({ text: `Execution ${out.execution}.` }); });
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  // Delivery records on the dashboard channel. Nothing here is sent anywhere else.
  const loadNotifications = async (el, a, flash = null) => {
    try {
      const { notifications = [] } = await client.listNotifications();
      el.innerHTML = flashHtml(flash) + (notifications.length ? notifications.map(n => `
        <article class="pa-msg" aria-label="Notification ${esc(n.key)}">
          <div><span class="pa-badge ${n.priority === 'urgent' ? 'pa-badge--sensitive' : ''}">${esc(n.priority)}</span>
            <span class="pa-badge">${esc(n.state.replace(/_/g, ' '))}</span>
            <span class="pa-badge">${esc(n.channel)}${n.external ? ' · external' : ' · not sent externally'}</span></div>
          <div>${esc(n.text)}</div>
          <div class="pa-muted">${esc(when(n.created_at))} · ${esc(n.notification_id)}
            ${typeof n.link === 'string' && n.link.startsWith('#/') ? ` · <a href="${esc(n.link)}">Open</a>` : ''}</div>
          ${n.state !== 'acknowledged' ? `<div class="pa-row" style="margin-top:6px"><button class="hd-btn hd-btn--sm" data-nack="${esc(n.notification_id)}" aria-label="Acknowledge ${esc(n.key)}">Acknowledge</button></div>` : ''}
        </article>`).join('') : '<div class="pa-muted">No notifications.</div>');
      el.querySelectorAll('[data-nack]').forEach(btn => btn.addEventListener('click', async () => {
        btn.disabled = true;
        try { await client.ackNotification(btn.dataset.nack); await loadNotifications(el, a, { text: 'Acknowledged.' }); }
        catch (e) { await loadNotifications(el, a, { error: true, text: errText(e) }); }
      }));
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const loadDiagnostics = async (el, a) => {
    try {
      const d = await client.assistantDiagnostics(a.assistant_id);
      el.innerHTML = `<div class="hd-table-wrap"><table class="hd-table"><tbody>
        ${Object.entries(d).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(String(v))}</td></tr>`).join('')}
      </tbody></table></div>`;
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const renderChat = (a) => `
    <div class="hd-card">
      <div class="hd-card__title">Chat</div>
      <div id="pa-threads">${skeleton('threads')}</div>
      <div id="pa-messages" class="pa-chat" style="min-height:180px;margin-top:10px"></div>
      <div class="pa-row" style="margin-top:10px">
        <input style="flex:1" placeholder="Message…" data-msg>
        <button class="hd-btn" data-send>Send</button>
        <button class="hd-btn hd-btn--ghost" data-voice title="Voice input">🎤</button>
        <button class="hd-btn hd-btn--ghost" data-speak title="Speak reply">🔊</button>
        <button class="hd-btn hd-btn--ghost" data-newthread>New thread</button>
      </div>
      <div id="pa-runstatus" class="pa-muted" style="margin-top:6px"></div>
    </div>`;

  let currentThread = null;
  let currentThreadAssistantId = null;
  const bindChat = async (container, a) => {
    const sendBtn = container.querySelector('[data-send]');
    const input = container.querySelector('[data-msg]');
    if (!sendBtn) return;
    // a thread belongs to one assistant; reset the selected thread when the
    // active assistant changes so we never load a foreign assistant's thread
    if (currentThreadAssistantId !== a.assistant_id) { currentThread = null; currentThreadAssistantId = a.assistant_id; }
    const threadsEl = container.querySelector('#pa-threads');
    const messagesEl = container.querySelector('#pa-messages');
    const statusEl = container.querySelector('#pa-runstatus');
    try {
      const { threads = [] } = await client.listAssistantThreads(a.assistant_id);
      if (!currentThread) currentThread = threads[0] ? threads[0].thread_id : null;
      if (!currentThread) {
        const created = await client.createThread(a.assistant_id, 'New conversation');
        currentThread = created.thread_id;
        threads.unshift(created);
      }
      threadsEl.innerHTML = `<div class="pa-row">${threads.map(t =>
        `<button class="hd-btn hd-btn--sm ${t.thread_id === currentThread ? 'pa-tab--active' : ''}" data-t="${esc(t.thread_id)}">${esc(t.title)}</button>`).join('')}</div>`;
      threadsEl.querySelectorAll('[data-t]').forEach(b => b.addEventListener('click', async () => { currentThread = b.dataset.t; await loadMessages(client, a, currentThread, messagesEl); }));
      await loadMessages(client, a, currentThread, messagesEl);
    } catch (error) { threadsEl.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
    sendBtn.addEventListener('click', async () => {
      const content = input.value.trim();
      if (!content || !currentThread) return;
      input.value = ''; statusEl.textContent = 'Running…';
      try {
        const { run } = await client.sendAssistantMessage(a.assistant_id, currentThread, content);
        await loadMessages(client, a, currentThread, messagesEl);
        statusEl.textContent = `Run ${run.run_id} · ${run.state}`;
      } catch (error) { statusEl.textContent = 'Error: ' + error.message; }
    });
    const voiceBtn = container.querySelector('[data-voice]');
    const speakBtn = container.querySelector('[data-speak]');
    const voiceMod = await import('./voice.js').catch(() => null);
    if (voiceMod && voiceMod.createVoiceInput && voiceBtn) {
      const v = voiceMod.createVoiceInput({
        win: window,
        onResult: (t) => { input.value = t; sendBtn.click(); },
        onError: () => { statusEl.textContent = 'Voice input unavailable.'; },
      });
      voiceBtn.addEventListener('click', () => v.start());
      if (!v.available) { voiceBtn.title = 'Speech recognition not available'; voiceBtn.disabled = true; }
    } else if (voiceBtn) { voiceBtn.style.display = 'none'; }
    if (voiceMod && voiceMod.speak && speakBtn) {
      speakBtn.addEventListener('click', () => {
        const last = messagesEl.querySelector('.pa-msg:last-of-type');
        const text = last ? last.textContent.trim() : '';
        const { spoken } = voiceMod.speak(text, { win: window });
        statusEl.textContent = spoken ? 'Speaking reply…' : 'Voice output unavailable in this browser.';
      });
    } else if (speakBtn) { speakBtn.style.display = 'none'; }
    const newThreadBtn = container.querySelector('[data-newthread]');
    if (newThreadBtn) newThreadBtn.addEventListener('click', async () => {
      const created = await client.createThread(a.assistant_id, 'New conversation');
      currentThread = created.thread_id; await bindChat(container, a); await render();
    });
  };

  const loadMessages = async (client, a, threadId, el) => {
    try {
      const { messages = [] } = await client.getThread(a.assistant_id, threadId);
      el.innerHTML = messages.length ? messages.map(m =>
        `<div class="pa-msg ${m.role === 'user' ? 'pa-msg--user' : ''}">${esc(m.content)}<div class="pa-muted">${esc(m.role)} · ${esc(m.provenance || 'direct')}</div></div>`).join('')
        : '<div class="pa-muted">No messages yet. Say hello.</div>';
      el.scrollTop = el.scrollHeight;
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  void render();
  return () => { disposed = true; };
}
