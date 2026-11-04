import { escapeHtml as esc } from './herdr-web-components/util.js';

// Dashboard previews without frames. Rendered foreign markup is the server's nh3 copy (scripts, frames, forms and
// event handlers are gone) and is placed inside an open shadow root under a paint-contained wrapper. PDFs are drawn on
// canvases by the vendored pdf.js. The raw file is only reachable through the sandboxed live route in a new tab.

export const STATIC_NOTE = 'Scripts, frames and embedded media are not shown in this static copy';
export const PDF_FAILED = 'This PDF could not be drawn';
const PDFJS_MODULE = '/vendor/pdfjs/pdf.min.mjs';
const PDFJS_WORKER = '/vendor/pdfjs/pdf.worker.min.mjs';
const MAX_PDF_PAGES = 200;
const KEPT_HREF = /^(?:https?:\/\/[^\s\\]|mailto:)/i;

/** True for an absolute http(s) or mailto link: the only links a static copy keeps. */
export const isKeptHref = href => typeof href === 'string' && KEPT_HREF.test(href.trim());

/**
 * Show the server-sanitised html in `container`. The wrapper carries `contain: paint` so nothing the copy styles
 * (position: fixed on the shadow host included) can paint outside it; the shadow root is on an inner element.
 * Links that are not absolute are unwrapped so a fragment link can never drive the SPA's hash router.
 */
export function mountStaticHtml(container, html, { label = 'Static preview', document: doc = globalThis.document } = {}) {
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
  wrapper.append(host);
  container.replaceChildren(note, wrapper);
  // The declarative template element does not attach through innerHTML, so the root is attached imperatively.
  const shadow = host.attachShadow({ mode: 'open' });
  shadow.innerHTML = String(html ?? '');
  for (const link of [...shadow.querySelectorAll('a')]) {
    if (isKeptHref(link.getAttribute('href'))) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    } else link.replaceWith(...link.childNodes);
  }
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
  const fail = () => { status.textContent = PDF_FAILED; return { pages: 0, destroy() {} }; };
  try {
    const pdfjs = await import(PDFJS_MODULE);
    pdfjs.GlobalWorkerOptions.workerSrc = new URL(PDFJS_WORKER, globalThis.location?.href ?? 'http://localhost/').href;
    loading = pdfjs.getDocument({ data: bytes, isEvalSupported: false, enableXfa: false, verbosity: 0 });
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
      const ratio = Math.max(1, Math.min(globalThis.devicePixelRatio || 1, 2, Math.sqrt(16e6 / (viewport.width * viewport.height))));
      const canvas = doc.createElement('canvas');
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
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
    const observer = Observer ? new Observer(entries => {
      for (const entry of entries) if (entry.isIntersecting) { observer.unobserve(entry.target); schedule(Number(entry.target.getAttribute('data-page'))); }
    }, { rootMargin: '300px' }) : null;
    if (observer) holders.forEach(holder => observer.observe(holder)); else schedule(1);
    const destroy = () => { observer?.disconnect(); loading.destroy(); };
    signal?.addEventListener('abort', destroy, { once: true });
    return { pages: total, destroy };
  } catch {
    loading?.destroy?.();
    return fail();
  }
}
