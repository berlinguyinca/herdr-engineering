// Browser-level checks for canonical fabric links in the session workspace (spec herdr-fabric-url-resolution-20261028).
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';

const LOCAL = 'http://localhost:3188/benchmarks?x=1#top';

async function withWorkspace(run) {
  const root = path.resolve(import.meta.dirname, '../../packages');
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('content-type', 'text/html'); res.end('<!doctype html><link rel="stylesheet" href="/herdr-design-system/theme.css"><link rel="stylesheet" href="/herdr-design-system/base.css"><link rel="stylesheet" href="/herdr-web-components/components.css"><link rel="stylesheet" href="/conversation.css"><div id="host" style="padding:20px"></div>'); return; }
    const file = path.resolve(root, '.' + req.url.split('?')[0]);
    if (!file.startsWith(root + path.sep)) { res.writeHead(404); res.end(); return; }
    try { res.setHeader('content-type', file.endsWith('.css') ? 'text/css' : 'text/javascript'); res.end(await readFile(file)); } catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    browser = await chromium.launch({headless: true, args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_CHROMIUM || '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'});
    const context = await browser.newContext({viewport: {width: 1400, height: 1000}, permissions: ['clipboard-read', 'clipboard-write']});
    const page = await context.newPage();
    const errors = []; page.on('pageerror', (e) => errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(async (local) => {
      const {mountSessionWorkspace} = await import('/session-workspace.js');
      Object.defineProperty(window.crypto, 'randomUUID', {value: undefined, configurable: true});
      window.local = local;
      window.resolveCalls = [];
      // what the gateway answers, by exact URL string; a test flips this table to simulate the service going away
      window.table = {[local]: {original: local, status: 'resolved', canonical: 'https://dev.lan:3188/benchmarks?x=1#top', service: 'web', host: '127.0.0.1'}};
      window.data = {events: [{seq: 1, rev: 1, kind: 'message', agent_id: 'main', data: {role: 'assistant', text: `Dashboard is at ${local} now.`}}], rev: 1, epoch: 'e', has_more: false,
        summary: {agents: [{agent_id: 'main'}], files: [], association: {}, counts: {message: 1}}};
      window.workspaceClient = {
        getInspector: async () => structuredClone(window.data), inspectorStreamUrl: () => '/stream',
        getInspectorEvent: async () => ({}), listSessions: async () => ({sessions: []}), sendMessage: async () => ({}),
        resolveFabricUrls: async (urls, machine) => {
          window.resolveCalls.push({urls: [...urls], machine});
          return {resolutions: urls.map((u) => window.table[u] || {original: u, canonical: null, status: 'unmapped', reason: ''})};
        },
      };
      window.view = mountSessionWorkspace(document.querySelector('#host'), window.workspaceClient,
        {session_id: 's', status: 'working', agent_session: {kind: 'id', value: 'real'}, terminal_id: 'term_s', host_id: 'bender'},
        {connect: (options) => { window.stream = options; return () => {}; }});
    }, LOCAL);
    await page.waitForSelector('[data-message-seq="1"]');
    await run(page);
    assert.deepEqual(errors, []);
  } finally { if (browser) await browser.close(); await new Promise((r) => server.close(r)); }
}

test('a localhost URL in agent output becomes the canonical https link, asking for the session\'s own host', () => withWorkspace(async (page) => {
  const link = page.locator('[data-message-seq="1"] a.md-fabric-link');
  await link.waitFor();
  assert.equal(await link.getAttribute('href'), 'https://dev.lan:3188/benchmarks?x=1#top');
  assert.equal(await link.getAttribute('data-original'), LOCAL);
  assert.equal(await link.getAttribute('rel'), 'noopener noreferrer nofollow');
  const calls = await page.evaluate(() => window.resolveCalls);
  assert.equal(calls[0].machine, 'bender');            // sourceMachine comes from the session's host_id
  assert.deepEqual(calls[0].urls, [LOCAL]);
  assert.match(await link.getAttribute('title'), /Original: http:\/\/localhost:3188[\s\S]*Fabric: https:\/\/dev\.lan:3188[\s\S]*Service: web[\s\S]*Host: 127\.0\.0\.1/);
}));

test('copy buttons put the exact original or fabric URL on the clipboard, and are reachable on touch', () => withWorkspace(async (page) => {
  await page.locator('a.md-fabric-link').waitFor();
  await page.locator('a.md-fabric-link').hover();
  await page.locator('[data-copy]', {hasText: 'Copy original'}).click();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), LOCAL);
  await page.locator('[data-copy]', {hasText: 'Copy fabric URL'}).click();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'https://dev.lan:3188/benchmarks?x=1#top');
  assert.equal(await page.locator('[data-copy]', {hasText: 'Copy fabric URL'}).getAttribute('data-copied'), 'true');
  // hover-less devices: the actions must not be hidden
  const touch = await page.evaluate(() => [...document.styleSheets].flatMap((s) => [...s.cssRules]).some((r) => r.media && /hover:\s*none/.test(r.media.mediaText) && /md-fabric-actions/.test(r.cssText)));
  assert.equal(touch, true);
}));

test('the stored message text is never rewritten', () => withWorkspace(async (page) => {
  await page.locator('a.md-fabric-link').waitFor();
  assert.equal(await page.evaluate(() => window.data.events[0].data.text), `Dashboard is at ${LOCAL} now.`);
}));

test('a service that is not registered shows an explained non-link, not a dead localhost link', () => withWorkspace(async (page) => {
  await page.evaluate(() => {
    window.table = {};
    window.data = {...window.data, events: [{seq: 1, rev: 1, kind: 'message', agent_id: 'main', data: {role: 'assistant', text: 'try http://localhost:9999/x'}}]};
    window.stream.onEvent({type: 'event', event: {seq: 2, rev: 2, kind: 'message', agent_id: 'main', data: {role: 'assistant', text: 'also http://localhost:9999/y '}}});
  });
  const note = page.locator('[data-message-seq="2"] .md-link-unreachable');
  await note.waitFor();
  assert.match(await note.getAttribute('title'), /No fabric service is registered/);
  assert.equal(await page.locator('[data-message-seq="2"] a[href*="9999"]').count(), 0);
}));

test('a URL still streaming in is left as written, then resolves once the stream settles', () => withWorkspace(async (page) => {
  const full = 'http://localhost:4000/live';
  await page.evaluate((full) => {
    window.table[full] = {original: full, status: 'resolved', canonical: 'https://dev.lan:4000/live', service: 'live'};
    window.stream.onEvent({type: 'event', event: {seq: 2, rev: 2, kind: 'message', agent_id: 'main', data: {role: 'assistant', text: 'now serving http://localhost:40'}}});
  }, full);
  const msg = page.locator('[data-message-seq="2"]');
  await msg.waitFor();
  assert.equal(await msg.locator('.md-link-unreachable, a.md-fabric-link').count(), 0);   // the prefix is not judged
  assert.equal((await page.evaluate(() => window.resolveCalls.flatMap((c) => c.urls))).some((u) => u.includes('localhost:40') && !u.includes('4000')), false);
  await page.evaluate(() => window.stream.onEvent({type: 'event', event: {seq: 2, rev: 3, kind: 'message', agent_id: 'main', data: {role: 'assistant', text: 'now serving http://localhost:4000/live'}}}));
  // still inside the settle window: the complete URL is the open tail, so it is still shown as written
  assert.equal(await msg.locator('a.md-fabric-link').count(), 0);
  await msg.locator('a.md-fabric-link').waitFor({timeout: 6000});                          // ~2.5 s of quiet later it resolves
  assert.equal(await msg.locator('a.md-fabric-link').getAttribute('href'), 'https://dev.lan:4000/live');
}));

test('with the gateway unreachable the page keeps working and the URL stays as written', () => withWorkspace(async (page) => {
  await page.evaluate(() => {
    window.workspaceClient.resolveFabricUrls = async () => { throw new Error('gateway down'); };
    window.stream.onEvent({type: 'event', event: {seq: 2, rev: 2, kind: 'message', agent_id: 'main', data: {role: 'assistant', text: 'see http://localhost:5000/z please'}}});
  });
  const msg = page.locator('[data-message-seq="2"]');
  await msg.waitFor();
  await page.waitForTimeout(300);
  assert.match(await msg.innerText(), /http:\/\/localhost:5000\/z/);
}));
