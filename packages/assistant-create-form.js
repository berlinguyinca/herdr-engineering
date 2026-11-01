// "+ New" on the Personal Assistant page: pick a type, optionally a parent, and see the `herdr-eng assistant create`
// command that does the same thing. The form posts to the same endpoint the CLI does, so the two always agree.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);
// Names a person typed go into a shell line they may copy, so they are always single-quoted.
const sh = (s) => `'${text(s).replace(/'/g, `'\\''`)}'`;

/** The CLI line equivalent to this form. */
export function createCommand({ name, type = 'rakazo', parent } = {}) {
  const parts = ['herdr-eng assistant create', sh(name || 'Personal Assistant'), '--type', sh(type || 'rakazo')];
  if (parent) parts.push('--parent', sh(parent));
  return parts.join(' ');
}

/** { types: [{type, kind, can_be_parent, requires, resources}], error? }; never throws, so the form still offers rakazo. */
export async function loadTypes(client, { signal } = {}) {
  try {
    const out = await client.assistantTypes({ signal });
    const types = list(out?.types).filter(t => t && typeof t.type === 'string');
    return { types: types.length ? types : [{ type: 'rakazo', kind: 'assistant', can_be_parent: true }] };
  } catch (err) {
    return { types: [{ type: 'rakazo', kind: 'assistant', can_be_parent: true }], error: text(err?.message) || 'request failed' };
  }
}

/** Parents a specialist may be linked under: enabled rakazo assistants. */
export function parentChoices(assistants) {
  return list(assistants).filter(a => a && a.enabled !== false && (a.type || 'rakazo') === 'rakazo');
}

export function renderCreateForm({ types, assistants, values = {}, error = '' }) {
  const type = values.type || 'rakazo';
  const spec = list(types).find(t => t.type === type);
  const specialist = spec?.kind === 'specialist';
  const parents = parentChoices(assistants);
  const parent = parents.find(p => p.assistant_id === values.parent_id);
  const needs = specialist ? [...list(spec.requires), ...Object.entries(spec.resources || {}).map(([k, v]) => `${k} ${v}`)] : [];
  return `<form class="hd-card pa-create" data-create-form aria-label="New assistant">
    <label>Name <input data-create-name maxlength="80" value="${esc(values.name || '')}" placeholder="Personal Assistant"></label>
    <label>Type <select data-create-type>${list(types).map(t =>
      `<option value="${esc(t.type)}" ${t.type === type ? 'selected' : ''}>${esc(t.type)}${t.kind === 'specialist' ? ' (specialist)' : ''}</option>`).join('')}</select></label>
    ${specialist ? `<label>Parent <select data-create-parent><option value="">None</option>${parents.map(p =>
      `<option value="${esc(p.assistant_id)}" ${p.assistant_id === values.parent_id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label>
      <p class="pa-muted">A link is for management only. It grants nothing${needs.length ? `; it will be placed on a host with ${esc(needs.join(', '))}` : ''}.</p>` : ''}
    ${error ? `<p class="pa-error" role="alert">${esc(error)}</p>` : ''}
    <p class="pa-muted">Same as: <code data-create-cli>${esc(createCommand({ name: values.name, type, parent: parent?.name }))}</code></p>
    <div class="pa-row"><button type="submit" class="hd-btn hd-btn--sm" data-create-go>Create</button>
      <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-create-cancel>Cancel</button></div>
  </form>`;
}

/** The POST body for the form; the same fields as the CLI sends. */
export function createBody({ name, type, parent_id, idempotency_key }) {
  const body = { name: text(name).trim() || 'Personal Assistant', type: type || 'rakazo' };
  if (parent_id) body.parent_id = parent_id;
  if (idempotency_key) body.idempotency_key = idempotency_key;     // one key per open form: a double click replays
  return body;
}
