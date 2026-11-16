import { escapeHtml as esc } from './herdr-web-components/util.js';

// Dashboard previews without frames (the one exception, a sandboxed report page, lives in artifact-rich-view.js). Rendered foreign markup is the server's nh3 copy (scripts, frames, forms and
// event handlers are gone) and is placed inside an open shadow root under a paint-contained wrapper. PDFs are drawn on
// canvases by the vendored pdf.js. The raw file is only reachable through the sandboxed live route in a new tab.

export const STATIC_NOTE = 'Scripts, frames, audio and video are not shown in this static copy';
export const PDF_FAILED = 'This PDF could not be drawn';
const PDFJS_MODULE = '/vendor/pdfjs/pdf.min.mjs';
const PDFJS_WORKER = '/vendor/pdfjs/pdf.worker.min.mjs';
const MAX_PDF_PAGES = 200;
const KEPT_HREF = /^(?:https?:\/\/[^\s\\]|mailto:)/i;

// On paper the scroll box must not clip the copy to its 640px screen height, and the screen-only note is dropped.
export const PRINT_CSS = '@media print{[data-static-preview]{max-height:none!important;overflow:visible!important;contain:none!important}[data-static-note]{display:none!important}}';

function ensurePrintStyle(doc) {
  const head = doc.head;
  if (!head || doc.querySelector?.('style[data-static-print]')) return;
  const style = doc.createElement('style');
  style.setAttribute('data-static-print', '');
  style.textContent = PRINT_CSS;
  head.append(style);
}

// The server drops the report's own stylesheet from this copy, so the highlight for a linked finding is added here.
export const TARGET_CSS = '.is-target{outline:2px solid Highlight;outline-offset:2px;background:color-mix(in srgb,Highlight 14%,transparent)}';

const fragmentId = href => {
  if (typeof href !== 'string' || !href.startsWith('#') || href.length < 2) return null;
  try { return decodeURIComponent(href.slice(1)); } catch { return null; }
};

/** Scroll `target` into view inside the preview, mark it (one at a time) and move keyboard focus to it. */
export function revealTarget(shadow, target) {
  for (const old of shadow.querySelectorAll?.('.is-target') ?? []) old.classList.remove('is-target');
  target.classList.add('is-target');
  if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
  target.scrollIntoView?.({ block: 'start' });
  target.focus?.({ preventScroll: true });
}

/** True for an absolute http(s) or mailto link: the only links a static copy keeps. */
export const isKeptHref = href => typeof href === 'string' && KEPT_HREF.test(href.trim());

/**
 * Show the server-sanitised html in `container`. The wrapper carries `contain: paint` so nothing the copy styles
 * (position: fixed on the shadow host included) can paint outside it; the shadow root is on an inner element.
 * Links that are not absolute are unwrapped so a fragment link can never drive the SPA's hash router. The one exception
 * is a fragment link whose target id exists inside the copy (a finding's "#" permalink, a table-of-contents entry): it
 * is kept, never reaches the router (the click is cancelled), scrolls to and highlights its target, and, when the caller
 * gives `permalink(id)`, carries a real deep-link href and reports the choice through `onNavigate(id, url)`.
 * `focus` names an id to reveal on mount.
 */
export function mountStaticHtml(container, html, { label = 'Static preview', document: doc = globalThis.document, theme, focus, permalink, onNavigate } = {}) {
  ensurePrintStyle(doc);
  const note = doc.createElement('p');
  note.className = 'hd-note';
  note.setAttribute('data-static-note', '');
  note.textContent = STATIC_NOTE;
  const wrapper = doc.createElement('div');
  wrapper.setAttribute('data-static-preview', '');
  wrapper.setAttribute('role', 'document');
  wrapper.setAttribute('aria-label', label);
  wrapper.setAttribute('style', 'contain:paint;overflow:auto;max-height:640px');
  const host = doc.createElement('div');
  // The dashboard's own theme (data-theme on the document root) drives the copy's light/dark; the copy's stylesheet
  // reads data-theme on its shadow host and falls back to the OS scheme when it is absent.
  const forced = theme ?? doc.documentElement?.getAttribute('data-theme');
  if (forced === 'light' || forced === 'dark') host.setAttribute('data-theme', forced);
  wrapper.append(host);
  container.replaceChildren(note, wrapper);
  // The declarative template element does not attach through innerHTML, so the root is attached imperatively.
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = String(html ?? '');
  let wired = false;
  for (const link of [...shadow.querySelectorAll('a')]) {
    // The server drops relative hrefs, so a permalink ("a.anchor") has none: it names the card or section that holds its heading.
    const id = fragmentId(link.getAttribute('href')) ?? (link.classList?.contains('anchor') ? link.closest?.('article[id],section[id]')?.getAttribute('id') : null);
    const target = id && typeof shadow.getElementById === 'function' ? shadow.getElementById(id) : null;
    if (target) {
      wired = true;
      const url = typeof permalink === 'function' ? permalink(id) : null;
      link.setAttribute('href', url || `#${encodeURIComponent(id)}`);   // an <a> without href is not keyboard-focusable
      link.addEventListener('click', event => {
        event.preventDefault();
        revealTarget(shadow, target);
        if (url) onNavigate?.(id, url);
      });
    } else if (isKeptHref(link.getAttribute('href'))) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    } else link.replaceWith(...link.childNodes);
  }
  if (wired || focus) {
    const style = doc.createElement('style');
    style.textContent = TARGET_CSS;
    shadow.append?.(style);
  }
  const first = focus && typeof shadow.getElementById === 'function' ? shadow.getElementById(focus) : null;
  if (first) revealTarget(shadow, first);
  return wrapper;
}

/** The link that opens the raw file, sandboxed, in a new tab. */
export const liveLink = url => `<a class="hd-btn" target="_blank" rel="noopener noreferrer" href="${esc(url)}" data-open-live>Open live view in a new tab</a>`;

/**
 * Draw the PDF `bytes` (ArrayBuffer or Uint8Array) as canvases in `container`. Pages are drawn as they scroll into
 * view. Resolves to `{ pages, destroy }`; an abort of `signal` destroys the document.
 */
export async function mountPdf(container, bytes, { signal, document: doc = globalThis.document } = {}) {
  const status = doc.createElement('p');
  status.className = 'hd-note';
  status.setAttribute('role', 'status');
  const pages = doc.createElement('div');
  pages.setAttribute('data-pdf-pages', '');
  container.replaceChildren(status, pages);
  let loading;
  let observer = null;
  const destroy = () => { observer?.disconnect(); loading?.destroy?.(); };
  const fail = () => { status.textContent = PDF_FAILED; return { pages: 0, destroy() {} }; };
  try {
    const pdfjs = await import(PDFJS_MODULE);
    pdfjs.GlobalWorkerOptions.workerSrc = new URL(PDFJS_WORKER, globalThis.location?.href ?? 'http://localhost/').href;
    loading = pdfjs.getDocument({ data: bytes, isEvalSupported: false, enableXfa: false, verbosity: 0 });
    signal?.addEventListener('abort', destroy, { once: true });   // before any await: an abort mid-load must tear down
    const pdf = await loading.promise;
    if (signal?.aborted) { await loading.destroy(); return { pages: 0, destroy() {} }; }
    const total = Math.min(pdf.numPages, MAX_PDF_PAGES);
    if (pdf.numPages > total) status.textContent = `Showing the first ${total} of ${pdf.numPages} pages`;
    const first = await pdf.getPage(1);
    const width = Math.max(200, Math.min(pages.clientWidth || container.clientWidth || 800, 1000) - 4);
    const base = first.getViewport({ scale: 1 });
    const scale = Math.max(0.2, Math.min(1.6, width / base.width));
    const holders = [];
    for (let n = 1; n <= total; n += 1) {
      const holder = doc.createElement('div');
      holder.className = 'hd-pdf-page';
      holder.setAttribute('data-page', String(n));
      holder.setAttribute('role', 'group');
      holder.setAttribute('aria-label', `Page ${n} of ${pdf.numPages}`);
      holder.style.width = `${Math.floor(base.width * scale)}px`;
      holder.style.height = `${Math.floor(base.height * scale)}px`;
      pages.append(holder);
      holders.push(holder);
    }
    let queue = Promise.resolve();
    const drawn = new Set();
    const draw = async n => {
      if (signal?.aborted) return;
      const holder = holders[n - 1];
      const page = n === 1 ? first : await pdf.getPage(n);
      const viewport = page.getViewport({ scale });
      holder.style.width = `${Math.floor(viewport.width)}px`;
      holder.style.height = `${Math.floor(viewport.height)}px`;
      // no floor at 1: a tall page must shrink below device pixels so the 16M-pixel cap always holds
      const ratio = Math.min(globalThis.devicePixelRatio || 1, 2, Math.sqrt(16e6 / (viewport.width * viewport.height)));
      const canvas = doc.createElement('canvas');
      canvas.width = Math.max(1, Math.floor(viewport.width * ratio));
      canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      const textLayer = doc.createElement('div');
      textLayer.className = 'textLayer';
      textLayer.style.setProperty('--scale-factor', String(viewport.scale));
      textLayer.style.setProperty('--total-scale-factor', String(viewport.scale));
      holder.replaceChildren(canvas, textLayer);
      await page.render({
        canvasContext: canvas.getContext('2d'), viewport,
        transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
        annotationMode: pdfjs.AnnotationMode.DISABLE,
      }).promise;
      await new pdfjs.TextLayer({ textContentSource: page.streamTextContent(), container: textLayer, viewport }).render();
      holder.setAttribute('data-drawn', 'true');
    };
    const schedule = n => {
      if (drawn.has(n)) return;
      drawn.add(n);
      queue = queue.then(() => draw(n)).catch(() => { status.textContent = PDF_FAILED; });
    };
    const Observer = globalThis.IntersectionObserver;
    observer = Observer ? new Observer(entries => {
      for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); schedule(Number(entry.target.getAttribute('data-page'))); }
    }, { rootMargin: '300px' }) : null;
    if (observer) holders.forEach(holder => observer.observe(holder)); else schedule(1);
    return { pages: total, destroy };
  } catch {
    loading?.destroy?.();
    return fail();
  }
}
