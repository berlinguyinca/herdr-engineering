// Build cache page (micro-spec 10) against a real control plane (build-cache-server.py) in a real browser: measured figures only,
// unavailable states that say why, the two write figures labelled apart, trace links, hidden without the capability, an operator-only
// dry-run preview that never offers apply, permission/error states, hostile server text rendered inert, and accessible semantics
// at desktop, phone and reflow widths.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import path from 'node:path';
import {chromium} from 'playwright';
import {cachedChromiums} from './helpers/fixture.mjs';
import {staticAudit, focusIndicatorAudit, summarize} from './a11y-checks.mjs';

const root = path.resolve(import.meta.dirname, '../..');
const WAIT = 30000;
const KEYS = {operator: 'k-op', viewer: 'k-view', bad: 'k-nope'};

async function step(name, fn) {try {return await fn();} catch (error) {error.message = `step "${name}": ${error.message}`; throw error;}}
async function launch() {
  try {return await chromium.launch({headless: true});}
  catch (error) {for (const executablePath of cachedChromiums()) {try {return await chromium.launch({headless: true, executablePath});} catch {/* next */}} throw error;}
}

// key: when set, every /api/v1/build-cache request carries that X-API-Key (the document and static files carry the operator key so the page loads).
async function withServer(mode, run, {viewport = {width: 1280, height: 900}, key = null} = {}) {
  const server = spawn(process.env.PYTHON || 'python3', [path.join(root, 'tests/playwright/build-cache-server.py'), mode], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = ''; server.stderr.on('data', (d) => stderr += d);
  let browser;
  try {
    const port = await new Promise((resolve, reject) => {
      let out = ''; const t = setTimeout(() => reject(new Error(stderr || 'server timeout')), 25000);
      server.stdout.on('data', (d) => {out += d; const m = out.match(/PORT=(\d+)/); if (m) {clearTimeout(t); resolve(m[1]);}});
      server.once('exit', (code) => {clearTimeout(t); reject(new Error(`server exit ${code}: ${stderr}`));});
    });
    browser = await launch();
    const context = await browser.newContext({viewport});
    if (mode === 'keyed') {
      await context.route('**/*', (route) => {
        const url = route.request().url();
        const k = url.includes('/api/v1/build-cache') ? key : KEYS.operator;
        return route.continue({headers: {...route.request().headers(), 'x-api-key': k}});
      });
    }
    const page = await context.newPage();
    const errors = []; page.on('pageerror', (e) => errors.push(e.message));
    const requests = []; page.on('request', (r) => requests.push(`${r.method()} ${r.url()}`));
    try {await run(page, `http://127.0.0.1:${port}`, requests);}
    catch (error) {
      const shown = await page.evaluate(() => document.getElementById('view')?.innerText.slice(0, 800)).catch(() => '(page gone)');
      error.message += `\n#view: ${JSON.stringify(shown)}\nserver stderr: ${stderr.slice(-600)}`;
      throw error;
    }
    assert.deepEqual(errors, []);
  } finally {await browser?.close(); server.kill();}
}

const open = (page, base, hash = '#/build-cache') => step('open', () => page.goto(`${base}/${hash}`));
const loaded = (page) => step('rows loaded', () => page.locator('[data-bc-builds], [data-bc-empty], [data-bc-status][role=alert]:not(:empty)').first().waitFor({timeout: WAIT}));
const card = (page, label) => page.locator('.bc-card', {has: page.locator('dt', {hasText: label})});

test('populated: measured hit rate, labelled write figures, host cache size, 50 rows then Load more, tab under Hosts', () => withServer('populated', async (page, base) => {
  await open(page, base); await loaded(page);
  await page.locator('#subnav a[aria-current=page]', {hasText: 'Build cache'}).waitFor({timeout: WAIT});
  assert.equal(await page.locator('herdr-app-shell a.hd-shell__link.active').textContent(), 'Hosts');
  const rate = await card(page, 'Cache hit rate').innerText();
  assert.match(rate, /\d+\.\d%/); assert.match(rate, /hits, .* misses/); assert.match(rate, /measured by sccache in \d+ of \d+ loaded builds/);
  const logical = await card(page, 'Logical writes (lower bound)').innerText();
  assert.match(logical, /lower bound, not disk writes/);
  const disk = await card(page, 'Host disk delta').innerText();
  assert.match(disk, /other work included/); assert.match(disk, /twice/);
  assert.match(await page.locator('[data-bc-host]').first().innerText(), /of 1\.0 GiB/);
  assert.equal(await page.locator('[data-bc-builds] tbody tr').count(), 50);
  assert.equal(await page.locator('[data-bc-host] [role=meter]').count(), 1);
  // Only builds that really lack a measurement say "unavailable"; nothing is shown as an estimate.
  assert.doesNotMatch(await page.locator('#view').innerText(), /saved|savings|estimate[sd]? (hit|sav)/i);
  await page.locator('[data-bc-more]').click();
  await page.waitForFunction(() => document.querySelectorAll('[data-bc-builds] tbody tr').length === 100, null, {timeout: WAIT});
  await page.locator('[data-bc-more]:not([aria-busy])').click();
  await page.locator('[data-bc-more]').waitFor({state: 'hidden', timeout: WAIT});
  assert.equal(await page.locator('[data-bc-builds] tbody tr').count(), 120);
  assert.match(await page.locator('[data-bc-count]').innerText(), /120 builds loaded · end of list/);
  assert.equal(await page.locator('[data-bc-build="b118"] [data-status=running_or_unknown]').count(), 1);
}));

test('trace links: build id opens the detail view, session and mission link to their pages', () => withServer('populated', async (page, base) => {
  await open(page, base); await loaded(page);
  const row = page.locator('[data-bc-build="b115"]');
  assert.equal(await row.locator('a[href="#/herd/session/sess-115"]').count(), 1);
  assert.equal(await row.locator('a[href="#/missions/msn-115"]').count(), 1);
  await row.locator('th a').click();
  await step('detail', () => page.locator('[data-bc-detail]').waitFor({timeout: WAIT}));
  assert.match(await page.locator('[data-bc-detail-id]').innerText(), /b115/);
  const detail = await page.locator('[data-bc-detail]').innerText();
  assert.match(detail, /Logical writes \(lower bound\)/); assert.match(detail, /Host disk delta/); assert.match(detail, /Cache hit rate/);
  await page.locator('a', {hasText: 'Back to build cache'}).first().click();
  await loaded(page);
  await open(page, base, '#/build-cache/does-not-exist');
  await step('not found', () => page.locator('[data-bc-status][role=alert]').filter({hasText: 'Build not found'}).waitFor({timeout: WAIT}));
}));

test('hostile server strings are inert text in the list, the detail and the links', () => withServer('populated', async (page, base) => {
  await open(page, base); await loaded(page);
  assert.equal(await page.locator('#view img').count(), 0);
  assert.equal(await page.evaluate(() => window.__pwned), undefined);
  assert.match(await page.locator('[data-bc-build="b119"]').innerText(), /<img src=x onerror="window\.__pwned=1">/);
  // The API refuses trace ids that are not plain identifiers (sanitize_trace_id), so a hostile session/mission never becomes a link.
  assert.equal(await page.locator('[data-bc-build="b119"] a[href*="/herd/session/"], [data-bc-build="b119"] a[href*="/missions/"]').count(), 0);
  await open(page, base, '#/build-cache/b119');
  await step('detail', () => page.locator('[data-bc-detail]').waitFor({timeout: WAIT}));
  assert.equal(await page.locator('#view img').count(), 0);
  assert.equal(await page.evaluate(() => window.__pwned), undefined);
}));

test('no log yet: empty state, figures unavailable with the reason, nothing invented', () => withServer('nolog', async (page, base) => {
  await open(page, base); await loaded(page);
  await page.locator('[data-bc-empty]').waitFor({timeout: WAIT});
  assert.match(await card(page, 'Cache hit rate').innerText(), /unavailable.*no builds are recorded yet/s);
  assert.match(await card(page, 'Logical writes').innerText(), /unavailable/);
  assert.equal(await page.locator('[data-bc-degraded]').count(), 1);
  assert.doesNotMatch(await card(page, 'Cache hit rate').innerText(), /\d%/);
}));

test('builds without sccache or write measurements: unavailable with the cause, never zero', () => withServer('unmeasured', async (page, base) => {
  await open(page, base); await loaded(page);
  assert.match(await card(page, 'Cache hit rate').innerText(), /unavailable/);
  assert.match(await card(page, 'Cache hit rate').innerText(), /sccache statistics were unavailable for 3 of 3 loaded builds/);
  assert.match(await card(page, 'Logical writes').innerText(), /unavailable/);
  assert.match(await card(page, 'Host disk delta').innerText(), /unavailable/);
  assert.ok((await page.locator('[data-bc-builds] [data-bc-unavailable]').count()) >= 6);
}));

test('policy disabled: a degraded notice, recorded builds still listed', () => withServer('policyoff', async (page, base) => {
  await open(page, base); await loaded(page);
  assert.match(await page.locator('[data-bc-degraded]').first().innerText(), /cache policy is disabled/);
  assert.equal(await page.locator('[data-bc-builds] tbody tr').count(), 3);
}));

test('capability absent: no Build cache tab anywhere, and the page itself says the API is not enabled', () => withServer('off', async (page, base) => {
  await open(page, base, '#/hosts');
  await step('hosts page', () => page.locator('#subnav a', {hasText: 'Scheduling'}).waitFor({timeout: WAIT}));
  assert.equal(await page.locator('#subnav a', {hasText: 'Build cache'}).count(), 0);
  assert.equal(await page.locator('herdr-app-shell a', {hasText: 'Build cache'}).count(), 0);
  await open(page, base, '#/build-cache');
  await step('not enabled', () => page.locator('[data-bc-status][role=alert]').filter({hasText: 'not enabled'}).waitFor({timeout: WAIT}));
  assert.equal(await page.locator('#subnav a', {hasText: 'Build cache'}).count(), 0);
}));

test('operator preview shows the dry-run plan and says apply is CLI-only; the page has no apply control and never calls execute', () => withServer('populated', async (page, base, requests) => {
  await open(page, base); await loaded(page);
  assert.equal(await page.getByRole('button', {name: /apply|execute|delete|evict now/i}).count(), 0);
  await page.getByRole('button', {name: 'Preview cleanup (dry run)'}).click();
  await step('plan', () => page.locator('[data-bc-plan]').waitFor({timeout: WAIT}));
  const out = await page.locator('[data-bc-preview-out]').innerText();
  assert.match(out, /Preview only\. Nothing was deleted\./); assert.match(out, /CLI-only/); assert.match(out, /herdr-eng build-cache-evict --apply/);
  assert.ok(requests.some((r) => r.startsWith('POST') && r.endsWith('/cleanup/preview')));
  assert.ok(!requests.some((r) => r.includes('cleanup/execute')));
  assert.equal(await page.evaluate(() => document.activeElement?.hasAttribute('data-bc-preview-out')), true);
}));

test('permissions: a viewer reads but is refused the preview (403); a bad key shows the sign-in message (401)', async () => {
  await withServer('keyed', async (page, base) => {
    await open(page, base); await loaded(page);
    await page.getByRole('button', {name: 'Preview cleanup (dry run)'}).click();
    await step('403', () => page.locator('[data-bc-preview-error]').waitFor({timeout: WAIT}));
    const text = await page.locator('[data-bc-preview-out]').innerText();
    assert.match(text, /Permission denied \(403\)/); assert.match(text, /only operators/); assert.match(text, /CLI-only/);
    assert.equal(await page.locator('[data-bc-plan]').count(), 0);
  }, {key: KEYS.viewer});
  await withServer('keyed', async (page, base) => {
    await open(page, base);
    await step('401', () => page.locator('[data-bc-status][role=alert]').filter({hasText: /Sign-in required.*401/}).waitFor({timeout: WAIT}));
    assert.equal(await page.locator('[data-bc-builds]').count(), 0);
    assert.equal(await page.locator('[data-bc-retry]').isVisible(), true);
  }, {key: KEYS.bad});
  await withServer('keyed', async (page, base) => {
    await open(page, base); await loaded(page);
    await page.getByRole('button', {name: 'Preview cleanup (dry run)'}).click();
    await step('plan', () => page.locator('[data-bc-plan]').waitFor({timeout: WAIT}));
  }, {key: KEYS.operator});
});

test('server unreachable after load: the error is announced and Retry is offered', () => withServer('populated', async (page, base) => {
  await open(page, base); await loaded(page);
  await page.route('**/api/v1/build-cache/**', (route) => route.abort());
  await page.evaluate(() => document.querySelector('[data-bc-retry]').hidden = false);
  await page.locator('[data-bc-retry]').click();
  await step('error', () => page.locator('[data-bc-status][role=alert]').filter({hasText: /could not load/}).waitFor({timeout: WAIT}));
  assert.equal(await page.locator('[data-bc-retry]').isVisible(), true);
}));

for (const [label, viewport] of [['desktop', {width: 1280, height: 800}], ['phone 375', {width: 375, height: 667}], ['reflow 320', {width: 320, height: 568}]]) {
  test(`accessible and responsive: ${label}`, () => withServer('populated', async (page, base) => {
    await open(page, base); await loaded(page);
    await page.getByRole('button', {name: 'Preview cleanup (dry run)'}).click();
    await page.locator('[data-bc-plan]').waitFor({timeout: WAIT});
    const found = [...await staticAudit(page, '#view'), ...await focusIndicatorAudit(page, '#view')];
    assert.deepEqual(found, [], summarize(found));
    // no horizontal page scroll; wide tables scroll inside their own focusable region
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'page scrolls horizontally');
    const regions = await page.locator('.bc-wrap').evaluateAll((els) => els.map((e) => [e.getAttribute('role'), e.getAttribute('aria-label'), e.tabIndex]));
    assert.ok(regions.length >= 1 && regions.every(([r, l, t]) => r === 'region' && l && t === 0));
    assert.equal(await page.locator('main h1, #view h1').count() >= 1, true);
    // keyboard: the preview button is reachable and activates with Enter
    await page.getByRole('button', {name: 'Preview cleanup (dry run)'}).focus();
    await page.keyboard.press('Enter');
    await page.locator('[data-bc-preview-out] [data-bc-plan]').waitFor({timeout: WAIT});
  }, {viewport}));
}

test('phone 390: builds table stacks into labelled rows, no inner scroll, still keyboard reachable', () => withServer('populated', async (page, base) => {
  await open(page, base); await loaded(page);
  const wrap = page.locator('.bc-wrap', {has: page.locator('[data-bc-builds]')});
  const {inner, outer} = await wrap.evaluate((e) => ({inner: e.scrollWidth, outer: e.clientWidth}));
  assert.ok(inner <= outer + 1, `builds table scrolls inside its region (${inner} > ${outer})`);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'page scrolls horizontally');
  const row = page.locator('[data-bc-builds] tbody tr').first();
  assert.equal(await row.evaluate((e) => getComputedStyle(e).display), 'block');
  // each value shows its column name next to it
  const labels = await row.locator('td').evaluateAll((els) => els.map((e) => getComputedStyle(e, '::before').content));
  assert.ok(labels.length >= 9 && labels.every((l) => l && l !== 'none' && l !== 'normal'), JSON.stringify(labels));
  // table semantics survive the layout change
  assert.equal(await page.locator('[data-bc-builds]').getAttribute('role'), 'table');
  assert.equal(await row.getAttribute('role'), 'row');
  // keyboard: the region and the first build link are reachable by Tab
  await wrap.focus();
  assert.equal(await wrap.evaluate((e) => document.activeElement === e), true);
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement?.closest('[data-bc-build]') != null), true);
}, {viewport: {width: 390, height: 844}}));
