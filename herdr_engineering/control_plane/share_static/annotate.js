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
  LIMITS, POLL_DELAYS, RETRY_DELAYS, annotationUrls, cleanRow, cleanText, describeAnchor, errorMessage, findQuote,
  isRawHref, statusLabel, textQuote, textQuoteFromString, unitBox,
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

// The text nodes of the content that carry visible text, in document order, with their offsets in the joined text.
function buildIndex(root) {
  const entries = [];
  const parts = [];
  let length = 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => (node.parentElement && SKIP_TEXT.has(node.parentElement.tagName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    entries.push({ node, start: length });
    parts.push(node.data);
    length += node.data.length;
  }
  return { entries, text: parts.join(''), byNode: new Map(entries.map((e) => [e.node, e.start])) };
}

// The offset in the joined text of a boundary point (container, offset).
function offsetOf(index, container, offset) {
  const known = index.byNode.get(container);
  if (known !== undefined) return known + Math.min(offset, container.data.length);
  const probe = document.createRange();
  try {
    probe.setStart(container, offset);
  } catch {
    return -1;
  }
  probe.collapse(true);
  let lo = 0;
  let hi = index.entries.length;
  while (lo < hi) {   // the first text node at or after the boundary
    const mid = (lo + hi) >> 1;
    if (probe.comparePoint(index.entries[mid].node, 0) >= 0) hi = mid;
    else lo = mid + 1;
  }
  return lo < index.entries.length ? index.entries[lo].start : index.text.length;
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

function textAnchorFromSelection(root, file) {
  const selection = document.getSelection();
  if (!selection || (selection.isCollapsed && !selection.toString())) return null;
  const index = buildIndex(root);
  const points = selectionPoints(root);
  let quote = null;
  if (points) {
    const start = offsetOf(index, points.startContainer, points.startOffset);
    const end = offsetOf(index, points.endContainer, points.endOffset);
    if (start >= 0 && end >= 0) quote = textQuote(index.text, start, end);
  } else if (selection.anchorNode === root.host || selection.focusNode === root.host) {
    quote = textQuoteFromString(index.text, selection.toString());   // a browser that only reports the host
  }
  return quote ? { kind: 'text_quote', file, ...quote } : null;
}

// ---------------------------------------------------------------------------------------------------- highlights

function makeHighlighter(root, onPick) {
  let marks = [];
  function clear() {
    for (const mark of marks) {
      const parent = mark.parentNode;
      if (!parent) continue;
      mark.replaceWith(...mark.childNodes);
      parent.normalize();
    }
    marks = [];
  }
  function wrap(row) {
    const index = buildIndex(root);
    const at = findQuote(index.text, row.anchor);
    if (at < 0) return;
    const end = at + row.anchor.exact.length;
    const hits = index.entries.filter((e) => e.start < end && e.start + e.node.data.length > at);
    for (const entry of hits.reverse()) {
      const from = Math.max(0, at - entry.start);
      const to = Math.min(entry.node.data.length, end - entry.start);
      if (to <= from) continue;
      try {
        const range = document.createRange();
        range.setStart(entry.node, from);
        range.setEnd(entry.node, to);
        const mark = document.createElement('mark');
        mark.style.setProperty('background', row.status === 'open' || row.status === 'submitted' || row.status === 'edited' ? 'rgba(255, 200, 0, 0.5)' : 'rgba(150, 150, 150, 0.35)');
        mark.style.setProperty('color', 'inherit');
        mark.style.setProperty('cursor', 'pointer');
        mark.addEventListener('click', () => onPick(row.annotation_id));
        range.surroundContents(mark);
        marks.push(mark);
      } catch {
        /* the range cannot be wrapped: the row stays in the list only */
      }
    }
  }
  return {
    apply(rows, file) {
      clear();
      let count = 0;
      for (const row of rows) {
        if (count >= MAX_HIGHLIGHTS) break;
        if (row.file === file && row.anchor && row.anchor.kind === 'text_quote' && typeof row.anchor.exact === 'string') {
          wrap(row);
          count += 1;
        }
      }
    },
  };
}

// ---------------------------------------------------------------------------------------------------- image

function drawImage(native, data, onRegion) {
  const box = h('div', { class: 'herdr-image-box', tabindex: '0', role: 'img', 'aria-label': `Image ${data.file}` });
  const image = h('img', { alt: data.file, draggable: 'false' });
  image.src = data.raw_href;
  const layer = h('div', { class: 'herdr-region-layer', 'aria-hidden': 'true' });
  const live = h('div', { class: 'herdr-region herdr-region-live' });
  live.hidden = true;
  box.append(image, layer, live);
  native.append(box);
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
  const first = await doc.getPage(1);
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
        const text = cleanText(selection.toString()).trim();
        if (page && text) onAnchor({ kind: 'pdf_page', file: data.file, page, exact: text.slice(0, LIMITS.exact) });
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
  // No explicit mode: with the default (cors) mode the POST keeps its Origin header; mode 'same-origin' made Firefox and
  // WebKit send "Origin: null" (verified), which the server refuses.
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
      const editButton = h('button', { type: 'button', text: 'Edit', 'aria-label': 'Edit this annotation' });
      editButton.addEventListener('click', () => startEdit(row, li, body, editButton));
      const removeButton = h('button', { type: 'button', class: 'danger', text: 'Delete', 'aria-label': 'Delete this annotation' });
      removeButton.addEventListener('click', () => remove(row));
      li.append(h('div', { class: 'actions' }, editButton, removeButton));
    }
    return li;
  }
  function render() {
    if (editing) { deferredRender = true; return; }
    const rows = merged();
    list.replaceChildren(...(rows.length ? rows.map(renderRow) : [h('li', { class: 'empty', text: 'Nothing yet.' })]));
    hooks.rows(rows);
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
    if (editing) return;
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
      save.disabled = true;
      say('Saving');
      const result = await change(urls.edit(row.annotation_id), row.annotation_id, { comment: text });
      save.disabled = false;
      if (result.status === 202) {
        const extra = overlay.get(row.annotation_id) || {};
        extra.body = text;
        overlay.set(row.annotation_id, extra);
        say('Saved. The change shows as Edited until it is processed.');
        editing = null;
        deferredRender = false;
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
    if (!confirm('Delete this annotation?')) return;
    say('Deleting');
    const result = await change(urls.remove(row.annotation_id), row.annotation_id, {});
    if (result.status === 202) {
      const extra = overlay.get(row.annotation_id) || {};
      extra.deleted = true;
      overlay.set(row.annotation_id, extra);
      say('Deleted.');
      render();
      expectProjection();
      listHead.focus();
    } else {
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
    return { setAnchor, focusRow() {} };
  }
  refresh().then(() => say(''));
  return {
    setAnchor,
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
  let panel = null;
  let canvas = { setRegions() {}, regionMode() {} };
  let lastRows = [];
  const highlighter = root ? makeHighlighter(root, (id) => panel && panel.focusRow(id)) : null;
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
      document.addEventListener('selectionchange', () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          const anchor = textAnchorFromSelection(root, data.file);
          if (anchor) panel.setAnchor(anchor);
        }, 150);
      });
    }
  }
  if (data.kind === 'image') {
    canvas = drawImage(native, data, annotate ? (box) => choose({ kind: 'image_region', file: data.file, ...box }) : null);
  } else if (data.kind === 'pdf') {
    canvas = await drawPdf(native, data, annotate ? choose : null);
  } else if (data.kind === 'document') {
    drawDocument(native, data);
  }
  hooks.rows(lastRows);
}

start();
