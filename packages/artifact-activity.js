// Activity tab of the artifact share panel: version history with merge parents, per-version file diffs, and
// (on demand) the conflicted or rejected commit attempts. Pure renderers plus a thin delegated-event mount.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { renderDiff } from './herdr-web-components/inspector.js';
import { formatWhen, show } from './artifact-collaborators.js';

const failure = error => String(error?.message || 'The request could not be completed.');
const parentsOf = v => (Array.isArray(v.parent_version_ids) && v.parent_version_ids.length
  ? v.parent_version_ids : v.parent_version_id ? [v.parent_version_id] : []).filter(Boolean);

export function renderHistoryRow(v, { selected = false, busy = false } = {}) {
  const parents = parentsOf(v);
  const merge = parents.length >= 2;
  const author = v.created_by || v.author_label || v.author || '';
  const parentHtml = parents.length
    ? `<p>${merge ? '<span class="hd-badge" data-version-merge>Merge</span> of parents' : 'Parent'}: ${parents.map(p => `<code>${esc(p)}</code>`).join(' + ')}</p>`
    : '';
  return `<li data-activity-version${selected ? ' aria-current="true"' : ''}><p><strong>v${show(v.version_number)}</strong>${author ? ` · ${esc(author)}` : ''}`
    + `${v.created_at ? ` · <time>${esc(formatWhen(v.created_at))}</time>` : ''}${v.message ? ` · ${esc(v.message)}` : ''}</p>${parentHtml}`
    + `<button type="button" class="hd-btn" data-activity-action="select" data-version-id="${esc(v.version_id ?? '')}"${busy ? ' disabled' : ''}>${selected ? 'Showing changes' : 'Show changes'}</button></li>`;
}

const conflictName = c => (typeof c === 'string' || c == null
  ? `<code>${show(c)}</code>`
  : `<code>${show(c?.name)}</code>${c?.kind ? ` (${esc(c.kind)})` : ''}`);

export function renderAttempt(a) {
  const names = Array.isArray(a.conflict_names) ? a.conflict_names : [];
  const when = formatWhen(a.received_at || a.decided_at || a.created_at);
  return `<li data-activity-attempt><p><strong>${show(a.outcome)}</strong> · ${show(a.actor_id || a.agent_id || 'unknown agent')}${when ? ` · <time>${esc(when)}</time>` : ''}</p>`
    + `${names.length ? `<p>Conflicts: ${names.map(conflictName).join(', ')}</p>` : ''}${a.reason ? `<p>Reason: ${esc(a.reason)}</p>` : ''}</li>`;
}

export function renderDiffFiles(diff) {
  const files = Array.isArray(diff?.files) ? diff.files : [];
  if (!files.length) return '<p class="hd-note">No file changes.</p>';
  return `<ul>${files.map(f => {
    const body = f.reason ? `<p class="hd-note" data-diff-reason>${esc(f.reason)}: no diff shown.</p>` : f.diff ? renderDiff(f.diff) : '';
    return `<li data-activity-file><p><code>${show(f.name)}</code> · ${show(f.change)}</p>${body}</li>`;
  }).join('')}</ul>`;
}

export function renderActivityPanel(state) {
  const status = state.error ? `<p role="alert">${esc(state.error)}</p>` : '';
  const history = state.versions.length
    ? `<ul>${state.versions.map(v => renderHistoryRow(v, { selected: v.version_id === state.selected, busy: state.diffBusy })).join('')}</ul>`
    : '<p class="hd-note">No versions yet.</p>';
  const diff = state.diffBusy ? '<p role="status">Loading changes…</p>' : state.diff ? `<h4>Changes in this version</h4>${renderDiffFiles(state.diff)}` : '';
  let attempts = '';
  if (state.showAttempts) {
    attempts = state.attempts === null ? '<p role="status">Loading attempts…</p>'
      : state.attempts.length ? `<ul>${state.attempts.map(renderAttempt).join('')}</ul>` : '<p class="hd-note">No rejected attempts.</p>';
  }
  return `<section data-activity-panel>${status}<h3>History</h3>${history}${diff}`
    + `<h3>Rejected attempts</h3><button type="button" class="hd-btn" data-activity-action="toggle-attempts" aria-expanded="${state.showAttempts}"${state.attemptsBusy ? ' disabled' : ''}>${state.showAttempts ? 'Hide rejected attempts' : 'Show rejected attempts'}</button>${attempts}</section>`;
}

export function mountActivity(container, client, artifactId, { versions = [], signal } = {}) {
  const aborter = new AbortController();
  const state = { versions, selected: null, diff: null, diffBusy: false, showAttempts: false, attempts: null, attemptsBusy: false, error: '' };
  let disposed = false, diffTicket = 0;
  const render = () => { if (!disposed && container.isConnected) container.innerHTML = renderActivityPanel(state); };

  const select = async versionId => {
    if (disposed || state.diffBusy) return;
    const ticket = ++diffTicket;
    state.selected = versionId; state.diff = null; state.diffBusy = true; state.error = ''; render();
    try {
      const diff = await client.artifactVersionDiff(artifactId, versionId, { signal: aborter.signal });
      if (disposed || ticket !== diffTicket) return;
      state.diff = diff;
    } catch (error) {
      if (!disposed && ticket === diffTicket) state.error = `Changes could not be loaded: ${failure(error)}`;
    } finally {
      if (ticket === diffTicket) { state.diffBusy = false; render(); }
    }
  };
  // The attempts list is fetched on the first reveal only; hiding and showing again reuses it.
  const toggleAttempts = async () => {
    if (disposed || state.attemptsBusy) return;
    state.showAttempts = !state.showAttempts;
    if (state.showAttempts && state.attempts === null) {
      state.attemptsBusy = true; state.error = ''; render();
      try {
        const data = await client.artifactCollabActivity(artifactId, { signal: aborter.signal });
        if (disposed) return;
        state.attempts = Array.isArray(data?.attempts) ? data.attempts : [];
      } catch (error) {
        if (disposed) return;
        state.showAttempts = false; state.error = `Attempts could not be loaded: ${failure(error)}`;
      } finally { state.attemptsBusy = false; }
    }
    render();
  };

  const onClick = event => {
    const button = event.target.closest?.('[data-activity-action]');
    if (!button || button.disabled) return;
    if (button.dataset.activityAction === 'select') void select(button.dataset.versionId);
    else if (button.dataset.activityAction === 'toggle-attempts') void toggleAttempts();
  };
  container.addEventListener('click', onClick);
  const dispose = () => {
    if (disposed) return;
    disposed = true; aborter.abort();
    container.removeEventListener('click', onClick);
    signal?.removeEventListener?.('abort', dispose);
    container.replaceChildren();
  };
  const setVersions = next => { state.versions = next; render(); };
  if (signal?.aborted) { dispose(); return { dispose, setVersions }; }
  signal?.addEventListener?.('abort', dispose, { once: true });
  render();
  return { dispose, setVersions };
}
