// The assistants on the Personal Assistant page as tab groups: each Rakazo assistant leads a group holding the specialists
// linked under it, so you can tab between them. Assistants with no parent that are not Rakazo ones sit in "Unlinked".
// Plain functions returning HTML so the states run under node; the view owns the clicks and arrow keys.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);
const isRakazo = (a) => !text(a.type) || a.type === 'rakazo';

/** [{ id, label, items: [assistant] }]: a Rakazo assistant first in its own group, its linked children after it. */
export function groupAssistants(assistants) {
  const all = list(assistants).filter((a) => a && text(a.assistant_id));
  const ids = new Set(all.map((a) => a.assistant_id));
  const groups = [];
  for (const parent of all.filter(isRakazo)) {
    groups.push({ id: parent.assistant_id, label: text(parent.name), items: [parent, ...all.filter((c) => !isRakazo(c) && c.parent_id === parent.assistant_id)] });
  }
  const placed = new Set(groups.flatMap((g) => g.items.map((a) => a.assistant_id)));
  const loose = all.filter((a) => !placed.has(a.assistant_id) && !(a.parent_id && ids.has(a.parent_id) && placed.has(a.parent_id)));
  if (loose.length) groups.push({ id: 'unlinked', label: 'Unlinked', items: loose });
  return groups;
}

/** Tab ids in the order a person tabs through them. */
export const tabOrder = (groups) => groups.flatMap((g) => g.items.map((a) => a.assistant_id));

const dot = (status) => (status === 'ready' || status === 'running' ? 'ready' : status === 'needs_setup' ? 'setup' : 'off');

export function renderTabs(assistants, selectedId, statusLabel = text) {
  const groups = groupAssistants(assistants);
  if (!groups.length) return '';
  return `<div class="pa-tabs-bar" role="tablist" aria-label="Assistants" aria-orientation="horizontal">${groups.map((g) =>
    `<div class="pa-tabgroup" role="group" aria-label="${esc(g.label)}" data-tabgroup="${esc(g.id)}">${g.items.map((a, i) => {
      const on = a.assistant_id === selectedId;
      return `<button type="button" role="tab" class="pa-atab ${on ? 'pa-atab--on' : ''} ${i === 0 && g.id !== 'unlinked' ? '' : 'pa-atab--child'}" `
        + `id="pa-atab-${esc(a.assistant_id)}" data-atab="${esc(a.assistant_id)}" aria-selected="${on}" tabindex="${on ? 0 : -1}" `
        + `title="${esc(statusLabel(a.status))}"><span class="pa-adot pa-adot--${dot(a.status)}"></span>${esc(text(a.name))}`
        + `${isRakazo(a) ? '' : ` <span class="pa-badge">${esc(text(a.type))}</span>`}</button>`;
    }).join('')}</div>`).join('')}</div>`;
}
