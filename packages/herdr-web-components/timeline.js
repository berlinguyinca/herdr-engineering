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

// Pure: list of timeline items -> HTML string.
export function renderTimeline(items) {
  if (!Array.isArray(items) || items.length === 0) {
    return EMPTY;
  }
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
      `<li class="hd-timeline__item" data-kind="${kind}">` +
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
}
