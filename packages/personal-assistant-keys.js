// Keyboard layer for the Personal Assistant view: single-key navigation, a shortcut sheet (?) and a command palette (Ctrl/Cmd+K).
//
// Rules this module keeps:
//  * Keys MOVE focus or open things. They never decide anything: approving, denying, sending and archiving stay on the
//    visible buttons (focus one, then Enter). A mistyped letter must not send mail.
//  * Nothing fires while a person is typing (input, textarea, select, contenteditable) or while a modifier is held, except
//    Ctrl/Cmd+K and Escape.
//  * The list of actions below drives the shortcut sheet and the palette, so they cannot drift from what the keys do.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const TYPING = 'input, textarea, select, [contenteditable=""], [contenteditable="true"]';
export const TABS = ['General', 'Permissions', 'Memory', 'Routines', 'Connectors', 'Delegations', 'Approvals', 'Proposals', 'Notifications', 'Diagnostics'];

/** Index to move to from `index` for a roving-focus key (arrows wrap, Home/End jump); null when the key is not one of them. */
export function roveIndex(key, index, length) {
  if (!length) return null;
  if (key === 'ArrowRight' || key === 'ArrowDown') return (index + 1) % length;
  if (key === 'ArrowLeft' || key === 'ArrowUp') return (index - 1 + length) % length;
  if (key === 'Home') return 0;
  if (key === 'End') return length - 1;
  return null;
}

/** Case-insensitive match where every typed word must appear in the label, in any order. */
export function matches(label, query) {
  const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  const text = String(label).toLowerCase();
  return words.every(w => text.includes(w));
}

/**
 * @param {HTMLElement} container the view root
 * @param {object} api  { assistants(): [{id,name}], selectId(id), tab(): string, openTab(name), toggleConfig(), newThread(), focusComposer() }
 * @returns {{dispose: () => void, openHelp: () => void, openPalette: () => void}}
 */
export function mountKeys(container, api) {
  const doc = container.ownerDocument;
  let overlay = null, returnFocus = null;

  const visible = () => container.isConnected && container.getClientRects().length > 0;
  const q = (sel) => container.querySelector(sel);
  const focusEl = (el) => { if (el) { el.focus(); return true; } return false; };
  const step = (list, current, delta) => list.length ? list[(Math.max(0, list.indexOf(current)) + delta + list.length) % list.length] : null;

  const actions = () => {
    const out = [
      { id: 'composer', keys: '/', label: 'Write a message', run: () => api.focusComposer() },
      { id: 'newthread', keys: 'n', label: 'New thread', run: () => api.newThread() },
      { id: 'threads', keys: 'j / k', label: 'Next / previous thread', run: () => stepThread(1), palette: false },
      { id: 'approvals', keys: 'a', label: 'Go to waiting approvals', run: () => focusApprovals() },
      { id: 'assistant', keys: '[ / ]', label: 'Previous / next assistant', run: () => stepAssistant(1), palette: false },
      { id: 'config', keys: ',', label: 'Show or hide Configure', run: () => api.toggleConfig() },
      { id: 'tabs', keys: '1 – 0', label: `Open a Configure tab (1 ${TABS[0]} … 0 ${TABS[9]})`, run: () => {}, palette: false },
      { id: 'help', keys: '?', label: 'Show keyboard shortcuts', run: () => openHelp() },
      { id: 'palette', keys: 'Ctrl/⌘ K', label: 'Command palette', run: () => openPalette(), palette: false },
    ];
    TABS.forEach((t, i) => out.push({ id: `tab:${t}`, keys: String((i + 1) % 10), label: `Configure: ${t}`, run: () => api.openTab(t), palette: true, sheet: false }));
    api.assistants().forEach(a => out.push({ id: `as:${a.id}`, keys: '', label: `Switch to ${a.name}`, run: () => api.selectId(a.id), palette: true, sheet: false }));
    return out;
  };

  const stepThread = (d) => {
    const btns = [...container.querySelectorAll('#pa-threads [data-t]')];
    const cur = btns.includes(doc.activeElement) ? doc.activeElement : btns.find(b => b.classList.contains('pa-tab--active'));
    const next = step(btns, cur, d);
    if (next) { next.click(); next.focus(); }
  };
  const stepAssistant = (d) => {
    const list = api.assistants();
    const cur = list.findIndex(a => a.selected);
    if (list.length > 1) api.selectId(list[(Math.max(0, cur) + d + list.length) % list.length].id);
  };
  const focusApprovals = () => {
    const btn = q('#pa-strip:not([hidden]) [data-strip-approve]');
    if (!focusEl(btn)) api.openTab('Approvals');
  };

  // ---- overlays: one at a time, focus trapped, Escape closes, focus returns to where it was
  const closeOverlay = () => {
    if (!overlay) return;
    overlay.remove(); overlay = null;
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
    returnFocus = null;
  };
  const openOverlay = (label, html) => {
    closeOverlay();
    returnFocus = doc.activeElement;
    overlay = doc.createElement('div');
    overlay.className = 'pa-overlay';
    overlay.innerHTML = `<div class="pa-dialog" role="dialog" aria-modal="true" aria-label="${esc(label)}">${html}</div>`;
    overlay.addEventListener('mousedown', e => { if (e.target === overlay) closeOverlay(); });
    overlay.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeOverlay(); return; }
      if (e.key !== 'Tab') return;
      const f = [...overlay.querySelectorAll('button, input, [tabindex="0"]')].filter(x => !x.disabled);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && doc.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && doc.activeElement === last) { e.preventDefault(); first.focus(); }
    });
    doc.body.appendChild(overlay);
    return overlay;
  };

  const openHelp = () => {
    const rows = actions().filter(a => a.keys && a.sheet !== false).map(a => `<tr><th scope="row"><kbd>${esc(a.keys)}</kbd></th><td>${esc(a.label)}</td></tr>`).join('');
    const o = openOverlay('Keyboard shortcuts', `<h2 class="pa-dialog__title">Keyboard shortcuts</h2>
      <table class="pa-keys"><tbody>${rows}
        <tr><th scope="row"><kbd>Esc</kbd></th><td>Leave the message box, or close this</td></tr>
        <tr><th scope="row"><kbd>Enter</kbd></th><td>Send (in the message box) · press a focused button</td></tr>
        <tr><th scope="row"><kbd>← →</kbd></th><td>Move between Configure tabs</td></tr></tbody></table>
      <p class="pa-muted">Keys only move around. Approve, Deny, Send and Archive always need Enter on the button itself.</p>
      <div class="pa-row"><button class="hd-btn" data-close>Close</button></div>`);
    o.querySelector('[data-close]').addEventListener('click', closeOverlay);
    o.querySelector('[data-close]').focus();
  };

  const openPalette = () => {
    const all = actions().filter(a => a.palette !== false);
    const o = openOverlay('Command palette', `<input class="pa-palette__input" role="combobox" aria-expanded="true" aria-controls="pa-palette-list" aria-autocomplete="list" placeholder="Type a command…" aria-label="Command">
      <ul id="pa-palette-list" class="pa-palette__list" role="listbox"></ul>`);
    const input = o.querySelector('input'), list = o.querySelector('ul');
    let shown = [], at = 0;
    const draw = () => {
      shown = all.filter(a => matches(a.label, input.value));
      at = Math.min(at, Math.max(0, shown.length - 1));
      list.innerHTML = shown.length ? shown.map((a, i) => `<li role="option" id="pa-opt-${i}" data-i="${i}" aria-selected="${i === at}" class="${i === at ? 'is-on' : ''}">${esc(a.label)}${a.keys ? `<kbd>${esc(a.keys)}</kbd>` : ''}</li>`).join('')
        : '<li class="pa-muted" role="presentation">No matching command</li>';
      input.setAttribute('aria-activedescendant', shown.length ? `pa-opt-${at}` : '');
    };
    const run = (i) => { const a = shown[i]; if (!a) return; closeOverlay(); a.run(); };
    input.addEventListener('input', () => { at = 0; draw(); });
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); at = roveIndex(e.key, at, shown.length) ?? at; draw(); }
      else if (e.key === 'Enter') { e.preventDefault(); run(at); }
    });
    list.addEventListener('mousedown', e => { const li = e.target.closest('[data-i]'); if (li) { e.preventDefault(); run(Number(li.dataset.i)); } });
    draw(); input.focus();
  };

  // ---- the keys themselves
  const onKey = (e) => {
    if (!visible()) return;
    if (!e.key) return;
    const altGr = typeof e.getModifierState === 'function' && e.getModifierState('AltGraph');   // AltGr reports ctrl+alt
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'k') { e.preventDefault(); overlay ? closeOverlay() : openPalette(); return; }
    if (overlay || e.defaultPrevented || ((e.ctrlKey || e.metaKey || e.altKey) && !altGr)) return;
    const t = e.target;
    if (t && t.closest && t.closest(TYPING)) {
      if (e.key === 'Escape' && t.matches('[data-msg]')) { t.blur(); }       // leave the message box, back to single-key mode
      return;
    }
    const k = e.key;
    const tab = TABS[k === '0' ? 9 : Number(k) - 1];
    if (/^[0-9]$/.test(k) && tab) { e.preventDefault(); api.openTab(tab); return; }
    const hit = { '/': 'composer', n: 'newthread', j: () => stepThread(1), k: () => stepThread(-1), a: 'approvals',
      '[': () => stepAssistant(-1), ']': () => stepAssistant(1), ',': 'config', '?': 'help' }[k];
    if (!hit) return;
    e.preventDefault();
    if (typeof hit === 'function') hit(); else actions().find(a => a.id === hit)?.run();
  };

  doc.addEventListener('keydown', onKey);
  return { dispose: () => { doc.removeEventListener('keydown', onKey); closeOverlay(); }, openHelp, openPalette };
}
