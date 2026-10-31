import { escapeHtml as esc } from './herdr-web-components/util.js';
import { renderMarkdown } from './conversation-markdown.js';
import { parseJson, mountJsonViewer } from './conversation-json.js';
import { completionAt as sigilAt, applyCompletion } from './collab-mention.js';

let nextId = 0;

export function ensureConversationStyles(doc) {
  if (!doc.querySelector('link[data-conversation-css]')) {
    const style = doc.createElement('link'); style.rel = 'stylesheet';
    style.href = new URL('./conversation.css', import.meta.url).href;
    style.dataset.conversationCss = ''; doc.head.appendChild(style);
  }
}

export function completionAt(text, caret) {
  const match = /(?:^|\s)([@/][^\s@<>]*)$/.exec(String(text).slice(0, caret));
  return match ? { start: caret - match[1].length, query: match[1].slice(1).toLowerCase(), trigger: match[1][0] } : null;
}

const ICONS = { person: '👤', group: '👥', robot: '🤖', file: '📄', issue: '#', command: '/', skill: '✦' };

// The caller owns authentication and delivery. Only its upload adapter can turn
// a local File into an artifact that onSend is allowed to hand to the runtime.
// options.urls: a fabric URL resolver ({resolver, prefetch}); the preview then links agent-reachable addresses. The draft itself is never changed.
export function mountComposer(host, options = {}) {
  const doc = host.ownerDocument;
  ensureConversationStyles(doc);
  const id = `conversation-${++nextId}`;
  const hint = options.hint || 'Enter sends · Shift+Enter adds a line · @ references';
  const uploadAvailable = typeof options.upload === 'function';
  const storageKey = options.draftKey ? `herdr.conversation.draft:${options.draftKey}` : null;
  let storage;
  try { storage = options.storage || doc.defaultView.sessionStorage; } catch { /* blocked storage is optional */ }
  host.innerHTML = `<form class="conversation-composer">
    <div class="conversation-editor-toolbar" aria-label="Message editor tools">
      <button type="button" data-expand aria-label="Expand editor" aria-expanded="false">Expand editor</button>
      <button type="button" data-mode="edit" aria-pressed="true">Edit</button><button type="button" data-mode="preview" aria-pressed="false">Preview</button>
      <button type="button" data-edit="bold" aria-label="Bold">Bold</button><button type="button" data-edit="list" aria-label="Bulleted list">List</button><button type="button" data-edit="code" aria-label="Code block">Code</button>
      <details class="conversation-more"><summary>More</summary><span class="conversation-more-tools">
        <button type="button" data-mode="split" aria-pressed="false">Split preview</button>
        <button type="button" data-edit="indent">Indent</button><button type="button" data-edit="outdent">Outdent</button><button type="button" data-edit="undo">Undo</button><button type="button" data-edit="redo">Redo</button>
        <button type="button" data-find>Find and replace</button><button type="button" data-edit="json">Format JSON</button><button type="button" data-wrap aria-pressed="true">Wrap</button></span></details>
    </div>
    <div class="conversation-find" hidden><label>Find text <input type="text" aria-label="Find text"></label><label>Replace with <input type="text" aria-label="Replace with"></label><button type="button" data-find-next>Find next</button><button type="button" data-replace>Replace all</button><button type="button" data-close-find>Close find</button></div>
    <div class="conversation-editor-body" data-mode="edit"><div class="conversation-input-wrap"><ul id="${id}-suggestions" class="conversation-suggestions" role="listbox" aria-label="References" hidden></ul>
    <textarea class="conversation-input" rows="2" maxlength="${Number(options.maxLength) || 8000}" aria-label="Message" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${id}-suggestions" aria-describedby="${id}-status" placeholder="${esc(options.placeholder || 'Message this session…')}" autocomplete="off"></textarea></div><div class="conversation-editor-preview conversation-markdown" aria-label="Message preview" tabindex="0" hidden></div></div><div class="conversation-editor-info" aria-live="off"></div>
    <div class="conversation-attachments" aria-label="Attachments"></div>
    <div class="conversation-actions"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-attach aria-label="Attach files"${uploadAvailable ? '' : ' disabled'}>Attach files</button><input type="file" multiple hidden aria-label="Upload files"><button type="submit" class="hd-btn" data-send>Send</button></div>
    ${uploadAvailable ? '' : '<p class="conversation-capability">Attachments are not supported by this connection.</p>'}
    <div id="${id}-status" class="conversation-status" role="status" aria-live="polite">${esc(hint)}</div></form>`;
  const form = host.querySelector('form'), input = host.querySelector('textarea');
  const popup = host.querySelector('[role=listbox]'), status = host.querySelector('.conversation-status');
  const attachButton = host.querySelector('[data-attach]'), files = host.querySelector('input[type=file]');
  const send = host.querySelector('[data-send]'), attachmentsBox = host.querySelector('.conversation-attachments');
  const expandButton = host.querySelector('[data-expand]'), editorBody = host.querySelector('.conversation-editor-body');
  const preview = host.querySelector('.conversation-editor-preview'), info = host.querySelector('.conversation-editor-info');
  const findBox = host.querySelector('.conversation-find'), findInput = findBox.querySelector('[aria-label="Find text"]'), replaceInput = findBox.querySelector('[aria-label="Replace with"]');
  let expanded = false, mode = 'edit', restoreFocus = null, jsonViewer = null;
  let disabled = Boolean(options.disabled), sending = false, disposed = false, composing = false;
  let items = [], selection = 0, attachments = [], attachmentId = 0;
  const listeners = [];
  const on = (el, event, fn) => { el.addEventListener(event, fn); listeners.push(() => el.removeEventListener(event, fn)); };
  const save = () => { try { if (storageKey) { if (input.value) storage?.setItem(storageKey, input.value); else storage?.removeItem(storageKey); } } catch { /* quota/private browsing */ } };
  try { input.value = storageKey ? storage?.getItem(storageKey) || '' : ''; } catch { /* optional draft */ }
  const grow = () => { if (expanded) { input.style.height = ''; return; } input.style.height = 'auto'; input.style.height = `${Math.min(Math.max(input.scrollHeight, 60), 180)}px`; };
  const message = (text, error = false) => { status.textContent = text; status.setAttribute('role', error ? 'alert' : 'status'); };
  const controls = () => {
    input.disabled = disabled || sending;
    attachButton.disabled = disabled || sending || !uploadAvailable;
    files.disabled = attachButton.disabled;
    send.disabled = disabled || sending || attachments.some((a) => a.state !== 'ready');
    send.textContent = sending ? 'Sending…' : 'Send';
    form.querySelectorAll('[data-edit], [data-replace]').forEach((button) => { button.disabled = disabled || sending; });
  };
  const close = () => { items = []; popup.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); };
  const drawSuggestions = () => {
    const option = (item, i) => {
      const icon = item.icon ? `<span class="conversation-suggestion-icon" data-icon="${esc(item.icon)}" aria-hidden="true">${ICONS[item.icon] || ''}</span>` : '';
      return `<li role="option" id="${id}-option-${i}" data-index="${i}" aria-selected="${i === selection}"${item.section ? ` data-section="${esc(item.section)}"` : ''}>${icon}<b>${esc(item.label)}</b>${item.detail ? `<span>${esc(item.detail)}</span>` : ''}</li>`;
    };
    // sectioned items (People / Agents / Files …) are listbox groups, each named by its heading
    const runs = [];
    items.forEach((item, i) => { if (!runs.length || runs.at(-1).section !== item.section) runs.push({ section: item.section, rows: [] }); runs.at(-1).rows.push(option(item, i)); });
    popup.innerHTML = runs.map((run) => (run.section
      ? `<li role="group" class="conversation-suggestions-group" aria-label="${esc(run.section)}"><span class="conversation-suggestions-section" aria-hidden="true">${esc(run.section)}</span><ul role="none">${run.rows.join('')}</ul></li>`
      : run.rows.join(''))).join('');
    popup.hidden = false; input.setAttribute('aria-expanded', 'true'); input.setAttribute('aria-activedescendant', `${id}-option-${selection}`);
    popup.querySelector(`#${id}-option-${selection}`)?.scrollIntoView({ block: 'nearest' });
  };
  // Sigil providers (`@`, `#`, `/`): one function per sigil, given the typed query and the caller's context.
  const providerItems = () => {
    const token = sigilAt(input.value, input.selectionStart);
    const provide = token && options.providers[token.sigil];
    if (!provide) return [];
    const ctx = typeof options.context === 'function' ? options.context() : options.context || {};
    return provide(token.query, { ...ctx, agent: token.agent }).slice(0, 12).map((item) => ({ id: `${item.section}:${item.insert}`, ...item }));
  };
  const suggest = () => {
    if (disabled || sending || composing) return close();
    if (options.providers) {
      const found = providerItems();
      if (!found.length) return close();
      if (found.length !== items.length || found.some((item, i) => item.id !== items[i]?.id)) selection = 0;
      items = found; drawSuggestions(); return;
    }
    const token = completionAt(input.value, input.selectionStart);
    if (!token) return close();
    const candidates = typeof options.suggestions === 'function' ? options.suggestions(token.query, token.trigger) : options.suggestions || [];
    const found = candidates.filter((item) => (item.trigger ? item.trigger === token.trigger : String(item.insertText || '').startsWith(token.trigger))
      && `${item.label || ''} ${item.insertText || ''}`.toLowerCase().includes(token.query)).slice(0, 8);
    if (!found.length) return close();
    if (found.length !== items.length || found.some((item, i) => item.id !== items[i]?.id)) selection = 0;
    items = found; drawSuggestions();
  };
  const snapshot = () => ({ value: input.value, start: input.selectionStart, end: input.selectionEnd });
  let current = snapshot(), undo = [], redo = [];
  const remember = () => {
    if (current.value !== input.value) { undo.push(current); if (undo.length > 100) undo.shift(); redo = []; }
    current = snapshot();
  };
  const updatePreview = () => {
    jsonViewer?.dispose(); jsonViewer = null;
    if (mode === 'edit') return;
    const text = input.value.trim();
    if (/^[{[]/.test(text) && parseJson(text).ok) jsonViewer = mountJsonViewer(preview, text);
    else { options.urls?.prefetch?.(input.value); preview.innerHTML = renderMarkdown(input.value, { urls: options.urls?.resolver || null }); }
  };
  const updateInfo = () => {
    const before = input.value.slice(0, input.selectionStart), lines = before.split('\n');
    info.textContent = `Line ${lines.length}, column ${lines.at(-1).length + 1} · ${input.value.length} characters`;
  };
  // options.check(text) -> a note or '' (e.g. "@zzz addresses nobody"). It flags; it never blocks a send.
  let flagged = false;
  const flag = () => {
    if (typeof options.check !== 'function' || sending) return;
    const note = options.check(input.value) || '';
    if (note) message(note); else if (flagged) message(expanded ? 'Enter adds a line · Use Send to deliver · Escape closes the editor' : hint);
    flagged = Boolean(note);
  };
  const changed = () => { save(); grow(); updateInfo(); updatePreview(); flag(); };
  const apply = (value, start, end = start) => {
    if (disabled || sending || composing) return false;
    if (value.length > input.maxLength) { message(`Message exceeds ${input.maxLength} characters.`, true); return false; }
    current = snapshot(); input.value = value; input.setSelectionRange(start, end); remember(); changed(); close(); input.focus(); return true;
  };
  const history = (reverse) => {
    const from = reverse ? redo : undo, to = reverse ? undo : redo;
    if (!from.length || disabled || sending || composing) return;
    to.push(snapshot()); const state = from.pop(); input.value = state.value; input.setSelectionRange(state.start, state.end);
    current = snapshot(); changed(); close(); input.focus();
  };
  const setMode = (value) => {
    mode = value; editorBody.dataset.mode = mode; preview.hidden = mode === 'edit';
    input.closest('.conversation-input-wrap').hidden = mode === 'preview';
    form.querySelectorAll('[data-mode]').forEach((button) => { if (button.tagName === 'BUTTON') button.setAttribute('aria-pressed', String(button.dataset.mode === mode)); });
    updatePreview(); if (mode === 'preview') preview.focus(); else input.focus();
  };
  const setExpanded = (value) => {
    if (expanded === value) return;
    const start = input.selectionStart, end = input.selectionEnd;
    if (value) restoreFocus = doc.activeElement;
    expanded = value; form.classList.toggle('conversation-composer--expanded', expanded);
    expandButton.textContent = expanded ? 'Close expanded editor' : 'Expand editor';
    expandButton.setAttribute('aria-label', expandButton.textContent); expandButton.setAttribute('aria-expanded', String(expanded));
    if (expanded) { form.setAttribute('role', 'dialog'); form.setAttribute('aria-modal', 'true'); form.setAttribute('aria-label', 'Expanded message editor'); }
    else { form.removeAttribute('role'); form.removeAttribute('aria-modal'); form.removeAttribute('aria-label'); }
    const expandedHint = 'Enter adds a line · Use Send to deliver · Escape closes the editor';
    if (status.textContent === hint || status.textContent === expandedHint) message(expanded ? expandedHint : hint);
    grow(); input.setSelectionRange(start, end);
    if (expanded) { if (mode === 'preview') preview.focus(); else input.focus(); }
    else if (restoreFocus?.isConnected) restoreFocus.focus(); else input.focus();
  };
  const edit = (action) => {
    if (disabled || sending || composing) return;
    if (action === 'undo' || action === 'redo') { history(action === 'redo'); return; }
    const text = input.value, start = input.selectionStart, end = input.selectionEnd, selection = text.slice(start, end);
    if (action === 'json') {
      const parsed = parseJson(text);
      if (!parsed.ok) { message(parsed.error, true); return; }
      if (apply(JSON.stringify(parsed.value, null, 2), 0)) message('Valid JSON · formatted'); return;
    }
    if (action === 'indent' || action === 'outdent' || action === 'list') {
      const from = text.lastIndexOf('\n', start - 1) + 1;
      const to = end > start && text[end - 1] === '\n' ? end - 1 : (text.indexOf('\n', end) < 0 ? text.length : text.indexOf('\n', end));
      const lines = text.slice(from, to).split('\n');
      const adjusted = lines.map((line) => action === 'outdent' ? line.replace(/^(?: {1,2}|\t)/, '') : `${action === 'list' ? '- ' : '  '}${line}`).join('\n');
      apply(text.slice(0, from) + adjusted + text.slice(to), from, from + adjusted.length); return;
    }
    const before = action === 'bold' ? '**' : '\n```\n', after = action === 'bold' ? '**' : '\n```\n';
    const body = selection || (action === 'bold' ? 'bold text' : 'code');
    apply(text.slice(0, start) + before + body + after + text.slice(end), start + before.length, start + before.length + body.length);
  };
  const choose = (index) => {
    if (options.providers) {             // the item's literal text, nothing else: what a harness reads is what was typed
      const token = sigilAt(input.value, input.selectionStart), item = items[index];
      if (!token || !item) return close();
      const next = applyCompletion(input.value, input.selectionStart, token.start, item);
      apply(next.text, next.caret); return;
    }
    const token = completionAt(input.value, input.selectionStart), item = items[index];
    if (!token || !item) return close();
    const head = input.value.slice(0, token.start) + item.insertText + ' ';
    apply(head + input.value.slice(input.selectionStart), head.length);
  };
  const revoke = (item) => { if (item.preview) doc.defaultView.URL.revokeObjectURL(item.preview); };
  const drawAttachments = () => {
    attachmentsBox.innerHTML = attachments.map((a) => `<div class="conversation-attachment">${a.preview ? `<img src="${esc(a.preview)}" alt="Preview of ${esc(a.file.name)}">` : ''}<span><b>${esc(a.file.name)}</b><small>${esc(a.error || (a.state === 'ready' ? 'Ready' : 'Uploading…'))}</small></span>${a.state === 'error' ? `<button type="button" data-retry="${a.id}" aria-label="Retry ${esc(a.file.name)}">Retry</button>` : ''}<button type="button" data-remove="${a.id}" aria-label="Remove ${esc(a.file.name)}"${sending ? ' disabled' : ''}>×</button></div>`).join('');
    controls();
  };
  const upload = async (item) => {
    item.state = 'uploading'; item.error = ''; drawAttachments();
    try {
      const artifact = await options.upload(item.file);
      if (disposed || !attachments.includes(item)) return;
      if (!artifact) throw new Error('Upload returned no attachment.');
      item.artifact = artifact; item.state = 'ready';
    } catch (error) {
      if (disposed || !attachments.includes(item)) return;
      item.state = 'error'; item.error = error.message || 'Upload failed';
    }
    drawAttachments();
  };
  const addFiles = (incoming) => {
    if (disposed || disabled || sending) return;
    if (!uploadAvailable) { message('Attachments are not supported by this connection.', true); return; }
    for (const file of Array.from(incoming)) {
      if (attachments.length >= 10) { message('Attach up to 10 files per message.', true); break; }
      const item = { id: ++attachmentId, file, state: 'uploading' };
      // SVG can contain active document content; only raster image previews are used.
      if (/^image\/(png|jpeg|gif|webp)$/.test(file.type)) item.preview = doc.defaultView.URL.createObjectURL(file);
      attachments.push(item); void upload(item);
    }
  };
  on(input, 'beforeinput', () => { current = snapshot(); });
  on(input, 'input', () => { remember(); changed(); suggest(); });
  on(input, 'select', () => { current = snapshot(); updateInfo(); });
  on(input, 'keyup', updateInfo);
  on(input, 'click', suggest);
  on(input, 'blur', close);
  on(input, 'compositionstart', () => { composing = true; close(); });
  on(input, 'compositionend', () => { composing = false; save(); suggest(); });
  on(input, 'keydown', (event) => {
    if (composing || event.isComposing || event.keyCode === 229) return;
    if (items.length && !popup.hidden) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); selection = (selection + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length; drawSuggestions(); return; }
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) { event.preventDefault(); choose(selection); return; }
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return; }
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); history(event.shiftKey); return; }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') { event.preventDefault(); history(true); return; }
    if (expanded && event.key === 'Tab') { event.preventDefault(); edit(event.shiftKey ? 'outdent' : 'indent'); return; }
    if (event.key === 'Enter' && !event.shiftKey && !expanded) { event.preventDefault(); form.requestSubmit(); }
  });
  on(expandButton, 'click', () => setExpanded(!expanded));
  on(form, 'click', (event) => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.dataset.mode) setMode(button.dataset.mode);
    if (button.dataset.edit) edit(button.dataset.edit);
  });
  on(form, 'keydown', (event) => {
    if (composing || event.isComposing || event.keyCode === 229) return;
    if (event.key === 'Escape') {
      if (!findBox.hidden) { event.preventDefault(); event.stopPropagation(); findBox.hidden = true; input.focus(); }
      else if (expanded) { event.preventDefault(); event.stopPropagation(); setExpanded(false); }
    }
    // Text fields in the find bar must never submit the conversation.
    if (event.key === 'Enter' && event.target.closest('.conversation-find')) { event.preventDefault(); if (event.target === findInput) findNext(); }
    if (expanded && event.key === 'Tab' && !event.defaultPrevented) {
      const focusable = Array.from(form.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex="0"]')).filter((el) => el.getClientRects().length);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  on(form.querySelector('[data-find]'), 'click', () => { findBox.hidden = !findBox.hidden; if (!findBox.hidden) findInput.focus(); });
  on(form.querySelector('[data-close-find]'), 'click', () => { findBox.hidden = true; input.focus(); });
  const findNext = () => {
    if (!findInput.value) return;
    const from = input.selectionEnd, index = input.value.indexOf(findInput.value, from);
    const found = index < 0 ? input.value.indexOf(findInput.value) : index;
    if (found < 0) message('No matches found.');
    else { if (mode === 'preview') setMode('split'); input.setSelectionRange(found, found + findInput.value.length); input.focus(); updateInfo(); }
  };
  on(form.querySelector('[data-find-next]'), 'click', findNext);
  on(form.querySelector('[data-replace]'), 'click', () => {
    if (!findInput.value) return;
    const parts = input.value.split(findInput.value), count = parts.length - 1;
    if (input.value.length + count * (replaceInput.value.length - findInput.value.length) > input.maxLength) { message(`Message exceeds ${input.maxLength} characters.`, true); return; }
    if (apply(parts.join(replaceInput.value), 0)) message(`${count} replacement${count === 1 ? '' : 's'}`);
  });
  on(form.querySelector('[data-wrap]'), 'click', (event) => {
    const wrap = input.wrap !== 'off'; input.wrap = wrap ? 'off' : 'soft'; event.currentTarget.setAttribute('aria-pressed', String(!wrap));
  });
  on(popup, 'mousedown', (event) => { const row = event.target.closest('[data-index]'); if (row) { event.preventDefault(); choose(Number(row.dataset.index)); } });
  on(attachButton, 'click', () => files.click());
  on(files, 'change', () => { addFiles(files.files); files.value = ''; });
  on(form, 'dragover', (event) => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); });
  on(form, 'drop', (event) => { if (event.dataTransfer?.files.length) { event.preventDefault(); addFiles(event.dataTransfer.files); } });
  on(input, 'paste', (event) => { if (event.clipboardData?.files.length) { event.preventDefault(); addFiles(event.clipboardData.files); } });
  on(attachmentsBox, 'click', (event) => {
    if (sending || disabled) return;
    const remove = event.target.closest('[data-remove]'), retry = event.target.closest('[data-retry]');
    if (remove) { attachments = attachments.filter((a) => { if (a.id !== Number(remove.dataset.remove)) return true; revoke(a); return false; }); drawAttachments(); }
    if (retry) { const item = attachments.find((a) => a.id === Number(retry.dataset.retry)); if (item) void upload(item); }
  });
  on(form, 'submit', async (event) => {
    event.preventDefault();
    if (disabled || sending || composing || disposed) return;
    const text = input.value;
    if (!text.trim() && !attachments.length) return;
    if (attachments.some((a) => a.state !== 'ready')) { message('Wait for uploads to finish, or retry or remove failed attachments.', true); return; }
    if (typeof options.onSend !== 'function') { message('Sending is not supported by this connection.', true); return; }
    sending = true; close(); controls(); drawAttachments(); message('Sending…');
    try {
      const receipt = await options.onSend(text, attachments.map((a) => a.artifact));
      if (receipt?.status === 'failed' || receipt?.status === 'error') throw new Error(receipt.error || 'Delivery failed');
      if (disposed) return;
      input.value = ''; undo = []; redo = []; current = snapshot(); changed(); flagged = false; attachments.forEach(revoke); attachments = [];
      message(receipt?.message || (receipt?.status === 'delivered' ? 'Delivered' : receipt?.status === 'accepted' ? 'Accepted; waiting for delivery.' : 'Sent'));
    } catch (error) { if (!disposed) message(error.message || 'Send failed. Your draft is saved.', true); }
    finally { sending = false; if (!disposed) { drawAttachments(); input.focus(); } }
  });
  controls(); grow(); updateInfo();
  return {
    focus: () => input.focus(),
    getDraft: () => input.value,
    setExpanded,
    setDisabled: (value) => { disabled = Boolean(value); controls(); if (disabled) close(); },
    dispose: () => { if (disposed) return; save(); jsonViewer?.dispose(); disposed = true; listeners.forEach((off) => off()); attachments.forEach(revoke); },
  };
}
