// The one frame of the dashboard library: a member artifact's own page in a fully sandboxed iframe (empty `sandbox`:
// no scripts, no same-origin, no forms, no popups). The server also sends the host's `sandbox` CSP on the content
// route, so the page stays inert even if this attribute were lost. The element is built by API with exactly this shape;
// tests/control_plane/test_no_frames.py allows this file and the report rich view, and pins the shape.

const CONTENT_ROUTE = /^(?:[a-z][a-z0-9+.-]*:\/\/[^/]+)?\/api\/v1\/library\/content\/[^/?#]+\/[^/?#]+\/[^?#]+$/i;

/** True only for the library content route of one file: the single `src` this view will frame. */
export function isContentUrl(url) {
  if (typeof url !== 'string' || !CONTENT_ROUTE.test(url)) return false;
  const segments = url.split('?')[0].split('/').map((s) => { try { return decodeURIComponent(s); } catch { return '..'; } });
  return !segments.some((s) => s === '.' || s === '..');
}

/** Put the sandboxed frame for `url` into `slot`. Throws for any `url` that is not a library content route. */
export function mountLibraryFrame(slot, { url, title = 'Artifact content', document: doc = globalThis.document }) {
  if (!isContentUrl(url)) throw new TypeError('The library only frames its content route');
  const frame = doc.createElement('iframe');
  frame.setAttribute('sandbox', '');
  frame.setAttribute('src', url);
  frame.setAttribute('title', title);
  frame.setAttribute('loading', 'lazy');
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.setAttribute('data-library-frame', '');
  slot.replaceChildren(frame);
  return frame;
}
