import { escapeHtml as esc } from './herdr-web-components/util.js';

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

// The caller owns authentication and delivery. Only its upload adapter can turn
// a local File into an artifact that onSend is allowed to hand to the runtime.
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
    <div class="conversation-input-wrap"><ul id="${id}-suggestions" class="conversation-suggestions" role="listbox" aria-label="References" hidden></ul>
    <textarea class="conversation-input" rows="2" maxlength="${Number(options.maxLength) || 8000}" aria-label="Message" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${id}-suggestions" aria-describedby="${id}-status" placeholder="${esc(options.placeholder || 'Message this session…')}" autocomplete="off"></textarea></div>
    <div class="conversation-attachments" aria-label="Attachments"></div>
    <div class="conversation-actions"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-attach aria-label="Attach files"${uploadAvailable ? '' : ' disabled'}>Attach files</button><input type="file" multiple hidden aria-label="Upload files"><button type="submit" class="hd-btn" data-send>Send</button></div>
    ${uploadAvailable ? '' : '<p class="conversation-capability">Attachments are not supported by this connection.</p>'}
    <div id="${id}-status" class="conversation-status" role="status" aria-live="polite">${esc(hint)}</div></form>`;
  const form = host.querySelector('form'), input = host.querySelector('textarea');
  const popup = host.querySelector('[role=listbox]'), status = host.querySelector('.conversation-status');
  const attachButton = host.querySelector('[data-attach]'), files = host.querySelector('input[type=file]');
  const send = host.querySelector('[data-send]'), attachmentsBox = host.querySelector('.conversation-attachments');
  let disabled = Boolean(options.disabled), sending = false, disposed = false, composing = false;
  let items = [], selection = 0, attachments = [], attachmentId = 0;
  const listeners = [];
  const on = (el, event, fn) => { el.addEventListener(event, fn); listeners.push(() => el.removeEventListener(event, fn)); };
  const save = () => { try { if (storageKey) { if (input.value) storage?.setItem(storageKey, input.value); else storage?.removeItem(storageKey); } } catch { /* quota/private browsing */ } };
  try { input.value = storageKey ? storage?.getItem(storageKey) || '' : ''; } catch { /* optional draft */ }
  const grow = () => { input.style.height = 'auto'; input.style.height = `${Math.min(Math.max(input.scrollHeight, 60), 180)}px`; };
  const message = (text, error = false) => { status.textContent = text; status.setAttribute('role', error ? 'alert' : 'status'); };
  const controls = () => {
    input.disabled = disabled || sending;
    attachButton.disabled = disabled || sending || !uploadAvailable;
    files.disabled = attachButton.disabled;
    send.disabled = disabled || sending || attachments.some((a) => a.state !== 'ready');
    send.textContent = sending ? 'Sending…' : 'Send';
  };
  const close = () => { items = []; popup.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); };
  const drawSuggestions = () => {
    popup.innerHTML = items.map((item, i) => `<li role="option" id="${id}-option-${i}" data-index="${i}" aria-selected="${i === selection}"><b>${esc(item.label)}</b>${item.detail ? `<span>${esc(item.detail)}</span>` : ''}</li>`).join('');
    popup.hidden = false; input.setAttribute('aria-expanded', 'true'); input.setAttribute('aria-activedescendant', `${id}-option-${selection}`);
    popup.children[selection]?.scrollIntoView({ block: 'nearest' });
  };
  const suggest = () => {
    if (disabled || sending || composing) return close();
    const token = completionAt(input.value, input.selectionStart);
    if (!token) return close();
    const candidates = typeof options.suggestions === 'function' ? options.suggestions(token.query, token.trigger) : options.suggestions || [];
    const found = candidates.filter((item) => (item.trigger ? item.trigger === token.trigger : String(item.insertText || '').startsWith(token.trigger))
      && String(item.label || item.insertText).toLowerCase().includes(token.query)).slice(0, 8);
    if (!found.length) return close();
    if (found.length !== items.length || found.some((item, i) => item.id !== items[i]?.id)) selection = 0;
    items = found; drawSuggestions();
  };
  const choose = (index) => {
    const token = completionAt(input.value, input.selectionStart), item = items[index];
    if (!token || !item) return close();
    const head = input.value.slice(0, token.start) + item.insertText + ' ';
    input.value = head + input.value.slice(input.selectionStart);
    input.setSelectionRange(head.length, head.length); close(); save(); grow(); input.focus();
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
  on(input, 'input', () => { save(); grow(); suggest(); });
  on(input, 'click', suggest);
  on(input, 'blur', close);
  on(input, 'compositionstart', () => { composing = true; close(); });
  on(input, 'compositionend', () => { composing = false; save(); suggest(); });
  on(input, 'keydown', (event) => {
    if (composing || event.isComposing || event.keyCode === 229) return;
    if (items.length && !popup.hidden) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); selection = (selection + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length; drawSuggestions(); return; }
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) { event.preventDefault(); choose(selection); return; }
      if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    }
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
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
      input.value = ''; save(); grow(); attachments.forEach(revoke); attachments = [];
      message(receipt?.message || (receipt?.status === 'delivered' ? 'Delivered' : receipt?.status === 'accepted' ? 'Accepted; waiting for delivery.' : 'Sent'));
    } catch (error) { if (!disposed) message(error.message || 'Send failed. Your draft is saved.', true); }
    finally { sending = false; if (!disposed) { drawAttachments(); input.focus(); } }
  });
  controls(); grow();
  return {
    focus: () => input.focus(),
    getDraft: () => input.value,
    setDisabled: (value) => { disabled = Boolean(value); controls(); if (disabled) close(); },
    dispose: () => { if (disposed) return; save(); disposed = true; listeners.forEach((off) => off()); attachments.forEach(revoke); },
  };
}
