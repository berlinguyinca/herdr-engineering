// Feedback tab of the artifact share panel: annotations that lab viewers left on shared versions (owner view).
//
// Every value that came from a viewer (email, body, quote, file name) reaches the DOM only as escaped text.
// Server error text is never shown: a failed read says "Feedback is unavailable" and offers Retry.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatWhen } from './artifact-collaborators.js';

export const STATUS_FILTERS = [['open', 'Open'], ['addressed', 'Addressed'], ['dismissed', 'Dismissed'], ['withdrawn', 'Withdrawn'], ['all', 'All']];
export const EMPTY_TEXT = 'No annotations yet. Lab viewers can annotate a shared version by switching a lab link to Annotate; public links cannot.';
const PAGE = 50;
const UNAVAILABLE = 'Feedback is unavailable right now.';
const NOT_FOUND = 'Feedback for this artifact could not be found.';

const pct = value => `${Math.round(Number(value) * 1000) / 10}%`;
const box = r => (r && [r.x, r.y, r.w, r.h].every(v => Number.isFinite(Number(v)))
  ? `${pct(r.x)}, ${pct(r.y)} (${pct(r.w)} × ${pct(r.h)})` : '');

export function renderAnchor(anchor) {
  const a = anchor && typeof anchor === 'object' ? anchor : {};
  let what;
  if (a.kind === 'text_quote') what = `…${esc(a.prefix ?? '')}<mark>${esc(a.exact ?? '')}</mark>${esc(a.suffix ?? '')}…`;
  else if (a.kind === 'pdf_page') {
    const region = box(a.region);
    what = `Page ${esc(a.page ?? '?')}${a.exact ? ` <mark>${esc(a.exact)}</mark>` : ''}${region ? ` Region ${esc(region)}` : ''}`;
  } else if (a.kind === 'image_region') what = `Region ${esc(box(a))}`;
  else what = 'Whole file';
  return `${what} in <code>${esc(a.file ?? '')}</code>`;
}

export function renderAnnotationRow(annotation, { artifactId = '', busy = false } = {}) {
  const a = annotation || {};
  const addressed = a.addressed_in_version_id
    ? ` · <a href="#/artifacts/${encodeURIComponent(artifactId)}?version=${encodeURIComponent(a.addressed_in_version_id)}">addressed in v${esc(a.addressed_in_version_number ?? '?')}</a>` : '';
  const dismiss = a.status === 'open'
    ? `<button type="button" class="hd-btn" data-feedback-action="dismiss" data-annotation-id="${esc(a.annotation_id ?? '')}"${busy ? ' disabled' : ''}>Dismiss</button>` : '';
  const when = formatWhen(a.created_at);
  return `<li data-feedback-row><p style="overflow-wrap:anywhere"><strong><bdi>${esc(a.author_email ?? 'unknown')}</bdi></strong>${a.author_role === 'owner' ? ' <span class="hd-badge">owner</span>' : ''}`
    + ` · v${esc(a.version_number ?? '?')} · ${renderAnchor(a.anchor)}${a.edited_at ? ' <em>(edited)</em>' : ''}${when ? ` · <time>${esc(when)}</time>` : ''}</p>`
    + `<p dir="auto" style="white-space:pre-wrap;overflow-wrap:anywhere;unicode-bidi:plaintext">${esc(a.body ?? '')}</p>`
    + `<p class="hd-note">${esc(a.status ?? '')}${addressed}</p>${dismiss}</li>`;
}

const friendly = error => (error?.status === 404 ? NOT_FOUND : UNAVAILABLE);

export function mountFeedbackPanel(container, client, artifactId, { signal } = {}) {
  const aborter = new AbortController();
  const state = { status: 'open', rows: [], next: null, loading: false, loadingMore: false, error: '', note: '', busy: new Set() };
  let disposed = false, ticket = 0;

  const render = () => {
    if (disposed || !container.isConnected) return;
    const filter = `<label>Show <select class="hd-input" data-feedback-filter>${STATUS_FILTERS.map(([v, l]) => `<option value="${v}"${v === state.status ? ' selected' : ''}>${l}</option>`).join('')}</select></label>`;
    let body;
    if (state.error) body = `<herdr-error message="${esc(state.error)}"></herdr-error><button type="button" class="hd-btn" data-feedback-action="retry">Retry</button>`;
    else if (state.loading) body = '<p role="status">Loading feedback…</p>';
    else if (!state.rows.length) body = `<p class="hd-note">${esc(EMPTY_TEXT)}</p>`;
    else body = `<ul>${state.rows.map(row => renderAnnotationRow(row, { artifactId, busy: state.busy.has(row.annotation_id) })).join('')}</ul>`;
    const more = state.next && !state.error && !state.loading
      ? `<button type="button" class="hd-btn" data-feedback-action="more"${state.loadingMore ? ' disabled' : ''}>Load more</button>` : '';
    container.innerHTML = `<section data-feedback-panel>${filter}${state.note ? `<p role="status">${esc(state.note)}</p>` : ''}${body}${more}</section>`;
  };

  const load = async ({ append = false } = {}) => {
    if (disposed) return;
    const mine = ++ticket;
    state.error = ''; state.note = '';
    if (append) state.loadingMore = true; else { state.loading = true; state.rows = []; state.next = null; }
    render();
    try {
      const page = await client.listArtifactAnnotations(artifactId, { status: state.status, limit: PAGE, before: append ? state.next : undefined, signal: aborter.signal });
      if (disposed || mine !== ticket) return;
      const rows = Array.isArray(page?.annotations) ? page.annotations : [];
      state.rows = append ? state.rows.concat(rows) : rows;
      state.next = page?.next ?? null;
    } catch (error) {
      if (disposed || mine !== ticket) return;
      state.error = friendly(error);
    } finally {
      if (!disposed && mine === ticket) { state.loading = false; state.loadingMore = false; render(); }
    }
  };

  const dismiss = async id => {
    if (disposed || !id || state.busy.has(id)) return;
    if (typeof globalThis.confirm !== 'function' || !globalThis.confirm('Dismiss this annotation? It will no longer appear as open.')) return;
    state.busy.add(id); render();
    try {
      await client.dismissArtifactAnnotation(artifactId, id, { signal: aborter.signal });
      if (disposed) return;
      state.busy.delete(id);
      await load();
    } catch (error) {
      if (disposed) return;
      state.busy.delete(id);
      const note = error?.status === 409 ? 'That annotation is no longer open.' : 'The annotation could not be dismissed.';
      await load();
      if (!disposed && !state.error) { state.note = note; render(); }
    }
  };

  const onClick = event => {
    const button = event.target.closest?.('[data-feedback-action]');
    if (!button || button.disabled) return;
    const action = button.dataset.feedbackAction;
    if (action === 'retry') void load();
    else if (action === 'more') { if (!state.loadingMore) void load({ append: true }); }
    else if (action === 'dismiss') void dismiss(button.dataset.annotationId);
  };
  const onChange = event => {
    const select = event.target.closest?.('[data-feedback-filter]');
    if (!select || !STATUS_FILTERS.some(([v]) => v === select.value)) return;
    state.status = select.value; void load();
  };
  container.addEventListener('click', onClick);
  container.addEventListener('change', onChange);
  const dispose = () => {
    if (disposed) return;
    disposed = true; aborter.abort();
    container.removeEventListener('click', onClick);
    container.removeEventListener('change', onChange);
    signal?.removeEventListener?.('abort', dispose);
    container.replaceChildren();
  };
  if (signal?.aborted) { dispose(); return { dispose }; }
  signal?.addEventListener?.('abort', dispose, { once: true });
  void load();
  return { dispose };
}
