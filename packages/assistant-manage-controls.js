// Quick management of assistants and their agent network on the Personal Assistant page: rename, unlink, archive from the
// graph, and restore or permanently delete the inactive ones. Plain functions returning HTML so the states run under node.
// Deleting needs the assistant archived first and its exact name typed; the server enforces both, this only asks.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);

/** Buttons on one assistant node of the graph. Archived or disabled assistants offer none here: they live under Inactive. */
export function nodeActions(node) {
  if (!node || node.enabled === false) return '';
  const id = esc(text(node.id));
  return `<span class="pa-node-actions">`
    + `<button type="button" class="hd-linkbtn" data-node-rename="${id}">Rename</button>`
    + (node.parent_id ? `<button type="button" class="hd-linkbtn" data-node-unlink="${id}">Remove from network</button>` : '')
    + `<button type="button" class="hd-linkbtn" data-node-archive="${id}">Archive</button></span>`;
}

/** The Inactive section: every archived or disabled assistant, with Restore and a Delete that asks for the name. */
export function renderInactive(assistants, { openDelete = '', error = '' } = {}) {
  const rows = list(assistants).filter((a) => a && a.enabled === false);
  if (!rows.length) return '<p class="pa-muted" data-inactive-empty>Nothing inactive.</p>';
  return `<ul class="pa-inactive" data-inactive>${rows.map((a) => {
    const id = esc(text(a.assistant_id));
    const open = openDelete === a.assistant_id;
    return `<li data-inactive-id="${id}"><strong>${esc(text(a.name))}</strong>`
      + `${text(a.type) && a.type !== 'rakazo' ? ` <span class="pa-badge">${esc(text(a.type))}</span>` : ''}`
      + ` <button type="button" class="hd-btn hd-btn--sm" data-restore="${id}">Restore</button>`
      + ` <button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-delete-open="${id}">Delete…</button>`
      + (open ? `<form class="pa-delete" data-delete-form="${id}"><label>Type <code>${esc(text(a.name))}</code> to delete it for good, `
        + `with its conversations <input data-delete-confirm autocomplete="off"></label>`
        + `<button type="submit" class="hd-btn hd-btn--danger hd-btn--sm" data-delete-go disabled>Delete forever</button>`
        + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-delete-cancel>Cancel</button>`
        + (error ? `<p class="pa-error" role="alert">${esc(error)}</p>` : '') + '</form>' : '')
      + '</li>';
  }).join('')}</ul>`;
}

/** True only when the typed text is exactly the assistant's name. */
export const deleteConfirmed = (assistant, typed) => !!assistant && typed === assistant.name;
