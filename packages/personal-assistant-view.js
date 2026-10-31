// Herdr Personal Assistant view (Rakazo integration, P2 settings + chat MVP).
// Built on the existing vanilla JS / Web Components stack and design tokens.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';
import { requestErrorDetail } from './dashboard-load-state.js';
import { mountKeys, roveIndex, TABS } from './personal-assistant-keys.js';

function statusClass(status) {
  return { ready: 'pa-status--ready', running: 'pa-status--running', disabled: 'pa-status--disabled',
           needs_attention: 'pa-status--needs' }[status] || 'pa-status--disabled';
}

function statusLabel(status) {
  return { ready: 'Ready', running: 'Running', disabled: 'Disabled', needs_attention: 'Needs attention' }[status] || status;
}

const SIGN_IN_ERRORS = {
  bad_state: 'That sign-in link was not valid. Choose Enable to start again.',
  expired: 'That sign-in took too long. Choose Enable to start again.',
  denied: 'Google sign-in was cancelled, so nothing was connected.',
  no_client: 'The Google client is not saved yet. Save it below, then choose Enable.',
  exchange_failed: 'Google did not accept the sign-in. Choose Enable to try again.',
  unexpected_scope: 'Google did not grant the permissions that were asked for. Choose Enable and approve them all.',
  no_refresh_token: 'Google did not give long-lived access. Remove the app in your Google account settings, then choose Enable again.',
  not_set_up: 'App connections are not set up. Ask the operator to create the encryption key.',
};

/** Only Google's own sign-in host may be navigated to; anything else the server (or a proxy) returns is refused. */
function isGoogleSignInUrl(url) {
  try { const u = new URL(url); return u.protocol === 'https:' && u.hostname === 'accounts.google.com'; } catch { return false; }
}

/** Reads ?connected= / ?error= from the hash (or the search) once, removes it from the address, returns a fixed-text flash. */
function takeSignInReturn(win = typeof window === 'undefined' ? null : window) {
  if (!win) return null;
  try {
    const loc = win.location, hashAt = loc.hash.indexOf('?');
    const fromHash = hashAt >= 0;
    const params = new URLSearchParams(fromHash ? loc.hash.slice(hashAt + 1) : loc.search);
    const connected = params.get('connected'), error = params.get('error');
    if (connected === null && error === null) return null;
    params.delete('connected'); params.delete('error');
    const rest = params.toString();
    const next = fromHash ? `${loc.pathname}${loc.search}${loc.hash.slice(0, hashAt)}${rest ? `?${rest}` : ''}`
      : `${loc.pathname}${rest ? `?${rest}` : ''}${loc.hash}`;
    win.history.replaceState(win.history.state, '', next);
    if (connected === 'google') return { text: 'Google connected.' };
    return { error: true, text: (error !== null && Object.hasOwn(SIGN_IN_ERRORS, error) ? SIGN_IN_ERRORS[error] : '') || 'Google sign-in did not finish. Choose Enable to try again.' };
  } catch { return null; }
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
  // Coming back from Google's sign-in (/#/personal-assistant?connected=google or ?error=<reason>): open the Connectors tab, say what
  // happened in fixed words (never text from the URL), and drop the query so a reload does not repeat the message.
  let appsFlash = null;
  const ret = takeSignInReturn();
  if (ret) { configOpen = true; state.tab = 'Connectors'; appsFlash = ret; }
  const offered = () => {
    const active = state.assistants.filter(isActive).sort((x, y) => stamp(y) - stamp(x));
    const list = showAll ? active : active.slice(0, 3);
    const sel = state.assistants.find(a => a.assistant_id === state.selectedId);
    return { list: sel && !list.includes(sel) ? [sel, ...list] : list, active: active.length, hidden: state.assistants.length - active.length };
  };

  // draw() rebuilds the whole view, which would drop keyboard focus; remember what had it and give it back.
  const focusKey = () => {
    const el = container.ownerDocument.activeElement;
    if (!el || !container.contains(el)) return null;
    for (const attr of ['data-tab', 'data-select', 'data-config', 'data-keys', 'data-showall', 'data-msg', 'data-new', 'data-newthread']) {
      if (el.hasAttribute(attr)) return { attr, value: el.getAttribute(attr), draft: el.value };
    }
    return null;
  };
  const restoreFocus = (k) => {
    if (!k) return;
    const el = [...container.querySelectorAll(`[${k.attr}]`)].find(x => x.getAttribute(k.attr) === k.value);
    if (!el) return;
    if (k.attr === 'data-msg' && k.draft) el.value = k.draft;
    el.focus();
  };

  const draw = () => {
    const keep = focusKey();
    const selected = state.assistants.find(a => a.assistant_id === state.selectedId);
    const { list, active, hidden } = offered();
    const more = Math.max(0, active - list.filter(isActive).length);
    container.innerHTML = `
      <div class="pa-bar">
        <label class="pa-pick">Assistant
          <select data-select aria-label="Assistant">${list.map(a => `<option value="${esc(a.assistant_id)}" ${a.assistant_id === state.selectedId ? 'selected' : ''}>${esc(a.name)} · ${esc(statusLabel(a.status))}</option>`).join('')}</select></label>
        ${more > 0 || showAll && active > 3 ? `<button class="hd-linkbtn" data-showall>${showAll ? 'Show recent only' : `Show all ${active}`}</button>` : ''}
        <span class="pa-grow"></span>
        <button class="hd-btn hd-btn--ghost" data-keys aria-haspopup="dialog" title="Keyboard shortcuts (?)">Keys <kbd>?</kbd></button>
        <button class="hd-btn hd-btn--ghost" data-config aria-expanded="${configOpen}" title="Show or hide Configure (,)">Configure</button>
        <button class="hd-btn" data-new>+ New</button>
      </div>
      ${hidden ? `<p class="pa-muted pa-hidden-note">${hidden} inactive assistant${hidden === 1 ? '' : 's'} not listed.</p>` : ''}
      ${selected ? `<div id="pa-strip" class="pa-strip" role="status" ${pending.some(ap => ap.assistant_id === selected.assistant_id) ? '' : 'hidden'}></div>` : ''}
      ${selected ? renderChat(selected) : `<div class="hd-card"><div class="pa-empty">${state.assistants.length ? 'No active assistant. Use Show all, or create one.' : 'No assistants yet. Create one to begin.'}</div></div>`}
      ${selected && configOpen ? `<section class="hd-card pa-config" aria-label="Configure ${esc(selected.name)}">
        <div class="pa-row"><div class="pa-status ${statusClass(selected.status)}"><span class="hd-status__dot"></span>${esc(statusLabel(selected.status))}</div>
          <span class="pa-muted">${esc(selected.assistant_id)}</span><span class="pa-badge">${selected.enabled ? 'enabled' : 'disabled'}</span></div>
        <div class="pa-tabs" role="tablist" aria-label="Configure ${esc(selected.name)}">${TABS.map(t =>
          `<button class="pa-tab ${state.tab === t ? 'pa-tab--active' : ''}" role="tab" id="pa-tab-${t}" aria-selected="${state.tab === t}" aria-controls="pa-panel" tabindex="${state.tab === t ? 0 : -1}" data-tab="${t}">${t}</button>`).join('')}</div>
        <div id="pa-panel" role="tabpanel" aria-labelledby="pa-tab-${state.tab}">${renderPanel(selected)}</div></section>` : ''}`;
    container.querySelector('[data-select]')?.addEventListener('change', e => { state.selectedId = e.target.value; draw(); });
    container.querySelector('[data-showall]')?.addEventListener('click', () => { showAll = !showAll; draw(); });
    container.querySelector('[data-config]')?.addEventListener('click', () => { configOpen = !configOpen; draw(); });
    if (selected) drawApprovalStrip(selected);
    container.querySelectorAll('[data-tab]').forEach(el => {
      el.addEventListener('click', () => { state.tab = el.dataset.tab; draw(); });
      el.addEventListener('keydown', e => {                      // roving focus: arrows move and open, Home/End jump
        const tabs = [...container.querySelectorAll('[data-tab]')];
        if (e.key === 'ArrowUp' || e.key === 'ArrowDown') return;      // a horizontal tablist keeps Up/Down for scrolling
        const next = roveIndex(e.key, tabs.indexOf(el), tabs.length);
        if (next === null || e.ctrlKey || e.metaKey || e.altKey) return;
        e.preventDefault(); state.tab = tabs[next].dataset.tab; draw();
        container.querySelector(`[data-tab="${state.tab}"]`)?.focus();
      });
    });
    container.querySelector('[data-keys]')?.addEventListener('click', () => keys.openHelp());
    container.querySelectorAll('[data-new]').forEach(el => el.addEventListener('click', async () => {
      const name = prompt('Assistant name', 'Personal Assistant');
      if (!name) return;
      await client.createAssistant({ name });
      await render();
    }));
    const panel = container.querySelector('#pa-panel');
    if (panel) bindPanel(panel, selected);
    bindChat(container, selected);
    restoreFocus(keep);
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
      const had = strip.contains(container.ownerDocument.activeElement) ? container.ownerDocument.activeElement.dataset : null;
      strip.hidden = !mine.length;
      strip.innerHTML = mine.map(ap => `<div class="pa-row"><span><strong>Approval needed</strong> · ${esc(ap.policy_snapshot)}</span><span class="pa-grow"></span>
        <button class="hd-btn hd-btn--sm" data-strip-approve="${esc(ap.approval_id)}">Approve</button>
        <button class="hd-btn hd-btn--danger hd-btn--sm" data-strip-deny="${esc(ap.approval_id)}">Deny</button></div>`).join('');
      strip.querySelectorAll('[data-strip-approve],[data-strip-deny]').forEach(b => b.addEventListener('click', async () => {
        const id = b.dataset.stripApprove || b.dataset.stripDeny;
        try { await client.decideApproval(id, b.dataset.stripApprove ? 'approved' : 'denied'); } catch { /* the strip refreshes below */ }
        drawApprovalStrip(a);
      }));
      if (had) strip.querySelector(had.stripApprove ? `[data-strip-approve="${had.stripApprove}"]` : `[data-strip-deny="${had.stripDeny}"]`)?.focus();
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
    if (connectors) loadConnectorsTab(connectors, a);
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

  // The Connectors tab: the Apps section first (one-click sign-in apps), then the drafts and reminders those apps produce, then the
  // generic HTTP/OpenAPI/MCP connector list. Each section fails on its own.
  const loadConnectorsTab = async (el, a) => {
    el.innerHTML = `<section id="pa-apps" aria-labelledby="pa-apps-h"></section>
      <section id="pa-drafts" aria-labelledby="pa-drafts-h" hidden></section>
      <section id="pa-reminders" aria-labelledby="pa-reminders-h"></section>
      <section id="pa-generic" aria-labelledby="pa-generic-h"><h3 id="pa-generic-h" class="pa-h3">Other connectors</h3><div id="pa-generic-body"></div></section>`;
    const generic = el.querySelector('#pa-generic-body');
    void loadConnectors(generic, a);
    void loadReminders(el.querySelector('#pa-reminders'));
    await loadApps(el);
  };

  const providerCard = (p, ready) => {
    const name = esc(p.name);
    if (p.kind === 'status') {
      const on = p.connected;
      return `<article class="pa-app" aria-label="${name}" data-app="${esc(p.id)}">
        <div class="pa-app__head"><strong>${name}</strong> <span class="pa-badge ${on ? 'pa-badge--granted' : ''}">status only</span></div>
        <p class="pa-muted pa-app__what">Pull requests and repositories, through the Herdr GitHub App.</p>
        <div class="pa-app__state" data-app-state>${on ? `Connected${p.account ? ` · ${esc(p.account)}` : ''}` : 'Not connected'}</div>
        <p class="pa-muted">Managed by the operator; there is nothing to enable here.</p></article>`;
    }
    const what = p.id === 'gmail' ? 'Search and read your mail. Drafts only; nothing is sent.' : 'See your upcoming events and find free time.';
    const needs = p.connected && p.status === 'needs_attention';
    let stateText, buttons = '';
    if (!ready || !p.client_configured) {
      stateText = 'Not set up';
      buttons = ready ? `<button class="hd-btn hd-btn--sm" disabled aria-label="Enable ${name}" title="Save the Google client below first">Enable</button>` : '';
    } else if (needs) {
      stateText = `Needs attention${p.account ? ` · ${esc(p.account)}` : ''}`;
      buttons = `<button class="hd-btn hd-btn--sm" data-app-enable="${esc(p.provider)}" aria-label="Sign in again for ${name}">Sign in again</button>
        <button class="hd-btn hd-btn--ghost hd-btn--sm" data-app-disconnect="${esc(p.provider)}" aria-label="Disconnect ${name}">Disconnect</button>`;
    } else if (p.connected) {
      stateText = `Connected as ${esc(p.account || 'your Google account')}`;
      buttons = `<button class="hd-btn hd-btn--ghost hd-btn--sm" data-app-disconnect="${esc(p.provider)}" aria-label="Disconnect ${name}">Disconnect</button>`;
    } else {
      stateText = 'Not connected';
      buttons = `<button class="hd-btn hd-btn--sm" data-app-enable="${esc(p.provider)}" aria-label="Enable ${name}">Enable</button>`;
    }
    const cls = needs ? 'pa-status--needs' : p.connected ? 'pa-status--ready' : 'pa-status--disabled';
    return `<article class="pa-app ${p.connected ? 'pa-app--on' : ''}" aria-label="${name}" data-app="${esc(p.id)}">
      <div class="pa-app__head"><strong>${name}</strong> <span class="pa-badge">read-only</span></div>
      <p class="pa-muted pa-app__what">${what}</p>
      <div class="pa-app__state"><span class="pa-status ${cls}" data-app-state>${stateText}</span></div>
      <div class="pa-row pa-app__actions">${buttons}</div></article>`;
  };

  const loadApps = async (root) => {
    const el = root.querySelector('#pa-apps');
    const heading = '<h3 id="pa-apps-h" class="pa-h3">Apps your assistant can use</h3>';
    try {
      const data = await client.listApps();
      if (disposed) return;
      const ready = data.ready === true;
      const providers = Array.isArray(data.providers) ? data.providers : [];
      const google = providers.find(p => p.provider === 'google');
      const googleOn = !!(google && google.connected);
      const flash = appsFlash;
      const redirect = typeof data.redirect_uri === 'string' ? data.redirect_uri : '';
      const saved = !!(google && google.client_configured);
      const setup = !ready
        ? '<p class="pa-muted" data-app-notready>Not set up — ask the operator to create the encryption key.</p>'
        : `<div class="pa-setup" data-app-setup>
            <h4 class="pa-h4">Google sign-in setup</h4>
            <p class="pa-muted">Google needs an OAuth client, made once in the Google Cloud console. Add this redirect address to it:</p>
            <div class="pa-row"><code class="pa-uri" data-redirect-uri>${esc(redirect)}</code>
              <button class="hd-btn hd-btn--ghost hd-btn--sm" data-copy-uri aria-label="Copy redirect address">Copy</button>
              <span class="pa-muted" data-copy-status role="status" aria-live="polite"></span></div>
            ${saved ? '<p class="pa-state-ok" data-client-saved>Client saved</p>' : ''}
            <details class="pa-details" ${saved ? '' : 'open'}><summary>${saved ? 'Replace the Google client' : 'Save the Google client'}</summary>
              <form data-client-form autocomplete="off">
                <div class="pa-field"><label for="pa-gcid">Client ID</label><input id="pa-gcid" data-client-id name="client_id" autocomplete="off" spellcheck="false" required></div>
                <div class="pa-field"><label for="pa-gsec">Client secret</label><input id="pa-gsec" data-client-secret name="client_secret" type="password" autocomplete="new-password" spellcheck="false" required></div>
                <div class="pa-row"><button class="hd-btn hd-btn--sm" type="submit">Save client</button></div>
              </form></details></div>`;
      el.innerHTML = `${heading}
        <div class="pa-flash-slot" data-apps-flash>${flash ? flashHtml(flash) : flashHtml(null)}</div>
        <div class="pa-apps">${providers.map(p => providerCard(p, ready)).join('')}
          <article class="pa-app pa-app--soon" aria-label="More apps"><div class="pa-app__head"><strong>Slack, Notion …</strong></div>
            <p class="pa-muted pa-app__what">Same card, same Enable button, once each is built.</p><div class="pa-app__state">Coming</div></article></div>
        ${setup}
        <div id="pa-blocked" data-blocked-section ${googleOn && google.status !== 'needs_attention' ? '' : 'hidden'}></div>`;
      appsFlash = null;
      const say = (f) => { el.querySelector('[data-apps-flash]').innerHTML = flashHtml(f); };
      el.querySelectorAll('[data-app-enable]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const { url } = await client.connectApp(b.dataset.appEnable);
          if (isGoogleSignInUrl(url)) { window.location.assign(url); return; }
          say({ error: true, text: 'The sign-in address was not valid, so nothing was opened.' });
        } catch (e) { say({ error: true, text: connectError(e) }); }
        b.disabled = false;
      }));
      el.querySelectorAll('[data-app-disconnect]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await client.disconnectApp(b.dataset.appDisconnect); appsFlash = { text: 'Disconnected. Gmail and Google Calendar share one Google sign-in.' }; await loadApps(root); }
        catch (e) { say({ error: true, text: errText(e) }); b.disabled = false; }
      }));
      el.querySelector('[data-copy-uri]')?.addEventListener('click', async () => {
        const out = el.querySelector('[data-copy-status]');
        try { await navigator.clipboard.writeText(redirect); out.textContent = 'Copied.'; }
        catch { out.textContent = 'Copy failed; select the address and copy it.'; }
      });
      el.querySelector('[data-client-form]')?.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const form = ev.currentTarget, secret = form.querySelector('[data-client-secret]');
        const btn = form.querySelector('button[type="submit"]');
        btn.disabled = true;
        try {
          await client.saveAppClient('google', form.querySelector('[data-client-id]').value, secret.value);
          secret.value = '';
          appsFlash = { text: 'Client saved.' };
          await loadApps(root);
        } catch (e) { secret.value = ''; say({ error: true, text: e && e.body && e.body.error === 'bad_client' ? 'Both the client ID and the secret are needed.' : errText(e) }); btn.disabled = false; }
      });
      void loadBlocked(root.querySelector('#pa-blocked'), googleOn && google.status !== 'needs_attention', Array.isArray(data.blocked_calendars) ? data.blocked_calendars : []);
      void loadDrafts(root, googleOn && google.status !== 'needs_attention');
    } catch (error) { el.innerHTML = `${heading}<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const connectError = (e) => {
    const code = e && e.body && e.body.error;
    if (code === 'no_client') return 'Save the Google client first.';
    if (code === 'not_set_up') return 'App connections are not set up. Ask the operator to create the encryption key.';
    return errText(e);
  };

  // Which calendars the assistant may never see. This list (blocked ones included) is shown to the owner only.
  const loadBlocked = async (el, on, ids = []) => {
    if (!el) return;
    if (!on) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    const head = '<h4 class="pa-h4" id="pa-blocked-h">Calendars the assistant must not see</h4>';
    el.innerHTML = `${head}<p class="pa-muted">Checked calendars are hidden from your assistant: it cannot list, read, or schedule on them.</p><div class="pa-muted">Loading calendars…</div>`;
    try {
      const { calendars = [] } = await client.listCalendars('google');
      if (disposed) return;
      const listed = new Set(calendars.map(c => c.id));
      const rows = [...calendars.map(c => ({ id: c.id, label: c.summary || c.id, primary: c.primary, blocked: c.blocked })),
        ...ids.filter(i => !listed.has(i)).map(i => ({ id: i, label: i, primary: false, blocked: true, unlisted: true }))];
      el.innerHTML = `${head}<p class="pa-muted">Checked calendars are hidden from your assistant: it cannot list, read, or schedule on them.</p>
        <form data-blocked-form><fieldset class="pa-fieldset"><legend class="pa-muted">Choose the calendars to hide</legend>
        ${rows.length ? rows.map((r, i) => `<label class="pa-check" for="pa-bc-${i}"><input type="checkbox" id="pa-bc-${i}" data-blocked-id="${esc(r.id)}" ${r.blocked ? 'checked' : ''}>
          <span>${esc(r.label)}${r.primary ? ' <span class="pa-badge">primary</span>' : ''}${r.unlisted ? ' <span class="pa-muted">(not in your calendar list)</span>' : ''}
          ${r.label !== r.id ? `<span class="pa-muted pa-check__id">${esc(r.id)}</span>` : ''}</span></label>`).join('') : '<div class="pa-muted">No calendars found.</div>'}
        </fieldset><div class="pa-row"><button class="hd-btn hd-btn--sm" type="submit">Save blocked calendars</button>
          <span class="pa-muted" data-blocked-status role="status" aria-live="polite"></span></div></form>`;
      el.querySelector('[data-blocked-form]').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const out = el.querySelector('[data-blocked-status]'), btn = ev.currentTarget.querySelector('button[type="submit"]');
        const chosen = [...el.querySelectorAll('[data-blocked-id]')].filter(x => x.checked).map(x => x.dataset.blockedId);
        btn.disabled = true;
        try { await client.setBlockedCalendars('google', chosen); out.textContent = chosen.length ? `Saved: ${chosen.length} calendar${chosen.length === 1 ? '' : 's'} hidden.` : 'Saved: no calendars hidden.'; }
        catch (e) { out.textContent = `Not saved: ${errText(e)}`; }
        btn.disabled = false;
      });
    } catch (e) { el.innerHTML = `${head}<div class="pa-muted" role="status">Calendars could not be loaded${e && e.body && e.body.error === 'needs_attention' ? '; sign in again first' : ''}.</div>`; }
  };

  let draftsGen = 0;       // a slower, older drafts request must not redraw after a newer one (or a Disconnect) has run
  const loadDrafts = async (root, on) => {
    const gen = ++draftsGen;
    const el = root.querySelector('#pa-drafts');
    if (!el) return;
    if (!on) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    const head = '<h3 id="pa-drafts-h" class="pa-h3">Gmail drafts</h3>';
    try {
      const { drafts = [] } = await client.listDrafts('google');
      if (disposed || gen !== draftsGen) return;
      const link = (u) => typeof u === 'string' && /^https:\/\/mail\.google\.com\//.test(u) ? u : '';
      el.innerHTML = `${head}<p class="pa-muted">Drafts in your Gmail account. Nothing here has been sent.</p><div role="status" aria-live="polite" data-drafts-status class="pa-flash"></div>
        ${drafts.length ? drafts.map(d => `<article class="pa-msg" aria-label="Draft ${esc(d.subject || 'no subject')}" data-draft="${esc(d.id)}">
          <div><strong>${esc(d.subject || '(no subject)')}</strong></div>
          <div class="pa-muted">To ${esc(d.to || '(nobody yet)')}</div>
          ${d.snippet ? `<div class="pa-snippet">${esc(d.snippet)}</div>` : ''}
          <div class="pa-row pa-row--tight">
            ${link(d.gmail_url) ? `<a class="hd-btn hd-btn--ghost hd-btn--sm" href="${esc(link(d.gmail_url))}" target="_blank" rel="noopener" aria-label="Open draft ${esc(d.subject || 'no subject')} in Gmail">Open in Gmail</a>` : ''}
            <button class="hd-btn hd-btn--danger hd-btn--sm" data-draft-discard="${esc(d.id)}" aria-label="Discard draft ${esc(d.subject || 'no subject')}">Discard</button>
            <button class="hd-btn hd-btn--sm" disabled aria-disabled="true" data-draft-send title="Sending from here is not available yet">Send (coming soon)</button>
          </div></article>`).join('') : '<div class="pa-muted">No drafts.</div>'}`;
      el.querySelectorAll('[data-draft-discard]').forEach(b => b.addEventListener('click', async () => {
        if (!window.confirm('Discard this Gmail draft? It is deleted from your Gmail account.')) return;
        b.disabled = true;
        try { await client.discardDraft('google', b.dataset.draftDiscard); await loadDrafts(root, true); root.querySelector('[data-drafts-status]').textContent = 'Draft discarded.'; }
        catch (e) { el.querySelector('[data-drafts-status]').textContent = `Not discarded: ${errText(e)}`; b.disabled = false; }
      }));
    } catch (e) { if (gen !== draftsGen) return; el.innerHTML = `${head}<div class="pa-muted" role="status">Drafts could not be loaded${e && e.body && e.body.error === 'needs_attention' ? '; sign in again first' : ''}.</div>`; }
  };

  const loadReminders = async (el, flash = null) => {
    const head = '<h3 id="pa-reminders-h" class="pa-h3">Reminders</h3>';
    try {
      const { reminders = [], timezone = '' } = await client.listReminders();
      if (disposed) return;
      const shown = reminders.filter(r => r.state !== 'cancelled');
      const at = (r) => String(r.fire_at || '').replace('T', ' ').slice(0, 16);
      el.innerHTML = `${head}${flashHtml(flash)}
        ${shown.length ? shown.map(r => `<article class="pa-msg pa-msg--meta" aria-label="Reminder ${esc(r.text)}" data-reminder="${esc(r.id)}">
          <div>${esc(r.text)}</div><div class="pa-muted">${esc(at(r))}${timezone ? ` (${esc(timezone)})` : ''} · ${esc(r.state)}</div>
          ${r.state === 'pending' ? `<div class="pa-row pa-row--tight"><button class="hd-btn hd-btn--danger hd-btn--sm" data-reminder-cancel="${esc(r.id)}" aria-label="Cancel reminder ${esc(r.text)}">Cancel</button></div>` : ''}
          </article>`).join('') : '<div class="pa-muted">No reminders.</div>'}`;
      el.querySelectorAll('[data-reminder-cancel]').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await client.cancelReminder(b.dataset.reminderCancel); await loadReminders(el, { text: 'Reminder cancelled.' }); }
        catch (e) { await loadReminders(el, { error: true, text: errText(e) }); }
      }));
    } catch (e) { el.innerHTML = `${head}<div class="pa-muted" role="status">${e && e.status === 409 ? 'Reminders are not set up.' : 'Reminders could not be loaded.'}</div>`; }
  };

  const loadConnectors = async (el, a) => {
    try {
      const { connectors = [] } = await client.listConnectors();
      el.innerHTML = `
        <div class="pa-row">
          <input data-cname placeholder="Name" aria-label="Connector name" style="flex:1">
          <select data-ckind aria-label="Connector kind"><option value="http">http</option><option value="openapi">openapi</option><option value="mcp">mcp</option></select>
          <input data-curl placeholder="https://host" aria-label="Connector base URL" style="flex:1.4">
          <input data-callow placeholder="Allowed host(s), comma-sep" aria-label="Allowed hosts, comma separated" style="flex:1.4">
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
        <input style="flex:1" placeholder="Message…  (press / to focus)" aria-label="Message" data-msg>
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
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendBtn.click(); } });
    sendBtn.addEventListener('click', async () => {
      const content = input.value.trim();
      if (!content) return;
      if (!currentThread) { statusEl.textContent = 'Still loading the conversation; your message is kept.'; return; }
      input.value = ''; statusEl.textContent = 'Running…';
      try {
        const { run } = await client.sendAssistantMessage(a.assistant_id, currentThread, content);
        await loadMessages(client, a, currentThread, messagesEl);
        statusEl.textContent = `Run ${run.run_id} · ${run.state}`;
      } catch (error) { statusEl.textContent = 'Error: ' + error.message; }
    });
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
      threadsEl.querySelectorAll('[data-t]').forEach(b => b.addEventListener('click', async () => {
        currentThread = b.dataset.t;
        threadsEl.querySelectorAll('[data-t]').forEach(x => x.classList.toggle('pa-tab--active', x === b));
        await loadMessages(client, a, currentThread, messagesEl);
      }));
      await loadMessages(client, a, currentThread, messagesEl);
    } catch (error) { threadsEl.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
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
    el.dataset.loading = threadId;
    try {
      const { messages = [] } = await client.getThread(a.assistant_id, threadId);
      if (el.dataset.loading !== threadId) return;             // a newer thread was picked while this was in flight
      el.innerHTML = messages.length ? messages.map(m =>
        `<div class="pa-msg ${m.role === 'user' ? 'pa-msg--user' : ''}">${esc(m.content)}<div class="pa-muted">${esc(m.role)} · ${esc(m.provenance || 'direct')}</div></div>`).join('')
        : '<div class="pa-muted">No messages yet. Say hello.</div>';
      el.scrollTop = el.scrollHeight;
    } catch (error) { el.innerHTML = `<herdr-error message="${esc(error.message)}"></herdr-error>`; }
  };

  const keys = mountKeys(container, {
    assistants: () => offered().list.map(a => ({ id: a.assistant_id, name: a.name, selected: a.assistant_id === state.selectedId })),
    selectId: (id) => { state.selectedId = id; draw(); },      // focus stays put, so [ and ] can be pressed again
    openTab: (t) => { configOpen = true; state.tab = t; draw(); container.querySelector(`[data-tab="${t}"]`)?.focus(); },
    toggleConfig: () => { configOpen = !configOpen; draw(); container.querySelector('[data-config]')?.focus(); },
    newThread: () => container.querySelector('[data-newthread]')?.click(),
    focusComposer: () => container.querySelector('[data-msg]')?.focus(),
  });
  void render();
  return () => { disposed = true; keys.dispose(); };
}
