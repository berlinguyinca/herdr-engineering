// Canonical journal browser shared by the dashboard and Personal Assistant.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { newOperationId } from './integrations-view.js';

export function memoryContent(content) {
  const text = String(content ?? '');
  try {
    const value = JSON.parse(text);
    if (value && Object.keys(value).sort().join(',') === 'labels,provenance,summary'
      && Array.isArray(value.labels) && [...value.labels].sort().join(',') === 'derived,extractive,unreviewed'
      && typeof value.summary === 'string' && value.provenance && typeof value.provenance === 'object' && !Array.isArray(value.provenance)) return value.summary;
  } catch { /* Other content is plain text, including unknown JSON envelopes. */ }
  return text;
}

const provenance = item => item.provenance ?? item.metadata ?? {};
const content = item => item.content ?? item.text ?? '';
const revision = item => item.revision ?? item.metadata?.revision;
const id = item => item.document_id ?? item.id;
const pretty = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const pre = text => `<pre style="white-space:pre-wrap;overflow-wrap:anywhere;font:inherit">${esc(text)}</pre>`;
const button = (action, label, disabled = false) => `<button type="button" class="hd-btn hd-btn--ghost" data-action="${action}" ${disabled ? 'disabled' : ''}>${label}</button>`;

export function mountSharedMemory(container, client, { onFreshness = () => {}, onUnavailable } = {}) {
  let disposed = false, generation = 0, controller, loading = false, registryReady = false;
  let domains = [], alias = '', kind = 'fact', offset = 0, total = 0, rows = [], selected = null, history = [], historyError = '';
  let query = '', appliedQuery = '', draft = '', newId = '', correction = '', error = '';
  const receipts = new Map();
  const domain = () => domains.find(d => d.alias === alias);
  const bank = () => domain()?.bank_ids?.[kind];
  const start = () => {
    controller?.abort(); controller = new AbortController();
    const version = ++generation;
    return { signal: controller.signal, current: () => !disposed && version === generation };
  };
  const fail = (err, request) => {
    if (!request.current()) return;
    loading = false;
    error = err.status === 409 ? 'Revision conflict. Reopen the note to read its current revision before correcting it.' : String(err.message || err);
    onFreshness(false, error); draw();
  };
  const receiptText = entry => {
    if (entry.receipt.status === 'pending') return entry.deleted ? 'Delete pending' : 'Saved · index pending';
    if (entry.receipt.status === 'completed') return entry.deleted ? 'Forgotten · deletion completed' : 'Saved · indexing completed';
    return `Operation ${entry.receipt.status || 'status unavailable'}`;
  };
  function draw() {
    if (disposed) return;
    const d = domain(), counts = d?.status ?? d?.counts;
    const selector = type => `<label>${type === 'personal' ? 'Personal' : 'Project'} domain <select data-domain="${type}" ${loading && !registryReady ? 'disabled' : ''}><option value="">Select ${type} domain…</option>${domains.filter(item => item.domain_type === type).map(item => `<option value="${esc(item.alias)}" ${item.alias === alias ? 'selected' : ''}>${esc(item.alias)} · ${esc(item.domain_id)}</option>`).join('')}</select></label>`;
    const operations = [...receipts.values()].filter(entry => entry.bank === bank());
    container.innerHTML = `<section class="hd-card" aria-label="Shared memory" aria-busy="${loading}">
      <div class="hd-card__title">Memory</div><p class="hd-card__sub">Choose one domain. Saved notes and session summaries stay in separate banks.</p>
      <div class="hd-inline" style="display:flex;flex-wrap:wrap;gap:12px">${selector('project')}${selector('personal')}${button('refresh', 'Refresh', loading)}</div>
      ${error ? `<p role="alert">${esc(error)}</p>` : ''}
      ${!registryReady ? '<p role="status">Loading domain registry…</p>' : !domains.length ? '<p>No memory domains are available to this operator.</p>' : !d ? '<p>Select a project or personal domain to view its memory.</p>' : `
        <p>${esc(d.domain_type)} · ${esc(d.domain_id)}${counts ? ` · ${esc(counts.active ?? '—')} active · ${esc(counts.deleted ?? '—')} deleted · ${esc(counts.pending ?? '—')} deliveries pending (registry snapshot)` : ''}</p>
        <div class="hd-inline" role="group" aria-label="Memory bank">${button('fact', 'Saved notes', kind === 'fact')}${button('summary', 'Session summaries', kind === 'summary')}</div>
        <form data-form="search" class="hd-inline" style="display:flex;flex-wrap:wrap;gap:8px;margin:12px 0"><label>Search <input name="query" data-field="query" value="${esc(query)}" maxlength="8192"></label><button class="hd-btn" ${loading ? 'disabled' : ''}>Search</button>${button('clear', 'Clear search', loading)}</form>
        ${appliedQuery ? '<p>Search uses the index; pending notes may not appear yet. Results are limited to 100.</p>' : ''}
        ${loading ? '<p role="status">Loading…</p>' : ''}
        <div style="overflow-x:auto"><table class="hd-table"><thead><tr><th>Document</th><th>Content</th><th>Revision</th><th>Provenance</th></tr></thead><tbody>${rows.map((item, index) => `<tr><td><button type="button" class="hd-btn hd-btn--ghost" data-open="${index}">${esc(id(item) ?? 'Unknown document')}</button></td><td>${pre(memoryContent(content(item)))}${item.truncated ? '<small>Excerpt · open for full content</small>' : ''}</td><td>${esc(revision(item) ?? '—')}</td><td>${pre(pretty(provenance(item)))}</td></tr>`).join('') || `<tr><td colspan="4">${loading ? 'Loading memory…' : 'No notes found.'}</td></tr>`}</tbody></table></div>
        <div class="hd-inline">${button('previous', 'Previous', loading || offset === 0)} <span>${rows.length ? offset + 1 : 0}–${offset + rows.length} of ${esc(total)}${appliedQuery ? ' search results' : ''}</span> ${button('next', 'Next', loading || offset + rows.length >= total || offset >= 100000)}</div>
        ${operations.length ? `<div role="status">${operations.map(entry => `<p>${esc(entry.documentId)} · revision ${esc(entry.receipt.revision)} · ${esc(receiptText(entry))}</p>`).join('')}<small>Refresh checks pending delivery status.</small></div>` : ''}
        ${selected ? `<section class="hd-card" aria-label="Full memory document"><h3>${esc(id(selected))}</h3><p>Revision ${esc(revision(selected) ?? '—')}</p>${pre(memoryContent(content(selected)))}<details><summary>Provenance</summary>${pre(pretty(provenance(selected)))}</details>
          <details open><summary>Revision history</summary>${historyError ? `<p>${esc(historyError)}</p>` : `<ul>${history.map(h => `<li>Revision ${esc(h.revision)} · ${esc(h.operation)}${h.content !== undefined ? pre(memoryContent(h.content)) : ''}</li>`).join('') || '<li>No revision history returned.</li>'}</ul>`}<small>Gateway history returns revision and operation only, at most 100 entries.</small></details>
          <form data-form="correct"><label>Correction <textarea name="content" data-field="correction" required style="width:100%;box-sizing:border-box">${esc(correction)}</textarea></label><button class="hd-btn" ${loading || !Number.isInteger(Number(revision(selected))) ? 'disabled' : ''}>Save correction</button> ${button('forget', 'Forget', loading)} ${button('close', 'Close', loading)}</form></section>` : ''}
        ${kind === 'fact' ? `<form data-form="save" class="hd-card"><h3>Explicit save</h3><label>Document ID (optional) <input name="documentId" data-field="newId" maxlength="512" value="${esc(newId)}"></label><label>Note <textarea name="content" data-field="draft" required style="width:100%;box-sizing:border-box">${esc(draft)}</textarea></label><button class="hd-btn" ${loading ? 'disabled' : ''}>Save note</button></form>` : ''}
      `}</section>`;
  }
  async function registry() {
    const request = start(); loading = true; error = ''; draw();
    try {
      const result = await client.listMemoryDomains({ signal: request.signal });
      if (!request.current()) return;
      domains = (result.domains ?? []).filter(item => ['project', 'personal'].includes(item.domain_type) && item.bank_ids?.fact && item.bank_ids?.summary);
      registryReady = true; loading = false; onFreshness(true); draw();
    } catch (err) {
      if (!request.current()) return;
      registryReady = true;
      if (err.status === 404 && onUnavailable) { loading = false; onUnavailable(); return; }
      fail(err, request);
    }
  }
  async function load({ checkOperations = false } = {}) {
    if (!bank()) return;
    const request = start(), currentBank = bank(); loading = true; error = ''; rows = []; selected = null; draw();
    try {
      if (checkOperations) {
        for (const entry of receipts.values()) {
          if (entry.bank !== currentBank || entry.receipt.status !== 'pending' || !entry.receipt.operation_id) continue;
          const receipt = await client.getMemoryOperation(currentBank, entry.receipt.operation_id, { signal: request.signal });
          if (!request.current()) return;
          entry.receipt = receipt;
        }
      }
      const result = appliedQuery
        ? await client.recallMemoryDocuments(currentBank, appliedQuery, { limit: 100, signal: request.signal })
        : await client.listMemoryDocuments(currentBank, { limit: 20, offset, signal: request.signal });
      if (!request.current()) return;
      const items = result.items ?? result.documents ?? result.results ?? [];
      total = appliedQuery ? items.length : result.total ?? items.length;
      rows = appliedQuery ? items.slice(offset, offset + 20) : items;
      loading = false; onFreshness(true); draw();
    } catch (err) { fail(err, request); }
  }
  async function open(index) {
    const documentId = id(rows[index]);
    if (!documentId || !bank()) return;
    const request = start(), currentBank = bank(); loading = true; error = ''; selected = null; draw();
    const results = await Promise.allSettled([
      client.getMemoryDocument(currentBank, documentId, { signal: request.signal }),
      client.getMemoryHistory(currentBank, documentId, { signal: request.signal }),
    ]);
    if (!request.current()) return;
    if (results[0].status === 'rejected') { fail(results[0].reason, request); return; }
    selected = results[0].value; correction = String(content(selected));
    history = results[1].status === 'fulfilled' ? results[1].value.history ?? [] : [];
    historyError = results[1].status === 'rejected' ? `History unavailable: ${results[1].reason.message}` : '';
    loading = false; draw();
  }
  async function mutate(deleted, item) {
    const currentBank = bank(), documentId = deleted ? id(selected) : item.document_id;
    const request = start(); loading = true; error = ''; draw();
    try {
      const receipt = deleted
        ? await client.deleteMemoryDocument(currentBank, documentId, { base_revision: Number(revision(selected)), signal: request.signal })
        : await client.saveMemoryDocument(currentBank, item, { signal: request.signal });
      if (!request.current()) return;
      const entry = { bank: currentBank, documentId, receipt, deleted };
      receipts.set(`${currentBank}\0${documentId}`, entry);
      // Counts from the registry are a snapshot; omit rather than display stale totals.
      const d = domain(); delete d.counts; delete d.status;
      if (!deleted && item.base_revision === 0) { draft = ''; newId = ''; }
      await load();
    } catch (err) { fail(err, request); }
  }
  function change(event) {
    if (!event.target.dataset.domain) return;
    alias = event.target.value; offset = 0; rows = []; selected = null; total = 0; query = ''; appliedQuery = ''; error = ''; draft = ''; newId = ''; correction = ''; kind = 'fact';
    if (bank()) void load();
    else { start(); loading = false; draw(); }
  }
  function input(event) {
    const field = event.target.dataset.field;
    if (field === 'query') query = event.target.value;
    if (field === 'draft') draft = event.target.value;
    if (field === 'newId') newId = event.target.value;
    if (field === 'correction') correction = event.target.value;
  }
  function click(event) {
    const target = event.target.closest?.('[data-action], [data-open]') ?? event.target;
    if (target.dataset.open !== undefined && !loading) { void open(Number(target.dataset.open)); return; }
    const action = target.dataset.action;
    if (!action || loading) return;
    if (action === 'refresh') { if (bank()) void load({ checkOperations: true }); else void registry(); return; }
    if (action === 'close') { selected = null; draw(); return; }
    if (action === 'forget' && selected) { void mutate(true); return; }
    if (action === 'fact' || action === 'summary') { kind = action; offset = 0; appliedQuery = ''; query = ''; }
    if (action === 'clear') { query = ''; appliedQuery = ''; offset = 0; }
    if (action === 'previous') offset = Math.max(0, offset - 20);
    if (action === 'next') offset = Math.min(100000, offset + 20);
    void load();
  }
  function submit(event) {
    const form = event.target.dataset.form;
    if (!form) return;
    event.preventDefault();
    if (loading || !bank()) return;
    if (form === 'search') { appliedQuery = query.trim(); offset = 0; void load(); return; }
    if (form === 'save' && draft.trim()) {
      // Stable ID stays in the form after an error so a retry cannot create duplicates.
      if (!newId.trim()) newId = `note-${typeof globalThis.crypto.randomUUID === 'function' ? globalThis.crypto.randomUUID() : newOperationId()}`;
      void mutate(false, { document_id: newId.trim(), content: draft, base_revision: 0 });
    }
    if (form === 'correct' && selected && correction.trim() && Number.isInteger(Number(revision(selected)))) {
      const metadata = {};
      for (const key of ['source_ref', 'session_id', 'turn_id', 'host_id', 'evidence']) if (provenance(selected)[key] !== undefined) metadata[key] = provenance(selected)[key];
      void mutate(false, { document_id: id(selected), content: correction, base_revision: Number(revision(selected)), metadata });
    }
  }
  container.addEventListener('change', change);
  container.addEventListener('input', input);
  container.addEventListener('click', click);
  container.addEventListener('submit', submit);
  void registry();
  return {
    refresh() { if (!disposed) return bank() ? load({ checkOperations: true }) : registry(); },
    dispose() {
      disposed = true; generation++; controller?.abort();
      for (const [name, handler] of [['change', change], ['input', input], ['click', click], ['submit', submit]]) container.removeEventListener(name, handler);
    },
  };
}
