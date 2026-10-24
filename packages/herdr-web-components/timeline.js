// herdr-web-components — mission timeline (pure renderer + light-DOM element).
//
// renderTimeline is a pure function (node-testable, no DOM). HerdrTimeline is
// a build-free custom element that renders into light DOM via renderTimeline.
import { formatTimestamp } from './format.js';
import { escapeHtml } from './util.js';

const EMPTY = '<herdr-empty message="No timeline events yet"></herdr-empty>';

// True when ref is a string with an http(s) scheme — the only hrefs allowed.
function isSafeHref(ref) {
  return typeof ref === 'string' &&
    /^(https?:\/\/)/.test(ref);
}

// Stable identity of an item across refreshes: its id when the server sends one, else what does not
// change when the item is edited (entity, kind, time). Identical items get an occurrence suffix, so
// duplicates pair one-to-one instead of colliding.
function itemKey(it, seen) {
  const id = it.id ?? it.event_id ?? it.sequence;
  const base = id != null ? `id:${id}` : JSON.stringify([it.entity_id ?? '', it.kind ?? '', it.ts ?? '']);
  const n = seen.get(base) ?? 0;
  seen.set(base, n + 1);
  return n ? `${base}#${n}` : base;
}

// Pure: list of timeline items -> HTML string.
export function renderTimeline(items) {
  if (!Array.isArray(items) || items.length === 0) {
    return EMPTY;
  }
  const seen = new Map();
  const rows = items.map((item) => {
    const it = item || {};
    const kind = escapeHtml(it.kind ?? '');
    const status = it.status;
    const badge =
      typeof status === 'string' && status.length > 0
        ? `<herdr-status stage="${escapeHtml(status)}"></herdr-status> `
        : '';
    const title = escapeHtml(it.title ?? '');
    const ts = it.ts ?? '';
    const rawTs = escapeHtml(ts);
    const link = isSafeHref(it.ref)
      ? ` <a href="${escapeHtml(it.ref)}" target="_blank" rel="noopener">link</a>`
      : '';
    return (
      `<li class="hd-timeline__item" data-kind="${kind}" data-key="${escapeHtml(itemKey(it, seen))}" data-sig="${escapeHtml(JSON.stringify([it.title ?? '', status ?? '', isSafeHref(it.ref) ? it.ref : '']))}">` +
      badge +
      `<span class="hd-timeline__title">${title}</span>` +
      `<time datetime="${rawTs}">${escapeHtml(formatTimestamp(ts))}</time>` +
      link +
      `</li>`
    );
  });
  return `<ol class="hd-timeline">${rows.join('')}</ol>`;
}

// <herdr-timeline> — light-DOM custom element.
// Items come from the `items` JSON attribute or the `items` property;
// both paths render through renderTimeline into this.innerHTML.
// Extend a safe base so this module stays importable in node (no DOM).
// In a browser this is HTMLElement; in node an inert stub.
const ELEMENT_BASE = typeof HTMLElement === 'function'
  ? HTMLElement
  : class {};

export class HerdrTimeline extends ELEMENT_BASE {
  static observedAttributes = ['items'];

  set items(list) {
    this._items = list;
    this.render();
  }

  get items() {
    return this._items;
  }

  attributeChangedCallback() {
    this.render();
  }

  connectedCallback() {
    this.render();
  }

  render() {
    if (this._items !== undefined && this.patch(this._items)) return;
    if (this._items === undefined) {
      const raw = this.getAttribute('items');
      if (raw == null) {
        this.innerHTML = renderTimeline([]);
        return;
      }
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch {
        parsed = null;
      }
      this.innerHTML = renderTimeline(parsed);
      return;
    }
    this.innerHTML = renderTimeline(this._items);
  }

  // A later item list keeps the nodes of items already shown (keyed by data-key) and slides the
  // new ones in (data-entering, the shared motion.css rule), unless calm mode or reduced motion
  // is on. Returns false when there is no rendered list to patch (first paint, empty list).
  patch(items) {
    const list = this.querySelector(':scope > ol.hd-timeline');
    if (!list || !Array.isArray(items) || !items.length) return false;
    const template = document.createElement('template');
    template.innerHTML = renderTimeline(items);
    const fresh = [...template.content.querySelectorAll('ol.hd-timeline > li')];
    const shown = new Map([...list.children].map((li) => [li.getAttribute('data-key'), li]));
    const animate = !calm();
    const wanted = fresh.map((li) => {
      const key = li.getAttribute('data-key'), old = shown.get(key);
      if (old) {
        shown.delete(key);
        if (old.getAttribute('data-sig') === li.getAttribute('data-sig')) return old; // unchanged: keep the node
        return li; // edited (e.g. a new status): replace in place, it is not a new item
      }
      if (animate) {
        li.setAttribute('data-entering', '');
        const clear = () => li.removeAttribute('data-entering');
        li.addEventListener('animationend', clear, { once: true });
        setTimeout(clear, 1000);
      }
      return li;
    });
    const keep = new Set(wanted);
    for (const li of [...list.children]) if (!keep.has(li)) li.remove();
    let cursor = list.firstElementChild;
    for (const li of wanted) {
      if (li === cursor) { cursor = li.nextElementSibling; continue; }
      list.insertBefore(li, cursor);
    }
    return true;
  }
}

function calm() {
  const mode = document.documentElement.getAttribute('data-calm');
  if (mode === 'off') return false;
  return mode === 'on' || (typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches);
}
