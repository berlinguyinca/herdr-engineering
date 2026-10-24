// T15 smoke: the attention inbox and progress panels in a real browser. They mount the pure renderers from
// packages/collab-attention.js, so no server is needed: hostile labels stay inert text, actions are labelled
// keyboard-reachable controls, severity is text, only github pull request links become anchors. Run: npm run test:ui
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=2</script>"\'&';
const item = (extra = {}) => ({ key: 'ci_failed|ci:1', category: 'ci_failed', severity: 'error', title: 'CI failed', subject: { node_id: 'ci:1', label: 'ci' }, fp: 'a'.repeat(24), state: 'open', ...extra });

async function withPage(fn) {
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><html lang="en"><body><main id="host"></main></body></html>'); return; }
    try { const file = new URL(`../../packages${req.url}`, import.meta.url); res.setHeader('Content-Type', 'application/javascript'); res.end(await readFile(file)); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    const executablePath = process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined);
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(async () => {
      const m = await import('/collab-attention.js');
      window.inbox = (d) => { document.querySelector('#host').innerHTML = m.renderInbox({ data: d }); };
      window.progress = (d) => { document.querySelector('#host').innerHTML = m.renderProgress({ data: d }); };
    });
    await fn(page);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

test('hostile labels are inert text and the controls are named, focusable and keyboard reachable', async () => withPage(async (page) => {
  await page.evaluate(([d]) => window.inbox(d), [{ items: [item({ subject: { label: evil }, title: evil, detail: { why: evil } })], counts: {} }]);
  assert.equal(await page.evaluate(() => window.pwned), undefined);
  assert.equal(await page.locator('#host img, #host script').count(), 0);
  assert.ok((await page.locator('#host').textContent()).includes('<img src=x'));
  assert.ok((await page.locator('#host').textContent()).includes('Error'));                 // severity is text, not colour alone
  assert.equal(await page.getByRole('button', { name: /^Acknowledge/ }).count(), 1);
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement?.tagName), 'BUTTON');
  assert.equal(await page.getByRole('combobox', { name: /^Snooze/ }).count(), 1);
}));

test('only github pull request links are anchors and a script url is never one', async () => withPage(async (page) => {
  const row = (url) => ({ task_id: 't', label: 'T', stage: 'complete', evidence: [{ kind: 'merged_pr', node_id: 'pr:x', label: 'PR', url, counted: true, why_not: [] }] });
  await page.evaluate(([d]) => window.progress(d), [{ room: { status: 'ok', tasks: 2, complete: 2, completion: 1, progress: 1 }, tasks: [row('https://github.com/o/r/pull/3'), row('javascript:window.pwned=1')] }]);
  assert.equal(await page.locator('#host a').count(), 1);
  assert.equal(await page.locator('#host a').getAttribute('href'), 'https://github.com/o/r/pull/3');
  assert.equal(await page.evaluate(() => window.pwned), undefined);
}));

test('missing evidence is said in words, never shown as a date', async () => withPage(async (page) => {
  await page.evaluate(([d]) => window.progress(d), [{ room: { status: 'ok', tasks: 3, complete: 0, completion: 0, progress: 0,
    estimate: { label: 'estimate', status: 'insufficient_evidence', reason: '0 tasks with timed completion evidence' } }, tasks: [] }]);
  assert.ok((await page.locator('#host').textContent()).toLowerCase().includes('insufficient evidence'));
  assert.equal(await page.getByRole('progressbar').count() + await page.locator('progress').count() > 0, true);
}));
