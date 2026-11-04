// Real-browser tests of the live (v/) and annotation (a/) views: a real share-public behind a TLS front, a real browser,
// real selections and drags, no mocks. Set BROWSERS=chromium,firefox,webkit to repeat the cross-browser flows.
//
//   PYTHON=<venv python> node --test tests/playwright/share-annotations.test.mjs
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import * as playwright from 'playwright';

const ROOT = path.resolve(import.meta.dirname, '../..');
const PYTHON = process.env.PYTHON || (fs.existsSync(path.join(ROOT, '.venv/bin/python')) ? path.join(ROOT, '.venv/bin/python') : 'python3');
const WANTED = (process.env.BROWSERS || 'chromium').split(',').map((s) => s.trim()).filter(Boolean);
const CHROMIUM_FALLBACK = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const PANEL = 'aside[data-annotate-panel]';

let server;
let listener;
let info;
const outside = [];
const browsers = new Map();
let userCount = 0;

async function launch(name) {
  const options = { headless: true };
  if (name === 'chromium') {
    options.args = ['--no-sandbox'];
    if (process.env.PLAYWRIGHT_CHROMIUM) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM;
  }
  try {
    return await playwright[name].launch(options);
  } catch (error) {
    if (name !== 'chromium' || !fs.existsSync(CHROMIUM_FALLBACK)) throw error;
    return playwright.chromium.launch({ ...options, executablePath: CHROMIUM_FALLBACK });
  }
}

async function control(method, route) {
  const response = await fetch(info.control + route, { method });
  return response.json();
}
const lines = () => control('GET', '/__lines');
const project = () => control('POST', '/__project');
const signin = (share, user) => control('POST', `/__signin/${share}/${user}`);
const freshUser = () => `user${Date.now().toString(36)}${userCount++}`;

before(async () => {
  listener = http.createServer((req, res) => { outside.push(req.url); res.end(); });
  await new Promise((resolve) => listener.listen(0, '127.0.0.1', resolve));
  server = spawn(PYTHON, [path.join(ROOT, 'tests/playwright/share-annotations-server.py'), String(listener.address().port)],
    { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  let buffer = '';
  info = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('annotation server did not start')), 40000);
    server.stdout.on('data', (d) => {
      buffer += d;
      const row = buffer.split('\n')[1];
      if (!row) return;
      try { const value = JSON.parse(row); clearTimeout(timer); resolve(value); } catch { /* wait for the rest */ }
    });
    server.stderr.on('data', (d) => process.stderr.write(`[share-annotations] ${d}`));
    server.once('exit', (code) => { clearTimeout(timer); reject(new Error(`server exited ${code}`)); });
  });
  for (const name of WANTED) {
    try { browsers.set(name, await launch(name)); } catch (error) { process.stderr.write(`browser ${name} unavailable: ${String(error.message).split('\n')[0]}\n`); }
  }
  assert.ok(browsers.has('chromium'), 'Chromium is required');
});

after(async () => {
  for (const browser of browsers.values()) await browser.close();
  server?.kill();
  listener?.close();
});

// ------------------------------------------------------------------------------------------------ helpers

async function newContext(name = 'chromium', viewport = { width: 1100, height: 800 }) {
  return browsers.get(name).newContext({ ignoreHTTPSErrors: true, viewport, acceptDownloads: true });
}

// A page that records every console error, page error, dialog, popup and request of its context.
async function open(context, url, { dialogs = 'dismiss' } = {}) {
  const page = await context.newPage();
  const seen = { errors: [], dialogs: [], requests: [], popups: 0 };
  page.on('console', (m) => { if (m.type() === 'error') seen.errors.push(m.text()); });
  page.on('pageerror', (e) => seen.errors.push(`pageerror: ${e.message}`));
  page.on('dialog', (d) => { seen.dialogs.push(d.message()); if (dialogs === 'accept') d.accept(); else d.dismiss(); });
  page.on('request', (r) => seen.requests.push({ url: r.url(), method: r.method(), headers: r.headers() }));
  context.on('page', () => { seen.popups += 1; });
  if (url) await page.goto(url);
  return { page, seen };
}

const annotateUrl = (session, file) => `${session.base}a/${file}`;
const panelText = (page) => page.locator(PANEL).evaluate((aside) => aside.shadowRoot.textContent);

async function waitPanel(page) {
  await page.locator(`${PANEL} textarea#comment`).waitFor();
}

// Select characters [from, to) of the first text node of `selector` inside the content's shadow root.
async function selectIn(page, selector, from, to) {
  await page.evaluate(([sel, a, b]) => {
    const root = document.getElementById('herdr-content').shadowRoot;
    const node = root.querySelector(sel).firstChild;
    document.getSelection().setBaseAndExtent(node, a, node, b === null ? node.data.length : b);
  }, [selector, from, to]);
}

async function selectAllOf(page, selector) {
  await page.evaluate((sel) => {
    const root = document.getElementById('herdr-content').shadowRoot;
    document.getSelection().selectAllChildren(root.querySelector(sel));
  }, selector);
}

const anchorQuote = (page) => page.locator(`${PANEL} .anchor-quote`);

async function submitComment(page, text) {
  await page.locator(`${PANEL} textarea#comment`).fill(text);
  await page.locator(`${PANEL} button[type=submit]`).click();
}

async function annotationLines(since = 0) {
  return (await lines()).slice(since);
}

function noErrors(seen, allow = []) {
  const bad = seen.errors.filter((e) => !/favicon/.test(e) && !allow.some((rx) => rx.test(e)));   // Firefox asks for /favicon.ico
  assert.deepEqual(bad, [], 'console errors');
}

function sameOriginOnly(seen) {
  for (const r of seen.requests) assert.ok(r.url.startsWith(info.origin + '/') || r.url.startsWith('data:'), `request left the origin: ${r.url}`);
}

// ------------------------------------------------------------------------------------------------ landing and switch

for (const name of WANTED) {
  test(`${name}: HTML lands live, runs its own script, and the switch navigates the same tab`, async (t) => {
    if (!browsers.has(name)) return t.skip(`${name} is not installed`);
    const context = await newContext(name);
    const session = await signin('html', freshUser());
    const { page, seen } = await open(context, `${info.origin}${session.landing}`);
    assert.match(page.url(), /\/f\/index\.html$/);
    await page.locator('#marker').waitFor();
    assert.equal(await page.locator('#marker').textContent(), 'live script ran');
    const toggle = page.locator('[data-herdr-mode-switch]');
    await toggle.waitFor();
    assert.equal(await toggle.count(), 1);
    assert.equal(await page.evaluate(() => fetch('index.html').then((r) => r.text()).then((x) => x.includes('data-herdr-mode-switch'))), false,
      'sub-requests never carry the link');
    await toggle.click();
    await page.waitForURL(/\/a\/index\.html$/);
    assert.equal(seen.popups, 0, 'no popup');
    assert.deepEqual(seen.errors.filter((e) => /navigat|block|sandbox/i.test(e) && !/favicon/.test(e)), []);
    await page.locator('nav[data-mode-switch] a[data-mode=live]').click();
    await page.waitForURL(/\/f\/index\.html$/);
    await page.locator('[data-herdr-mode-switch]').waitFor();
    await context.close();
  });
}

test('other kinds land on v/ and are drawn; the bar switches both ways', async () => {
  const context = await newContext();
  const user = freshUser();
  const png = await signin('png', user);
  const first = await open(context, `${info.origin}${png.landing}`);
  assert.match(first.page.url(), /\/v\/pic\.png$/);
  await first.page.waitForFunction(() => document.querySelector('#herdr-native img')?.naturalWidth === 200);
  assert.equal(await first.page.locator('nav a[data-mode=live]').getAttribute('aria-current'), 'page');
  await first.page.locator('nav a[data-mode=annotate]').click();
  await first.page.waitForURL(/\/a\/pic\.png$/);
  await first.page.locator('nav a[data-mode=live]').click();
  await first.page.waitForURL(/\/v\/pic\.png$/);
  noErrors(first.seen);

  const pdf = await signin('pdf', user);
  const second = await open(context, `${info.origin}${pdf.landing}`);
  assert.match(second.page.url(), /\/v\/report\.pdf$/);
  await second.page.locator('.herdr-pdf-page[data-drawn=true] canvas').first().waitFor();
  assert.equal(await second.page.locator('.herdr-pdf-page').count(), 3);
  noErrors(second.seen);
  sameOriginOnly(second.seen);
  await context.close();
});

// ------------------------------------------------------------------------------------------------ HTML annotation

for (const name of WANTED) {
  test(`${name}: select, create, project, reload, edit, delete`, async (t) => {
    if (!browsers.has(name)) return t.skip(`${name} is not installed`);
    const context = await newContext(name);
    const session = await signin('html', freshUser());
    const { page, seen } = await open(context, annotateUrl(session, 'index.html'));
    await waitPanel(page);
    const before = (await lines()).length;
    const hostile = '<img src=x onerror=window.__x=1>';

    await selectIn(page, '#first', 4, 19);                      // "quick brown fox"
    await anchorQuote(page).filter({ hasText: 'quick brown fox' }).waitFor();
    await submitComment(page, hostile);
    await page.locator(`${PANEL} .row-body`).filter({ hasText: hostile }).waitFor();
    assert.equal(await page.evaluate(() => window.__x), undefined);
    assert.equal(await page.locator(`${PANEL} .row-body img`).count(), 0, 'comment text is text');
    assert.doesNotMatch(await panelText(page), /\b(null|undefined)\b/, 'no stray null or undefined in the panel');

    const made = await annotationLines(before);
    assert.equal(made.length, 1);
    assert.equal(made[0].op, 'create');
    assert.equal(made[0].anchor.exact, 'quick brown fox');
    assert.ok(made[0].anchor.prefix.endsWith('The ') && made[0].anchor.prefix.length <= 64);
    assert.ok(made[0].anchor.suffix.startsWith(' jumps over the lazy dog. A second sentence follows here.') && made[0].anchor.suffix.length <= 64);
    assert.equal(made[0].body, hostile);

    await project();
    await page.reload();
    await waitPanel(page);
    await page.locator(`${PANEL} .badge[data-status=open]`).waitFor();
    assert.ok(await page.evaluate(() => document.getElementById('herdr-content').shadowRoot.querySelectorAll('mark').length) >= 1, 'own annotation is highlighted');

    await page.locator(`${PANEL} button:has-text("Edit")`).click();
    const edit = page.locator(`${PANEL} li.row textarea`);
    await edit.fill('second thoughts');
    await page.locator(`${PANEL} button:has-text("Save")`).click();
    await page.locator(`${PANEL} .row-body`).filter({ hasText: 'second thoughts' }).waitFor();
    assert.equal((await annotationLines(before)).at(-1).op, 'edit');
    await project();
    await page.locator(`${PANEL} .badge`).filter({ hasText: 'Open, edited' }).waitFor({ timeout: 15000 });

    await page.evaluate(() => { window.confirm = () => true; });    // the page's own confirm; Playwright would accept it too
    page.once('dialog', () => {});
    await page.locator(`${PANEL} button:has-text("Delete")`).click();
    await page.locator(`${PANEL} li.empty`).waitFor();
    assert.equal((await annotationLines(before)).at(-1).op, 'delete');
    noErrors(seen);
    sameOriginOnly(seen);
    await context.close();
  });
}

test('an in-content link on a/ stays on a/ and the next annotation names that file', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page, seen } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  // the sanitiser gives every content link target=_blank with rel=noopener, so the second page opens in its own tab
  const [second] = await Promise.all([context.waitForEvent('page'), page.locator('#next').click()]);
  await second.waitForURL(/\/a\/page2\.html$/);
  const target = { page: second, seen };
  await waitPanel(target.page);
  const before = (await lines()).length;
  await submitComment(target.page, 'on page two');
  await target.page.locator(`${PANEL} .row-body`).filter({ hasText: 'on page two' }).waitFor();
  const made = await annotationLines(before);
  assert.equal(made[0].anchor.file, 'page2.html');
  assert.equal(made[0].anchor.kind, 'document');
  noErrors(seen);
  await context.close();
});

test('selections: spanning elements, RTL, emoji, CR, and a huge selection', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page, seen } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  const take = async (selector, comment) => {
    const before = (await lines()).length;
    await selectAllOf(page, selector);
    await page.waitForTimeout(400);
    await submitComment(page, comment);
    await page.locator(`${PANEL} .row-body`).filter({ hasText: comment }).waitFor();
    const made = await annotationLines(before);
    assert.equal(made.length, 1, selector);
    return made[0].anchor;
  };
  const spanning = await take('#span', 'spans bold and italic');
  assert.equal(spanning.exact, 'Alpha bold and italic end of line.');
  const rtl = await take('#rtl', 'rtl');
  assert.ok(rtl.exact.includes('مرحبا') && rtl.exact.includes('שלום'));
  const emoji = await take('#emoji', 'emoji');
  assert.ok(emoji.exact.includes('🎉😀') && emoji.exact.includes('👨‍👩‍👧'));
  const cr = await take('#cr', 'carriage');
  assert.ok(!/\r/.test(JSON.stringify(cr)), 'no carriage return is ever sent');
  const huge = await take('#huge', 'huge selection');
  assert.equal(huge.exact.length, 1000);
  assert.ok(huge.prefix.length <= 64 && huge.suffix.length <= 64);
  noErrors(seen);
  await context.close();
});

test('a triple click (a real pointer selection) is captured', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  await page.locator('#first').click({ clickCount: 3 });
  await anchorQuote(page).filter({ hasText: 'quick brown fox' }).waitFor();
  await page.locator(`${PANEL} button:has-text("Whole file instead")`).click();
  assert.match(await panelText(page), /Whole file: index\.html/);
  await context.close();
});

// ------------------------------------------------------------------------------------------------ hostile content

test('evil.html on a/: no script, no frames, no external request; bar and panel work; submit works', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const before = outside.length;
  const { page, seen } = await open(context, annotateUrl(session, 'evil.html'));
  await waitPanel(page);
  await page.waitForTimeout(500);
  assert.deepEqual(seen.dialogs, []);
  assert.equal(await page.evaluate(() => window.__x), undefined);
  const frames = await page.evaluate(() => {
    const count = (root) => root.querySelectorAll('iframe,frame,object,embed').length;
    return count(document) + count(document.getElementById('herdr-content').shadowRoot);
  });
  assert.equal(frames, 0);
  assert.equal(await page.locator('nav[data-mode-switch]').isVisible(), true);
  assert.equal(await page.locator(PANEL).isVisible(), true);
  assert.equal(await page.locator('aside[data-annotate-panel]').count(), 1, 'the content cannot add a second panel');
  const before2 = (await lines()).length;
  await submitComment(page, 'works despite the hostile page');
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'works despite' }).waitFor();
  assert.equal((await annotationLines(before2)).length, 1);
  assert.equal(outside.length, before, 'nothing reached the external listener');
  assert.deepEqual(seen.dialogs, []);
  sameOriginOnly(seen);
  noErrors(seen, [/Refused to|Content Security Policy|Failed to load resource/]);
  await context.close();
});

test('evil.html on f/ (the accepted tradeoff): the link is injected once and a/ is reachable by URL', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const raw = await context.request.get(`${session.base}f/evil.html`, { headers: { 'Sec-Fetch-Dest': 'document' } });
  const body = await raw.text();
  assert.equal(body.split('data-herdr-mode-switch').length - 1, 1);
  const view = await context.request.get(annotateUrl(session, 'evil.html'));
  assert.equal(view.status(), 200);
  await context.close();
});

test('the raw file on f/ cannot forge an annotation or read the annotation page', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page } = await open(context, `${session.base}f/index.html`);
  await page.locator('#marker').waitFor();
  const before = (await lines()).length;
  const result = await page.evaluate(async () => {
    const out = {};
    try {
      const r = await fetch('../annotations', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Herdr-Annotate': 'x'.repeat(43) },
        body: JSON.stringify({ anchor: { kind: 'document', file: 'index.html' }, comment: 'forged' }) });
      out.post = r.status;
    } catch (e) { out.post = `threw:${e.name}`; }
    try {
      const r = await fetch('../a/index.html');
      out.read = (await r.text()).length;
    } catch (e) { out.read = `threw:${e.name}`; }
    return out;
  });
  assert.ok(typeof result.post === 'string' || result.post === 404, JSON.stringify(result));
  assert.equal(typeof result.read, 'string', 'the body of the annotation page is unreadable from the raw file');
  assert.equal((await lines()).length, before);
  await context.close();
});

test('hostile CSS cannot cover the panel or the bar (hit test)', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  for (const file of ['cover.html', 'evil.html']) {
    const { page } = await open(context, annotateUrl(session, file));
    await waitPanel(page);
    await page.waitForTimeout(400);
    const hits = await page.evaluate(() => {
      const aside = document.querySelector('aside[data-annotate-panel]');
      const rect = aside.getBoundingClientRect();
      const out = [];
      for (const [fx, fy] of [[0.5, 0.2], [0.2, 0.1], [0.8, 0.3]]) {
        const el = document.elementFromPoint(rect.left + rect.width * fx, Math.min(window.innerHeight - 2, rect.top + 200 * fy * 5));
        out.push(el === aside ? 'panel' : `${el && el.tagName}`);
      }
      const nav = document.querySelector('nav[data-mode-switch] a[data-mode=annotate]').getBoundingClientRect();
      const bar = document.elementFromPoint(nav.left + nav.width / 2, nav.top + nav.height / 2);
      out.push(bar && bar.closest('nav[data-mode-switch]') ? 'nav' : `${bar && bar.tagName}`);
      return out;
    });
    assert.deepEqual(hits, ['panel', 'panel', 'panel', 'nav'], file);
    await page.locator(`${PANEL} textarea#comment`).click();
    await page.keyboard.type('still clickable');
    assert.equal(await page.locator(`${PANEL} textarea#comment`).inputValue(), 'still clickable');
    await page.close();
  }
  await context.close();
});

// ------------------------------------------------------------------------------------------------ requests and CSRF

test('the CSRF header goes only on same-origin annotation POSTs; nothing else is requested', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page, seen } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  await page.waitForTimeout(500);
  await selectIn(page, '#first', 4, 9);
  await anchorQuote(page).filter({ hasText: 'quick' }).waitFor();
  await submitComment(page, 'csrf check');
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'csrf check' }).waitFor();
  const token = await page.locator('meta[name="herdr-annotate-csrf"]').getAttribute('content');
  assert.ok(token && token.length >= 20);
  sameOriginOnly(seen);
  for (const r of seen.requests) {
    const header = r.headers['x-herdr-annotate'];
    const isAnnotationPost = r.method === 'POST' && /\/k\/[^/]+\/annotations(\/ann_[A-Za-z0-9_-]{22}\/(edit|delete))?$/.test(new URL(r.url).pathname);
    if (header !== undefined) {
      assert.ok(isAnnotationPost, `the CSRF header went to ${r.method} ${r.url}`);
      assert.equal(header, token);
      assert.equal(r.headers['content-type'], 'application/json', 'exactly application/json');
    }
    if (isAnnotationPost) assert.equal(header, token);
  }
  assert.ok(seen.requests.some((r) => r.method === 'GET' && r.url.endsWith('/annotations') && r.headers['x-herdr-annotate'] === undefined), 'the list GET carries no token');
  const storage = await page.evaluate(() => ({ cookie: document.cookie, ls: (() => { try { return localStorage.length; } catch { return -1; } })() }));
  assert.equal(storage.cookie, '');
  assert.ok(!JSON.stringify(await page.context().cookies()).includes(token));
  const text = await page.evaluate(() => document.body.innerText + document.querySelector('aside').shadowRoot.textContent);
  assert.ok(!text.includes(token), 'the token is never written into the page text');
  await context.close();
});

// ------------------------------------------------------------------------------------------------ capture per kind

test('the counter and the maximum length: the character after the limit is refused', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  const box = page.locator(`${PANEL} textarea#comment`);
  assert.equal(await box.getAttribute('maxlength'), String(info.maxComment));
  await box.click();
  await page.keyboard.type('a'.repeat(info.maxComment + 5), { delay: 0 });
  assert.equal((await box.inputValue()).length, info.maxComment);
  assert.match(await panelText(page), new RegExp(`${info.maxComment} / ${info.maxComment}`));
  await context.close();
});

test('PDF: page 3 text selection, a region, and isEvalSupported is false', async () => {
  const context = await newContext();
  const session = await signin('pdf', freshUser());
  const { page, seen } = await open(context, annotateUrl(session, 'report.pdf'));
  await waitPanel(page);
  await page.locator('.herdr-pdf-page[data-page="3"]').scrollIntoViewIfNeeded();
  await page.locator('.herdr-pdf-page[data-page="3"][data-drawn=true] .textLayer span').first().waitFor({ timeout: 20000 });
  await page.evaluate(() => {
    const layer = document.querySelector('.herdr-pdf-page[data-page="3"] .textLayer');
    document.getSelection().selectAllChildren(layer);
  });
  await anchorQuote(page).filter({ hasText: 'target sentence' }).waitFor();
  const before = (await lines()).length;
  await submitComment(page, 'pdf text');
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'pdf text' }).waitFor();
  const made = (await annotationLines(before))[0].anchor;
  assert.equal(made.kind, 'pdf_page');
  assert.equal(made.page, 3);
  assert.match(made.exact, /Page three target sentence here/);

  await page.locator('.herdr-pdf-page[data-page="1"]').scrollIntoViewIfNeeded();
  await page.locator('.herdr-pdf-page[data-page="1"][data-drawn=true]').waitFor();
  await page.evaluate(() => document.getSelection().removeAllRanges());
  await page.locator(`${PANEL} button:has-text("Mark region")`).click();
  assert.equal(await page.locator(`${PANEL} button:has-text("Mark region")`).getAttribute('aria-pressed'), 'true');
  const rect = await page.locator('.herdr-pdf-page[data-page="1"]').boundingBox();
  await page.mouse.move(rect.x + rect.width * 0.1, rect.y + rect.height * 0.1);
  await page.mouse.down();
  await page.mouse.move(rect.x + rect.width * 0.5, rect.y + rect.height * 0.4, { steps: 5 });
  await page.mouse.up();
  await page.locator(`${PANEL} .anchor-title`).filter({ hasText: /Page 1, region/ }).waitFor();
  await submitComment(page, 'pdf region');
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'pdf region' }).waitFor();
  const region = (await annotationLines(before)).at(-1).anchor;
  assert.equal(region.page, 1);
  for (const key of ['x', 'y', 'w', 'h']) assert.ok(region.region[key] >= 0 && region.region[key] <= 1);
  assert.ok(region.region.x + region.region.w <= 1 && region.region.y + region.region.h <= 1);
  noErrors(seen);
  sameOriginOnly(seen);

  // the vendored module is the only pdf.js code; every getDocument call passes the hardened options
  const source = fs.readFileSync(path.join(ROOT, 'herdr_engineering/control_plane/share_static/annotate.js'), 'utf8');
  const calls = source.match(/getDocument\(\{[^}]*\}/gs) || [];
  assert.equal(calls.length, 1);
  assert.match(calls[0], /isEvalSupported:\s*false/);
  assert.match(calls[0], /enableXfa:\s*false/);
  await context.close();
});

test('image: a pointer drag gives an image_region in [0, 1]', async () => {
  const context = await newContext();
  const session = await signin('png', freshUser());
  const { page, seen } = await open(context, annotateUrl(session, 'pic.png'));
  await waitPanel(page);
  await page.waitForFunction(() => document.querySelector('#herdr-native img')?.naturalWidth === 200);
  const rect = await page.locator('.herdr-image-box img').boundingBox();
  await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.25);
  await page.mouse.down();
  await page.mouse.move(rect.x + rect.width * 0.7, rect.y + rect.height * 0.9, { steps: 6 });
  await page.mouse.up();
  await page.locator(`${PANEL} .anchor-title`).filter({ hasText: /Image region/ }).waitFor();
  const before = (await lines()).length;
  await submitComment(page, 'image region');
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'image region' }).waitFor();
  const made = (await annotationLines(before))[0].anchor;
  assert.equal(made.kind, 'image_region');
  assert.ok(Math.abs(made.x - 0.2) < 0.03 && Math.abs(made.y - 0.25) < 0.03 && Math.abs(made.w - 0.5) < 0.03 && Math.abs(made.h - 0.65) < 0.03, JSON.stringify(made));
  assert.ok(made.x + made.w <= 1 && made.y + made.h <= 1);
  noErrors(seen);
  await context.close();
});

for (const name of WANTED.filter((n) => n !== 'chromium')) {
  test(`${name}: image drag and PDF drawing`, async (t) => {
    if (!browsers.has(name)) return t.skip(`${name} is not installed`);
    const context = await newContext(name);
    const session = await signin('png', freshUser());
    const { page } = await open(context, annotateUrl(session, 'pic.png'));
    await waitPanel(page);
    await page.waitForFunction(() => document.querySelector('#herdr-native img')?.naturalWidth === 200);
    const rect = await page.locator('.herdr-image-box img').boundingBox();
    await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
    await page.mouse.down();
    await page.mouse.move(rect.x + rect.width * 0.6, rect.y + rect.height * 0.6, { steps: 4 });
    await page.mouse.up();
    await page.locator(`${PANEL} .anchor-title`).filter({ hasText: /Image region/ }).waitFor();
    await context.close();
  });
}

test('a file of another type takes a whole-file comment (document anchor)', async () => {
  const context = await newContext();
  const session = await signin('par', freshUser());
  const { page, seen } = await open(context, annotateUrl(session, 'data.parquet'));
  await waitPanel(page);
  assert.equal(await page.locator('#herdr-native a[download]').count(), 1);
  const before = (await lines()).length;
  await submitComment(page, 'whole file note');
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'whole file note' }).waitFor();
  assert.deepEqual((await annotationLines(before))[0].anchor, { kind: 'document', file: 'data.parquet' });
  noErrors(seen);
  await context.close();
});

// ------------------------------------------------------------------------------------------------ errors and retries

test('503 keeps the text; 409 retries until the row is projected; a dismissed row cannot be changed', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);

  await control('POST', '/__break');
  await submitComment(page, 'while the file is broken');
  await page.locator(`${PANEL} .message[data-kind=error]`).filter({ hasText: 'unavailable right now' }).waitFor();
  assert.equal(await page.locator(`${PANEL} textarea#comment`).inputValue(), 'while the file is broken', 'the text is kept');
  await control('POST', '/__fix');
  await page.locator(`${PANEL} button[type=submit]`).click();
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'while the file is broken' }).waitFor();

  // edit straight away: 409 until the tick runs, and the panel retries on its own
  await page.locator(`${PANEL} button:has-text("Edit")`).click();
  await page.locator(`${PANEL} li.row textarea`).fill('edited quickly');
  await page.locator(`${PANEL} button:has-text("Save")`).click();
  await page.locator(`${PANEL} .message`).filter({ hasText: 'Still saving' }).waitFor();
  await project();
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'edited quickly' }).waitFor({ timeout: 20000 });
  assert.equal((await lines()).at(-1).op, 'edit');

  // the owner dismisses it; the panel still shows Edit until it looks again, and then refuses with a fixed message
  const id = (await lines()).at(-1).annotation_id;
  await project();
  await control('POST', `/__status/${id}/dismissed`);
  await page.locator(`${PANEL} button:has-text("Edit")`).click();
  await page.locator(`${PANEL} li.row textarea`).fill('too late');
  await page.locator(`${PANEL} button:has-text("Save")`).click();
  await page.locator(`${PANEL} .message[data-kind=error]`).filter({ hasText: 'can no longer be changed' }).waitFor({ timeout: 20000 });
  await page.locator(`${PANEL} button:has-text("Cancel")`).click();
  await page.locator(`${PANEL} .badge[data-status=dismissed]`).waitFor();
  assert.equal(await page.locator(`${PANEL} button:has-text("Edit")`).count(), 0, 'dismissed rows have no actions');
  await context.close();
});

test('429 is shown as a fixed message', async () => {
  const context = await newContext();
  const session = await signin('html', 'ratelimited');
  const { page } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  await page.evaluate(async () => {
    const token = document.querySelector('meta[name="herdr-annotate-csrf"]').content;
    for (let i = 0; i < 61; i += 1) {
      await fetch('../annotations', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Herdr-Annotate': token },
        body: JSON.stringify({ anchor: { kind: 'document', file: 'index.html' }, comment: `bulk ${i}` }) });
    }
  });
  await submitComment(page, 'one too many');
  await page.locator(`${PANEL} .message[data-kind=error]`).filter({ hasText: 'Too many annotations' }).waitFor();
  assert.equal(await page.locator(`${PANEL} textarea#comment`).inputValue(), 'one too many');
  await context.close();
});

test('statuses: Open, Addressed in v2, Dismissed, Submitted', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  const before = (await lines()).length;
  for (const text of ['s one', 's two']) {
    await submitComment(page, text);
    await page.locator(`${PANEL} .row-body`).filter({ hasText: text }).waitFor();
  }
  await page.locator(`${PANEL} .badge[data-status=submitted]`).first().waitFor();
  const ids = (await annotationLines(before)).map((l) => l.annotation_id);
  await project();
  await control('POST', `/__status/${ids[0]}/addressed`);
  await control('POST', `/__status/${ids[1]}/dismissed`);
  await page.locator(`${PANEL} button:has-text("Refresh")`).click();
  await page.locator(`${PANEL} .badge`).filter({ hasText: 'Addressed in v2' }).waitFor({ timeout: 15000 });
  await page.locator(`${PANEL} .badge`).filter({ hasText: 'Dismissed' }).waitFor();
  await context.close();
});

// ------------------------------------------------------------------------------------------------ keyboard, layout

test('keyboard: tab order, Enter submits, Escape cancels an edit and focus returns to Edit', async () => {
  const context = await newContext();
  const session = await signin('html', freshUser());
  const { page } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  const focused = () => page.evaluate(() => {
    const active = document.querySelector('aside[data-annotate-panel]').shadowRoot.activeElement;
    return active ? `${active.tagName.toLowerCase()}${active.type ? `:${active.type}` : ''}|${active.textContent.slice(0, 20)}` : 'none';
  });
  await page.locator(`${PANEL} textarea#comment`).focus();
  await page.keyboard.type('typed by keyboard');
  await page.keyboard.press('Tab');
  assert.match(await focused(), /^button:submit\|Submit/);
  await page.keyboard.press('Enter');
  await page.locator(`${PANEL} .row-body`).filter({ hasText: 'typed by keyboard' }).waitFor();
  assert.match(await focused(), /^textarea/, 'focus returns to the comment box');
  await page.locator(`${PANEL} button:has-text("Edit")`).focus();
  await page.keyboard.press('Enter');
  assert.match(await focused(), /^textarea/, 'focus moves into the edit box');
  await page.keyboard.press('Escape');
  assert.match(await focused(), /^button:button\|Edit/, 'Escape cancels and focus returns to Edit');
  assert.equal(await page.locator(`${PANEL} li.row textarea`).count(), 0);
  for (const label of await page.locator(`${PANEL} li.row button`).evaluateAll((b) => b.map((x) => x.getAttribute('aria-label')))) assert.ok(label);
  await context.close();
});

test('narrow viewport: no horizontal scroll and the panel is below the content', async () => {
  const context = await newContext('chromium', { width: 320, height: 640 });
  const session = await signin('html', freshUser());
  const { page } = await open(context, annotateUrl(session, 'index.html'));
  await waitPanel(page);
  await page.waitForTimeout(300);
  const layout = await page.evaluate(() => {
    const aside = document.querySelector('aside[data-annotate-panel]').getBoundingClientRect();
    const stage = document.getElementById('herdr-stage').getBoundingClientRect();
    return { over: document.documentElement.scrollWidth - document.documentElement.clientWidth, asideTop: aside.top + scrollY, stageBottom: stage.bottom + scrollY };
  });
  assert.ok(layout.over <= 0, `horizontal overflow ${layout.over}`);
  assert.ok(layout.asideTop >= layout.stageBottom - 1, JSON.stringify(layout));
  await context.close();
});

// ------------------------------------------------------------------------------------------------ public and revoked

test('a public share lands on raw f/ with no link and has no v/ or a/ routes; a forged POST is the 404', async () => {
  const context = await newContext();
  const page = await context.newPage();
  await page.goto(info.public);
  assert.match(page.url(), /\/f\/index\.html$/);
  assert.equal(await page.locator('[data-herdr-mode-switch]').count(), 0);
  const base = `${info.origin}/s/${info.tokens.pub}`;
  const fake = 'x'.repeat(40);
  for (const route of ['v', 'a']) assert.equal((await context.request.get(`${base}/k/${fake}/${route}/index.html`)).status(), 404);
  const post = await context.request.post(`${base}/k/${fake}/annotations`, { data: { anchor: { kind: 'document', file: 'index.html' }, comment: 'x' },
    headers: { 'X-Herdr-Annotate': 'y'.repeat(43), Origin: info.origin } });
  assert.equal(post.status(), 404);
  await context.close();
});

test('revoking mid-session shows "session ended" and keeps the typed text', async () => {
  const context = await newContext();
  const session = await signin('par', freshUser());
  const { page } = await open(context, annotateUrl(session, 'data.parquet'));
  await waitPanel(page);
  await page.locator(`${PANEL} textarea#comment`).fill('typed before the revoke');
  await control('POST', '/__revoke/par');
  await page.locator(`${PANEL} button[type=submit]`).click();
  await page.locator(`${PANEL} .message[data-kind=error]`).filter({ hasText: 'session ended' }).waitFor();
  assert.equal(await page.locator(`${PANEL} textarea#comment`).inputValue(), 'typed before the revoke');
  await context.close();
});
