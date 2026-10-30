// Share card: viewer links for one artifact (spec 2026-10-30 artifact sharing GUI, section A).
//
// Three layers: pure renderers (HTML strings built only from escaped values), a DOM-free controller that owns the
// calls and their in-flight guards, and a thin mount that binds the controller to fixed DOM regions.
// The approve URL exists only in controller.state.link and in the value of the one dialog input; it never appears in
// any rendered HTML string, attribute, storage, URL or log, and closing or disposing the panel drops it.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export const AUDIENCES = [['private', 'Private'], ['lab', 'Group (lab)'], ['public', 'Public']];
const AUDIENCE_LABEL = Object.fromEntries(AUDIENCES);
const PUBLIC_DEFAULT_DAYS = '30';
const REVOCABLE = new Set(['pending', 'live']);
const MAX_LAB_DAYS = 3650;

const humanSize = value => {
  if (value === null || value === undefined || value === '') return 'size unknown';
  const size = Number(value);
  if (!Number.isFinite(size)) return 'size unknown';
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  return `${(size / 1024 ** 3).toFixed(1)} GB`;
};
const audienceLabel = audience => AUDIENCE_LABEL[audience] || String(audience ?? 'unknown');
const message = error => String(error?.message || 'The request could not be completed.');
const splitList = value => String(value || '').split(/[,\n]/).map(item => item.trim()).filter(Boolean);
const wrap = 'overflow-wrap:anywhere';

export function defaultForm(headVersionId) {
  return { versionId: headVersionId || '', audience: 'private', emails: '', groups: '', publicDays: PUBLIC_DEFAULT_DAYS, labDays: '', title: '', entry: '' };
}

const shareableVersions = versions => (versions || []).filter(version => version && version.version_id && (!version.state || version.state === 'ready'));

// Which form buttons are disabled; shared by the renderer and the mount (which updates them without re-rendering inputs).
export function formButtons(state) {
  const privateAudience = state.form.audience === 'private';
  return {
    review: Boolean(state.busy || privateAudience || !state.form.versionId),
    generate: Boolean(state.busy || privateAudience || !state.pending),
  };
}

const days = (value, low, high, label) => {
  const text = String(value ?? '').trim();
  const number = /^\d+$/.test(text) ? Number(text) : NaN;
  if (!Number.isSafeInteger(number) || number < low || number > high) throw new Error(`${label} must be a whole number of days from ${low} to ${high}.`);
  return number;
};

// The create body for the current form; throws an Error whose message is shown to the user.
export function buildShareRequest(form, { allowEntry = true } = {}) {
  if (form.audience === 'private') throw new Error('Private: No link is created.');
  if (!form.versionId) throw new Error('Choose a ready version to share.');
  const body = { version_id: form.versionId, audience: form.audience };
  if (form.audience === 'public') {
    body.expires_in_days = days(form.publicDays, 1, 90, 'A public link expiry');
  } else if (form.audience === 'lab') {
    const emails = splitList(form.emails), groups = splitList(form.groups);
    if (!emails.length && !groups.length) throw new Error('A group link needs at least one email or group.');
    if (emails.length) body.allow_emails = emails;
    if (groups.length) body.allow_groups = groups;
    if (String(form.labDays ?? '').trim()) body.expires_in_days = days(form.labDays, 1, MAX_LAB_DAYS, 'A group link expiry');
  } else {
    throw new Error('Choose who can open the link.');
  }
  const title = String(form.title || '').trim();
  if (title) body.title = title;
  const entry = String(form.entry || '').trim();
  if (allowEntry && entry) body.entry = entry;
  return body;
}

export function renderShareForm(state) {
  const { form } = state;
  const disabled = formButtons(state);
  const ready = shareableVersions(state.versions);
  const versionOptions = ready.map(version => `<option value="${esc(version.version_id)}"${version.version_id === form.versionId ? ' selected' : ''}>v${esc(version.version_number ?? '?')} · ${esc(version.version_id)}${version.version_id === state.headVersionId ? ' (current)' : ''}</option>`).join('');
  const audience = AUDIENCES.map(([value, label]) => `<option value="${value}"${value === form.audience ? ' selected' : ''}>${esc(label)}</option>`).join('');
  let fields = '';
  if (form.audience === 'private') {
    fields = '<p data-share-private>Private: No link is created. Only people with access to this artifact here can see it.</p>';
  } else if (form.audience === 'public') {
    fields = `<label style="display:block">Expires after (days, 1–90)<input class="hd-input" type="number" inputmode="numeric" data-share-field="publicDays" min="1" max="90" step="1" required value="${esc(form.publicDays)}"></label><p class="hd-note">Anyone with the link can open it until it expires.</p>`;
  } else {
    fields = `<label style="display:block">Emails (comma or new line separated)<textarea class="hd-input" rows="2" data-share-field="emails" style="width:100%">${esc(form.emails)}</textarea></label><label style="display:block">Groups (comma or new line separated)<textarea class="hd-input" rows="2" data-share-field="groups" style="width:100%">${esc(form.groups)}</textarea></label><label style="display:block">Expires after (days, optional, 1–${MAX_LAB_DAYS})<input class="hd-input" type="number" inputmode="numeric" data-share-field="labDays" min="1" max="${MAX_LAB_DAYS}" step="1" placeholder="No expiry" value="${esc(form.labDays)}"></label>`;
  }
  const extra = form.audience === 'private' ? '' : `<label style="display:block">Title (optional)<input class="hd-input" data-share-field="title" maxlength="240" value="${esc(form.title)}" style="width:100%"></label>${state.allowEntry ? `<label style="display:block">Entry file (optional)<input class="hd-input" data-share-field="entry" value="${esc(form.entry)}" style="width:100%"></label>` : ''}`;
  return `<form data-share-form novalidate><div class="hd-inline" style="flex-wrap:wrap"><label>Version<select class="hd-input" data-share-field="versionId">${versionOptions || '<option value="">No ready version</option>'}</select></label><label>Audience<select class="hd-input" data-share-field="audience">${audience}</select></label></div>${fields}${extra}<div class="hd-inline" style="flex-wrap:wrap"><button type="button" class="hd-btn" data-share-action="review"${disabled.review ? ' disabled' : ''}>Review</button><button type="button" class="hd-btn" data-share-action="generate"${disabled.generate ? ' disabled' : ''}>Generate link</button></div></form>`;
}

export function renderSummary(summary) {
  if (!summary) return '';
  const files = Array.isArray(summary.files) ? summary.files : [];
  const count = Number.isSafeInteger(summary.file_count) ? summary.file_count : files.length;
  const more = count - files.length;
  const warnings = Array.isArray(summary.warnings) ? summary.warnings : [];
  return `<div data-share-summary style="${wrap}"><p><strong>${esc(count)}</strong> file(s), ${esc(humanSize(summary.total_bytes))} in total${summary.audience ? ` · ${esc(audienceLabel(summary.audience))}` : ''}${summary.expires_at ? ` · expires ${esc(summary.expires_at)}` : ''}${summary.entry ? ` · opens <code>${esc(summary.entry)}</code>` : ''}</p>${files.length ? `<ul data-share-files>${files.map(file => `<li><code>${esc(file.name)}</code> · ${esc(humanSize(file.size_bytes))}</li>`).join('')}</ul>` : ''}${more > 0 ? `<p>…and ${esc(more)} more file${more === 1 ? '' : 's'}.</p>` : ''}${warnings.length ? `<ul data-share-warnings role="note">${warnings.map(warning => `<li>Warning: ${esc(warning)}</li>`).join('')}</ul>` : ''}</div>`;
}

export function eventCounts(events) {
  const counts = { opened: 0, denied: 0 };
  for (const event of events || []) if (event?.kind === 'opened' || event?.kind === 'denied') counts[event.kind] += 1;
  return counts;
}

export function renderEvents(events) {
  if (!events?.length) return '<p>No events recorded.</p>';
  return `<ul data-share-events-list style="${wrap}">${events.map(event => {
    const detail = typeof event.detail === 'string' ? event.detail : event.detail ? JSON.stringify(event.detail) : '';
    return `<li>${esc(event.at || '')} · <strong>${esc(event.kind || 'event')}</strong>${event.actor ? ` · ${esc(event.actor)}` : ''}${detail && detail !== '{}' ? ` · <code>${esc(detail)}</code>` : ''}</li>`;
  }).join('')}</ul>`;
}

// eventsEntry: undefined (not loaded) or { open, loading, events, error } from the controller.
export function renderShareRow(share, eventsEntry) {
  const recipients = share.audience === 'public' ? ['Anyone with the link'] : [...(share.allow_emails || []), ...(share.allow_groups || [])];
  const counts = Array.isArray(eventsEntry?.events) ? eventCounts(eventsEntry.events) : null;
  const id = esc(share.share_id);
  let drawer = '';
  if (eventsEntry?.open) {
    drawer = `<div data-share-events>${eventsEntry.loading ? '<p role="status">Loading events…</p>' : eventsEntry.error ? `<p role="alert">Events unavailable: ${esc(eventsEntry.error)}</p>` : renderEvents(eventsEntry.events)}</div>`;
  }
  return `<li data-share-row data-share-id="${id}" style="${wrap}"><p><strong>${esc(share.title || share.share_id)}</strong> · ${esc(audienceLabel(share.audience))} · <span data-share-state>${esc(share.state || 'unknown')}</span></p><p>Recipients: ${recipients.map(item => esc(item)).join(', ') || 'none'} · Version <code>${esc(share.version_id || '')}</code> · Expires: ${share.expires_at ? esc(share.expires_at) : 'Never'}</p><p data-share-counts>${counts ? `${counts.opened} opened · ${counts.denied} denied` : 'Open the events to see opened/denied counts.'}</p><div class="hd-inline" style="flex-wrap:wrap"><button type="button" class="hd-btn" data-share-action="events" data-share-id="${id}" aria-expanded="${eventsEntry?.open ? 'true' : 'false'}">${eventsEntry?.open ? 'Hide events' : 'Events'}</button>${REVOCABLE.has(share.state) ? `<button type="button" class="hd-btn" data-share-action="revoke" data-share-id="${id}">Revoke</button>` : ''}</div>${drawer}</li>`;
}

// The dialog holds an empty readonly input; the mount sets its .value so the URL never enters any HTML string.
export function renderLinkDialog(summary) {
  return `<div role="dialog" aria-label="New share link" data-share-link class="hd-card" style="${wrap}"><p><strong>Link created.</strong> This link cannot be shown again: copy it now. To share again, generate a new link.</p><input class="hd-input" type="text" data-share-url readonly aria-label="Share link" style="width:100%;box-sizing:border-box"><div class="hd-inline" style="flex-wrap:wrap"><button type="button" class="hd-btn" data-share-action="copy">Copy</button><button type="button" class="hd-btn" data-share-action="close">Close</button></div><p role="status" data-share-copy-status></p>${renderSummary(summary)}</div>`;
}

// ------------------------------------------------------------------------------------------------ controller

export function createShareController(client, artifactId, { versions = [], headVersionId, allowEntry = true, signal, onChange = () => {} } = {}) {
  const ready = shareableVersions(versions);
  const head = ready.some(version => version.version_id === headVersionId) ? headVersionId : ready[0]?.version_id;
  const state = {
    form: defaultForm(head), versions, headVersionId, allowEntry,
    shares: null, listError: null, listLoading: false,
    busy: false, pending: null, link: null, error: null, notice: '',
    events: new Map(),
  };
  const aborter = new AbortController();
  let listGeneration = 0, actionGeneration = 0;
  const ctrl = { state, disposed: false };
  const live = () => !ctrl.disposed;
  const bounded = () => AbortSignal.any([aborter.signal, AbortSignal.timeout(15000)]);
  const changed = kind => { if (live()) onChange(kind); };

  ctrl.refresh = async () => {
    if (!live()) return;
    const ticket = ++listGeneration;
    state.listLoading = true; changed('list');
    try {
      const result = await client.listArtifactShares(artifactId, { signal: bounded() });
      if (!live() || ticket !== listGeneration) return;
      if (!Array.isArray(result?.shares)) throw new Error('Invalid share list response');
      state.shares = result.shares; state.listError = null;
    } catch (error) {
      if (!live() || ticket !== listGeneration) return;
      state.shares = null; state.listError = message(error);
    } finally {
      if (live() && ticket === listGeneration) { state.listLoading = false; changed('list'); }
    }
  };

  // One state-changing call at a time; the flag is set before the first await so a double click is a no-op.
  const action = async (run, notice) => {
    if (!live() || state.busy) return;
    state.busy = true; state.error = null; state.notice = notice || '';
    const ticket = ++actionGeneration;
    changed('busy');
    try { await run(() => live() && ticket === actionGeneration); }
    catch (error) { if (live()) state.error = message(error); }
    finally {
      if (live()) { state.busy = false; state.notice = ''; changed('busy'); void ctrl.refresh(); }
    }
  };

  ctrl.setField = (name, value) => {
    if (!live() || !(name in state.form)) return;
    state.form[name] = String(value ?? '');
    if (name === 'audience' && state.form.audience === 'public' && !state.form.publicDays) state.form.publicDays = PUBLIC_DEFAULT_DAYS;
    // The reviewed summary no longer describes the form: Generate must wait for a new Review.
    const hadPending = Boolean(state.pending);
    state.pending = null;
    if (name === 'audience') changed('form');
    else if (hadPending) changed('pending');
  };

  ctrl.review = async () => {
    if (!live() || state.busy || state.form.audience === 'private') return;
    let body;
    try { body = buildShareRequest(state.form, { allowEntry }); }
    catch (error) { state.error = message(error); changed('error'); return; }
    state.link = null; state.pending = null;
    await action(async current => {
      const result = await client.createArtifactShare(artifactId, body, { signal: bounded() });
      if (!current()) return;
      if (!result?.share?.share_id) throw new Error('The server did not return the new share.');
      state.pending = { shareId: result.share.share_id, summary: result.summary || null };
    }, 'Checking what this link would expose…');
  };

  ctrl.generate = async () => {
    if (!live() || state.busy || !state.pending || state.form.audience === 'private') return;
    // Take the reviewed share now: a failed or repeated click can never approve it (or another) twice.
    const { shareId } = state.pending;
    state.pending = null; state.link = null;
    await action(async current => {
      const result = await client.approveArtifactShare(artifactId, shareId, { signal: bounded() });
      if (!current()) return;
      if (typeof result?.url !== 'string' || !result.url) throw new Error('The server did not return a link.');
      state.link = { url: result.url, summary: result.summary || null };
    }, 'Generating the link…');
  };

  ctrl.revoke = async (shareId, ask = text => globalThis.confirm?.(text) ?? false) => {
    if (!live() || state.busy || !shareId) return;
    if (!ask('Revoke this link? People who have it will no longer be able to open it.')) return;
    await action(async () => { await client.revokeArtifactShare(artifactId, shareId, { signal: bounded() }); }, 'Revoking the link…');
  };

  ctrl.toggleEvents = async shareId => {
    if (!live() || !shareId) return;
    const entry = state.events.get(shareId);
    if (entry?.open) { entry.open = false; changed('events'); return; }
    if (entry) { entry.open = true; if (entry.loading || Array.isArray(entry.events)) { changed('events'); return; } }
    const next = entry || { open: true };
    next.loading = true; next.error = null;
    state.events.set(shareId, next); changed('events');
    try {
      const result = await client.artifactShareEvents(artifactId, shareId, { signal: bounded() });
      if (!live()) return;
      if (!Array.isArray(result?.events)) throw new Error('Invalid events response');
      next.events = result.events;
    } catch (error) {
      if (!live()) return;
      next.error = message(error);
    } finally {
      if (live()) { next.loading = false; changed('events'); }
    }
  };

  ctrl.closeLink = () => {
    if (!live()) return;
    state.link = null; changed('link');
    void ctrl.refresh();
  };

  ctrl.dispose = () => {
    if (ctrl.disposed) return;
    ctrl.disposed = true;
    state.link = null; state.pending = null;
    aborter.abort();
    signal?.removeEventListener?.('abort', ctrl.dispose);
  };

  if (signal?.aborted) ctrl.dispose(); else signal?.addEventListener?.('abort', ctrl.dispose, { once: true });
  return ctrl;
}

// ------------------------------------------------------------------------------------------------ mount

export function mountSharePanel(container, client, artifactId, { versions = [], headVersionId, signal, allowEntry = true } = {}) {
  container.innerHTML = '<section data-share-panel><p class="hd-note">Viewer links give people outside this dashboard read-only access to one pinned version.</p><div data-share-form-region></div><div role="status" data-share-status></div><div data-share-review-region></div><div data-share-link-region></div><h3>Links</h3><div data-share-list-region></div></section>';
  const region = name => container.querySelector(`[data-share-${name}-region]`);
  const status = container.querySelector('[data-share-status]');
  let formKey = null, shownLink = null;

  const render = () => {
    if (ctrl.disposed || !container.isConnected) return;
    const { state } = ctrl;
    const key = state.form.audience;
    if (key !== formKey) { region('form').innerHTML = renderShareForm(state); formKey = key; }
    else {
      const disabled = formButtons(state);
      for (const name of ['review', 'generate']) {
        const button = region('form').querySelector(`[data-share-action="${name}"]`);
        if (button) button.disabled = disabled[name];
      }
    }
    status.innerHTML = state.error ? `<p role="alert">${esc(state.error)}</p>` : state.notice ? `<p>${esc(state.notice)}</p>` : '';
    region('review').innerHTML = state.pending ? `<div class="hd-card" data-share-review><h3>Review before generating</h3>${renderSummary(state.pending.summary)}<p>Generate link publishes exactly these files.</p></div>` : '';
    if (state.link !== shownLink) {
      shownLink = state.link;
      const linkRegion = region('link');
      if (state.link) {
        linkRegion.innerHTML = renderLinkDialog(state.link.summary);
        linkRegion.querySelector('[data-share-url]').value = state.link.url;
      } else {
        linkRegion.replaceChildren();
      }
    }
    const list = region('list');
    if (state.shares) {
      list.innerHTML = state.shares.length ? `<ul class="hd-share-list" style="list-style:none;padding:0">${state.shares.map(share => renderShareRow(share, state.events.get(share.share_id))).join('')}</ul>` : '<herdr-empty message="No links yet"></herdr-empty>';
    } else if (state.listError) {
      list.innerHTML = `<herdr-error message="${esc(`Links unavailable: ${state.listError}`)}"></herdr-error><button type="button" class="hd-btn" data-share-action="retry">Retry</button>`;
    } else {
      list.innerHTML = '<p role="status">Loading links…</p>';
    }
  };

  const ctrl = createShareController(client, artifactId, { versions, headVersionId, allowEntry, signal, onChange: render });

  const onClick = event => {
    const button = event.target.closest?.('[data-share-action]');
    if (!button || !container.contains(button) || button.disabled) return;
    const id = button.dataset.shareId;
    switch (button.dataset.shareAction) {
      case 'review': void ctrl.review(); break;
      case 'generate': void ctrl.generate(); break;
      case 'revoke': void ctrl.revoke(id); break;
      case 'events': void ctrl.toggleEvents(id); break;
      case 'retry': void ctrl.refresh(); break;
      case 'close': ctrl.closeLink(); break;
      case 'copy': {
        const input = container.querySelector('[data-share-url]');
        const copyStatus = container.querySelector('[data-share-copy-status]');
        if (!input) return;
        const fallback = () => { input.focus(); input.select(); if (copyStatus) copyStatus.textContent = 'Selected: press Ctrl+C (⌘C) to copy.'; };
        if (navigator.clipboard?.writeText) {
          navigator.clipboard.writeText(input.value).then(() => { if (copyStatus) copyStatus.textContent = 'Copied.'; }, fallback);
        } else fallback();
        break;
      }
      default: break;
    }
  };
  const onField = event => {
    const field = event.target.closest?.('[data-share-field]');
    if (!field || !container.contains(field)) return;
    ctrl.setField(field.dataset.shareField, field.value);
  };
  const onSubmit = event => { if (event.target.closest?.('[data-share-form]')) event.preventDefault(); };
  container.addEventListener('click', onClick);
  container.addEventListener('input', onField);
  container.addEventListener('change', onField);
  container.addEventListener('submit', onSubmit);

  const dispose = () => {
    ctrl.dispose();
    container.removeEventListener('click', onClick);
    container.removeEventListener('input', onField);
    container.removeEventListener('change', onField);
    container.removeEventListener('submit', onSubmit);
    // Removing the panel removes the one-time URL with it.
    container.replaceChildren();
  };
  if (signal?.aborted) { dispose(); return { dispose }; }
  signal?.addEventListener?.('abort', dispose, { once: true });
  render();
  void ctrl.refresh();
  return { dispose };
}
