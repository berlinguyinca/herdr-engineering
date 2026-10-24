import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

// Real shared view/client in Chromium, with HTTP contract fixtures. Registry/MCP
// are already implemented; these browser HTTP adapters are not yet wired at HEAD.
async function withSkills(surface, fn, { scopes = ['private', 'room'], failList = false } = {}) {
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<link rel="stylesheet" href="/collab.css"><div id="view"></div>'); return; }
    try { res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(new URL(`../../packages/${req.url.slice(1)}`, import.meta.url))); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const manifest = (scope, version = '1.2.0') => ({ skill_id: 's1', name: `${scope} summary`, version, creator_id: 'alice', scope,
    room_id: scope === 'room' ? 'r1' : null, capabilities: ['files:read'], files: {
      'SKILL.md': { size_bytes: 42, sha256: 'b'.repeat(64) }, 'requirements.lock': { size_bytes: 9, sha256: 'c'.repeat(64) } },
    dependency_lock: 'requirements.lock', provenance: { artifact_id: 'artifact-1', file_ids: ['file-1'], parameters: { delimiter: ',' }, runtime: 'python-3.12', prompt: '<script>window.skillExecuted=true</script>' },
    input_schema: {}, output_schema: {}, tests: ['test.py'], fixtures: [], trust: 'untrusted', execution: 'harness_only' });
  const draft = { draft_id: 'd1', skill_id: 's1', version: '2.0.0', status: 'draft', target_room_id: 'r1', publish_scopes: scopes,
    sha256: 'a'.repeat(64), manifest: { ...manifest('private', '2.0.0'), name: 'Next summary' } };
  const calls = [], holds = new Map(), state = { published: false, failList };
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined) });
    const page = await browser.newPage(); page.setDefaultTimeout(2000);
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.route('**/api/v1/collab/**', async route => {
      const req = route.request(), url = new URL(req.url()), path = url.pathname.replace('/api/v1/collab', '');
      const body = req.postDataJSON(); calls.push({ path, body, query: url.search, method: req.method(), authorization: req.headers().authorization });
      let result;
      if (holds.has(path)) result = await holds.get(path);
      else if (path === '/config') result = { client_id: 'web', hosted_ui_domain: origin, public_url: origin };
      else if (path === '/me') result = { user_id: 'alice', status: 'approved' };
      else if (path === '/rooms') result = { rooms: [{ room_id: 'r1', name: 'Lab', role: 'owner' }] };
      else if (path === '/skills') {
        const scope = url.searchParams.get('scope');
        result = state.failList ? { http: 503, error: 'Skill library unavailable' } : { skills: ['1.2.0', '1.1.0'].map(version => ({ skill_id: 's1', version, status: 'published', sha256: 'a'.repeat(64), manifest: manifest(scope, version) })), drafts: scope === 'private' && !state.published ? [draft] : [] };
      } else if (path === '/skills/drafts/d1') result = draft;
      else if (path === '/skills/drafts/d1/publish') { state.published = true; result = { skill_id: 's1', version: '2.0.0' }; }
      else if (path === '/skills/s1/versions/1.2.0/download') result = { skill_id: 's1', version: '1.2.0', url: 'https://objects.example/package', expires_at: Date.now() / 1000 + 60 };
      else result = { http: 404, error: 'Unknown endpoint' };
      await route.fulfill({ status: result.http || 200, contentType: 'application/json', body: JSON.stringify(result) }).catch(() => {});
    });
    await page.route('https://objects.example/**', route => route.fulfill({ contentType: 'application/json', headers: { 'content-disposition': 'attachment; filename="skill-package.json"' }, body: '{"signed":"package"}' }));
    await page.goto(origin);
    await page.evaluate(async surface => {
      sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: `h.${btoa(JSON.stringify({ exp: Date.now() / 1000 + 600 }))}.s`, id: 'id' }));
      const { mountCollab } = await import('/collab-view.js');
      window.view = mountCollab(document.querySelector('#view'), { surface }); await window.view.ready;
    }, surface);
    await page.getByRole('tab', { name: 'Skills', exact: true }).click();
    await fn({ page, calls, holds, state, draft });
    assert.deepEqual(errors, []);
    assert.equal(await page.evaluate(() => Boolean(window.skillExecuted)), false);
  } finally { holds.clear(); await browser?.close(); await new Promise(resolve => server.close(resolve)); }
}

for (const surface of ['public', 'dashboard']) {
  test(`skills ${surface}: scope visibility, immutable versions, review, download and agent install handoff`, async () => withSkills(surface, async ({ page, calls }) => {
    for (const scope of ['private', 'room', 'shared']) {
      await page.getByRole('tab', { name: scope, exact: true }).click();
      const card = page.locator('[data-skill-version="1.2.0"]');
      await card.getByRole('heading', { name: `${scope} summary` }).waitFor();
      await page.locator('[data-skill-version="1.1.0"]').waitFor();
      assert.equal(await page.getByRole('button', { name: 'Review draft', exact: true }).count(), scope === 'private' ? 1 : 0);
    }
    const card = page.locator('[data-skill-version="1.2.0"]');
    await card.getByRole('button', { name: 'Review version' }).click();
    const review = page.getByRole('region', { name: 'Skill review' });
    for (const text of ['files:read', 'SKILL.md', 'requirements.lock', 'artifact-1', 'file-1', 'python-3.12']) assert.ok((await review.textContent()).includes(text));
    const download = page.waitForEvent('download');
    await card.getByRole('button', { name: 'Download', exact: true }).click();
    assert.equal((await download).suggestedFilename(), 'skill-package.json');
    await card.getByRole('button', { name: 'Install with agent' }).click();
    const instructions = page.getByLabel('Agent skill install instructions');
    assert.deepEqual(JSON.parse(await instructions.inputValue()), { name: 'skills_install', arguments: { skill_id: 's1', version: '1.2.0' } });
    assert.equal(calls.filter(c => /install|execute/.test(c.path)).length, 0);
    assert.equal(calls.filter(c => c.path.endsWith('/download')).length, 1);
    assert.ok(calls.filter(c => c.path.startsWith('/skills')).every(c => c.authorization.startsWith('Bearer ')));
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  }));

  for (const [label, scopes, target] of [['member', ['private'], 'private'], ['owner', ['private', 'room'], 'room'], ['maintainer', ['private', 'shared'], 'shared']]) {
    test(`skills ${surface}: ${label} reviews and confirms only server-authorized publication`, async () => withSkills(surface, async ({ page, calls }) => {
      await page.getByRole('button', { name: 'Review draft', exact: true }).click();
      const scope = page.getByRole('combobox', { name: /^Publication visibility/ });
      await scope.waitFor();
      assert.deepEqual(await scope.locator('option').evaluateAll(els => els.map(el => el.value)), scopes);
      await scope.selectOption(target);
      const confirm = page.getByRole('button', { name: 'Confirm publication' });
      assert.equal(await confirm.isDisabled(), true);
      assert.equal(calls.filter(c => c.path.endsWith('/publish')).length, 0);
      await page.getByLabel('I reviewed this version and its publication visibility').check();
      await confirm.click();
      await page.getByRole('status').filter({ hasText: 'Published version 2.0.0' }).waitFor();
      assert.deepEqual(calls.find(c => c.path.endsWith('/publish')).body, { scope: target });
      assert.equal(await page.getByRole('button', { name: 'Review draft', exact: true }).count(), 0);
    }, { scopes }));
  }

  for (const [label, result] of [
    ['empty object', {}],
    ['missing skill ID', { version: '2.0.0' }],
    ['missing version', { skill_id: 's1' }],
    ['wrong skill ID', { skill_id: 'other', version: '2.0.0' }],
    ['wrong version', { skill_id: 's1', version: '1.2.0' }],
    ['unpublished status', { skill_id: 's1', version: '2.0.0', status: 'draft' }],
    ['null status', { skill_id: 's1', version: '2.0.0', status: null }],
  ]) {
    test(`skills ${surface}: rejects successful HTTP publication with ${label}`, async () => withSkills(surface, async ({ page, calls, holds, state }) => {
      await page.getByRole('button', { name: 'Review draft', exact: true }).click();
      holds.set('/skills/drafts/d1/publish', Promise.resolve(result));
      const acknowledged = page.getByLabel('I reviewed this version and its publication visibility');
      const confirm = page.getByRole('button', { name: 'Confirm publication' });
      const listRequests = calls.filter(c => c.path === '/skills').length;
      await acknowledged.check();
      await confirm.click();
      await page.getByRole('alert').filter({ hasText: 'Invalid skill publication response' }).waitFor();
      assert.equal(await page.getByRole('status').filter({ hasText: 'Published version' }).count(), 0);
      assert.equal(calls.filter(c => c.path === '/skills').length, listRequests, 'invalid acknowledgment must not trigger a success refresh');
      assert.equal(await page.getByRole('region', { name: 'Skill review' }).isVisible(), true);
      assert.equal(await acknowledged.isChecked(), false);
      assert.equal(await confirm.isDisabled(), true);
      assert.equal(await page.getByRole('button', { name: 'Cancel review' }).isEnabled(), true);
      // A new explicit confirmation can accept the exact identity, with published
      // status supplied. Existing publication tests cover status-less responses.
      holds.set('/skills/drafts/d1/publish', Promise.resolve({ skill_id: 's1', version: '2.0.0', status: 'published' }));
      state.published = true;
      await acknowledged.check();
      await confirm.click();
      await page.getByRole('status').filter({ hasText: 'Published version 2.0.0' }).waitFor();
      assert.equal(calls.filter(c => c.path.endsWith('/publish')).length, 2);
    }));
  }

  test(`skills ${surface}: cancel does not publish; revoked authority and download denial stay visible`, async () => withSkills(surface, async ({ page, calls, holds }) => {
    await page.getByRole('button', { name: 'Review draft', exact: true }).click();
    await page.getByRole('button', { name: 'Cancel review' }).click();
    assert.equal(await page.getByRole('combobox', { name: /^Publication visibility/ }).count(), 0);
    assert.equal(calls.filter(c => c.path.endsWith('/publish')).length, 0);
    await page.getByRole('button', { name: 'Review draft', exact: true }).click();
    holds.set('/skills/drafts/d1/publish', Promise.resolve({ http: 403, error: 'Publication authority revoked' }));
    await page.getByLabel('I reviewed this version and its publication visibility').check();
    await page.getByRole('button', { name: 'Confirm publication' }).click();
    await page.getByRole('alert').filter({ hasText: 'Publication authority revoked' }).waitFor();
    holds.set('/skills/s1/versions/1.2.0/download', Promise.resolve({ http: 403, error: 'Skill access revoked' }));
    await page.locator('[data-skill-version="1.2.0"]').getByRole('button', { name: 'Download', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Skill access revoked' }).waitFor();
  }));

  test(`skills ${surface}: failed and malformed lists are not empty libraries; retry works`, async () => withSkills(surface, async ({ page, state, holds }) => {
    await page.getByRole('alert').filter({ hasText: 'Skill library unavailable' }).waitFor();
    assert.equal(await page.getByText('No published skills in this visibility.').count(), 0);
    state.failList = false;
    holds.set('/skills', Promise.resolve({}));
    await page.getByRole('button', { name: 'Refresh skills' }).click();
    await page.getByRole('alert').filter({ hasText: /invalid.*response/i }).waitFor();
    holds.delete('/skills');
    await page.getByRole('button', { name: 'Refresh skills' }).click();
    await page.locator('[data-skill-version="1.2.0"]').waitFor();
  }, { failList: true }));

  test(`skills ${surface}: stale draft review cannot replace a new scope`, async () => withSkills(surface, async ({ page, holds, draft }) => {
    let release; holds.set('/skills/drafts/d1', new Promise(resolve => { release = resolve; }));
    const requested = page.waitForRequest(req => req.url().endsWith('/skills/drafts/d1'));
    await page.getByRole('button', { name: 'Review draft', exact: true }).click(); await requested;
    await page.getByRole('tab', { name: 'shared', exact: true }).click();
    await page.locator('[data-skill-version="1.2.0"]').getByRole('heading', { name: 'shared summary' }).waitFor();
    release(draft);
    await page.waitForTimeout(100);
    assert.equal(await page.getByRole('combobox', { name: /^Publication visibility/ }).count(), 0);
  }));

  test(`skills ${surface}: reopening a pending draft cannot submit a second publication`, async () => withSkills(surface, async ({ page, calls, holds }) => {
    await page.getByRole('button', { name: 'Review draft', exact: true }).click();
    await page.getByLabel('I reviewed this version and its publication visibility').check();
    let release; holds.set('/skills/drafts/d1/publish', new Promise(resolve => { release = resolve; }));
    const requested = page.waitForRequest(req => req.url().endsWith('/publish'));
    await page.getByRole('button', { name: 'Confirm publication' }).click(); await requested;
    await page.getByRole('button', { name: 'Review draft', exact: true }).click();
    await page.getByLabel('I reviewed this version and its publication visibility').check();
    await page.getByRole('button', { name: 'Confirm publication' }).click();
    try {
      await page.getByRole('alert').filter({ hasText: 'Publication already in progress' }).waitFor();
      assert.equal(calls.filter(c => c.path.endsWith('/publish')).length, 1);
    } finally { release({ skill_id: 's1', version: '2.0.0' }); }
  }));

  test(`skills ${surface}: pending publication cannot submit twice or update a disposed view`, async () => withSkills(surface, async ({ page, calls, holds }) => {
    await page.getByRole('button', { name: 'Review draft', exact: true }).click();
    await page.getByLabel('I reviewed this version and its publication visibility').check();
    let release; holds.set('/skills/drafts/d1/publish', new Promise(resolve => { release = resolve; }));
    const requested = page.waitForRequest(req => req.url().endsWith('/publish'));
    await page.getByRole('button', { name: 'Confirm publication' }).click(); await requested;
    await page.locator('[data-publication-form]').dispatchEvent('submit');
    assert.equal(calls.filter(c => c.path.endsWith('/publish')).length, 1);
    await page.evaluate(() => window.view.dispose());
    const before = await page.locator('#view').innerHTML(), count = calls.length;
    release({ skill_id: 's1', version: '2.0.0' }); await page.waitForTimeout(100);
    assert.equal(await page.locator('#view').innerHTML(), before);
    assert.equal(calls.length, count);
  }));
}
