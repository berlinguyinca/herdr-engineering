// T11 smoke: the owner's quarantine view in a real browser. It mounts the pure renderers from
// packages/collab-proposals.js, so it needs no server: hostile text from a remote agent must stay inert text,
// a link proposal must not become an anchor, a patch is shown line by line, and the accept/reject controls
// are keyboard-reachable buttons only while the proposal is in quarantine. Run: npm run test:ui
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=2</script>"\'&';
const prop = (extra = {}) => ({ proposal_id: 'prp_1', task_id: 'grant_1', agent_id: 'agent_1', delegate_label: 'Dana', kind: 'doc', title: 'Notes', state: 'quarantined',
  digest: 'cd'.repeat(32), size: 12, content: 'hello', url: null, refs: { paths: ['src/a.py'], questions: ['q1'] }, flags: [], promoted: false, ...extra });

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
      const m = await import('/collab-proposals.js');
      window.review = (p) => { document.querySelector('#host').innerHTML = m.renderReview(p); };
      window.list = (rows) => { document.querySelector('#host').innerHTML = m.renderProposalList({ proposals: rows }, null); };
    });
    await fn(page);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

test('hostile proposal text is inert and shown as text', async () => withPage(async (page) => {
  await page.evaluate(([p]) => window.review(p), [prop({ title: evil, content: evil, delegate_label: evil })]);
  assert.equal(await page.evaluate(() => window.pwned), undefined);
  assert.equal(await page.locator('#host img, #host script').count(), 0);
  assert.ok((await page.getByRole('article').textContent()).includes('<img src=x'));
  assert.ok((await page.getByRole('article').textContent()).includes('UNTRUSTED'));
}));

test('a link proposal is plain text, not a link', async () => withPage(async (page) => {
  await page.evaluate(([p]) => window.review(p), [prop({ kind: 'link', content: null, url: 'https://example.org/path' })]);
  assert.equal(await page.locator('#host a').count(), 0);
  assert.ok((await page.locator('#host').textContent()).includes('https://example.org/path'));
}));

test('accept and reject are keyboard-reachable buttons only while quarantined, and nothing offers apply or a shell', async () => withPage(async (page) => {
  await page.evaluate(([p]) => window.review(p), [prop({ kind: 'patch', content: '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n' })]);
  assert.equal(await page.getByRole('button', { name: /accept into the graph/i }).count(), 1);
  assert.equal(await page.getByRole('button', { name: /reject/i }).count(), 1);
  assert.equal(await page.getByRole('button', { name: /apply|run|shell|terminal|execute|merge/i }).count(), 0);
  await page.keyboard.press('Tab');                                  // the diff block is focusable, then the buttons
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement?.tagName), 'INPUT');   // the reason field
  assert.equal(await page.locator('.cx-diff--a').count(), 1);
  assert.equal(await page.locator('.cx-diff--d').count(), 1);
  await page.evaluate(([p]) => window.review(p), [prop({ state: 'rejected' })]);
  assert.equal(await page.locator('button').count(), 0);
  await page.evaluate(([p]) => window.review(p), [prop({ state: 'superseded' })]);
  assert.equal(await page.locator('button').count(), 0);
}));

test('instruction-like text carries a visible warning', async () => withPage(async (page) => {
  await page.evaluate(([p]) => window.review(p), [prop({ flags: ['instruction_like_text'] })]);
  assert.equal(await page.getByRole('alert').count(), 1);
  assert.ok((await page.getByRole('alert').textContent()).includes('Do not paste it into an assistant'));
}));
