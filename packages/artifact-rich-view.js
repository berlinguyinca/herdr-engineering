// The one frame the dashboard shows: a report's author-supplied HTML page in a fully sandboxed iframe (empty
// `sandbox`, so no scripts, no same-origin, no forms, no popups). The server also sends a `sandbox` CSP on the rich
// route, so the page stays inert even if this attribute were lost. The element is built by API with exactly this shape;
// tests/control_plane/test_no_frames.py allows this file and nothing else, and pins the shape.

export const RICH_NOTE = 'Scripts are disabled in this view; download the HTML asset to run it';
const RICH_ROUTE = /^(?:[a-z][a-z0-9+.-]*:\/\/[^/]+)?\/api\/v1\/artifacts\/[^/?#]+\/versions\/[^/?#]+\/rich$/i;

/** True for a media type that is HTML (parameters ignored). */
export const isHtmlType = type => typeof type === 'string' && type.split(';', 1)[0].trim().toLowerCase() === 'text/html';

/** True when the report version has an HTML asset to show (the server decides which one, or refuses). */
export const hasRichView = assets => Array.isArray(assets) && assets.some(asset => isHtmlType(asset?.media_type));

/** True only for the rich route of a version: the single `src` this view will frame. */
export const isRichUrl = url => typeof url === 'string' && RICH_ROUTE.test(url);

/** The panel's static markup: a toggle, an empty slot for the frame, and the honesty hint. */
export function richViewHtml() {
  return `<section class="hd-card" data-rich-view><h2>Rich view</h2><p>The author's own page, shown in an isolated frame.</p>`
    + `<button type="button" class="hd-btn" data-rich-toggle aria-expanded="false">Show rich view</button>`
    + `<div data-rich-slot></div></section>`;
}

/**
 * Put the sandboxed frame for `url` into `slot`, with the new-tab link and the hint under it. Throws for any `url`
 * that is not the rich route, so no other source can ever be framed through this function.
 */
export function mountRichFrame(slot, { url, liveUrl, title = 'Report rich view', document: doc = globalThis.document }) {
  if (!isRichUrl(url)) throw new TypeError('The rich view only frames the rich route');
  const frame = doc.createElement('iframe');
  frame.setAttribute('sandbox', '');
  frame.setAttribute('src', url);
  frame.setAttribute('title', title);
  frame.setAttribute('loading', 'lazy');
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.setAttribute('data-rich-frame', '');
  const note = doc.createElement('p');
  note.className = 'hd-note';
  note.setAttribute('data-rich-note', '');
  note.textContent = RICH_NOTE;
  const link = doc.createElement('a');
  link.className = 'hd-btn';
  link.setAttribute('target', '_blank');
  link.setAttribute('rel', 'noopener noreferrer');
  link.setAttribute('href', liveUrl || url);
  link.setAttribute('data-rich-open', '');
  link.textContent = 'Open in new tab';
  slot.replaceChildren(frame, note, link);
  return frame;
}

/** Wire the toggle of a `richViewHtml` panel: the frame is created on first show, removed on hide. */
export function wireRichView(root, { url, liveUrl, title, document: doc = globalThis.document }) {
  const toggle = root.querySelector('[data-rich-toggle]');
  const slot = root.querySelector('[data-rich-slot]');
  toggle.addEventListener('click', () => {
    const show = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(show));
    toggle.textContent = show ? 'Hide rich view' : 'Show rich view';
    if (show) mountRichFrame(slot, { url, liveUrl, title, document: doc });
    else slot.replaceChildren();
  });
}
