import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

// Real browser, shared client/view and renderers; HTTP fixtures are not live
// storage/Flasheic/job integration proof. Adapter tests use explicit providers.
function pdfFixture() {
  const stream = 'BT /F1 24 Tf 30 100 Td (Preview PDF fixture) Tj ET';
  const objects = ['<</Type/Catalog/Pages 2 0 R>>', '<</Type/Pages/Count 1/Kids[3 0 R]>>',
    '<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 160]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>',
    '<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>', `<</Length ${stream.length}>>\nstream\n${stream}\nendstream`];
  let pdf = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, i) => { offsets.push(pdf.length); pdf += `${i + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = pdf.length;
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<</Size 6/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF`;
  return pdf;
}
const contents = {
  md: '# Safe heading\n<img src="https://evil.example/leak" onerror="parent.pwned=1"><script>parent.pwned=1</script>\n[x](javascript:alert(1))\n[link](https://example.org)',
  xml: '<root><item a="&lt;img onerror=bad()&gt;">&lt;script&gt;bad()&lt;/script&gt;</item></root>',
  hostile: '<!DOCTYPE x [<!ENTITY x SYSTEM "https://evil.example/leak">]><x>&x;</x>',
  json: '{"safe":{"nested":42},"hostile":"<img src=x onerror=bad()>"}',
  yaml: 'root:\n  child: <script>bad()</script>\n  alias: &a [*a]',
  msp: 'Name: First\nNum Peaks: 2\n10 2; 20 4\n\nName: Second\nNum Peaks: 1\n30 7',
  pdf: pdfFixture(),
  unknown: 'binary', mzml: '<mzML/>', big: 'x'.repeat(200001), invalid: '{broken', fakepdf: '<script>parent.pwned=1</script>',
};
const extensions = { md: 'md', xml: 'xml', hostile: 'xml', json: 'json', yaml: 'yaml', msp: 'msp', pdf: 'pdf', unknown: 'exe', mzml: 'mzML', big: 'md', invalid: 'json', fakepdf: 'pdf' };
async function withPreviews(surface, fn) {
  const csp = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' https:; frame-src 'self' blob:; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.setHeader('Content-Security-Policy', csp); res.end('<link rel="stylesheet" href="/collab.css"><div id="view"></div>'); return; }
    try { res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'text/javascript'); res.end(await readFile(new URL(`../../packages/${req.url.slice(1)}`, import.meta.url))); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  let browser;
  const calls = [], holds = new Map(), requests = [];
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined) });
    const page = await browser.newPage(); page.setDefaultTimeout(3000);
    const errors = []; page.on('pageerror', e => errors.push(e.message)); page.on('request', r => requests.push(r));
    const origin = `http://127.0.0.1:${server.address().port}`;
    const room = { room_id: 'r', name: 'Preview room', role: 'viewer', members: [] };
    const files = Object.entries(extensions).map(([id, ext]) => ({ file_id: id, room_id: 'r', relative_path: `${id}.${ext}`, status: 'available', size_bytes: id === 'big' ? 200001 : contents[id].length }));
    await page.route('**/api/v1/collab/**', async route => {
      const path = new URL(route.request().url()).pathname.replace('/api/v1/collab', '');
      const body = route.request().postDataJSON(); calls.push({ path, body });
      let result;
      if (holds.has(path)) result = await holds.get(path);
      else if (path === '/config') result = { client_id: 'web', hosted_ui_domain: origin, public_url: origin };
      else if (path === '/me') result = { user_id: 'member', status: 'approved' };
      else if (path === '/rooms') result = { rooms: [room] };
      else if (path === '/rooms/r') result = room;
      else if (path === '/rooms/r/files') result = { files, has_more: false };
      else if (path.endsWith('/access')) result = { url: `https://objects.example/${path.split('/').at(-2)}?grant=secret`, expires_at: Date.now() / 1000 + 60 };
      else if (path.endsWith('/state')) { await new Promise(r => setTimeout(r, 80)); result = { changed: false, seq: 1 }; }
      else result = {};
      await route.fulfill({ status: result.http || 200, contentType: 'application/json', body: JSON.stringify(result) }).catch(() => {});
    });
    await page.route('https://objects.example/**', async route => {
      const id = new URL(route.request().url()).pathname.slice(1);
      if (holds.has(id)) await holds.get(id);
      await route.fulfill({ contentType: id.endsWith('pdf') ? 'application/pdf' : 'text/plain', headers: { 'Access-Control-Allow-Origin': '*' }, body: contents[id] }).catch(() => {});
    });
    await page.goto(origin);
    await page.evaluate(async surface => {
      sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: `h.${btoa(JSON.stringify({ exp: Date.now() / 1000 + 600 }))}.s`, id: 'id' }));
      window.view = (await import('/collab-view.js')).mountCollab(document.querySelector('#view'), { surface }); await window.view.ready;
    }, surface);
    await page.locator('[data-room="r"]').click(); await page.getByRole('tab', { name: 'Files', exact: true }).click();
    const open = async id => { const button = page.locator(`[data-file-id="${id}"]`).getByRole('button', { name: 'Preview', exact: true }); await button.focus(); await button.click(); };
    await fn({ page, open, calls, requests, holds, files }); assert.deepEqual(errors, []);
  } finally { await browser?.close(); await new Promise(r => server.close(r)); }
}

for (const surface of ['public', 'dashboard']) {
  test(`preview ${surface}: hostile Markdown/XML stay inert in opaque sandboxes, with source and tree controls`, async () => withPreviews(surface, async ({ page, open, requests }) => {
    await open('md');
    let frame = page.frameLocator('[data-file-preview] iframe');
    await frame.getByRole('heading', { name: 'Safe heading' }).waitFor();
    assert.equal(await frame.locator('script,img,object,embed').count(), 0);
    assert.equal(await page.locator('[data-file-preview] iframe').getAttribute('sandbox'), '');
    await frame.getByText('Source', { exact: true }).click();
    assert.match(await frame.locator('pre').textContent(), /<script>/);
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    await open('xml'); frame = page.frameLocator('[data-file-preview] iframe[title="xml preview"]');
    await frame.locator('details').first().waitFor();
    assert.match(await frame.locator('body').textContent(), /<script>bad/);
    assert.equal(await frame.locator('script,img').count(), 0);
    await open('hostile'); await page.locator('[data-file-preview] [role="alert"]').filter({ hasText: /XML/ }).waitFor();
    assert.equal(requests.some(r => r.url().includes('evil.example')), false);
    assert.equal(requests.filter(r => r.url().startsWith('https://objects.example')).every(r => !r.headers().authorization && !r.headers().cookie && !r.headers().referer), true);
  }));

  test(`preview ${surface}: JSON/YAML trees and MSP library selection expose read-only data`, async () => withPreviews(surface, async ({ page, open }) => {
    for (const id of ['json', 'yaml']) {
      await open(id); const frame = page.frameLocator(`[data-file-preview] iframe[title="${id} preview"]`);
      await frame.locator('details').first().waitFor(); assert.equal(await frame.locator('script,img').count(), 0);
      assert.match(await frame.locator('body').textContent(), id === 'json' ? /42/ : /child/);
    }
    await open('msp');
    await page.getByLabel('Spectrum').selectOption('1');
    const frame = page.frameLocator('[data-file-preview] iframe');
    await frame.getByRole('heading', { name: 'Second' }).waitFor();
    assert.equal(await frame.locator('svg line[data-peak]').count(), 1);
    assert.match(await frame.locator('svg title').textContent(), /30.*7/);
    assert.equal(await page.locator('[contenteditable=true]').count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }));

  test(`preview ${surface}: PDF is isolated and bounded, executable impostors rejected, missing providers visible`, async () => withPreviews(surface, async ({ page, open, requests }) => {
    await open('pdf');
    await page.locator('[data-file-preview] iframe[src^="blob:"]').waitFor();
    assert.equal(await page.locator('[data-file-preview] iframe').getAttribute('sandbox'), '');
    await page.getByText(/browser.*PDF|PDF.*browser/i).waitFor();
    await open('fakepdf'); await page.locator('[data-file-preview] [role=alert]').filter({ hasText: /PDF/ }).waitFor();
    await open('unknown'); await page.getByText(/No preview renderer/).waitFor();
    await open('mzml'); await page.getByText(/Flasheic.*unavailable/).waitFor();
    await open('big'); await page.getByText(/limit.*asynchronous|asynchronous.*unavailable/i).waitFor();
    assert.equal(requests.some(r => /objects.example\/(unknown|mzml|big)/.test(r.url())), false);
    await open('invalid'); await page.locator('[data-file-preview] [role=alert]').filter({ hasText: /JSON/ }).waitFor();
  }));

  test(`preview ${surface}: replacement, denial and disposal cannot leave stale content or late responses`, async () => withPreviews(surface, async ({ page, open, holds }) => {
    let release; holds.set('md', new Promise(r => { release = r; }));
    await open('md'); await open('json');
    await page.frameLocator('[data-file-preview] iframe').getByText('42', { exact: true }).waitFor();
    release(); await page.waitForTimeout(100);
    assert.equal(await page.frameLocator('[data-file-preview] iframe').getByRole('heading', { name: 'Safe heading' }).count(), 0);
    holds.set('/rooms/r/files/json/access', Promise.resolve({ http: 403, error: 'Permission revoked' }));
    await open('json'); await page.getByRole('alert').filter({ hasText: 'Permission revoked' }).waitFor();
    assert.equal(await page.locator('[data-file-preview] iframe').count(), 0);
    holds.delete('/rooms/r/files/json/access');
    let end; holds.set('xml', new Promise(r => { end = r; }));
    await open('xml'); await page.evaluate(() => window.view.dispose());
    const before = await page.locator('#view').innerHTML(); end(); await page.waitForTimeout(100);
    assert.equal(await page.locator('#view').innerHTML(), before);
  }));
}

test('preview optional adapters display chromatogram plots and derived artifacts only after fresh authorization', async () => withPreviews('public', async ({ page, calls }) => {
  await page.evaluate(async () => {
    const { renderPreview } = await import('/collab-previews.js');
    const file = { room_id: 'r', file_id: 'mzml', relative_path: 'a.mzML', status: 'available' };
    const source = { url: 'https://objects.example/mzml?grant=secret', expires_at: Date.now() / 1000 + 60 };
    const chrom = renderPreview(file, source, { flasheic: async () => ({ room_id: 'r', file_id: 'mzml', traces: [{ name: 'TIC', points: [[0, 2], [1, 8]] }] }) });
    document.querySelector('[data-file-preview]').replaceChildren(chrom); document.querySelector('[data-file-preview]').hidden = false;
    await chrom.ready;
  });
  await page.frameLocator('[data-file-preview] iframe').getByRole('heading', { name: 'TIC' }).waitFor();
  assert.equal(await page.frameLocator('[data-file-preview] iframe').locator('polyline').count(), 1);
  await page.evaluate(async () => {
    const { renderPreview } = await import('/collab-previews.js');
    const { CollabApi } = await import('/herdr-web-client/collab.js');
    const file = { room_id: 'r', file_id: 'big', relative_path: 'big.md', status: 'available', size_bytes: 200001 };
    const job = { room_id: 'r', file_id: 'big', job_id: 'j', status: 'pending' };
    const element = renderPreview(file, { url: 'https://objects.example/big', expires_at: Date.now() / 1000 + 60 }, {
      api: new CollabApi(() => ({ access: 'token' })), interval: 0,
      jobs: { request: async () => job, status: async () => ({ ...job, status: 'ready', artifacts: [{ room_id: 'r', file_id: 'json', relative_path: 'derived.json', status: 'available' }] }) },
    });
    document.querySelector('[data-file-preview]').replaceChildren(element); await element.ready;
  });
  await page.getByRole('button', { name: 'Preview derived.json' }).click();
  await page.frameLocator('[data-file-preview] iframe').getByText('42', { exact: true }).waitFor();
  assert.deepEqual(calls.filter(c => c.path.endsWith('/files/json/access')).map(c => c.body), [{ purpose: 'preview' }]);
}));

test('preview timeout remains visible even when a service adapter does not honor cancellation', async () => withPreviews('public', async ({ page }) => {
  await page.evaluate(async () => {
    const { renderPreview } = await import('/collab-previews.js');
    const schedule = window.setTimeout;
    window.setTimeout = (callback, ms, ...args) => schedule(callback, ms === 60000 ? 20 : ms, ...args);
    try {
      const element = renderPreview({ room_id: 'r', file_id: 'mzml', relative_path: 'x.mzML', status: 'available' }, { url: 'https://objects.example/mzml', expires_at: Date.now() / 1000 + 60 }, { flasheic: () => new Promise(() => {}) });
      document.querySelector('[data-file-preview]').replaceChildren(element); document.querySelector('[data-file-preview]').hidden = false;
    } finally { window.setTimeout = schedule; }
  });
  await page.locator('[data-file-preview] [role=alert]').filter({ hasText: /timed out/ }).waitFor();
}));

test('preview PDF replacement revokes its ephemeral blob without exposing signed URLs in the DOM', async () => withPreviews('public', async ({ page, open }) => {
  await page.evaluate(() => {
    window.revokedPreviews = []; const revoke = URL.revokeObjectURL.bind(URL);
    URL.revokeObjectURL = value => { window.revokedPreviews.push(value); revoke(value); };
  });
  await open('pdf'); const pdf = page.locator('[data-file-preview] iframe[src^="blob:"]'); await pdf.waitFor();
  const url = await pdf.getAttribute('src');
  assert.doesNotMatch(await page.locator('[data-file-preview]').innerHTML(), /grant=secret/);
  await open('json'); await page.frameLocator('[data-file-preview] iframe[title="json preview"]').getByText('42', { exact: true }).waitFor();
  assert.deepEqual(await page.evaluate(() => window.revokedPreviews), [url]);
}));
