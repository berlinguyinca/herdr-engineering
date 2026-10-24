// Activity as a mounted view. Rows are keyed by event_id through patchList, so new events
// prepend (data-entering) without recreating existing rows. Selection is kept by event_id and the
// active type filter persists across refreshes. A user scrolled away from the top is not moved:
// new events wait behind an "N new" pill until it is clicked or the user scrolls back up.
import { patchList, patchText, patchAttr, morph } from './patch.js';
import { liveRefresher } from './refresh-bus.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { describeEvent } from './herdr-web-components/humanize.js';

const STYLE = `<style>
    .ac-workspace{display:grid;grid-template-columns:minmax(620px,1600px) minmax(320px,1fr);gap:var(--density-gap,16px);align-items:start;width:100%}
    .ac-stream{min-width:0}.ac-stream-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:8px}.ac-stream-head h2{margin:0}.ac-stream-head span,.ac-eyebrow{font-size:12px;color:var(--text-secondary)}
    .ac-new{position:sticky;top:8px;z-index:1;display:block;margin:0 auto 8px;min-height:32px;padding:4px 14px;border:1px solid var(--accent);border-radius:999px;background:var(--accent);color:var(--accent-contrast,#fff);font:inherit;font-size:13px;cursor:pointer}.ac-new[hidden]{display:none}
    .ac-list{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
    .ac-row{border-bottom:1px solid var(--border-subtle)}.ac-row:last-child{border-bottom:0}.ac-row[open]{background:var(--bg)}
    .ac-row summary{display:grid;grid-template-columns:minmax(140px,220px) minmax(0,1fr) auto;gap:14px;align-items:center;padding:10px 12px;cursor:pointer;list-style-position:inside;font-size:13px}
    .ac-row summary strong{overflow-wrap:anywhere}.ac-kind,.ac-time{font-size:12px;color:var(--text-secondary)}.ac-time{white-space:nowrap}
    .ac-detail{margin:0;padding:0 32px 8px;font-size:12px;line-height:1.5;color:var(--text-secondary);overflow-wrap:anywhere}.ac-payload{margin:0 32px 12px;max-height:240px;overflow:auto;white-space:pre-wrap;font-size:11px;line-height:1.45;background:var(--surface);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);padding:10px}
    .ac-row[data-muted=true]{opacity:.65}
    .ac-rail{position:sticky;top:16px;display:grid;gap:var(--density-gap,16px);min-width:0}.ac-panel{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:var(--density-panel-padding,16px)}.ac-panel h2{font-size:16px;margin:0 0 10px}.ac-panel p{color:var(--text-secondary);line-height:1.5}
    .ac-filter-list{display:flex;flex-wrap:wrap;gap:8px;min-width:0}.ac-filter{min-height:36px;max-width:100%;border:1px solid var(--border);border-radius:999px;background:var(--bg);color:var(--text);padding:6px 10px;cursor:pointer;overflow-wrap:anywhere;white-space:normal}.ac-filter[aria-pressed=true]{border-color:var(--accent);background:var(--accent);color:var(--accent-contrast,#fff)}
    .ac-repeat-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:14px 0 0;padding:0;list-style:none}.ac-repeat-list li{display:flex;justify-content:space-between;gap:8px;min-width:0;border-top:1px solid var(--border-subtle);padding-top:8px;font-size:12px}.ac-repeat-list li span{min-width:0;overflow-wrap:anywhere}.ac-repeat-list strong{flex:0 0 auto;font-size:14px}.ac-filter-status{margin:10px 0 0;font-size:12px;color:var(--text-secondary)}
    .ac-selected{min-width:0}.ac-selected>*{min-width:0;max-width:100%;box-sizing:border-box}.ac-selected-head{display:flex;justify-content:space-between;gap:12px;align-items:start}.ac-selected-head>div{min-width:0;max-width:100%}.ac-selected-head h2{min-width:0;margin:3px 0 0;overflow-wrap:anywhere;word-break:break-word}.ac-selected-head time{flex:0 0 auto;font-size:12px;color:var(--text-secondary);white-space:nowrap}.ac-selected-meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:14px 0}.ac-selected-meta div{min-width:0}.ac-selected-meta dt{font-size:11px;color:var(--text-secondary)}.ac-selected-meta dd{margin:3px 0 0;overflow-wrap:anywhere}.ac-selected-detail{overflow-wrap:anywhere;word-break:break-word}.ac-selected .ac-payload{margin:12px 0 0;max-width:100%;max-height:360px;box-sizing:border-box;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
    .ac-selection-status{position:fixed;inset:0 auto auto 0;width:1px;max-width:1px;height:1px;padding:0;margin:0;overflow:hidden;overflow-wrap:anywhere;word-break:break-all;clip-path:inset(50%);white-space:normal;border:0;contain:strict}
    @media(min-width:2200px){.ac-workspace{grid-template-columns:minmax(900px,1600px) minmax(760px,1fr)}.ac-rail{grid-template-columns:minmax(260px,.7fr) minmax(360px,1.3fr)}}
    @media(max-width:1300px){.ac-workspace{grid-template-columns:minmax(0,1fr)}.ac-rail{position:static;grid-template-columns:repeat(2,minmax(0,1fr))}}
    @media(max-width:650px){.ac-rail{grid-template-columns:minmax(0,1fr)}.ac-row summary{grid-template-columns:minmax(0,1fr);gap:4px;padding:12px}.ac-row summary>*{min-width:0}.ac-kind,.ac-time{grid-column:1}.ac-time{white-space:normal;overflow-wrap:anywhere}.ac-detail{padding:0 12px 8px}.ac-payload{margin:0 12px 12px;overflow-wrap:anywhere}.ac-selected-meta{grid-template-columns:minmax(0,1fr)}.ac-selected-head{display:block}.ac-selected-head time{display:block;margin-top:6px}}
  </style>`;

// missionTitles: async () => {mission_id: title}, best effort.
export async function loadActivity(client, missionTitles = async () => ({})) {
  const { events } = await client.activity(50);
  const titles = (events || []).length ? (await missionTitles()) || {} : {};
  return entriesFor(events || [], titles);
}

function entriesFor(events, missionTitles) {
  return events.map((event, index) => {
    const description = describeEvent(event, { missionTitles });
    const rawPayload = JSON.stringify(event.payload || {}, null, 2);
    return {
      id: String(event.event_id || `index-${index}`),
      eventId: event.event_id || '',
      type: event.event_type || '',
      title: description.title, kind: description.kind, detail: description.detail, muted: description.muted,
      time: formatTimestamp(event.source_timestamp),
      payload: rawPayload.length > 8000 ? `${rawPayload.slice(0, 7999)}…` : rawPayload,
    };
  });
}

const rowHtml = (e) => `<details class="ac-row" data-event-id="${esc(e.eventId)}" data-event-type="${esc(e.type)}" data-muted="${e.muted}"><summary><strong>${esc(e.title)}</strong><span class="ac-kind">${esc(e.kind)}</span><time class="ac-time">${e.time}</time></summary><p class="ac-detail">${esc(e.detail)}</p><pre class="ac-payload" aria-label="Original event payload">${esc(e.payload)}</pre></details>`;

const selectedHtml = (e) => `<div class="ac-selected-head"><div><span class="ac-eyebrow">Selected event</span><h2>${esc(e.title)}</h2></div><time>${e.time}</time></div>
    <dl class="ac-selected-meta"><div><dt>Type</dt><dd>${esc(e.type || 'Event')}</dd></div><div><dt>Event ID</dt><dd><code>${esc(e.eventId || 'unavailable')}</code></dd></div></dl>
    <p class="ac-selected-detail">${esc(e.detail)}</p><pre class="ac-payload" aria-label="Selected event payload">${esc(e.payload)}</pre>`;

export function mountActivity(container, client, { data = null, missionTitles } = {}) {
  container.innerHTML = `${STYLE}<div class="hd-live-head" style="display:flex;flex-wrap:wrap;align-items:baseline;gap:12px"><h1>Activity</h1><herdr-freshness interval="5000"></herdr-freshness></div>
  <div data-activity-empty></div>
  <div class="ac-workspace" hidden>
    <section class="ac-stream" aria-labelledby="activity-stream-title"><div class="ac-stream-head"><h2 id="activity-stream-title">Chronological stream</h2><span data-activity-count></span></div>
      <button class="ac-new" type="button" data-activity-new hidden></button>
      <div class="ac-list"></div>
    </section>
    <aside class="ac-rail" aria-label="Activity summary and selected event">
      <section class="ac-panel"><h2>Event types</h2><div class="ac-filter-list" role="group" aria-label="Filter activity by event type"></div>
        <p class="ac-filter-status" role="status"></p>
        <h2 style="margin-top:18px">Repeated types</h2><ul class="ac-repeat-list"></ul>
      </section>
      <p class="ac-selection-status" data-activity-selection-status role="status" aria-live="polite"></p>
      <section class="ac-panel ac-selected" data-activity-selected></section>
    </aside>
  </div>`;
  const $ = (s) => container.querySelector(s);
  const empty = $('[data-activity-empty]'), workspace = $('.ac-workspace'), list = $('.ac-list');
  const count = $('[data-activity-count]'), pill = $('[data-activity-new]'), filterList = $('.ac-filter-list');
  const filterStatus = $('.ac-filter-status'), repeats = $('.ac-repeat-list');
  const selected = $('[data-activity-selected]'), selectionStatus = $('[data-activity-selection-status]');

  let entries = [];
  let filter = 'all';
  let selectedId = null;
  let pending = null;
  let first = true;

  const byId = (id) => entries.find((e) => e.id === id);
  const rows = () => [...list.children].filter((r) => r.matches('.ac-row') && !r.hasAttribute('data-leaving'));
  const awayFromTop = () => list.getBoundingClientRect().top < 0;

  function select(id) {
    const entry = byId(id);
    if (!entry) return;
    selectedId = id;
    patchAttr(selected, 'data-event-id', entry.eventId);
    morph(selected, selectedHtml(entry));
    patchText(selectionStatus, `Selected event: ${entry.title}`);
  }

  function applyFilter() {
    let shown = 0;
    for (const row of rows()) {
      const hide = filter !== 'all' && row.dataset.eventType !== filter;
      patchAttr(row, 'hidden', hide);
      if (!hide) shown += 1;
    }
    patchText(filterStatus, filter === 'all' ? `Showing all ${shown} events` : `Showing ${shown} ${filter} events`);
    const current = rows().find((r) => r.getAttribute('data-key') === selectedId);
    if (!current || current.hidden) {
      const next = rows().find((r) => !r.hidden);
      if (next) select(next.getAttribute('data-key'));
    }
  }

  function apply(next) {
    pending = null;
    patchAttr(pill, 'hidden', true);
    entries = next;
    patchAttr(workspace, 'hidden', !entries.length);
    morph(empty, entries.length ? '' : '<herdr-empty message="No activity yet"></herdr-empty>');
    patchText(count, `${entries.length} events`);
    patchList(list, entries, { key: (e) => e.id, render: rowHtml });
    if (first) { for (const el of list.querySelectorAll('[data-entering]')) el.removeAttribute('data-entering'); first = false; }

    const counts = new Map();
    for (const e of entries) counts.set(e.type, (counts.get(e.type) || 0) + 1);
    if (filter !== 'all' && !counts.has(filter)) filter = 'all';
    const types = [...counts.keys()];
    const button = (type, label, n) => `<button class="ac-filter" type="button" data-key="${esc(type)}" data-event-filter="${esc(type)}" aria-pressed="${type === filter}">${label} <strong>${n}</strong></button>`;
    morph(filterList, button('all', 'All', entries.length) + types.map((t) => button(t, esc(t), counts.get(t))).join(''));
    morph(repeats, types.filter((t) => counts.get(t) > 1).map((t) => `<li data-repeat-type="${esc(t)}"><span>${esc(t)}</span><strong>${counts.get(t)}</strong></li>`).join('') || '<li><span>No repeated event types</span></li>');

    // Selection follows its event_id; an evicted event falls back to the newest (then first visible).
    if (!selectedId || !byId(selectedId)) selectedId = entries[0]?.id ?? null;
    if (selectedId) select(selectedId);
    applyFilter();
  }

  // While the user reads further down, new events wait behind the pill; nothing in the list moves.
  function receive(next) {
    const shown = new Set(entries.map((e) => e.id));
    const fresh = next.filter((e) => !shown.has(e.id)).length;
    if (!first && fresh && entries.length && awayFromTop()) {
      pending = next;
      patchText(pill, `${fresh} new`);
      patchAttr(pill, 'hidden', false);
      return;
    }
    apply(next);
  }

  const onFocus = (event) => { const row = event.target.closest?.('.ac-row'); if (row && event.target.matches('summary')) select(row.getAttribute('data-key')); };
  const onToggle = (event) => { const row = event.target; if (row.matches?.('.ac-row') && row.open) select(row.getAttribute('data-key')); };
  const onFilter = (event) => {
    const button = event.target.closest?.('[data-event-filter]');
    if (!button) return;
    filter = button.dataset.eventFilter;
    for (const b of filterList.querySelectorAll('[data-event-filter]')) patchAttr(b, 'aria-pressed', String(b === button));
    applyFilter();
  };
  const flush = () => { if (pending) apply(pending); };
  const onPill = () => { flush(); window.scrollTo(0, 0); };
  const onScroll = () => { if (pending && !awayFromTop()) flush(); };
  list.addEventListener('focusin', onFocus);
  list.addEventListener('toggle', onToggle, true);
  filterList.addEventListener('click', onFilter);
  pill.addEventListener('click', onPill);
  window.addEventListener('scroll', onScroll, { passive: true });

  const live = liveRefresher(() => loadActivity(client, missionTitles), receive, () => container.querySelector('herdr-freshness'));
  if (data) live.applied(data); else live.refresh();
  return {
    refresh: (hint) => live.refresh(hint),
    dispose() {
      live.dispose();
      window.removeEventListener('scroll', onScroll);
    },
  };
}
