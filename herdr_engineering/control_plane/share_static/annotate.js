// The only script of our own views (allowed by the response's CSP nonce), loaded as a module.
//
// Live mode (v/) draws the file by kind. Annotation mode (a/) also builds the panel and captures anchors. Rules:
//  * Nothing is built from markup: every node comes from createElement and gets text through textContent or a property,
//    so neither the shared file's content nor any annotation text can be parsed as HTML. Links are never made from user
//    text, and the only link targets are our own relative raw-file paths.
//  * The CSRF token is read from the meta tag only, is sent only in the X-Herdr-Annotate header of same-origin
//    annotation requests, and is never stored, logged or written into the page.
//  * No cookie, no web storage, no network call except the annotation endpoints, the raw file and the vendored pdf.js.
//  * The shared content lives in a shadow root and is sanitised by the server; this script only reads it, and the
//    highlights it adds are small wrapper elements made with the Range API (a failure leaves the list as it is).
import {
  LIMITS, POLL_DELAYS, RETRY_DELAYS, annotationUrls, clipHead, cleanRow, cleanText, describeAnchor, errorMessage, findQuote,
  isRawHref, boxText, statusLabel, textQuote, textQuoteFromString, unitBox,
} from './anchor.js';

const PDFJS_MODULE = '/s/static/vendor/pdfjs/pdf.min.mjs';
const PDFJS_WORKER = '/s/static/vendor/pdfjs/pdf.worker.min.mjs';
const SKIP_TEXT = new Set(['STYLE', 'SCRIPT', 'TEMPLATE', 'NOSCRIPT', 'TEXTAREA']);
const MAX_PDF_PAGES = 2000;
const MAX_HIGHLIGHTS = 60;

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === false || value === null || value === undefined) continue;
    if (name === 'text') node.textContent = value;
    else if (name.startsWith('on')) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? '' : String(value));
  }
  node.append(...kids.filter((kid) => kid !== null && kid !== undefined && kid !== false));
  return node;
}

function readData() {
  const node = document.getElementById('herdr-view-data');
  if (!node) return null;
  try {
    const data = JSON.parse(node.textContent);
    const ok = data && typeof data.file === 'string' && ['html', 'text', 'image', 'pdf', 'document'].includes(data.kind)
      && (data.mode === 'live' || data.mode === 'annotate') && isRawHref(data.raw_href);
    return ok ? data : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------------------------------- content root

function mountContentRoot() {
  const host = document.getElementById('herdr-content');
  if (!host) return null;
  let root = host.shadowRoot;
  if (!root) {   // the browser did not attach the declarative shadow root: attach an open one and move the content in
    const template = host.querySelector(':scope > template');
    root = host.attachShadow({ mode: 'open' });
    if (template) root.append(template.content);
  }
  return root;
}

// ---------------------------------------------------------------------------------------------------- text model
// One model of "the text of the content" for capture and highlighting, so a stored quote can always be found again:
// the text nodes a reader can see (not display:none, visibility:hidden or font-size:0, and not in style, script or
// textarea elements), in document order, with a newline between two blocks (or at a line break). Whitespace-only
// nodes next to a block are layout, not text. Walking is lazy: capture reads only the selection and its
// neighbourhood; the whole-document index used for highlights is built in time slices.
const BLOCK = new Set(['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'DD', 'DETAILS', 'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION',
  'FIGURE', 'FOOTER', 'FORM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER', 'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION',
  'SUMMARY', 'TABLE', 'TBODY', 'TD', 'TFOOT', 'TH', 'THEAD', 'TR', 'UL']);
const MAX_SCAN_NODES = 400000;
const MAX_INDEX_CHARS = 8 * 1024 * 1024;
const SLICE_MS = 8;
const yieldToMain = () => new Promise((resolve) => { setTimeout(resolve, 0); });

function makeModel(root) {
  const shown = new Map();
  const zeroFont = new Map();
  function elementShown(el) {
    let value = shown.get(el);
    if (value === undefined) {
      value = true;
      try {
        if (typeof el.checkVisibility === 'function') value = el.checkVisibility({ checkVisibilityCSS: true, visibilityProperty: true });
        else value = getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden';
      } catch { value = true; }
      shown.set(el, value);
    }
    return value;
  }
  function readable(el) {
    if (!elementShown(el)) return false;
    let zero = zeroFont.get(el);
    if (zero === undefined) {
      try { zero = getComputedStyle(el).fontSize === '0px'; } catch { zero = false; }
      zeroFont.set(el, zero);
    }
    return !zero;
  }
  const isBlock = (node) => !!node && node.nodeType === 1 && BLOCK.has(node.tagName);
  function textOk(node) {
    const parent = node.parentElement;
    if (parent && (SKIP_TEXT.has(parent.tagName) || !readable(parent))) return false;
    if (node.data.length === 0) return false;
    if (/^\s*$/.test(node.data) && (isBlock(node.previousSibling) || isBlock(node.nextSibling)
      || (!node.previousSibling && !node.nextSibling && (!parent || isBlock(parent))))) return false;
    return true;
  }
  const blockOf = (node) => {
    let el = node.parentElement;
    while (el && !BLOCK.has(el.tagName)) el = el.parentElement;
    return el;
  };
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (node.nodeType === 3) return NodeFilter.FILTER_ACCEPT;
      if (SKIP_TEXT.has(node.tagName) || !elementShown(node)) return NodeFilter.FILTER_REJECT;
      return node.tagName === 'BR' ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    },
  });
  // The next (dir > 0) or previous readable text node from the walker's position; `st.br` records a line break passed.
  function step(dir, st) {
    for (;;) {
      const node = dir > 0 ? walker.nextNode() : walker.previousNode();
      if (!node) return null;
      if (node.nodeType === 1) { st.br = true; continue; }
      if (textOk(node)) return node;
    }
  }
  const separator = (a, b, st) => (st.br || blockOf(a) !== blockOf(b) ? '\n' : '');
  const lastDeep = (node) => { let n = node; while (n.lastChild) n = n.lastChild; return n; };
  // The first readable text at or after (dir > 0) / last readable text at or before (dir < 0) a boundary point.
  function seek(dir, container, offset) {
    const st = { br: false };
    if (container.nodeType === 3) {
      if (textOk(container)) return { node: container, off: offset };
      walker.currentNode = container;
    } else if (dir > 0) {
      const child = container.childNodes[offset];
      walker.currentNode = child || lastDeep(container);
      if (child && child.nodeType === 3 && textOk(child)) return { node: child, off: 0 };
    } else {
      const child = offset > 0 ? container.childNodes[offset - 1] : null;
      if (child) {
        const deep = lastDeep(child);
        walker.currentNode = deep;
        if (deep.nodeType === 3 && textOk(deep)) return { node: deep, off: deep.data.length };
      } else {
        walker.currentNode = container;
      }
    }
    const node = step(dir, st);
    return node ? { node, off: dir > 0 ? 0 : node.data.length } : null;
  }
  // The quote of a selection's two boundary points, reading only the selection and its neighbourhood.
  function quoteOf(range) {
    const start = seek(1, range.startContainer, range.startOffset);
    const end = seek(-1, range.endContainer, range.endOffset);
    if (!start || !end) return null;
    const ordered = start.node === end.node ? start.off < end.off
      : !!(start.node.compareDocumentPosition(end.node) & Node.DOCUMENT_POSITION_FOLLOWING);
    if (!ordered) return null;
    const cap = LIMITS.exact + LIMITS.context + 1200;
    let text = '';
    let selLen = null;
    let current = start.node;
    let from = start.off;
    let prev = null;
    const st = { br: false };
    walker.currentNode = start.node;
    for (;;) {
      if (prev) text += separator(prev, current, st);
      st.br = false;
      if (current === end.node && selLen === null) {
        text += current.data.slice(from, end.off);
        selLen = text.length;
        text += current.data.slice(end.off);
      } else {
        text += current.data.slice(from);
      }
      prev = current;
      from = 0;
      if (selLen !== null ? text.length >= selLen + LIMITS.context : text.length >= cap) break;
      current = step(1, st);
      if (!current) break;
    }
    if (selLen === null) selLen = text.length;
    let before = start.node.data.slice(0, start.off);
    const back = { br: false };
    walker.currentNode = start.node;
    let after = start.node;
    while (before.length < LIMITS.context * 2) {
      const node = step(-1, back);
      if (!node) break;
      before = node.data + separator(node, after, back) + before;
      back.br = false;
      after = node;
    }
    return textQuote(before + text, before.length, before.length + selLen);
  }
  // The joined text and the (node, start) of each text node, built in slices so no task runs long.
  async function buildIndex(alive) {
    walker.currentNode = root;
    const entries = [];
    const parts = [];
    let length = 0;
    let prev = null;
    let count = 0;
    let deadline = performance.now() + SLICE_MS;
    const st = { br: false };
    for (;;) {
      const node = step(1, st);
      if (!node) break;
      const sep = prev ? separator(prev, node, st) : '';
      st.br = false;
      if (sep) { parts.push(sep); length += sep.length; }
      entries.push({ node, start: length });
      parts.push(node.data);
      length += node.data.length;
      prev = node;
      count += 1;
      if (count > MAX_SCAN_NODES || length > MAX_INDEX_CHARS) break;
      if (count % 128 === 0 && performance.now() > deadline) {
        await yieldToMain();
        if (!alive()) return null;
        walker.currentNode = node;
        deadline = performance.now() + SLICE_MS;
      }
    }
    return { entries, text: parts.join('') };
  }
  // The fallback for a browser that reports only the host element: the first place the selected string occurs.
  async function quoteOfString(selected) {
    const index = await buildIndex(() => true);
    return index ? textQuoteFromString(index.text, selected) : null;
  }
  return { quoteOf, buildIndex, quoteOfString };
}

// The current selection as two points inside `root`, or null. Browsers differ: composed ranges (Safari, current
// Chromium and Firefox), the shadow root's own selection (Chromium), and last the plain selection.
function selectionPoints(root) {
  const selection = document.getSelection();
  if (!selection) return null;
  const inside = (r) => r && root.contains(r.startContainer) && root.contains(r.endContainer);
  if (typeof selection.getComposedRanges === 'function') {
    let ranges = null;
    try { ranges = selection.getComposedRanges({ shadowRoots: [root] }); } catch { ranges = null; }
    if (!ranges) { try { ranges = selection.getComposedRanges(root); } catch { ranges = null; } }
    if (ranges && ranges.length && inside(ranges[0])) return ranges[0];
  }
  if (typeof root.getSelection === 'function') {
    const own = root.getSelection();
    if (own && own.rangeCount && inside(own.getRangeAt(0))) return own.getRangeAt(0);
  }
  if (selection.rangeCount && inside(selection.getRangeAt(0))) return selection.getRangeAt(0);
  return null;
}

const squash = (text) => text.replace(/\s+/g, '').toLowerCase();
const REFUSED = 'That selection could not be captured exactly (part of it is hidden or unusual). Select a smaller piece, or comment on the whole file.';

// {anchor} for the current selection inside the content, {refused} when the captured text differs from what the reader
// selected (so hidden text can never ride along), or null when nothing is selected there.
async function textAnchorFromSelection(root, model, file) {
  const selection = document.getSelection();
  if (!selection || (selection.isCollapsed && !selection.toString())) return null;
  const points = selectionPoints(root);
  let quote = null;
  if (points) {
    quote = model.quoteOf(points);
  } else if (selection.anchorNode === root.host || selection.focusNode === root.host) {
    quote = await model.quoteOfString(selection.toString());
  } else {
    return null;
  }
  if (!quote) return null;
  const shown = squash(selection.toString().slice(0, 20000));
  if (shown && !shown.startsWith(squash(quote.exact))) return { refused: REFUSED };
  return { anchor: { kind: 'text_quote', file, ...quote } };
}

// ---------------------------------------------------------------------------------------------------- highlights

function makeHighlighter(root, model, onPick) {
  let marks = [];
  let token = 0;
  let lastSignature = null;
  function clear() {
    for (const mark of marks) {
      const parent = mark.parentNode;
      if (!parent) continue;
      mark.replaceWith(...mark.childNodes);
      parent.normalize();
    }
    marks = [];
  }
  function wrap(span, entries) {
    let lo = 0;
    let hi = entries.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (entries[mid].start + entries[mid].node.data.length <= span.at) lo = mid + 1;
      else hi = mid;
    }
    const hits = [];
    for (let i = lo; i < entries.length && entries[i].start < span.end; i += 1) hits.push(entries[i]);
    const current = ['open', 'submitted', 'edited'].includes(span.row.status);
    for (const entry of hits.reverse()) {
      const from = Math.max(0, span.at - entry.start);
      const to = Math.min(entry.node.data.length, span.end - entry.start);
      if (to <= from) continue;
      try {
        const range = document.createRange();
        range.setStart(entry.node, from);
        range.setEnd(entry.node, to);
        const mark = document.createElement('mark');
        mark.style.setProperty('background', current ? 'rgba(255, 200, 0, 0.5)' : 'rgba(150, 150, 150, 0.35)');
        mark.style.setProperty('color', 'inherit');
        mark.style.setProperty('cursor', 'pointer');
        mark.addEventListener('click', () => onPick(span.row.annotation_id));
        range.surroundContents(mark);
        marks.push(mark);
      } catch {
        /* the range cannot be wrapped: the row stays in the list only */
      }
    }
  }
  return {
    // Highlights the quotes of `rows` in this file. Does nothing when the set did not change since the last call; builds
    // the text index once; works in time slices; a newer call abandons an older one.
    async apply(rows, file) {
      const live = (r) => ['open', 'submitted', 'edited'].includes(r.status);
      const wanted = rows.filter((r) => r.file === file && r.anchor && r.anchor.kind === 'text_quote' && typeof r.anchor.exact === 'string')
        .slice(0, MAX_HIGHLIGHTS);
      const signature = wanted.map((r) => [r.annotation_id, live(r), r.anchor.exact, r.anchor.prefix, r.anchor.suffix].join('\u0001')).join('\u0002');
      if (signature === lastSignature) return;
      lastSignature = signature;
      token += 1;
      const mine = token;
      clear();
      if (!wanted.length) return;
      const index = await model.buildIndex(() => mine === token);
      if (!index || mine !== token) return;
      const spans = [];
      let deadline = performance.now() + SLICE_MS;
      for (const row of wanted) {
        const at = findQuote(index.text, row.anchor);
        if (at >= 0) spans.push({ at, end: at + row.anchor.exact.length, row });
        if (performance.now() > deadline) {
          await yieldToMain();
          if (mine !== token) return;
          deadline = performance.now() + SLICE_MS;
        }
      }
      spans.sort((a, b) => b.at - a.at);
      let limit = Infinity;
      for (const span of spans) {
        if (span.end > limit) continue;     // overlaps a later quote: it stays in the list only
        limit = span.at;
        wrap(span, index.entries);
      }
    },
  };
}

// ---------------------------------------------------------------------------------------------------- image

function drawImage(native, data, onRegion) {
  const box = h('div', { class: 'herdr-image-box', tabindex: '0', role: 'group', 'aria-label': `Image ${data.file}`,
    'aria-describedby': 'herdr-image-hint' });
  const hint = h('p', { id: 'herdr-image-hint', class: 'herdr-image-hint', 'aria-live': 'polite',
    text: onRegion ? 'To mark a region: drag with a pointer, or focus the image and use the arrow keys to move a region, Shift and the arrow keys to resize it, and Enter to comment on it.' : '' });
  const image = h('img', { alt: data.file, draggable: 'false' });
  image.src = data.raw_href;
  const layer = h('div', { class: 'herdr-region-layer', 'aria-hidden': 'true' });
  const live = h('div', { class: 'herdr-region herdr-region-live' });
  live.hidden = true;
  box.append(image, layer, live);
  native.append(box, hint);
  if (!onRegion) return { setRegions() {} };
  box.classList.add('herdr-annotatable');
  let start = null;
  const place = (el, rect) => {
    el.style.setProperty('left', `${rect.x * 100}%`);
    el.style.setProperty('top', `${rect.y * 100}%`);
    el.style.setProperty('width', `${rect.w * 100}%`);
    el.style.setProperty('height', `${rect.h * 100}%`);
  };
  const local = (event) => {
    const r = image.getBoundingClientRect();
    return { x: event.clientX - r.left, y: event.clientY - r.top, w: r.width, h: r.height };
  };
  box.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || !event.isPrimary) return;
    start = local(event);
    box.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  box.addEventListener('pointermove', (event) => {
    if (!start) return;
    const now = local(event);
    const preview = unitBox(start.x, start.y, now.x, now.y, now.w, now.h);
    live.hidden = !preview;
    if (preview) place(live, preview);
  });
  const finish = (event, commit) => {
    if (!start) return;
    const now = local(event);
    const result = commit ? unitBox(start.x, start.y, now.x, now.y, now.w, now.h) : null;
    start = null;
    live.hidden = true;
    if (result) onRegion(result);
  };
  // Keyboard alternative: arrows move a default region, Shift+arrows resize it, Enter uses it, Escape drops it.
  let keyed = null;
  const clampTo = (value, low, high) => Math.min(high, Math.max(low, value));
  box.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { keyed = null; live.hidden = true; hint.textContent = 'Region dropped.'; return; }
    if (event.key === 'Enter' && keyed) {
      event.preventDefault();
      const result = unitBox(keyed.x, keyed.y, keyed.x + keyed.w, keyed.y + keyed.h, 1, 1);
      keyed = null;
      live.hidden = true;
      if (result) onRegion(result, true);
      return;
    }
    const delta = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (!delta) return;
    event.preventDefault();
    const step = 0.02;
    keyed = keyed || { x: 0.35, y: 0.35, w: 0.3, h: 0.3 };
    if (event.shiftKey) {
      keyed.w = clampTo(keyed.w + delta[0] * step, 0.02, 1 - keyed.x);
      keyed.h = clampTo(keyed.h + delta[1] * step, 0.02, 1 - keyed.y);
    } else {
      keyed.x = clampTo(keyed.x + delta[0] * step, 0, 1 - keyed.w);
      keyed.y = clampTo(keyed.y + delta[1] * step, 0, 1 - keyed.h);
    }
    place(live, keyed);
    live.hidden = false;
    hint.textContent = `Region ${boxText(keyed)}. Press Enter to comment on it.`;
  });
  box.addEventListener('pointerup', (event) => finish(event, true));
  box.addEventListener('pointercancel', (event) => finish(event, false));
  return {
    setRegions(rows) {
      layer.replaceChildren();
      for (const row of rows) {
        const region = h('div', { class: 'herdr-region' });
        place(region, row.anchor);
        layer.append(region);
      }
    },
  };
}

// ---------------------------------------------------------------------------------------------------- pdf

function pdfConfig(data) {
  const cfg = data.pdfjs && typeof data.pdfjs === 'object' ? data.pdfjs : {};
  const same = (value, fallback) => (typeof value === 'string' && value.startsWith('/s/static/vendor/pdfjs/') && !value.includes('..') ? value : fallback);
  return { module: same(cfg.module, PDFJS_MODULE), worker: same(cfg.worker, PDFJS_WORKER) };
}

async function drawPdf(native, data, onAnchor) {
  const status = h('p', { class: 'herdr-pdf-status', role: 'status', text: 'Loading the PDF' });
  const pages = h('div', { class: 'herdr-pdf-pages' });
  native.append(status, pages);
  const fail = () => {
    status.textContent = 'This PDF could not be drawn';
    status.append(' ', h('a', { href: data.raw_href, rel: 'noopener', text: 'Open the PDF' }));
  };
  let pdfjs;
  let doc;
  try {
    const config = pdfConfig(data);
    pdfjs = await import(config.module);
    pdfjs.GlobalWorkerOptions.workerSrc = new URL(config.worker, location.href).href;
    doc = await pdfjs.getDocument({
      url: new URL(data.raw_href, location.href).href,
      isEvalSupported: false,
      enableXfa: false,
      disableAutoFetch: true,
      verbosity: 0,
    }).promise;
  } catch {
    fail();
    return { setRegions() {}, regionMode() {} };
  }
  status.textContent = '';
  const total = Math.min(doc.numPages, MAX_PDF_PAGES);
  if (doc.numPages > total) status.textContent = `Showing the first ${total} of ${doc.numPages} pages`;
  let first;
  try { first = await doc.getPage(1); } catch {   // a zero-page or damaged document
    fail();
    return { setRegions() {}, regionMode() {} };
  }
  const width = Math.max(200, Math.min(native.clientWidth || 800, 1000) - 4);
  const base = first.getViewport({ scale: 1 });
  const scale = Math.max(0.2, Math.min(1.6, width / base.width));
  const holders = [];
  let regionOn = false;
  let drag = null;
  const regionLayers = new Map();
  for (let n = 1; n <= total; n += 1) {
    const holder = h('div', { class: 'herdr-pdf-page', 'data-page': String(n), role: 'group', 'aria-label': `Page ${n} of ${doc.numPages}` });
    holder.style.setProperty('width', `${Math.floor(base.width * scale)}px`);
    holder.style.setProperty('height', `${Math.floor(base.height * scale)}px`);
    const layer = h('div', { class: 'herdr-region-layer', 'aria-hidden': 'true' });
    regionLayers.set(n, layer);
    pages.append(holder);
    holders.push(holder);
  }
  let queue = Promise.resolve();
  const drawn = new Set();
  async function renderPage(n) {
    const holder = holders[n - 1];
    const page = n === 1 ? first : await doc.getPage(n);
    const viewport = page.getViewport({ scale });
    holder.style.setProperty('width', `${Math.floor(viewport.width)}px`);
    holder.style.setProperty('height', `${Math.floor(viewport.height)}px`);
    const ratio = Math.max(1, Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(16e6 / (viewport.width * viewport.height))));
    const canvas = h('canvas');
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    canvas.style.setProperty('width', `${Math.floor(viewport.width)}px`);
    canvas.style.setProperty('height', `${Math.floor(viewport.height)}px`);
    const textLayer = h('div', { class: 'textLayer' });
    textLayer.style.setProperty('--scale-factor', String(viewport.scale));
    textLayer.style.setProperty('--total-scale-factor', String(viewport.scale));
    holder.replaceChildren(canvas, textLayer, regionLayers.get(n));
    await page.render({
      canvasContext: canvas.getContext('2d'),
      viewport,
      transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
      annotationMode: pdfjs.AnnotationMode.DISABLE,
    }).promise;
    await new pdfjs.TextLayer({ textContentSource: page.streamTextContent(), container: textLayer, viewport }).render();
    holder.setAttribute('data-drawn', 'true');
  }
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const n = Number(entry.target.getAttribute('data-page'));
      observer.unobserve(entry.target);
      if (drawn.has(n)) continue;
      drawn.add(n);
      queue = queue.then(() => renderPage(n)).catch(() => {
        holders[n - 1].replaceChildren(h('p', { class: 'herdr-pdf-status', text: 'This page could not be drawn' }));
      });
    }
  }, { rootMargin: '400px 0px' });
  for (const holder of holders) observer.observe(holder);

  if (onAnchor) {
    const pageOf = (node) => {
      const el = node && node.nodeType === 1 ? node : node && node.parentElement;
      const holder = el && el.closest ? el.closest('.herdr-pdf-page') : null;
      return holder && pages.contains(holder) ? Number(holder.getAttribute('data-page')) : 0;
    };
    pages.addEventListener('pointerdown', (event) => {
      const holder = event.target.closest ? event.target.closest('.herdr-pdf-page') : null;
      if (!holder || event.button !== 0 || !event.isPrimary || !(regionOn || event.altKey)) return;
      const rect = holder.getBoundingClientRect();
      drag = { holder, x: event.clientX - rect.left, y: event.clientY - rect.top };
      holder.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    pages.addEventListener('pointerup', (event) => {
      if (!drag) return;
      const rect = drag.holder.getBoundingClientRect();
      const box = unitBox(drag.x, drag.y, event.clientX - rect.left, event.clientY - rect.top, rect.width, rect.height);
      const page = Number(drag.holder.getAttribute('data-page'));
      drag = null;
      if (box) onAnchor({ kind: 'pdf_page', file: data.file, page, region: box });
    });
    pages.addEventListener('pointercancel', () => { drag = null; });
    pages.addEventListener('click', (event) => {
      const selection = document.getSelection();
      if (regionOn || event.altKey || (selection && selection.toString())) return;
      const page = pageOf(event.target);
      if (page) onAnchor({ kind: 'pdf_page', file: data.file, page });
    });
    let selectionTimer = 0;
    document.addEventListener('selectionchange', () => {
      clearTimeout(selectionTimer);
      selectionTimer = setTimeout(() => {
        const selection = document.getSelection();
        if (!selection || selection.isCollapsed || !selection.rangeCount) return;
        const page = pageOf(selection.getRangeAt(0).startContainer);
        const text = clipHead(cleanText(selection.toString()).trim(), LIMITS.exact);
        if (page && text) onAnchor({ kind: 'pdf_page', file: data.file, page, exact: text });
      }, 150);
    });
  }
  return {
    regionMode(on) { regionOn = on; pages.classList.toggle('herdr-region-mode', on); },
    setRegions(rows) {
      for (const layer of regionLayers.values()) layer.replaceChildren();
      for (const row of rows) {
        const layer = regionLayers.get(row.anchor.page);
        if (!layer || !row.anchor.region) continue;
        const el = h('div', { class: 'herdr-region' });
        const r = row.anchor.region;
        el.style.setProperty('left', `${r.x * 100}%`);
        el.style.setProperty('top', `${r.y * 100}%`);
        el.style.setProperty('width', `${r.w * 100}%`);
        el.style.setProperty('height', `${r.h * 100}%`);
        layer.append(el);
      }
    },
  };
}

function drawDocument(native, data) {
  const link = h('a', { href: data.raw_href, rel: 'noopener', text: data.kind === 'pdf' ? 'Open the PDF' : `Download ${data.file}` });
  if (data.kind !== 'pdf') link.setAttribute('download', '');
  native.append(h('p', { class: 'herdr-file-link' }, link));
}

// ---------------------------------------------------------------------------------------------------- panel

function csrfToken() {
  const meta = document.querySelector('meta[name="herdr-annotate-csrf"]');
  const value = meta ? meta.getAttribute('content') : null;
  return value && /^[A-Za-z0-9_-]{20,128}$/.test(value) ? value : null;
}

async function send(url, token, payload) {
  // The session is in the URL path AND the HttpOnly write cookie must accompany every annotation request (same-origin
  // fetches from this page carry it; an f/ page's requests never do). No explicit mode: with the default (cors) mode the POST
  // keeps its Origin header; an explicit mode 'same-origin' together with the no-referrer policy made Firefox and WebKit
  // send "Origin: null" (verified), which the server refuses.
  const init = { credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer' };
  if (payload === undefined) {
    init.headers = { Accept: 'application/json' };
  } else {
    init.method = 'POST';
    init.headers = { 'Content-Type': 'application/json', 'X-Herdr-Annotate': token };
    init.body = JSON.stringify(payload);
  }
  try {
    const response = await fetch(url, init);
    let json = null;
    try { json = await response.json(); } catch { json = null; }
    return { status: response.status, json };
  } catch {
    return { status: 0, json: null };
  }
}

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
const errorCode = (json) => (json && typeof json.error === 'string' && /^[a-z_]{1,30}$/.test(json.error) ? json.error : '');

function buildPanel(aside, data, hooks) {
  const urls = annotationUrls(location.pathname);
  const token = csrfToken();
  const shadow = aside.shadowRoot || aside.attachShadow({ mode: 'open' });
  shadow.replaceChildren(h('link', { rel: 'stylesheet', href: '/s/static/annotate.css' }));
  const wholeFile = () => ({ kind: 'document', file: data.file });
  let anchor = wholeFile();
  let serverRows = [];
  const overlay = new Map();
  let editing = null;
  const busy = new Set();      // rows with a request in flight: their buttons are disabled and a second click is ignored
  let renderToken = 0;
  let renderSignature = null;
  let deferredRender = false;
  let pollTimer = 0;
  let pollRound = 0;

  const title = h('h2', { id: 'panel-title', tabindex: '-1', text: 'Annotating a static copy' });
  const expires = data.expires_at ? new Date(data.expires_at * 1000).toLocaleString() : null;
  const note = h('p', { class: 'note', text: `You are commenting on the copy of the file as it was shared; the live page may differ.${expires ? ` Your link works until ${expires}.` : ''}` });
  const anchorTitle = h('p', { class: 'anchor-title', id: 'anchor-title' });
  const anchorQuote = h('blockquote', { class: 'anchor-quote', tabindex: '0', 'aria-labelledby': 'anchor-title' });
  const reset = h('button', { type: 'button', text: 'Whole file instead' });
  const regionButton = data.kind === 'pdf' ? h('button', { type: 'button', 'aria-pressed': 'false', text: 'Mark region' }) : null;
  const comment = h('textarea', { id: 'comment', rows: '4', maxlength: String(data.max_comment_chars || 4000), 'aria-describedby': 'count' });
  const counter = h('span', { id: 'count', class: 'count' });
  const submit = h('button', { type: 'submit', class: 'primary', text: 'Submit' });
  const message = h('p', { class: 'message', role: 'status', 'aria-live': 'polite' });
  const list = h('ul', { class: 'rows' });
  const listHead = h('h3', { id: 'rows-title', tabindex: '-1', text: 'Your annotations' });
  const refreshButton = h('button', { type: 'button', class: 'quiet', text: 'Refresh' });
  const form = h('form', { novalidate: true },
    h('label', { for: 'comment', text: 'Your comment' }), comment,
    h('div', { class: 'meta' }, counter, submit));
  shadow.append(h('div', { class: 'panel' }, title, note,
    h('section', { 'aria-labelledby': 'anchor-title' }, anchorTitle, anchorQuote,
      h('div', { class: 'actions' }, reset, regionButton)),
    form, message,
    h('div', { class: 'list-head' }, listHead, refreshButton), list));

  function say(text, kind = 'info') {
    message.textContent = text;
    message.setAttribute('data-kind', kind);
  }
  function updateCounter() {
    counter.textContent = `${comment.value.length} / ${comment.maxLength}`;
  }
  function describe() {
    const { title: t, quote } = describeAnchor(anchor, data.file);
    anchorTitle.textContent = t;
    anchorQuote.textContent = quote;
    anchorQuote.hidden = !quote;
    reset.disabled = anchor.kind === 'document';
  }
  function setAnchor(next) {
    anchor = next;
    describe();
  }

  function merged() {
    const out = [];
    const seen = new Set();
    for (const row of serverRows) {
      seen.add(row.annotation_id);
      const extra = overlay.get(row.annotation_id);
      if (extra && extra.deleted) continue;
      if (extra && extra.body !== undefined && extra.body !== row.body) {
        out.push({ ...row, body: extra.body, status: row.status === 'open' ? 'edited' : row.status });
      } else {
        out.push(row);
      }
    }
    const fresh = [];
    for (const [id, extra] of overlay) if (!seen.has(id) && extra.row && !extra.deleted) fresh.push(extra.row);
    return [...fresh.reverse(), ...out];
  }
  function reconcile() {
    const seen = new Map(serverRows.map((r) => [r.annotation_id, r]));
    for (const [id, extra] of [...overlay]) {
      const row = seen.get(id);
      if (extra.deleted && !row) overlay.delete(id);
      if (row) delete extra.row;
      if (row && extra.body !== undefined && extra.body === row.body) delete extra.body;
      if (!extra.deleted && !extra.row && extra.body === undefined) overlay.delete(id);
    }
  }

  function renderRow(row) {
    const current = row.file === data.file;
    const li = h('li', { class: 'row', 'data-id': row.annotation_id, tabindex: '-1' });
    const { title: t, quote } = describeAnchor(row.anchor, row.file || data.file);
    const head = h('div', { class: 'row-head' }, h('span', { class: 'badge', 'data-status': row.status, text: statusLabel(row) }),
      current ? null : h('span', { class: 'file', text: `in ${row.file}` }));
    li.append(head, h('p', { class: 'row-anchor', text: t }));
    if (quote) li.append(h('blockquote', { text: quote }));
    const body = h('p', { class: 'row-body', text: row.body });
    li.append(body);
    if (['open', 'submitted', 'edited'].includes(row.status)) {
      const editButton = h('button', { type: 'button', text: 'Edit', 'aria-label': 'Edit this annotation', disabled: busy.has(row.annotation_id) });
      editButton.addEventListener('click', () => startEdit(row, li, body, editButton));
      const removeButton = h('button', { type: 'button', class: 'danger', text: 'Delete', 'aria-label': 'Delete this annotation', disabled: busy.has(row.annotation_id) });
      removeButton.addEventListener('click', () => remove(row));
      li.append(h('div', { class: 'actions' }, editButton, removeButton));
    }
    return li;
  }
  function render() {
    if (editing) { deferredRender = true; return; }
    const rows = merged();
    hooks.rows(rows);
    const signature = rows.map((r) => [r.annotation_id, r.status, r.body, r.edited_at, r.addressed_in_version_number, busy.has(r.annotation_id)].join('\u0001')).join('\u0002');
    if (signature === renderSignature) return;     // nothing changed: keep the list (and the reader's focus) as it is
    renderSignature = signature;
    renderToken += 1;
    const mine = renderToken;
    list.replaceChildren();
    if (!rows.length) { list.append(h('li', { class: 'empty', text: 'Nothing yet.' })); return; }
    const addChunk = (from) => {      // a long list is built in chunks so no task runs long
      if (mine !== renderToken) return;
      list.append(...rows.slice(from, from + 40).map(renderRow));
      if (from + 40 < rows.length) setTimeout(() => addChunk(from + 40), 0);
    };
    addChunk(0);
  }

  async function refresh() {
    const result = await send(urls.list, token);
    if (result.status !== 200 || !result.json || !Array.isArray(result.json.annotations)) {
      if (result.status !== 200) say(errorMessage(result.status, errorCode(result.json)), 'error');
      return false;
    }
    serverRows = result.json.annotations.map(cleanRow).filter(Boolean).slice(0, LIMITS.maxRows);
    reconcile();
    render();
    return true;
  }
  function poll() {
    clearTimeout(pollTimer);
    if (!overlay.size) { pollRound = 0; return; }
    if (pollRound >= POLL_DELAYS.length) return;
    pollTimer = setTimeout(async () => {
      pollRound += 1;
      await refresh();
      poll();
    }, POLL_DELAYS[pollRound]);
  }
  function expectProjection() {
    pollRound = 0;
    poll();
  }

  // An edit or delete right after create is refused (409) until the worker has projected the row: look again, wait, retry.
  async function change(url, id, payload) {
    let result = await send(url, token, payload);
    for (let attempt = 0; result.status === 409 && attempt < RETRY_DELAYS.length; attempt += 1) {
      say('Still saving — trying again');
      await sleep(RETRY_DELAYS[attempt]);
      await refresh();
      const known = serverRows.find((r) => r.annotation_id === id);
      if (known && known.status !== 'open') break;
      result = await send(url, token, payload);
    }
    return result;
  }

  function startEdit(row, li, body, editButton) {
    if (editing || busy.has(row.annotation_id)) return;
    editing = row.annotation_id;
    const box = h('textarea', { rows: '4', maxlength: String(comment.maxLength), 'aria-label': 'Edit your comment' });
    box.value = row.body;
    const save = h('button', { type: 'button', class: 'primary', text: 'Save' });
    const cancel = h('button', { type: 'button', text: 'Cancel' });
    const bar = h('div', { class: 'actions' }, save, cancel);
    const end = (focus) => {
      editing = null;
      box.replaceWith(body);
      bar.remove();
      if (deferredRender) { deferredRender = false; render(); }
      const target = list.querySelector(`li[data-id="${row.annotation_id}"] button`);
      if (focus && target) target.focus();
    };
    body.replaceWith(box);
    editButton.closest('.actions').hidden = true;
    li.append(bar);
    box.focus();
    cancel.addEventListener('click', () => { editButton.closest('.actions').hidden = false; end(true); });
    box.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { event.preventDefault(); cancel.click(); }
    });
    save.addEventListener('click', async () => {
      const text = cleanText(box.value);
      if (!text.trim()) { say(errorMessage(400, 'empty'), 'error'); box.focus(); return; }
      if (busy.has(row.annotation_id)) return;
      busy.add(row.annotation_id);
      save.disabled = true;
      say('Saving');
      const result = await change(urls.edit(row.annotation_id), row.annotation_id, { comment: text });
      busy.delete(row.annotation_id);
      save.disabled = false;
      if (result.status === 202) {
        const extra = overlay.get(row.annotation_id) || {};
        extra.body = text;
        overlay.set(row.annotation_id, extra);
        say('Saved. The change shows as Edited until it is processed.');
        editing = null;
        deferredRender = false;
        renderSignature = null;
        render();
        expectProjection();
        const again = list.querySelector(`li[data-id="${row.annotation_id}"] button`);
        if (again) again.focus();
      } else {
        say(errorMessage(result.status, errorCode(result.json), !serverRows.some((r) => r.annotation_id === row.annotation_id)), 'error');
        if (result.status === 409) refresh();
        box.focus();
      }
    });
  }

  async function remove(row) {
    if (editing === row.annotation_id || busy.has(row.annotation_id)) return;
    busy.add(row.annotation_id);
    render();
    if (!confirm('Delete this annotation?')) { busy.delete(row.annotation_id); render(); return; }
    say('Deleting');
    const result = await change(urls.remove(row.annotation_id), row.annotation_id, {});
    busy.delete(row.annotation_id);
    if (result.status === 202) {
      const extra = overlay.get(row.annotation_id) || {};
      extra.deleted = true;
      overlay.set(row.annotation_id, extra);
      say('Deleted.');
      renderSignature = null;
      render();
      expectProjection();
      listHead.focus();
    } else {
      renderSignature = null;
      render();
      say(errorMessage(result.status, errorCode(result.json), !serverRows.some((r) => r.annotation_id === row.annotation_id)), 'error');
      if (result.status === 409) refresh();
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const text = cleanText(comment.value);
    if (!text.trim()) { say(errorMessage(400, 'empty'), 'error'); comment.focus(); return; }
    submit.disabled = true;
    say('Saving');
    const sent = anchor;
    const result = await send(urls.create, token, { anchor: sent, comment: text });
    submit.disabled = false;
    const created = result.json && result.json.annotation;
    if (result.status === 201 && created && typeof created.annotation_id === 'string') {
      const row = cleanRow({ annotation_id: created.annotation_id, body: text, status: 'submitted', file: sent.file, anchor: sent });
      if (row) overlay.set(row.annotation_id, { row });
      comment.value = '';
      updateCounter();
      setAnchor(wholeFile());
      hooks.reset();
      say('Submitted. It shows as Open once it is processed.');
      render();
      expectProjection();
      comment.focus();
    } else {
      say(errorMessage(result.status, errorCode(result.json)), 'error');
    }
  });
  reset.addEventListener('click', () => { setAnchor(wholeFile()); hooks.reset(); comment.focus(); });
  if (regionButton) {
    regionButton.addEventListener('click', () => {
      const on = regionButton.getAttribute('aria-pressed') !== 'true';
      regionButton.setAttribute('aria-pressed', String(on));
      hooks.regionMode(on);
    });
  }
  refreshButton.addEventListener('click', async () => { say(''); if (await refresh()) say('Up to date.'); });
  comment.addEventListener('input', updateCounter);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && urls) refresh(); });

  describe();
  updateCounter();
  if (!urls || !token) {
    say('Annotations are not available on this page.', 'error');
    submit.disabled = true;
    return { setAnchor, say, focusComment() {}, focusRow() {} };
  }
  refresh().then(() => say(''));
  return {
    setAnchor,
    say,
    focusComment() { comment.focus(); },
    focusRow(id) {
      const row = list.querySelector(`li[data-id="${id}"]`);
      if (row) { row.scrollIntoView({ block: 'nearest' }); row.focus(); }
    },
  };
}

// ---------------------------------------------------------------------------------------------------- start

async function start() {
  const data = readData();
  const native = document.getElementById('herdr-native');
  if (!data || !native) return;
  const annotate = data.mode === 'annotate';
  const aside = annotate ? document.querySelector('aside[data-annotate-panel]') : null;
  const root = mountContentRoot();
  const model = root ? makeModel(root) : null;
  let panel = null;
  let canvas = { setRegions() {}, regionMode() {} };
  let lastRows = [];
  const highlighter = root ? makeHighlighter(root, model, (id) => panel && panel.focusRow(id)) : null;
  const choose = (anchor) => { if (panel) panel.setAnchor(anchor); };
  const hooks = {
    rows(rows) {
      lastRows = rows;
      if (highlighter) highlighter.apply(rows, data.file);
      const here = rows.filter((r) => r.file === data.file && r.anchor);
      canvas.setRegions(here.filter((r) => (r.anchor.kind === 'image_region' && data.kind === 'image') || (r.anchor.kind === 'pdf_page' && r.anchor.region)));
    },
    reset() {
      const selection = document.getSelection();
      if (selection) selection.removeAllRanges();
    },
    regionMode(on) { canvas.regionMode(on); },
  };
  if (annotate && aside) {
    panel = buildPanel(aside, data, hooks);
    if (root) {
      let timer = 0;
      let latest = 0;
      document.addEventListener('selectionchange', () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          latest += 1;
          const mine = latest;
          const result = await textAnchorFromSelection(root, model, data.file);
          if (!result || mine !== latest) return;
          if (result.anchor) panel.setAnchor(result.anchor);
          else panel.say(result.refused, 'error');
        }, 150);
      });
    }
  }
  if (data.kind === 'image') {
    canvas = drawImage(native, data, annotate ? (box, fromKeyboard) => {
      choose({ kind: 'image_region', file: data.file, ...box });
      if (fromKeyboard && panel) panel.focusComment();
    } : null);
  } else if (data.kind === 'pdf') {
    canvas = await drawPdf(native, data, annotate ? choose : null);
  } else if (data.kind === 'document') {
    drawDocument(native, data);
  }
  hooks.rows(lastRows);
}

start();
