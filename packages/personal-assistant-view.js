// Herdr Personal Assistant view (Rakazo integration, P2 settings + chat MVP).
// Built on the existing vanilla JS / Web Components stack and design tokens.
import { escapeHtml as esc } from './herdr-web-components/util.js';

function statusClass(status) {
  return { ready: 'pa-status--ready', running: 'pa-status--running', disabled: 'pa-status--disabled',
           needs_attention: 'pa-status--needs' }[status] || 'pa-status--disabled';
}

function statusLabel(status) {
  return { ready: 'Ready', running: 'Running', disabled: 'Disabled', needs_attention: 'Needs attention' }[status] || status;
}

export function mountPersonalAssistant(container, client) {
  let disposed = false;
  let state = { assistants: [], selectedId: null, tab: 'General' };

  const render = async () => {
    container.innerHTML = '<herdr-spinner label="Loading Personal Assistant"></herdr-spinner>';
    try {
      const { assistants = [] } = await client.listAssistants();
      if (disposed) return;
      state.assistants = assistants;
      if (!state.selectedId && assistants.length) state.selectedId = assistants[0].assistant_id;
      draw();
    } catch (error) {
      if (!disposed) container.innerHTML = `<herdr-error message="${esc(error.message || 'Personal Assistant could not load')}"></herdr-error>`;
    }
  };

  const draw = () => {
    const selected = state.assistants.find(a => a.assistant_id === state.selectedId);
    container.innerHTML = `
      <div class="hd-card" style="margin-bottom:16px">
        <div class="hd-card__title">Personal Assistant</div>
        <div class="hd-card__sub">Persistent, user-scoped assistant with policy-controlled tools, approvals and memory.</div>
        <div class="pa-row" style="margin-top:10px">
          <button class="hd-btn" data-new>+ New assistant</button>
        </div>
      </div>
      <div class="pa-shell">
        <div class="pa-shell__side">
          ${state.assistants.length ? state.assistants.map(a => `
            <div class="pa-item ${a.assistant_id === state.selectedId ? 'pa-item--active' : ''}" data-pick="${esc(a.assistant_id)}">
              <div class="pa-item__name">${esc(a.name)}</div>
              <div class="pa-item__meta">${esc(a.status)} · model ${esc(a.model_policy)}</div>
            </div>`).join('') : '<div class="pa-empty">No assistants yet. Create one to begin.</div>'}
        </div>
        <div class="pa-main">
          <div class="pa-col">
            ${selected ? `
              <div class="hd-card">
                <div class="pa-row">
                  <div class="pa-status ${statusClass(selected.status)}"><span class="hd-status__dot"></span>${esc(statusLabel(selected.status))}</div>
                  <span class="pa-muted">${esc(selected.assistant_id)}</span>
                  <span class="pa-badge">${selected.enabled ? 'enabled' : 'disabled'}</span>
                </div>
                <div class="pa-tabs">
                  ${['General', 'Permissions', 'Memory', 'Approvals', 'Diagnostics'].map(t =>
                    `<button class="pa-tab ${state.tab === t ? 'pa-tab--active' : ''}" data-tab="${t}">${t}</button>`).join('')}
                </div>
                <div id="pa-panel">${renderPanel(selected)}</div>
              </div>` : '<div class="hd-card"><div class="pa-empty">Select or create an assistant.</div></div>'}
          </div>
          <div class="pa-col">
            ${selected ? renderChat(selected) : ''}
          </div>
        </div>
      </div>`;

    container.querySelectorAll('[data-pick]').forEach(el => el.addEventListener('click', () => {
      state.selectedId = el.dataset.pick; draw();
    }));
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

  const renderPanel = (a) => {
    if (state.tab === 'General') return renderGeneral(a);
    if (state.tab === 'Permissions') return '<div id="pa-perms"><herdr-spinner label="Loading permissions"></herdr-spinner></div>';
    if (state.tab === 'Memory') return '<div id="pa-memory"><herdr-spinner label="Loading memory"></herdr-spinner></div>';
    if (state.tab === 'Approvals') return '<div id="pa-approvals"><herdr-spinner label="Loading approvals"></herdr-spinner></div>';
    return '<div id="pa-diagnostics"><herdr-spinner label="Loading diagnostics"></herdr-spinner></div>';
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
    const approvals = panel.querySelector('#pa-approvals');
    if (approvals) loadApprovals(approvals, a);
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

  const loadApprovals = async (el, a) => {
    try {
      const { approvals = [] } = await client.listApprovals();
      const mine = approvals.filter(ap => ap.assistant_id === a.assistant_id);
      el.innerHTML = mine.length ? mine.map(ap => `
        <div class="pa-approval pa-msg">
          <div><strong>${esc(ap.policy_snapshot)}</strong> · ${esc(ap.decision)}</div>
          <div class="pa-muted">${esc(ap.approval_id)}</div>
          ${ap.decision === 'pending' ? `<div class="pa-row" style="margin-top:6px">
            <button class="hd-btn hd-btn--sm" data-approve="${esc(ap.approval_id)}">Approve</button>
            <button class="hd-btn hd-btn--danger hd-btn--sm" data-deny="${esc(ap.approval_id)}">Deny</button></div>` : ''}
        </div>`).join('') : '<div class="pa-muted">No approvals.</div>';
      el.querySelectorAll('[data-approve],[data-deny]').forEach(btn => btn.addEventListener('click', async () => {
        await client.decideApproval(btn.dataset.approve || btn.dataset.deny, btn.dataset.approve ? 'approved' : 'denied');
        await loadApprovals(el, a);
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
      <div id="pa-threads"><herdr-spinner label="Loading threads"></herdr-spinner></div>
      <div id="pa-messages" class="pa-chat" style="min-height:180px;margin-top:10px"></div>
      <div class="pa-row" style="margin-top:10px">
        <input style="flex:1" placeholder="Message…" data-msg>
        <button class="hd-btn" data-send>Send</button>
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
