import { escapeHtml as esc } from './herdr-web-components/util.js';

export const HEAD_MOVED = 'The head moved — reload and try again';

const parentsOf = version => {
  const list = Array.isArray(version.parent_version_ids) && version.parent_version_ids.length
    ? version.parent_version_ids
    : version.parent_version_id ? [version.parent_version_id] : [];
  return list.filter(Boolean);
};

export function renderVersionRow(version, { headVersionId, canRestore = false, busy = false } = {}) {
  const id = version.version_id;
  const isHead = id === headVersionId;
  const parents = parentsOf(version);
  const merge = parents.length >= 2;
  const parentHtml = parents.length
    ? `<p>${merge ? '<strong data-version-merge>Merge</strong> of parents' : 'Parent'}: ${parents.map(parent => `<code>${esc(parent)}</code>`).join(' + ')}</p>`
    : '';
  const restore = canRestore && !isHead
    ? `<button type="button" class="hd-btn" data-history-action="restore" data-version-id="${esc(id)}"${busy ? ' disabled' : ''}>Restore this version</button>`
    : '';
  return `<li data-version-row data-version-id="${esc(id)}"><p><strong>v${esc(version.version_number)}</strong> · ${esc(version.state || 'unknown')}${isHead ? ' · <span class="hd-badge" data-version-head>current</span>' : ''} · ${esc(version.created_by || 'unknown author')} · <time>${esc(version.created_at || '')}</time> · <code>${esc(id)}</code></p>${parentHtml}${restore}</li>`;
}

export function renderVersionList(versions, { headVersionId, canRestore = false, busy = false } = {}) {
  return `<ul>${versions.map(version => renderVersionRow(version, { headVersionId, canRestore, busy })).join('')}</ul>`;
}

export function createHistoryController(client, artifactId, { versions = [], headVersionId, canRestore = false, signal, onChange = () => {}, onVersions = () => {} } = {}) {
  const state = { versions, headVersionId, canRestore, busy: false, notice: '', error: '' };
  const aborter = new AbortController();
  const ctrl = { state, disposed: false };
  const live = () => !ctrl.disposed;
  const changed = () => { if (live()) onChange(); };
  const refresh = async () => {
    const [artifact, list] = await Promise.all([
      client.getArtifact(artifactId, { signal: aborter.signal }),
      client.artifactVersions(artifactId, { signal: aborter.signal, summary: true }),
    ]);
    if (!live()) return false;
    if (!Array.isArray(list?.versions)) throw new Error('Invalid version list response');
    state.versions = list.versions; state.headVersionId = artifact.current_version_id;
    onVersions(state.versions, state.headVersionId);
    return true;
  };
  ctrl.restore = async versionId => {
    if (!live() || state.busy || !state.canRestore || versionId === state.headVersionId) return;
    state.busy = true; state.notice = ''; state.error = ''; changed();
    try {
      await client.restoreArtifactVersion(artifactId, versionId, { signal: aborter.signal });
      if (!live()) return;
      if (await refresh()) {
        const head = state.versions.find(version => version.version_id === state.headVersionId);
        state.notice = head ? `Restored as v${head.version_number}` : 'Restored';
      }
    } catch (error) {
      if (!live()) return;
      if (error?.status === 409) {
        state.error = HEAD_MOVED;
        try { await refresh(); } catch { /* the message already says to reload */ }
      } else state.error = error?.message || 'Restore failed';
    } finally {
      if (live()) { state.busy = false; changed(); }
    }
  };
  ctrl.dispose = () => {
    if (ctrl.disposed) return;
    ctrl.disposed = true; aborter.abort();
    signal?.removeEventListener?.('abort', ctrl.dispose);
  };
  if (signal?.aborted) ctrl.dispose(); else signal?.addEventListener?.('abort', ctrl.dispose, { once: true });
  return ctrl;
}

export function mountHistory(container, client, artifactId, { versions = [], headVersionId, canRestore = false, signal, onVersions } = {}) {
  const render = () => {
    if (ctrl.disposed || !container.isConnected) return;
    const { state } = ctrl;
    container.innerHTML = `<div role="status" data-history-status>${state.error ? `<p role="alert">${esc(state.error)}</p>` : state.notice ? `<p>${esc(state.notice)}</p>` : ''}</div>${renderVersionList(state.versions, { headVersionId: state.headVersionId, canRestore: state.canRestore, busy: state.busy })}`;
  };
  const ctrl = createHistoryController(client, artifactId, { versions, headVersionId, canRestore, signal, onChange: render, onVersions });
  const onClick = event => {
    const button = event.target.closest?.('[data-history-action]');
    if (!button || !container.contains(button) || button.disabled) return;
    if (button.dataset.historyAction === 'restore') void ctrl.restore(button.dataset.versionId);
  };
  container.addEventListener('click', onClick);
  const dispose = () => {
    ctrl.dispose();
    container.removeEventListener('click', onClick);
    container.replaceChildren();
  };
  if (signal?.aborted) { dispose(); return { dispose, controller: ctrl }; }
  signal?.addEventListener?.('abort', dispose, { once: true });
  render();
  return { dispose, controller: ctrl };
}
