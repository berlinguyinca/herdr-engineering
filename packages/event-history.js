// Per-entity history loads independently from the detail page. Cursors are
// opaque; older pages remain selected while the live stream reports new work.
import {morph, patchText, patchAttr} from './patch.js';
import {enter} from './motion.js';
import {eventKey} from './activity-view.js';
import {describeEvent} from './herdr-web-components/humanize.js';
import {formatTimestamp} from './herdr-web-components/format.js';
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {classifyDashboardError, dashboardErrorMessage} from './dashboard-load-state.js';

export function mountEventHistory(container, client, entityType, entityId, {
  missionTitles = async () => ({}), onStatus = () => {}, timeoutMs = 15000, slowMs = 350,
} = {}) {
  let disposed = false, page = null, before = null, titles = {}, latestKey = '', unseen = false;
  let pinned = false, failedNavigation = null;
  let pending = null, controller = null, generation = 0, background = false, slow = false, error = '';
  const newer = [];
  container.innerHTML = `<section data-event-history aria-label="Recorded events" style="overflow-wrap:anywhere;min-width:0">
    <p data-event-history-status role="status" aria-live="polite"></p>
    <div data-event-history-progress hidden><progress aria-label="Loading recorded events" style="width:100%"></progress></div>
    <nav class="hd-inline" aria-label="Recorded event pages">
      <button type="button" class="hd-btn hd-btn--sm" data-history-action="older" aria-label="Older recorded events">Older events</button>
      <button type="button" class="hd-btn hd-btn--sm" data-history-action="newer" aria-label="Newer recorded events">Newer events</button>
      <button type="button" class="hd-btn hd-btn--sm" data-history-action="latest" aria-label="Return to latest recorded events" hidden>Latest events</button>
      <button type="button" class="hd-btn hd-btn--sm" data-history-action="retry" hidden>Retry recorded events</button>
    </nav>
    <div data-event-history-rows role="list" aria-label="Recorded event entries" tabindex="0" style="max-height:480px;overflow:auto"></div>
  </section>`;
  const el = name => container.querySelector(`[data-event-history-${name}]`);
  const button = action => container.querySelector(`[data-history-action="${action}"]`);
  const skeleton = '<div class="hd-skeleton" data-skeleton aria-hidden="true" style="min-height:64px"></div>';

  function toggle(element, property, value) {
    if (element[property] !== Boolean(value)) element[property] = Boolean(value);
  }
  function draw(animate = false) {
    if (disposed || !container.isConnected) return;
    const loading = Boolean(pending);
    const message = error || (!page ? 'Loading recorded events…' : !page.events.length
      ? 'No events for this entity yet.' : before !== null || pinned
        ? `Reading older recorded events.${unseen ? ' New events are available.' : ''}`
        : `${page.events.length} recent recorded events.${page.has_more ? ' Older events are available.' : ''}`);
    patchText(el('status'), message);
    // A background poll keeps the readable page quiet; initial/page navigation
    // owns the progress indicator because the requested page is not ready yet.
    toggle(el('progress'), 'hidden', !(loading && slow && !background));
    patchAttr(el('rows'), 'aria-busy', String(loading && !page));
    if (page) {
      const previousKeys = new Set([...el('rows').children].map(child => child.dataset.key));
      const html = page.events.map(event => {
        const description = describeEvent(event, {missionTitles: titles});
        return `<div class="hd-card" role="listitem" data-key="${esc(eventKey(event))}"><div class="hd-card__title">${esc(description.title)}</div><div class="hd-card__sub">${esc(description.detail)}</div><div class="hd-card__sub">seq ${esc(event.sequence ?? 'unknown')} · ${esc(formatTimestamp(event.source_timestamp))}</div></div>`;
      }).join('');
      morph(el('rows'), html);
      if (animate) for (const child of el('rows').children) {
        if (!previousKeys.has(child.dataset.key)) enter(child);
      }
    } else if (!error) {
      if (el('rows').innerHTML !== skeleton) el('rows').innerHTML = skeleton;
    } else el('rows').replaceChildren();
    toggle(button('older'), 'disabled', !page?.has_more || (loading && !background));
    toggle(button('newer'), 'disabled', !newer.length || (loading && !background));
    toggle(button('latest'), 'hidden', before === null && !unseen && !pinned);
    toggle(button('latest'), 'disabled', loading && !background);
    toggle(button('retry'), 'hidden', !error);
    toggle(button('retry'), 'disabled', loading && !background);
  }

  function load(target, action = 'refresh') {
    if (disposed || document.hidden || !container.isConnected) return Promise.resolve();
    const userAction = ['older', 'newer', 'latest', 'retry'].includes(action);
    if (pending) {
      if (!userAction || !background) return pending;
      controller.abort(); // A live check must never consume a navigation click.
    }
    const request = new AbortController(); controller = request;
    const revision = ++generation;
    const started = Date.now();
    background = action === 'refresh' && Boolean(page);
    const rows = el('rows');
    const selection = document.getSelection?.();
    const reading = rows.scrollHeight - rows.scrollTop - rows.clientHeight > 8
      || Boolean(selection?.toString() && rows.contains(selection.anchorNode));
    const probe = background && (before !== null || pinned || reading);
    slow = false; error = '';
    const query = new URLSearchParams({entity_type: entityType, entity_id: entityId,
      limit: probe ? '1' : '50'});
    if (target !== null && !probe) query.set('before', target);
    const slowTimer = setTimeout(() => {if (revision === generation) {slow = true; draw();}}, slowMs);
    const deadline = setTimeout(() => request.abort(new DOMException('Recorded events timed out', 'TimeoutError')), timeoutMs);
    pending = Promise.resolve().then(async () => {
      try {
        const response = await client.request(`/api/v1/events?${query}`, {signal: request.signal});
        if (disposed || revision !== generation || request.signal.aborted || !container.isConnected) return;
        if (!Array.isArray(response?.events)) throw new Error('Invalid recorded events response');
        if (probe) {
          const event = response.events.at(-1);
          unseen = unseen || Boolean(event && eventKey(event) !== latestKey);
        } else {
          if (action === 'older') newer.push({before, page});
          if (action === 'latest') newer.length = 0;
          before = target;
          page = response;
          pinned = false;
          if (userAction && action !== 'retry') rows.scrollTop = 0;
          if (before === null) {
            latestKey = response.events.length ? eventKey(response.events.at(-1)) : '';
            unseen = false;
          }
        }
        failedNavigation = null;
        onStatus(true);
      } catch (failure) {
        if (disposed || revision !== generation || document.hidden) return;
        const cause = request.signal.aborted ? request.signal.reason : failure;
        const detail = classifyDashboardError(cause, Date.now() - started);
        error = dashboardErrorMessage('Recorded events', detail);
        failedNavigation = {target, action};
        if (failure?.status === 409) error += ' Return to the latest events to refresh this history.';
        if (failure?.requestId) error += ` Request ${failure.requestId}.`;
        onStatus(false, error);
      } finally {
        clearTimeout(slowTimer); clearTimeout(deadline);
        if (revision === generation) {pending = null; controller = null; slow = false; draw(background && !probe);}
      }
    });
    draw();
    return pending;
  }

  async function click(event) {
    const control = event.target.closest?.('[data-history-action]');
    if (!control || !container.contains(control) || control.disabled) return;
    const action = control.dataset.historyAction;
    const ownedFocus = document.activeElement === control;
    const target = action === 'older' ? page?.next_cursor
      : action === 'newer' ? newer.at(-1)?.before : action === 'latest' ? null
        : failedNavigation ? failedNavigation.target : before;
    if (target === undefined) return;
    if (action === 'newer') {
      const previous = newer.pop();
      before = previous.before; page = previous.page; pinned = true;
      error = ''; failedNavigation = null; draw(); el('rows').scrollTop = 0;
    } else await load(target, action === 'retry' && failedNavigation ? failedNavigation.action : action);
    if (disposed) return;
    const active = document.activeElement;
    if (active === control || (ownedFocus && active === document.body)) {
      const same = button(action);
      const fallback = before === null ? button('older') : button('newer');
      if (!same || !fallback) return;
      if (!same.hidden && !same.disabled) same.focus({preventScroll: true});
      else if (!fallback.disabled) fallback.focus({preventScroll: true});
      else {el('status').tabIndex = -1; el('status').focus({preventScroll: true});}
    }
  }
  function visibility() {if (document.hidden) controller?.abort(); else void load(before);}
  container.addEventListener('click', click);
  document.addEventListener('visibilitychange', visibility);
  Promise.resolve().then(missionTitles).then(value => {
    if (!disposed) {titles = value || {}; draw();}
  }).catch(() => {}); // Names are optional enrichment, never a history dependency.
  const ready = load(null, 'initial');
  return {ready, refresh: () => load(before), html: () => container.innerHTML,
    dispose() {disposed = true; generation++; controller?.abort();
      container.removeEventListener('click', click); document.removeEventListener('visibilitychange', visibility);}};
}
