import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

// Real browser, shared client/view and renderers; HTTP fixtures are not live
// storage/Flasheic/job integration proof. Adapter tests use explicit providers.
function pdfFixture(pages = ['Preview PDF fixture'], action = '') {
  const literal = text => text.replace(/[\\()]/g, char => '\\' + char);
  const objects = [`<</Type/Catalog/Pages 2 0 R ${action}>>`, '', '<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>'];
  const kids = [];
  for (const text of pages) {
    const page = objects.length + 1, stream = `BT /F1 14 Tf 10 100 Td (${literal(text)}) Tj ET`;
    kids.push(`${page} 0 R`);
    objects.push(`<</Type/Page/Parent 2 0 R/MediaBox[0 0 600 160]/Resources<</Font<</F1 3 0 R>>>>/Contents ${page + 1} 0 R>>`,
      `<</Length ${stream.length}>>\nstream\n${stream}\nendstream`);
  }
  objects[1] = `<</Type/Pages/Count ${pages.length}/Kids[${kids.join(' ')}]>>`;
  let pdf = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, i) => { offsets.push(pdf.length); pdf += `${i + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = pdf.length;
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<</Size ${objects.length + 1}/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF`;
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
    const page = await browser.newPage(); page.setDefaultTimeout(10000);
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
    await page.frameLocator('[data-file-preview] iframe').getByText('Preview PDF fixture', { exact: true }).waitFor();
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

for (const outcome of ['success', 'rejection']) {
  test(`preview artifact ${outcome} arriving after a newer selection has no effects`, async () => withPreviews('public', async ({ page, requests }) => {
    await page.evaluate(async () => {
      const { renderPreview } = await import('/collab-previews.js');
      const file = { room_id: 'r', file_id: 'big', relative_path: 'big.md', status: 'available', size_bytes: 200001 };
      const artifacts = ['md', 'pdf'].map(id => ({ room_id: 'r', file_id: id, relative_path: `${id}.${id}`, status: 'available' }));
      window.artifactGrants = {};
      window.revokedArtifacts = [];
      const revoke = URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL = value => {
        window.revokedArtifacts.push(value);
        revoke(value);
      };
      window.artifactPreview = renderPreview(file, { url: 'https://objects.example/big', expires_at: Date.now() / 1000 + 60 }, {
        api: { fileAccess: (_room, id) => new Promise((resolve, reject) => { window.artifactGrants[id] = { resolve, reject }; }) },
        jobs: { request: async () => ({ ...file, job_id: 'j', status: 'ready', artifacts }), status: async () => { throw new Error('Unexpected poll'); } },
      });
      const host = document.querySelector('[data-file-preview]'); host.replaceChildren(window.artifactPreview); host.hidden = false;
      await window.artifactPreview.ready;
      const buttons = [...host.querySelectorAll('button')];
      // Keep the actual handler promises so each out-of-order completion is
      // fully handled before asserting, without sleep-based race tests.
      window.olderArtifact = buttons[0].onclick();
      window.newerArtifact = buttons[1].onclick();
      window.artifactGrants.pdf.resolve({ url: 'https://objects.example/pdf', expires_at: Date.now() / 1000 + 60 });
      await window.newerArtifact;
    });
    const pdf = page.locator('[data-file-preview] iframe[src^="blob:"]'); await pdf.waitFor();
    const url = await pdf.getAttribute('src');
    await page.evaluate(async outcome => {
      if (outcome === 'success') window.artifactGrants.md.resolve({ url: 'https://objects.example/md', expires_at: Date.now() / 1000 + 60 });
      else window.artifactGrants.md.reject(new Error('Older permission denial'));
      await window.olderArtifact;
    }, outcome);
    assert.equal(await pdf.count(), 1, 'newer PDF must remain selected');
    assert.equal(await pdf.getAttribute('src'), url);
    assert.deepEqual(await page.evaluate(() => window.revokedArtifacts), [], 'stale outcome must not dispose the active child');
    assert.equal(requests.some(r => r.url().startsWith('https://objects.example/md')), false, 'stale success must not start content rendering');
    assert.equal(await page.locator('[data-file-preview] [role=alert]').count(), 0);
    await page.evaluate(() => window.artifactPreview.dispose());
    assert.deepEqual(await page.evaluate(() => window.revokedArtifacts), [url], 'active child remains owned and is disposed with the parent');
  }));
}

// A PDF preview must contain painted pixels and searchable extracted text,
// rather than merely a sandboxed native viewer that Chromium cannot display.
test('preview PDF paints a page and searches inert text using a dedicated worker', async () => withPreviews('public', async ({ page, open }) => {
  const workers = []; page.on('worker', worker => workers.push(worker.url()));
  await open('pdf');
  const frame = page.frameLocator('[data-file-preview] iframe');
  await frame.getByText('Preview PDF fixture', { exact: true }).waitFor();
  const image = frame.locator('img[alt="PDF page 1"]'); await image.waitFor();
  const pixels = await image.evaluate(image => {
    const canvas = document.createElement('canvas'); canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    canvas.getContext('2d').drawImage(image, 0, 0);
    return [...canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data].filter((n, i) => i % 4 !== 3 && n < 128).length;
  });
  assert.ok(pixels > 100, 'PDF glyphs must actually paint non-white pixels');
  await page.getByRole('searchbox', { name: 'Search PDF' }).fill('fixture');
  await page.getByRole('status').filter({ hasText: '1 matching page' }).waitFor();
  await page.getByRole('searchbox', { name: 'Search PDF' }).fill('absent');
  await page.getByRole('status').filter({ hasText: 'No matches' }).waitFor();
  assert.ok(workers.some(url => url.endsWith('/vendor/pdfjs/pdf.worker.mjs')));
  assert.equal(await page.locator('[data-file-preview] iframe').getAttribute('sandbox'), '');
  assert.equal(await frame.locator('script,object,embed,a,form').count(), 0);
}));

for (const kind of ['markdown', 'json', 'xml', 'yaml', 'msp']) {
  test(`preview ${kind} parser hang is terminated while room controls remain responsive`, async () => withPreviews('public', async ({ page, open }) => {
    // Replace only the worker HTTP response, not the Worker API or parser client.
    // This executes a real infinite loop on the isolated worker thread.
    await page.route('**/collab-preview-worker.js', route => route.fulfill({ contentType: 'text/javascript', body: 'self.onmessage = () => { while (true) {} };' }));
    await page.evaluate(() => {
      window.workerTerminations = 0;
      const terminate = Worker.prototype.terminate;
      Worker.prototype.terminate = function () { window.workerTerminations++; return terminate.call(this); };
      const schedule = window.setTimeout;
      window.setTimeout = (fn, ms, ...args) => {
        if (ms === 2000) {
          window.setTimeout = schedule;
          return schedule(fn, 250, ...args);
        }
        return schedule(fn, ms, ...args);
      };
    });
    await open(kind === 'markdown' ? 'md' : kind);
    await page.locator('[data-file-preview] [role=alert]').filter({ hasText: /timed out/i }).waitFor();
    assert.equal(await page.evaluate(() => window.workerTerminations), 1);
    await page.unroute('**/collab-preview-worker.js');
    await open('json');
    await page.frameLocator('[data-file-preview] iframe').getByText('42', { exact: true }).waitFor();
    assert.equal(await page.locator('[data-file-id="json"] button:disabled').count(), 0);
  }));
}


test('preview PDF multipage search and document actions remain inert', async () => withPreviews('dashboard', async ({ page, open, requests }) => {
  const hostile = '<script>parent.pwned=1</script>';
  const pdf = pdfFixture(['First page', `Second page ${hostile}`], '/OpenAction<</S/JavaScript/JS(app.alert\\(123\\))>>');
  await page.route('https://objects.example/pdf?*', route => route.fulfill({ contentType: 'application/pdf', body: pdf }));
  let dialogs = 0; page.on('dialog', dialog => { dialogs++; dialog.dismiss(); });
  await open('pdf');
  const frame = page.frameLocator('[data-file-preview] iframe');
  await frame.getByText(`Second page ${hostile}`, { exact: true }).waitFor();
  assert.equal(await frame.locator('img').count(), 2);
  assert.equal(await frame.locator('script,iframe,object,embed,a,form').count(), 0);
  assert.equal(await page.evaluate(() => window.pwned), undefined); assert.equal(dialogs, 0);
  await page.getByRole('searchbox', { name: 'Search PDF' }).fill('second');
  await page.getByRole('button', { name: 'Go to page 2' }).click();
  assert.match(await page.locator('[data-file-preview] iframe').getAttribute('src'), /#pdf-page-2$/);
  assert.equal(requests.some(request => request.url().includes('evil.example')), false);
}));

test('preview PDF page cap and malformed data fail visibly', async () => withPreviews('public', async ({ page, open }) => {
  await page.route('https://objects.example/pdf?*', route => route.fulfill({ contentType: 'application/pdf', body: pdfFixture(Array(51).fill('Page')) }));
  await open('pdf'); await page.locator('[data-file-preview] [role=alert]').filter({ hasText: /PDF page limit/ }).waitFor();
  await page.unroute('https://objects.example/pdf?*');
  await page.route('https://objects.example/pdf?*', route => route.fulfill({ contentType: 'application/pdf', body: '%PDF-1.7 broken' }));
  await open('pdf'); await page.locator('[data-file-preview] [role=alert]').filter({ hasText: /Invalid PDF/ }).waitFor();
  await open('json'); await page.frameLocator('[data-file-preview] iframe').getByText('42', { exact: true }).waitFor();
}));

test('preview PDF hung dedicated worker times out and terminates without a synchronous fallback', async () => withPreviews('public', async ({ page, open }) => {
  await page.route('**/vendor/pdfjs/pdf.worker.mjs', route => route.fulfill({ contentType: 'text/javascript', body: 'while (true) {}' }));
  await page.evaluate(async () => {
    await import('/vendor/pdfjs/pdf.mjs');
    window.pdfTerminations = 0;
    const terminate = Worker.prototype.terminate;
    Worker.prototype.terminate = function () { window.pdfTerminations++; return terminate.call(this); };
    const schedule = window.setTimeout;
    window.setTimeout = (fn, ms, ...args) => schedule(fn, ms === 15000 ? 500 : ms, ...args);
  });
  await open('pdf');
  await page.locator('[data-file-preview] [role=alert]').filter({ hasText: /PDF preview timed out/ }).waitFor();
  assert.equal(await page.evaluate(() => window.pdfTerminations), 1);
  await open('json'); await page.frameLocator('[data-file-preview] iframe').getByText('42', { exact: true }).waitFor();
}));

test('preview replacement terminates an active parser before its deadline', async () => withPreviews('public', async ({ page, open }) => {
  await page.route('**/collab-preview-worker.js', route => route.fulfill({ contentType: 'text/javascript', body: 'self.onmessage = () => { while (true) {} };' }));
  await page.evaluate(() => {
    window.parserTerminations = 0;
    const terminate = Worker.prototype.terminate;
    Worker.prototype.terminate = function () { window.parserTerminations++; return terminate.call(this); };
  });
  const workerStarted = page.waitForEvent('worker'); await open('md'); await workerStarted;
  await open('unknown'); await page.getByText(/No preview renderer/).waitFor();
  assert.equal(await page.evaluate(() => window.parserTerminations), 1);
  assert.equal(await page.locator('[data-file-preview] [role=alert]').count(), 0);
}));

test('preview PDF preserves non-ASCII extracted text in its inert HTML document', async () => withPreviews('public', async ({ page, open }) => {
  const pdf = pdfFixture(['café']).replace('/BaseFont/Helvetica', '/BaseFont/Helvetica/Encoding/WinAnsiEncoding');
  // Rebuild xref offsets after changing the font dictionary.
  const objects = [...pdf.matchAll(/(\d+) 0 obj\n/g)].map(match => match.index);
  const xref = pdf.indexOf('xref\n');
  const body = pdf.slice(0, xref) + `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${objects.map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<</Size ${objects.length + 1}/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF`;
  await page.route('https://objects.example/pdf?*', route => route.fulfill({ contentType: 'application/pdf', body: Buffer.from(body, 'latin1') }));
  await open('pdf');
  const text = page.frameLocator('[data-file-preview] iframe').locator('p'); await text.waitFor();
  assert.equal(await text.textContent(), 'café');
  await page.getByRole('searchbox', { name: 'Search PDF' }).fill('café');
  await page.getByRole('status').filter({ hasText: '1 matching page' }).waitFor();
}));
