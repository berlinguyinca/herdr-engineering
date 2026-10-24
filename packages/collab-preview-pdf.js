export const PDF_LIMIT = 20 * 1024 * 1024;
const PAGE_LIMIT = 50, PIXEL_LIMIT = 16000000, TEXT_LIMIT = 200000;

// PDF.js runs as trusted application code, never as code inside the document.
// Only rasterized canvases and literal text enter the opaque, scriptless frame.
export function renderPdf(bytes, { signal } = {}) {
  if (bytes.byteLength > PDF_LIMIT) throw new Error('PDF size limit exceeded.');
  if (new TextDecoder().decode(bytes.slice(0, 5)) !== '%PDF-') throw new Error('Invalid PDF signature.');
  const host = document.createElement('div'), frame = document.createElement('iframe');
  const search = document.createElement('input'), status = document.createElement('p'), matches = document.createElement('div');
  search.type = 'search'; search.setAttribute('aria-label', 'Search PDF'); search.maxLength = 200; search.disabled = true;
  status.setAttribute('role', 'status'); status.textContent = 'Rendering PDF…';
  frame.title = 'PDF preview'; frame.setAttribute('sandbox', ''); frame.referrerPolicy = 'no-referrer';
  frame.style.cssText = 'width:100%;height:520px;border:1px solid #888;min-width:0';
  host.append(search, status, matches, frame);
  let port, worker, loading, renderTask, url, disposed = false, timer, rejectCancelled;
  const cancelled = new Promise((_, reject) => { rejectCancelled = reject; });
  const stop = () => {
    renderTask?.cancel();
    // Do not wait on a hung worker's acknowledgement to reclaim its thread.
    loading?.destroy().catch(() => {}); worker?.destroy(); port?.terminate();
    port = null; worker = null; loading = null;
  };
  const check = () => { if (disposed) throw new DOMException('Preview cancelled.', 'AbortError'); };
  host.dispose = () => {
    if (disposed) return;
    disposed = true; clearTimeout(timer); signal?.removeEventListener('abort', abort);
    stop(); rejectCancelled(new DOMException('Preview cancelled.', 'AbortError'));
    search.oninput = null; matches.replaceChildren(); frame.removeAttribute('src');
    if (url) { URL.revokeObjectURL(url); url = null; }
  };
  const abort = () => host.dispose();
  signal?.addEventListener('abort', abort, { once: true });
  const run = async () => {
    signal?.throwIfAborted(); check();
    const pdfjs = await import('./vendor/pdfjs/pdf.mjs'); check();
    // An explicit real Worker port prevents PDF.js's synchronous fake fallback.
    pdfjs.GlobalWorkerOptions.workerSrc = new URL('./vendor/pdfjs/pdf.worker.mjs', import.meta.url).href;
    port = new Worker(pdfjs.GlobalWorkerOptions.workerSrc, { type: 'module' });
    port.onerror = event => { event.preventDefault(); rejectCancelled(new Error('PDF worker unavailable.')); };
    worker = new pdfjs.PDFWorker({ port });
    loading = pdfjs.getDocument({ data: bytes, worker, enableXfa: false,
      disableFontFace: true, useSystemFonts: false, useWasm: false,
      useWorkerFetch: false, isOffscreenCanvasSupported: false,
      isImageDecoderSupported: false, maxImageSize: 4000000,
      canvasMaxAreaInBytes: 16000000, stopAtErrors: true,
      standardFontDataUrl: new URL('./vendor/pdfjs/standard_fonts/', import.meta.url).href,
    });
    loading.onPassword = () => rejectCancelled(new Error('Password-protected PDF: use Download.'));
    const pdf = await loading.promise; check();
    if (pdf.numPages > PAGE_LIMIT) throw new Error('PDF page limit exceeded. Use Download.');
    const doc = document.implementation.createHTMLDocument('PDF preview');
    const charset = doc.createElement('meta'); charset.setAttribute('charset', 'utf-8');
    const policy = doc.createElement('meta'); policy.httpEquiv = 'Content-Security-Policy';
    policy.content = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
    const style = doc.createElement('style');
    style.textContent = 'body{margin:12px;font:14px system-ui;color:#222;background:white;overflow-wrap:anywhere}img{max-width:100%;height:auto}p{white-space:pre-wrap}';
    doc.head.append(charset, policy, style);
    const texts = []; let pixels = 0, characters = 0;
    for (let index = 1; index <= pdf.numPages; index++) {
      const page = await pdf.getPage(index); check();
      const base = page.getViewport({ scale: 1 });
      if (!Number.isFinite(base.width * base.height) || base.width <= 0 || base.height <= 0) throw new Error('Invalid PDF page dimensions.');
      const scale = Math.min(1.5, 1600 / Math.max(base.width, base.height));
      const viewport = page.getViewport({ scale });
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.ceil(viewport.width)); canvas.height = Math.max(1, Math.ceil(viewport.height));
      pixels += canvas.width * canvas.height;
      if (pixels > PIXEL_LIMIT) throw new Error('PDF pixel limit exceeded. Use Download.');
      renderTask = page.render({ canvasContext: canvas.getContext('2d'), viewport, annotationMode: pdfjs.AnnotationMode.DISABLE });
      await renderTask.promise; renderTask = null; check();
      const section = doc.createElement('section'); section.id = `pdf-page-${index}`;
      const title = doc.createElement('h2'); title.textContent = `Page ${index}`;
      const image = doc.createElement('img'); image.alt = `PDF page ${index}`; image.src = canvas.toDataURL('image/png');
      canvas.width = canvas.height = 0;
      // Stream text so a malicious page cannot accumulate unbounded strings.
      const reader = page.streamTextContent().getReader(); const parts = [];
      try {
        while (true) {
          const { value, done } = await reader.read(); check(); if (done) break;
          for (const item of value.items) {
            if (typeof item.str !== 'string') continue;
            characters += item.str.length + 1;
            if (characters > TEXT_LIMIT) throw new Error('PDF text limit exceeded. Use Download.');
            parts.push(item.str + (item.hasEOL ? '\n' : ' '));
          }
        }
      } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      const text = parts.join('').trim(), paragraph = doc.createElement('p'); paragraph.textContent = text;
      texts.push(text); section.append(title, image, paragraph); doc.body.append(section);
      page.cleanup();
    }
    check();
    url = URL.createObjectURL(new Blob(['<!doctype html>', doc.documentElement.outerHTML], { type: 'text/html;charset=utf-8' }));
    frame.src = url; search.disabled = false;
    const updateSearch = () => {
      const query = search.value.trim().toLocaleLowerCase(); matches.replaceChildren();
      const found = texts.flatMap((text, index) => query && text.toLocaleLowerCase().includes(query) ? [index + 1] : []);
      status.textContent = !query ? `${texts.length} PDF page${texts.length === 1 ? '' : 's'}` : found.length ? `${found.length} matching page${found.length === 1 ? '' : 's'}` : 'No matches';
      for (const page of found) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = `Go to page ${page}`;
        button.onclick = () => { if (!disposed) frame.src = `${url}#pdf-page-${page}`; };
        matches.append(button);
      }
    };
    search.oninput = updateSearch; updateSearch();
  };
  timer = setTimeout(() => rejectCancelled(new Error('PDF preview timed out. Use Download.')), 15000);
  host.ready = Promise.race([run(), cancelled]).catch(error => { host.dispose(); throw error; }).finally(() => { clearTimeout(timer); });
  return host;
}
