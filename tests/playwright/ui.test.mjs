// Playwright UI tests for the control-plane SPA (Spec §91 loading/error states).
//
// These cover the regression reported as "Failed to fetch / too many requests"
// on Overview, Activity, Analytics, etc. The SPA re-renders (re-fetching all
// data for the view) on SSE events; an un-throttled refresh trips the API rate
// limiter (429 "too many requests") and exhausts the HTTP/1.1 connection pool
// ("Failed to fetch"). The server fixture (server.py) streams gapped event
// bursts so this path is exercised under real load. Each test uses a fresh
// server + browser so the per-IP rate budget (60s window) is not shared across
// tests, keeping the assertions deterministic.
//
// Run: npm run test:ui   (or node --test tests/playwright/ui.test.mjs)
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON
  || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python'))
    ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');
// Playwright resolves its own bundled browser (matching the pinned
// devDependency). On dev boxes whose cached browser build lags the installed
// Playwright version (this host has chromium-1243 while Playwright 1.64 wants
// headless_shell-1248), fall back to a locally-pinned chromium. Override with
// PLAYWRIGHT_CHROMIUM to force a specific executable.
const EXE_FALLBACK = process.env.PLAYWRIGHT_CHROMIUM
  || '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';

const ROUTES = [
  ['/overview', 'Overview'],
  ['/activity', 'Activity'],
  ['/analytics', 'Analytics'],
  ['/missions', 'Finished'],
  ['/herd', 'Agents'],
  ['/hosts', 'beast'],
  ['/services', 'web service on beast'],
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function startServer(env = {}) {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', 'server.py')], {
    env: { ...process.env, ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const port = await new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('server start timeout')), 20000);
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      let m;
      while ((m = buf.match(/PORT=(\d+)/))) {
        clearTimeout(timer);
        resolve(Number(m[1]));
        buf = buf.slice(m.index + m[0].length);
      }
    });
    proc.on('exit', (c) => reject(new Error(`server exited early (${c})`)));
    proc.stderr.on('data', (d) => process.stderr.write(`[srv] ${d}`));
  });
  return { proc, port };
}

async function launchBrowser() {
  const args = ['--no-sandbox'];
  if (process.env.PLAYWRIGHT_CHROMIUM) {
    return chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM, args });
  }
  try {
    return await chromium.launch({ headless: true, args }); // auto-detect bundled browser
  } catch (err) {
    if (fs.existsSync(EXE_FALLBACK)) {
      return chromium.launch({ headless: true, executablePath: EXE_FALLBACK, args });
    }
    throw err;
  }
}

// Start a fresh server + browser, run `fn`, then tear both down. A fresh
// server per test isolates the API rate budget (60s sliding window) so tests
// are deterministic.
async function withApp(fn, env = {}) {
  const srv = await startServer(env);
  let browser;
  try {
    browser = await launchBrowser();
    const page = await browser.newPage();
    const base = `http://127.0.0.1:${srv.port}`;
    return await fn({ page, base });
  } finally {
    if (browser) { try { await browser.close(); } catch { /* noop */ } }
    try { srv.proc.kill(); } catch { /* noop */ }
  }
}

const bodyText = (page) =>
  page.evaluate(() => document.body ? document.body.innerText : '');

test('every page renders without rate-limit / fetch errors', async () => {
  // No event burst (BURST=0): the SPA renders once per view with no SSE churn,
  // so content presence is deterministic and never races a live re-render.
  await withApp(async ({ page, base }) => {
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(`pageerror: ${e.message}`));
    for (const [route, needle] of ROUTES) {
      await page.goto(`${base}/#${route}`);
      // waitForFunction is the authoritative render check (the needle must
      // actually appear). A view's own mount re-render can briefly show a
      // spinner immediately after, so we do NOT re-assert the needle here --
      // we only check for error cards, which never appear during a normal
      // (non-burst) render.
      await page.waitForFunction(
        (n) => (document.body ? document.body.innerText : '').includes(n),
        needle, { timeout: 15000 });
      const text = await bodyText(page);
      assert.ok(!text.includes('too many requests'),
        `rate-limit error on ${route}:\n${text.slice(0, 400)}`);
      assert.ok(!text.includes('Failed to fetch'),
        `fetch error on ${route}:\n${text.slice(0, 400)}`);
    }
    assert.deepEqual(pageErrors, [], 'no uncaught page exceptions expected');
  }, { BURST: '0' });
});

test('overview survives a heavy event burst without rate limiting', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/overview`);
    await page.waitForFunction(
      () => (document.body ? document.body.innerText : '').includes('Overview'),
      null, { timeout: 15000 });
    // The throttled live refresh re-renders (and briefly shows the spinner),
    // so we only assert that NO error card ever appears during the burst and
    // that the overview recovers afterwards — never that the word "Overview"
    // is present at every instant.
    const start = Date.now();
    let sawError = null;
    while (Date.now() - start < 15000) {
      await sleep(500);
      const text = await bodyText(page);
      if (text.includes('too many requests')) { sawError = 'too many requests'; break; }
      if (text.includes('Failed to fetch')) { sawError = 'Failed to fetch'; break; }
    }
    assert.ok(!sawError, `rate-limit/fetch error appeared during burst: ${sawError}`);
    // The view must recover (not be stuck on an error card).
    await page.waitForFunction(
      () => (document.body ? document.body.innerText : '').includes('Overview'),
      null, { timeout: 10000 });
  });
});

test('pi-sourced mission detail renders by mission_key', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/missions`);
    await page.locator('[data-filter=recorded]').click();
    // Recorded missions remain available through their history group.
    // The href encodes the complete namespace; the hash router decodes it once.
    await page.waitForSelector('a[href="#/missions/pi%3AMSN-PI1"]', { timeout: 15000 });
    await page.click('a:has-text("Pi mission")');
    // Detail route uses the mission_key, not the bare mission_id (UI-005).
    await page.waitForFunction(
      () => (location.hash === `#/missions/${encodeURIComponent('pi:MSN-PI1')}`
        || location.hash === '#/missions/pi:MSN-PI1'),
      null, { timeout: 15000 });
    // The mission resolves by key: title renders, not a "Mission not found" card.
    await page.waitForFunction(
      () => document.querySelector('#view h1')?.textContent === 'Pi mission',
      null, { timeout: 15000 });
    assert.ok(!(await bodyText(page)).includes('Mission not found'));
    // The event stream is queried by mission_key, so the seeded stage event
    // (recorded under 'pi:MSN-PI1') must render — not the empty state.
    await page.waitForFunction(() => {
      const el = document.getElementById('mission-stream');
      return el && !el.innerHTML.includes('No events for this entity yet');
    }, null, { timeout: 15000 });
    await page.locator('[data-history-panel=events] > summary').click();
    const streamText = await page.locator('#mission-stream').innerText();
    assert.ok(streamText.includes('MissionStageChanged'),
      `stage event missing from stream:\n${streamText.slice(0, 400)}`);
  }, { BURST: '0' });
});

test('mission detail renders the timeline', async () => {
  await withApp(async ({ page, base }) => {
    // Legacy plain-colon bookmarks remain valid; the app encodes the API path.
    await page.goto(`${base}/#/missions/pi:MSN-PI1`);
    await page.locator('[data-history-panel=timeline] > summary').click();
    // Merged timeline (UI-009): stage events + test run + merged PR.
    await page.waitForSelector('herdr-timeline .hd-timeline__item', { timeout: 15000 });
    const kinds = await page.evaluate(() =>
      Array.from(document.querySelectorAll('herdr-timeline .hd-timeline__item'))
        .map((el) => el.getAttribute('data-kind')));
    for (const kind of ['stage', 'test', 'pr', 'merge']) {
      assert.ok(kinds.includes(kind), `timeline item with data-kind="${kind}" missing (got ${kinds.join(', ')})`);
    }
    // Screenshot for the convergence-program review (kept untracked when no
    // other screenshots are committed to the repo).
    fs.mkdirSync(path.join(import.meta.dirname, 'screenshots'), { recursive: true });
    await page.screenshot({
      path: path.join(import.meta.dirname, 'screenshots', 'mission-timeline.png'),
      fullPage: true,
    });
  }, { BURST: '0' });
});

// ---------------------------------------------------------------- agent inspector (spec 0200)
async function sessionHash(base) {
  const r = await fetch(`${base}/api/v1/sessions`);
  const { sessions } = await r.json();
  return `#/herd/session/${sessions[0].session_id}`;
}

async function postInspector(base, sid, events) {
  const r = await fetch(`${base}/api/v1/ingest`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entity_type: 'inspector', entity_id: sid, event_type: 'inspector.batch', payload: { events } }),
  });
  assert.equal(r.status, 200);
}

test('inspector: tabs, redaction/ACL, reasoning gating, search, layout, live tool status', async () => {
  await withApp(async ({ page, base }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    const hash = await sessionHash(base);
    const sid = hash.split('/').pop();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`${base}/${hash}`);
    await page.locator('#advanced-inspector > summary').click();
    await page.waitForSelector('.hd-ins__tab', { timeout: 15000 });
    const inspector = () => page.locator('#ins-root');

    // default view = conversation
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('make the parser faster'));
    // wide viewport => split layout
    assert.ok(await inspector().evaluate((el) => el.classList.contains('hd-ins--split')));

    // reasoning hidden by default preference, even though it is exposed
    await page.click('[data-view=reasoning]');
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('Reasoning is hidden'));
    assert.ok(!(await inspector().innerText()).includes('profile before changing'));
    await page.click('#ins-show-reasoning');
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('profile before changing'));

    // tools: failed + subagent attribution + streaming status
    await page.click('[data-view=tools]');
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('make build'));
    let text = await inspector().innerText();
    assert.ok(text.includes('builder') && text.includes('running') && text.includes('error'));
    await postInspector(base, sid, [{ kind: 'tool', call_id: 'c2', phase: 'end', output: 'built', exit_code: 0, agent_id: 'builder' }]);
    await page.waitForFunction(() => [...document.querySelectorAll('.hd-ins__row')]
      .some((r) => r.innerText.includes('make build') && /\bok\b/.test(r.innerText) && !/running/.test(r.innerText)), null, { timeout: 8000 });

    // files + diff inspection
    await page.click('[data-view=files]');
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('src/parser.py'));
    assert.ok(await page.locator('.hd-diff__add').count() > 0);

    // tests + failure reporting
    await page.click('[data-view=tests]');
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('test_parse_empty'));

    // usage / context / associations
    await page.click('[data-view=usage]');
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('Context'));
    text = await inspector().innerText();
    assert.ok(text.includes('perf/parser') && text.includes('o/r#42') && text.includes('gpt-4o-mini'));

    // searchable history
    await page.click('[data-view=all]');
    await page.fill('#ins-q', 'AssertionError');
    await page.waitForFunction(() => {
      const t = document.querySelector('.hd-ins__list')?.innerText || '';
      return t.includes('test_parse_empty') && !t.includes('make the parser faster');
    });

    // responsive: narrow => stacked
    await page.setViewportSize({ width: 600, height: 900 });
    await page.waitForFunction(() => document.querySelector('#ins-root')?.classList.contains('hd-ins--stacked'));
    assert.deepEqual(errors, []);
  }, { BURST: '0' });
});

test('inspector: open server treats the viewer as operator; sessions without telemetry say so', async () => {
  await withApp(async ({ page, base }) => {
    const r = await fetch(`${base}/api/v1/sessions`);
    const { sessions } = await r.json();
    await page.goto(`${base}/#/herd/session/${sessions[0].session_id}`);
    await page.locator('#advanced-inspector > summary').click();
    await page.waitForSelector('.hd-ins__tab');
    await page.click('[data-view=reasoning]');
    await page.locator('#ins-list .hd-empty__message').filter({ hasText: /not reported|does not expose/ }).waitFor();
    const text = await page.locator('#ins-root').innerText();
    assert.ok(/No inspector telemetry|not reported|does not expose/.test(text), text);
  }, { BURST: '0', INSPECTOR_SEED: '0' });
});

test('inspector: live events do not clobber typing, open prefs form, scroll position or tab (review regressions)', async () => {
  await withApp(async ({ page, base }) => {
    const hash = await sessionHash(base);
    const sid = hash.split('/').pop();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`${base}/${hash}`);
    await page.locator('#advanced-inspector > summary').click();
    await page.waitForSelector('.hd-ins__tab');
    const msgs = (n, tag) => Array.from({ length: n }, (_, i) => ({ kind: 'message', role: 'assistant', text: `${tag}-${i} `.padEnd(300, 'x') }));
    await postInspector(base, sid, msgs(60, 'bulk'));
    await page.waitForFunction(() => document.querySelectorAll('.hd-ins__row').length >= 60, null, { timeout: 8000 });

    // open prefs, edit, then let live events arrive: edits must survive
    await page.click('#ins-prefs');
    await page.fill('[data-pref=tool_output_lines]', '77');
    await page.type('#ins-q', 'bulk', { delay: 60 });
    for (let i = 0; i < 4; i++) { await postInspector(base, sid, msgs(2, `live${i}`)); await sleep(250); }
    await sleep(800);
    assert.equal(await page.inputValue('[data-pref=tool_output_lines]'), '77');
    assert.equal(await page.inputValue('#ins-q'), 'bulk');
    await page.fill('#ins-q', '');
    await page.waitForFunction(() => document.querySelectorAll('.hd-ins__row').length >= 60);

    // reading older rows: scroll position is preserved when new events arrive
    await page.evaluate(() => { const l = document.querySelector('.hd-ins__list'); l.scrollTop = 600; });
    const before = await page.evaluate(() => document.querySelector('.hd-ins__list').scrollTop);
    assert.ok(before > 100, `list must be scrollable (scrollTop=${before})`);
    await postInspector(base, sid, msgs(3, 'late'));
    await page.waitForFunction(() => document.querySelector('.hd-ins__list').innerText.includes('late-2'), null, { timeout: 8000 });
    const after = await page.evaluate(() => document.querySelector('.hd-ins__list').scrollTop);
    assert.ok(after >= before - 5, `scroll jumped: ${before} -> ${after}`);
  }, { BURST: '0', INSPECTOR_SEED: '0' });
});

test('inspector: a slow response for the previous tab never overwrites the current tab', async () => {
  await withApp(async ({ page, base }) => {
    const hash = await sessionHash(base);
    await page.route('**/inspector?*view=tools*', async (route) => { await sleep(1500); await route.continue(); });
    await page.goto(`${base}/${hash}`);
    await page.locator('#advanced-inspector > summary').click();
    await page.waitForSelector('.hd-ins__tab');
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('make the parser faster'));
    await page.click('[data-view=tools]');
    await page.click('[data-view=conversation]');
    await sleep(2500);
    const text = await page.locator('#ins-list').innerText();
    assert.ok(text.includes('make the parser faster') && !text.includes('make build'), text.slice(0, 300));
  }, { BURST: '0' });
});

test('recorded mission history remains inspectable with advancing report ages and no writes', async () => {
  await withApp(async ({ page, base }) => {
    const writes = [];
    page.on('request', req => { if (req.method() !== 'GET') writes.push(req.url()); });
    await page.clock.install();
    await page.goto(`${base}/#/missions`);
    await page.locator('[data-filter="recorded"]').click();
    const record = page.locator('.mc-row[data-mission-key="pi:MSN-PI1"]');
    await record.waitFor();
    assert.match(await record.innerText(), /Recorded/);
    assert.doesNotMatch(await record.innerText(), /Live telemetry/);
    const before = await record.locator('[data-recorded-age]').innerText();
    await page.clock.runFor(3000);
    assert.notEqual(await record.locator('[data-recorded-age]').innerText(), before);
    await record.locator('.mc-title').click();
    await page.locator('#mission-history').waitFor();
    assert.deepEqual(writes, []);
  }, { BURST: '0' });
});

function liveProjection(overrides = {}) {
  const now = new Date().toISOString();
  return {
    contract_version: 1, mission_key: 'pi:MSN-live', host_id: 'beast', instance_id: 'pi-instance', session_id: 'native-pi-session',
    observed_at: now, snapshot_at: now, connection: 'connected',
    mission: { id: 'MSN-live', title: 'Live Pi mission', status: 'EXECUTING' },
    freshness: { state: 'live', age_seconds: 2, stale_after_seconds: 30, disconnect_after_seconds: 120 },
    activity: { health: 'active', currentActivity: { summary: 'Running authentication tests' }, currentActivityStartedAt: now,
      acceptanceCoverage: { completed: 1, total: 4 }, lastOutputAt: now, lastHeartbeatAt: now,
      workerDetails: [{ workerId: 'reviewer', health: 'stalled', state: 'running', route: 'flash', model: 'metabolomics/qwen3.8', currentActivity: 'Waiting for model', lastOutputPreview: '<script>window.outputExecuted=true</script>\n4 tests passed' }],
      changes: { changedFiles: ['src/auth.ts'] }, tests: { passed: 4, failed: 0, completed: 4, total: 6, running: true },
      activity: [{ at: now, summary: 'Started authentication suite' }],
    }, ...overrides,
  };
}

test('current mission groups lead the list and details inspect workers without guessing session links', async () => {
  await withApp(async ({page, base}) => {
    let bulk = 0, detail = 0;
    await page.clock.install();
    await page.route('**/api/v1/mission-activity', r => { bulk++; return r.fulfill({json: {activities: [liveProjection()], server_time: new Date().toISOString()}}); });
    await page.route('**/api/v1/missions/pi%3AMSN-live/activity', r => { detail++; return r.fulfill({json: {activity: liveProjection()}}); });
    await page.goto(`${base}/#/missions`);
    await page.waitForSelector('[data-mission-key="pi:MSN-live"]');
    assert.match(await bodyText(page), /Live telemetry · stalled/);
    assert.match(await bodyText(page), /1 current/);
    assert.equal(await page.locator('[data-group=attention]').evaluate(el => el.open), true);
    assert.equal(await page.locator('#mc-tab-finished').getAttribute('aria-selected'), 'false');
    await page.getByRole('link', {name: 'Live Pi mission', exact: true}).click();
    await page.waitForSelector('[data-worker="reviewer"]');
    await page.locator('[data-worker="reviewer"] summary').click();
    assert.match(await bodyText(page), /4 tests passed/);
    assert.equal(await page.evaluate(() => window.outputExecuted), undefined);
    assert.equal(await page.locator('#view a[href*="/herd/session/"]').count(), 0);
    await page.locator('[data-disclosure=changes] > summary').click();
    assert.match(await bodyText(page), /src\/auth.ts/);
    assert.equal(bulk, 1); assert.equal(detail, 1);
    await page.clock.runFor(5500);
    await page.waitForTimeout(40);
    assert.equal(detail, 2);
    await page.waitForFunction(() => document.querySelector('[data-worker]')?.open);
    assert.equal(await page.locator('[data-worker="reviewer"]').evaluate(el => el.open), true);
  }, {BURST: '0'});
});

test('live activity clocks advance without requests, polls pause hidden and clean up on navigation', async () => {
  await withApp(async ({page, base}) => {
    let calls = 0, writes = 0;
    page.on('request', req => { if (req.method() !== 'GET') writes++; });
    await page.route('**/api/v1/mission-activity', async r => {
      calls++;
      // Browser time advances while hidden; model a newly collected snapshot on resume.
      const stamp = await page.evaluate(() => new Date().toISOString());
      return r.fulfill({json: {activities: [liveProjection({snapshot_at: stamp, observed_at: stamp})], server_time: stamp}});
    });
    await page.clock.install();
    await page.goto(`${base}/#/missions`);
    await page.waitForSelector('[data-report-age]');
    await page.clock.runFor(3000);
    assert.ok(parseInt(await page.locator('[data-report-age]').textContent(), 10) >= 4);
    assert.equal(calls, 1);
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', {configurable: true, value: true}); document.dispatchEvent(new Event('visibilitychange')); });
    await page.clock.runFor(40000); assert.equal(calls, 1);
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', {configurable: true, value: false}); document.dispatchEvent(new Event('visibilitychange')); });
    await page.waitForFunction(() => document.querySelector('[data-freshness]')?.textContent.includes('Live telemetry'));
    await page.waitForTimeout(30); assert.equal(calls, 2);
    await page.evaluate(() => { location.hash = '#/hosts'; });
    await page.waitForFunction(() => document.querySelector('#view h1')?.textContent === 'Hosts');
    await page.clock.runFor(20000); assert.equal(calls, 2); assert.equal(writes, 0);
  }, {BURST: '0'});
});

test('telemetry failures retain last known activity and age to disconnected without false crash', async () => {
  await withApp(async ({page, base}) => {
    let calls = 0;
    await page.route('**/api/v1/mission-activity', r => ++calls === 1
      ? r.fulfill({json: {activities: [liveProjection()], server_time: new Date().toISOString()}})
      : r.fulfill({status: 503, json: {error: 'unavailable'}}));
    await page.clock.install(); await page.goto(`${base}/#/missions`);
    await page.waitForSelector('[data-report-age]');
    await page.clock.runFor(6000);
    await page.waitForFunction(() => document.querySelector('.ml-connection')?.textContent.includes('interrupted'));
    await page.clock.runFor(130000);
    await page.locator('#mc-tab-unconfirmed').click();
    assert.match(await bodyText(page), /Disconnected telemetry · current state unknown/);
    assert.match(await bodyText(page), /Running authentication tests/);
    assert.doesNotMatch(await page.locator('#live-missions').innerText(), /crashed/i);
    assert.ok(calls < 12, `backoff bounds requests: ${calls}`);
  }, {BURST: '0'});
});

test('mission polling is single flight and late responses cannot replace a new route', async () => {
  await withApp(async ({page, base}) => {
    let calls = 0, held;
    await page.route('**/api/v1/mission-activity', r => { calls++; held = r; });
    await page.clock.install(); await page.goto(`${base}/#/missions`);
    await page.waitForFunction(() => document.querySelector('#live-missions'));
    await page.waitForTimeout(50);
    await page.clock.runFor(10000); assert.equal(calls, 1);
    await page.evaluate(() => { location.hash = '#/hosts'; });
    await page.waitForFunction(() => document.querySelector('#view h1')?.textContent === 'Hosts');
    await held.fulfill({json: {activities: [liveProjection()]}}).catch(() => {});
    await page.clock.runFor(30000);
    assert.equal(calls, 1);
    assert.equal(await page.locator('#view h1').textContent(), 'Hosts');
    assert.equal(await page.locator('[data-mission-key]').count(), 0);
  }, {BURST: '0'});
});

test('updating revisions retain labeled last-known evidence and freeze elapsed work', async () => {
  await withApp(async ({page, base}) => {
    let calls = 0;
    const p = liveProjection({mission_revision: 1, activity_revision: 1});
    p.activity_snapshot_at = p.snapshot_at;
    await page.route('**/api/v1/missions/pi%3AMSN-live/activity', r => {
      calls++;
      return r.fulfill({json: {activity: calls === 1 ? p : {...p, connection: 'updating', mission_revision: 2,
        freshness: {...p.freshness, state: 'updating', age_seconds: 0}}}});
    });
    await page.clock.install(); await page.goto(`${base}/#/missions/pi%3AMSN-live`);
    await page.waitForSelector('[data-worker]');
    await page.clock.runFor(6000);
    await page.waitForFunction(() => document.querySelector('[data-freshness]')?.dataset.state === 'updating');
    assert.match(await bodyText(page), /Updating telemetry · current details unavailable/);
    assert.match(await bodyText(page), /Last recorded measurements/);
    await page.locator('[data-disclosure=diagnostics] > summary').click();
    const elapsed = page.locator('[data-disclosure=diagnostics] [data-clock]').first();
    const value = await elapsed.textContent();
    await page.clock.runFor(6000);
    assert.equal(await elapsed.textContent(), value);
    assert.match(await bodyText(page), /Activity elapsed at last snapshot/);
    assert.doesNotMatch(await bodyText(page), /Live telemetry/);
  }, {BURST: '0'});
});

test('Pi detail lifecycle history refreshes on real SSE without replacing live inspection', async () => {
  await withApp(async ({page, base}) => {
    const p = liveProjection({mission_key: 'pi:MSN-PI1', mission: {id: 'MSN-PI1', title: 'Pi mission', status: 'EXECUTING'}});
    await page.route('**/api/v1/missions/pi%3AMSN-PI1/activity', r => r.fulfill({json: {activity: p}}));
    await page.goto(`${base}/#/missions/pi%3AMSN-PI1`);
    await page.waitForSelector('[data-worker="reviewer"]');
    await page.locator('[data-worker="reviewer"] summary').click();
    await page.locator('[data-history-panel=events] > summary').click();
    await page.locator('[data-history-panel=timeline] > summary').click();
    await page.evaluate(() => { window.livePaneBefore = document.querySelector('#live-mission-detail'); });
    const response = await fetch(`${base}/api/v1/ingest`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      source: 'pi', entity_type: 'mission', entity_id: 'MSN-PI1', event_type: 'MissionStopped', payload: {title: 'Pi mission', stage: 'complete'},
    })});
    assert.equal(response.status, 200);
    await page.waitForFunction(() => document.querySelector('#mission-history herdr-status')?.getAttribute('stage') === 'complete', null, {timeout: 12000});
    assert.equal(await page.evaluate(() => window.livePaneBefore === document.querySelector('#live-mission-detail')), true);
    assert.equal(await page.locator('[data-worker="reviewer"]').evaluate(el => el.open), true);
    assert.equal(await page.locator('[data-history-panel=events]').evaluate(el=>el.open),true);
    assert.equal(await page.locator('[data-history-panel=timeline]').evaluate(el=>el.open),true);
    assert.match(await page.locator('#mission-stream').innerText(), /complete|stopped/i);
    await page.waitForFunction(() => /complete|stopped/i.test(document.querySelector('#mission-timeline')?.innerText || ''), null, {timeout: 5000});
    assert.match(await page.locator('#mission-timeline').innerText(), /complete|stopped/i);
  }, {BURST: '0'});
});


test('service catalog filters, archive inspection and groups survive live refresh on mobile', async () => {
  await withApp(async ({page,base}) => {
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.setViewportSize({width:390,height:844});
    await page.goto(`${base}/#/services`);
    await page.locator('#sc-search').waitFor();
    assert.equal(await page.locator('#sc-history').evaluate(el=>el.open),false);
    assert.equal(await page.locator('.sc-row').filter({hasText:'gateway'}).count(),0);
    await page.locator('#sc-history > summary').click();
    assert.match(await page.locator('#sc-history-counts').innerText(), /1.*Archived/s);
    assert.equal(await page.locator('.sc-row').filter({hasText:'Archived parser'}).count(),0);
    assert.equal(await page.locator('.sc-row').filter({hasText:'Inactive parser'}).count(),0);
    await page.locator('[data-filter=unknown]').click();
    assert.equal(await page.locator('.sc-row').count(),1);
    assert.match(await page.locator('.sc-row').innerText(), /Stale healthy parser/);
    assert.match(await page.locator('.sc-health').innerText(), /Unconfirmed/);
    await page.locator('[data-filter=archived]').click();
    assert.equal(await page.locator('.sc-row').count(),1);
    assert.match(await page.locator('.sc-row').innerText(), /Archived parser/);
    await page.locator('[data-filter=current]').click();
    await page.locator('#sc-search').fill('spectra 8080');
    assert.equal(await page.locator('.sc-row').count(),1);
    assert.match(await page.locator('.sc-row').innerText(), /Parse experimental spectra/);
    await page.locator('#sc-expand').click();
    await page.waitForTimeout(6000);
    assert.equal(await page.locator('#sc-search').inputValue(),'spectra 8080');
    assert.equal(await page.locator('.sc-group[open]').count(),1);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.locator('#sc-search').fill('');
    await page.locator('#sc-archived').check();
    await page.locator('#sc-host').selectOption('beast');
    await page.locator('#sc-expand').click();
    const archived=page.locator('.sc-row').filter({hasText:'Archived parser'});
    assert.equal(await archived.count(),1);
    assert.equal(await page.locator('a[href^="javascript:"]').count(),0);
    assert.equal(await page.evaluate(()=>window.catalogXss),undefined);
    await archived.locator('.sc-name').click();
    await page.locator('.sc-detail').waitFor();
    assert.match(await bodyText(page), /Archived services are hidden/);
    assert.match(await bodyText(page), /Unavailable since/);
    assert.deepEqual(errors,[]);
  }, {SERVICES_SEED:'1',BURST:'1',BURST_GAP:'1'});
});


test('mission catalog stays compact, searchable and keyboard stable across changed live snapshots', async () => {
 await withApp(async ({page,base})=>{
  let calls=0;
  const current=liveProjection();current.mission.title='Repair the parser '+ 'long objective '.repeat(100)+'SEARCH_TAIL';
  current.activity.workerDetails=Array.from({length:40},(_,i)=>({workerId:'private-worker-'+i,state:'running',health:'active'}));
  const stale=Array.from({length:35},(_,i)=>liveProjection({mission_key:'pi:old-'+i,mission:{id:'old-'+i,title:'Disconnected mission '+i,status:'EXECUTING'},connection:'disconnected',freshness:{state:'disconnected',age_seconds:200}}));
  await page.route('**/api/v1/mission-activity',r=>{calls++;return r.fulfill({json:{activities:[{...current,activity:{...current.activity,currentActivity:{summary:'Running parser tests '+calls}}},...stale]}});});
  await page.clock.install();await page.goto(`${base}/#/missions`);
  await page.locator('.mc-row[data-mission-key="pi:MSN-live"]').waitFor();
  assert.equal(await page.locator('[data-group=lastknown]').count(),0);
  assert.match(await page.locator('#mc-tab-unconfirmed').innerText(),/35/);
  assert.equal(await page.locator('#mc-tab-finished').getAttribute('aria-selected'),'false');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollHeight)<1500);
  // The independent history response may replace this row while it is measured.
  await page.waitForFunction(()=>{
    const row=document.querySelector('.mc-row[data-mission-key="pi:MSN-live"]');
    const height=row?.getBoundingClientRect().height;
    return height>0&&height<230;
  });
  assert.doesNotMatch(await bodyText(page),/private-worker-/);
  await page.locator('#mc-search').fill('SEARCH_TAIL');
  assert.equal(await page.locator('.mc-row').count(),1);
  await page.evaluate(()=>{window.catalogSearch=document.querySelector('#mc-search');});
  await page.clock.runFor(5500);await page.waitForTimeout(50);
  assert.equal(await page.locator('#mc-search').inputValue(),'SEARCH_TAIL');
  assert.equal(await page.evaluate(()=>document.activeElement===window.catalogSearch),true);
  await page.locator('.mc-title').focus();
  await page.clock.runFor(5500);await page.waitForTimeout(50);
  assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('mc-title')),true);
  await page.locator('#mc-search').fill('');
  await page.locator('#mc-tab-unconfirmed').click();
  await page.locator('[data-group=lastknown][open]').waitFor();
  await page.locator('[data-more=lastknown]').focus();
  await page.clock.runFor(5500);await page.waitForTimeout(50);
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.more),'lastknown');
  await page.locator('[data-more=lastknown]').click();
  assert.equal(await page.locator('[data-group=lastknown] .mc-row').count(),20);
  await page.locator('[data-group=lastknown] > summary').click();
  await page.clock.runFor(5500);await page.waitForTimeout(50);
  assert.equal(await page.locator('[data-group=lastknown]').evaluate(e=>e.open),false);
  assert.equal(await page.evaluate(()=>document.activeElement.tagName),'SUMMARY');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.ok(calls>=5);
 },{BURST:'0'});
});

test('same-group freshness transitions relabel activity and freeze waiting clocks', async()=>{
 await withApp(async({page,base})=>{
  const p=liveProjection();p.activity={health:'unknown',currentActivity:{summary:'Waiting for a report'},waitingReason:'gateway_busy',waitingSince:new Date(Date.now()-10000).toISOString()};
  let calls=0;
  await page.route('**/api/v1/mission-activity',r=>++calls===1?r.fulfill({json:{activities:[p]}}):r.fulfill({status:503,json:{error:'offline'}}));
  await page.clock.install();await page.goto(`${base}/#/missions`);
  await page.locator('#mc-tab-unconfirmed').click();
  await page.locator('.mc-row').waitFor();
  assert.match(await page.locator('.mc-activity').innerText(),/^Activity:/);
  await page.clock.runFor(32000);await page.waitForTimeout(50);
  assert.match(await page.locator('.mc-activity').innerText(),/^Last known activity:/);
  assert.match(await page.locator('.mc-wait').innerText(),/at last snapshot/);
  const age=await page.locator('[data-wait-age]').innerText();
  await page.clock.runFor(10000);assert.equal(await page.locator('[data-wait-age]').innerText(),age);
 },{BURST:'0'});
});

test('recorded history requests time out and retry without blocking live missions or late navigation', async()=>{
 await withApp(async({page,base})=>{
  let records=0, held, releaseRetries;
  const retriesAllowed = new Promise(resolve => { releaseRetries = resolve; });
  await page.route('**/api/v1/missions', async r=>{
   records++;
   if(records===1){held=r;return;}
   // Initial SSE replay can retry immediately after the 15s abort. Keep that
   // successful response from clearing the error before we observe it.
   await retriesAllowed;
   return r.continue();
  });
  await page.route('**/api/v1/mission-activity',r=>r.fulfill({json:{activities:[liveProjection()]}}));
  try {
   await page.clock.install();await page.goto(`${base}/#/missions`);
   await page.locator('.mc-row').first().waitFor();
   await page.clock.runFor(16000);await page.waitForFunction(()=>document.querySelector('.ml-connection').textContent.includes('Recorded history could not refresh'));
   releaseRetries();
   await page.clock.runFor(15000);
   await page.locator('[data-filter=recorded]').click();
   await page.locator('.mc-title').filter({hasText:'Alpha'}).waitFor();
   assert.ok(records>=2);
   await page.evaluate(()=>{location.hash='#/hosts';});await page.waitForFunction(()=>document.querySelector('#view h1')?.textContent==='Hosts');
   await held.fulfill({json:{missions:[]}}).catch(()=>{});
   assert.equal(await page.locator('#view h1').innerText(),'Hosts');
  } finally { releaseRetries(); }
 },{BURST:'0'});
});

test('mission detail preserves explicit disclosure state and an inspected worker moving into history',async()=>{
 await withApp(async({page,base})=>{
  let phase=0;
  const p=liveProjection();
  await page.route('**/api/v1/missions/pi%3AMSN-live/activity',r=>{
   const value=structuredClone(p);value.activity.currentActivity.summary='phase '+phase;
   if(phase>=2){value.activity.workerDetails[0].state='completed';value.activity.workerDetails[0].endedAt=new Date().toISOString();}
   return r.fulfill({json:{activity:value}});
  });
  await page.clock.install();await page.goto(`${base}/#/missions/pi%3AMSN-live`);
  await page.locator('[data-worker=reviewer]').waitFor();
  await page.locator('[data-disclosure=brief] > summary').click();
  await page.locator('[data-disclosure=diagnostics] > summary').click();
  await page.locator('[data-worker=reviewer] > summary').click();
  await page.evaluate(()=>{window.stableWorker=document.querySelector('[data-worker=reviewer]');});
  await page.clock.runFor(5500);await page.waitForTimeout(50);
  assert.equal(await page.evaluate(()=>window.stableWorker===document.querySelector('[data-worker=reviewer]')),true);
  phase=1;await page.clock.runFor(5500);await page.waitForTimeout(50);
  assert.equal(await page.locator('[data-disclosure=brief]').evaluate(e=>e.open),true);
  assert.equal(await page.locator('[data-disclosure=diagnostics]').evaluate(e=>e.open),true);
  await page.locator('[data-disclosure=brief] > summary').click();
  phase=2;await page.clock.runFor(5500);await page.waitForTimeout(50);
  assert.equal(await page.locator('[data-disclosure=brief]').evaluate(e=>e.open),false);
  assert.equal(await page.locator('[data-disclosure=worker-history]').evaluate(e=>e.open),true);
  assert.equal(await page.locator('[data-worker=reviewer]').isVisible(),true);
  assert.equal(await page.locator('[data-worker=reviewer]').evaluate(e=>e.open),true);
  await page.locator('[data-disclosure=worker-history] > summary').click();
  phase=3;await page.clock.runFor(5500);await page.waitForTimeout(50);
  assert.equal(await page.locator('[data-disclosure=worker-history]').evaluate(e=>e.open),false);
 },{BURST:'0'});
});

test('leaving session detail cancels its initial read and cannot overwrite the next page',async()=>{
 await withApp(async({page,base})=>{
  let held, started;
  const seen=new Promise(resolve=>{started=resolve;});
  await page.route('**/api/v1/sessions/late-session',r=>{held=r;started();});
  await page.goto(`${base}/#/herd/session/late-session`);await seen;
  const canceled=page.waitForEvent('requestfailed',{predicate:r=>r.url().endsWith('/api/v1/sessions/late-session'),timeout:5000});
  await page.evaluate(()=>{location.hash='#/hosts';});
  await canceled;
  await page.waitForFunction(()=>document.querySelector('#view h1')?.textContent==='Hosts');
  await held.fulfill({json:{session_id:'late-session',agent_role:'Late session',status:'active'}}).catch(()=>{});
  assert.equal(await page.locator('#view h1').innerText(),'Hosts');
 },{BURST:'0'});
});

test('mission outcome tabs isolate terminal records and query links survive real SSE refresh',async()=>{
 await withApp(async({page,base})=>{
  const records=[{mission_id:'open-record',title:'Recorded open',status:'active',stage:'planning',updated_at:new Date().toISOString()},
   {mission_id:'closed-record',title:'Recorded closed',status:'active',stage:'closed'},
   {mission_id:'unknown-record',title:'Unconfirmed record',status:'active'}];
  const projections=[liveProjection({mission_key:'pi:running',mission:{id:'running',title:'Running mission',status:'EXECUTING'},activity:{health:'active'}}),
   liveProjection({mission_key:'pi:failure',mission:{id:'failure',title:'Failed mission',status:'FAILED'}}),
   liveProjection({mission_key:'pi:done',mission:{id:'done',title:'Finished mission',status:'COMPLETE'},freshness:{state:'disconnected',age_seconds:400}}),
   liveProjection({mission_key:'pi:closed',mission:{id:'closed',title:'Closed mission',status:'CLOSED'}})];
  let reads=0, refreshed;const nextRead=new Promise(resolve=>{refreshed=resolve;});
  await page.route('**/api/v1/missions',r=>{reads++;if(refreshed){const done=refreshed;refreshed=null;done();}return r.fulfill({json:{missions:records}});});
  await page.route('**/api/v1/mission-activity',r=>r.fulfill({json:{activities:projections}}));
  await page.clock.install();await page.goto(`${base}/#/missions`);
  await page.waitForFunction(()=>document.querySelector('#mc-tab-current')?.innerText.includes('2'));
  assert.match(await page.locator('#mc-tab-failed').innerText(),/1/);assert.match(await page.locator('#mc-tab-finished').innerText(),/3/);
  assert.equal(await page.locator('[data-mission-key="pi:failure"]').count(),0);
  assert.equal(await page.locator('[data-mission-key="pi:done"]').count(),0);
  await page.locator('#mc-tab-current').focus();await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#mc-tab-unconfirmed').getAttribute('aria-selected'),'true');
  assert.match(await page.locator('.mc-row').innerText(),/Unconfirmed record/);
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#mc-tab-failed').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('.mc-row').count(),1);
  await page.keyboard.press('End');assert.equal(await page.locator('#mc-tab-finished').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('.mc-row').count(),3);assert.match(await page.locator('[data-mission-key="closed-record"]').innerText(),/Closed/);
  await page.goto(`${base}/#/missions?tab=failed&status=attention`);
  await page.locator('[data-mission-key="pi:failure"]').waitFor();
  await page.locator('#mc-search').fill('Failed mission');
  await page.evaluate(()=>{window.missionCatalogBefore=document.getElementById('live-missions');});
  const countBefore=reads;const afterEvent=new Promise(resolve=>{refreshed=resolve;});
  const response=await fetch(`${base}/api/v1/ingest`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entity_type:'mission',entity_id:'MSN-PI1',event_type:'MissionUpdated',source:'pi',payload:{title:'Pi mission',stage:'implementation'}})});
  assert.equal(response.status,200);
  await page.clock.runFor(6000);await afterEvent;
  await page.waitForFunction(()=>document.querySelector('.mc-row'));
  assert.ok(reads>countBefore);
  assert.equal(await page.evaluate(()=>window.missionCatalogBefore===document.getElementById('live-missions')),true);
  assert.equal(await page.locator('#mc-search').inputValue(),'Failed mission');
  assert.equal(await page.locator('#mc-tab-failed').getAttribute('aria-selected'),'true');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 },{BURST:'0'});
});

test('empty mission sources finish loading and mission query filters stay selected',async()=>{
 await withApp(async({page,base})=>{
  let empty=true;
  await page.route('**/api/v1/missions',r=>r.fulfill({json:{missions:[]}}));
  await page.route('**/api/v1/mission-activity',r=>r.fulfill({json:{activities:empty?[]:[liveProjection()]}}));
  await page.goto(`${base}/#/missions`);
  await page.waitForFunction(()=>document.querySelector('#mc-tab-current')?.innerText.trim()==='Current 0');
  assert.match(await page.locator('#mc-tab-failed').innerText(),/0/);assert.match(await page.locator('#mc-tab-finished').innerText(),/0/);
  assert.doesNotMatch(await page.locator('#view').innerText(),/Loading|0\+/);
  empty=false;await page.goto(`${base}/#/missions?tab=current&status=attention`);
  await page.locator('.mc-row').waitFor();
  assert.equal(await page.locator('#mc-status').inputValue(),'attention');
  await page.locator('#mc-search').fill('Live');assert.equal(await page.locator('#mc-status').inputValue(),'attention');
 },{BURST:'0'});
});

test('sessions tabs keep closed records separate, retain filters and encode exact detail IDs',async()=>{
 await withApp(async({page,base})=>{
  const id='herdr:beast:%2Frepo:w1:p3';const now=new Date().toISOString();
  const closed={session_id:id,agent_role:'Closed stage',status:'active',stage:'closed',cwd:'/work/repo',host_id:'beast',updated_at:now};
  const sessions=[...Array.from({length:25},(_,i)=>({session_id:`open-${i}`,agent_role:`pi ${i}`,status:'active',host_id:'beast',cwd:'/work/parser',updated_at:now})),
   {session_id:'stale-open',agent_role:'Old open',status:'active',updated_at:new Date(Date.now()-16*60000).toISOString()},
   {session_id:'failed',agent_role:'Failed agent',status:'failed',host_id:'beast',updated_at:now},closed,
   ...['stopped','closed','completed'].map(status=>({session_id:status,agent_role:status,status,updated_at:now}))];
  let reads=0,next;const refreshed=new Promise(resolve=>{next=resolve;});
  await page.route('**/api/v1/sessions',r=>{reads++;if(reads>1)next();return r.fulfill({json:{sessions:sessions.map(s=>({...s,summary:`Report ${reads}`}))}});});
  await page.route('**/api/v1/missions',r=>r.fulfill({json:{missions:[]}}));
  await page.route(`**/api/v1/sessions/${encodeURIComponent(id)}`,r=>r.fulfill({json:closed}));
  let eventId,eventsSeen;const eventRead=new Promise(resolve=>{eventsSeen=resolve;});
  await page.route('**/api/v1/events?*',r=>{eventId=new URL(r.request().url()).searchParams.get('entity_id');eventsSeen();return r.fulfill({json:{events:[]}});});
  const writes=[],errors=[];page.on('request',r=>{if(r.method()!=='GET')writes.push(r.url());});page.on('pageerror',e=>errors.push(e.message));
  await page.clock.install();await page.goto(`${base}/#/herd`);
  await page.waitForFunction(()=>document.querySelector('#ss-tab-current [data-count]')?.textContent==='25');
  const catalogGeometry=await page.locator('.ss-row').first().evaluate(row=>({top:row.getBoundingClientRect().top,height:row.getBoundingClientRect().height}));
  assert.ok(catalogGeometry.top<=330,`expected records near the top of the viewport, got ${catalogGeometry.top}`);
  assert.ok(catalogGeometry.height<=96,`expected compact desktop agent rows, got ${catalogGeometry.height}`);
  assert.equal(await page.locator('.ss-row').count(),20);await page.locator('#ss-next').click();assert.equal(await page.locator('.ss-row').count(),5);
  await page.locator('#ss-tab-current').focus();await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#ss-tab-unconfirmed').getAttribute('aria-selected'),'true');
  assert.match(await page.locator('.ss-row').innerText(),/Old open/);
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('#ss-tab-failed').getAttribute('aria-selected'),'true');assert.equal(await page.locator('.ss-row').count(),1);
  await page.keyboard.press('End');assert.equal(await page.locator('#ss-tab-finished').getAttribute('aria-selected'),'true');assert.equal(await page.locator('.ss-row').count(),4);
  await page.locator('#ss-tab-current').click();await page.locator('#ss-search').fill('open-1');
  await page.locator('[data-session-link]').first().focus();
  const focused=await page.evaluate(()=>document.activeElement.closest('[data-session-id]').dataset.sessionId);
  await page.clock.runFor(5500);await refreshed;
  await page.waitForFunction(()=>document.querySelector('.ss-summary')?.textContent.includes('Report 2'));
  assert.equal(await page.locator('#ss-search').inputValue(),'open-1');
  assert.equal(await page.evaluate(()=>document.activeElement.closest('[data-session-id]')?.dataset.sessionId),focused);
  await page.locator('#ss-search').fill('');await page.locator('#ss-tab-finished').click();await page.locator('#ss-search').fill('Closed stage');
  await page.locator('[data-session-link]').click();await page.locator('#session-details').waitFor();await eventRead;
  assert.equal(eventId,id);assert.match(await page.locator('#view').innerText(),/Finished agent/);assert.doesNotMatch(await page.locator('.sd-head').innerText(),/reported active/);
  assert.equal(await page.locator('#msg-form').count(),0);assert.equal(await page.locator('#session-details').evaluate(el=>el.open),false);
  await page.locator('#session-details > summary').click();assert.ok((await page.locator('#session-details').innerText()).includes(id));
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
 },{BURST:'0'});
});

test('overview keeps healthy panels through partial errors and links to the exact outcome and service filters',async()=>{
 await withApp(async({page,base})=>{
  let liveReads=0;
  const sessions=[{session_id:'open',agent_role:'Parser',status:'active',updated_at:new Date().toISOString()},
   {session_id:'failed',agent_role:'Broken parser',status:'failed'},{session_id:'closed',status:'stopped'}];
  await page.route('**/api/v1/sessions',r=>r.fulfill({json:{sessions}}));
  await page.route('**/api/v1/missions',r=>r.fulfill({json:{missions:[{mission_id:'closed',status:'closed',title:'Closed mission'}]}}));
  await page.route('**/api/v1/mission-activity',r=>++liveReads===1?r.fulfill({status:503,json:{error:'temporarily unavailable'}}):r.fulfill({json:{activities:[liveProjection({activity:{health:'active'}})]}}));
  await page.route('**/api/v1/health',r=>r.fulfill({status:503,json:{error:'health unavailable'}}));
  await page.clock.install();await page.goto(`${base}/#/overview`);
  await page.waitForFunction(()=>document.querySelector('[data-overview=sessions]')?.textContent.includes('1Recently reported agents'));
  await page.waitForFunction(()=>document.querySelector('[data-overview=missions]')?.textContent.includes('Could not refresh'));
  assert.equal(await page.locator('#view h1').innerText(),'Overview');
  assert.equal(await page.locator('[data-overview=missions] a[href*="status=attention"] strong').innerText(),'—');assert.match(await page.locator('[data-overview=sessions]').innerText(),/Parser/);
  assert.doesNotMatch(await page.locator('[data-overview=sessions] .ov-list').innerText(),/Broken parser/);
  await page.clock.runFor(5500);
  await page.waitForFunction(()=>document.querySelector('[data-overview=missions] a[href*="status=running"] strong')?.textContent==='1');
  await page.locator('a[href="#/herd?tab=failed"]').click();await page.locator('#ss-tab-failed[aria-selected=true]').waitFor();await page.locator('.ss-row').waitFor();assert.equal(await page.locator('.ss-row').count(),1);
  await page.goto(`${base}/#/overview`);await page.locator('a[href="#/missions?tab=finished"]').waitFor();await page.locator('a[href="#/missions?tab=finished"]').click();
  await page.locator('#mc-tab-finished[aria-selected=true]').waitFor();await page.locator('.mc-row').waitFor();assert.match(await page.locator('.mc-row').innerText(),/Closed mission/);
  await page.goto(`${base}/#/overview`);await page.locator('a[href="#/services?status=unavailable"]').waitFor();await page.locator('a[href="#/services?status=unavailable"]').click();
  await page.locator('#sc-status').waitFor();assert.equal(await page.locator('#sc-status').inputValue(),'unavailable');
  await page.setViewportSize({width:1920,height:1080});await page.goto(`${base}/#/overview`);await page.locator('[data-overview=sessions] .ov-row').waitFor();
  const desktopLayout=await page.evaluate(()=>{
   const content=document.querySelector('herdr-app-shell').shadowRoot.querySelector('.hd-shell__content').getBoundingClientRect();
   const box=key=>document.querySelector(`[data-overview=${key}]`).getBoundingClientRect();
   return {contentWidth:content.width,missions:box('missions'),fleet:box('fleet'),sessions:box('sessions'),ci:box('ci')};
  });
  assert.ok(desktopLayout.contentWidth>=1400,`expected operational content to use desktop width, got ${desktopLayout.contentWidth}`);
  assert.ok(desktopLayout.missions.width>desktopLayout.sessions.width,'current missions should remain the primary overview panel');
  assert.ok(desktopLayout.fleet.top<desktopLayout.sessions.bottom,'fleet status should share the upper overview region');
  assert.ok(Math.abs(desktopLayout.sessions.top-desktopLayout.ci.top)<2,'secondary overview panels should align in a desktop row');
  await page.setViewportSize({width:390,height:844});await page.goto(`${base}/#/overview`);await page.locator('[data-overview=sessions] .ov-row').waitFor();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 },{BURST:'0',SERVICE_CATALOG:'1'});
});

test('sessions retain keyboard focus when refreshed records leave the filter and stop reads hidden or after navigation',async()=>{
 await withApp(async({page,base})=>{
  let reads=0;
  await page.route('**/api/v1/sessions',r=>{reads++;return r.fulfill({json:{sessions:[
   {session_id:'first',agent_role:'agent',status:'active',updated_at:new Date().toISOString(),summary:reads===1?'needlecontext':'removed'},
   {session_id:'second',agent_role:'agent',status:'active',updated_at:new Date().toISOString(),summary:reads<=2?'needlecontext':'removed'}]}});});
  await page.route('**/api/v1/missions',r=>r.fulfill({json:{missions:[]}}));
  await page.clock.install();await page.goto(`${base}/#/herd`);await page.locator('.ss-row').first().waitFor();
  await page.locator('#ss-search').fill('needlecontext');await page.locator('[data-session-id=first] [data-session-link]').focus();
  await page.clock.runFor(5500);await page.waitForFunction(()=>document.querySelectorAll('.ss-row').length===1);
  assert.equal(await page.evaluate(()=>document.activeElement.closest('[data-session-id]')?.dataset.sessionId),'second');
  await page.clock.runFor(5500);await page.waitForFunction(()=>document.querySelectorAll('.ss-row').length===0);
  assert.equal(await page.evaluate(()=>document.activeElement.id),'ss-search');
  const before=reads;
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'));});
  await page.clock.runFor(16000);assert.equal(reads,before);
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));});
  await page.waitForFunction(()=>document.querySelector('#ss-result')?.textContent.includes('0 matching'));
  await page.evaluate(()=>{location.hash='#/hosts';});await page.locator('#view h1').filter({hasText:'Hosts'}).waitFor();
  const after=reads;await page.clock.runFor(16000);assert.equal(reads,after);
 },{BURST:'0'});
});

test('session inspection renders before optional mission context and cancels that context on navigation',async()=>{
 await withApp(async({page,base})=>{
  let held,ready;const requested=new Promise(resolve=>{ready=resolve;});
  await page.route('**/api/v1/sessions/context-session',r=>r.fulfill({json:{session_id:'context-session',agent_role:'Context agent',status:'active',mission_id:'some-mission'}}));
  await page.route('**/api/v1/missions',r=>{held=r;ready();});
  await page.goto(`${base}/#/herd/session/context-session`);await requested;
  await page.locator('#session-details').waitFor();assert.match(await page.locator('#view h1').innerText(),/Context agent/);
  assert.match(await page.locator('#session-mission').innerText(),/not resolved/);
  const canceled=page.waitForEvent('requestfailed',{predicate:r=>r.url().endsWith('/api/v1/missions'),timeout:5000});
  await page.evaluate(()=>{location.hash='#/hosts';});await canceled;await page.locator('#view h1').filter({hasText:'Hosts'}).waitFor();
  await held.fulfill({json:{missions:[{mission_id:'some-mission',title:'Late mission',sessions:[{session_id:'context-session'}]}]}}).catch(()=>{});
  assert.equal(await page.locator('#view h1').innerText(),'Hosts');
 },{BURST:'0'});
});

// These timing tests isolate the advanced inspector from the independently
// streamed session workspace, while using the real client and backend.
async function inspectorHarness(page,base,sid) {
  await page.route('**/inspector-harness**',r=>r.fulfill({contentType:'text/html',body:`<!doctype html><div id="inspector-host"></div><script type="module">
    import {mountInspector} from '/inspector-view.js';
    import {HerdrClient} from '/herdr-web-client/client.js';
    mountInspector(document.getElementById('inspector-host'),new HerdrClient(''),new URLSearchParams(location.search).get('session'));
  </script>`}));
  await page.goto(`${base}/inspector-harness?session=${encodeURIComponent(decodeURIComponent(sid))}`);
}

test('inspector resynchronizes events written between the initial snapshot and stream subscription',async()=>{
 await withApp(async({page,base})=>{
  const hash=await sessionHash(base),sid=hash.split('/').pop();
  let releaseSnapshot,releaseStream,snapshotReady,snapshotDone;
  const snapshotAllowed=new Promise(resolve=>{releaseSnapshot=resolve;});
  const streamAllowed=new Promise(resolve=>{releaseStream=resolve;});
  const captured=new Promise(resolve=>{snapshotReady=resolve;});
  const fulfilled=new Promise(resolve=>{snapshotDone=resolve;});
  let reads=0;
  await page.addInitScript(()=>{
   const Native=window.EventSource;
   window.EventSource=class extends Native{
    constructor(url,options){super(url,options);if(String(url).includes('/inspector/stream'))this.addEventListener('message',event=>{
     try{if(JSON.parse(event.data).type==='connected')window.inspectorConnected=true;}catch{}
    });}
   };
  });
  await page.route('**/inspector?*',async r=>{
   reads++;
   if(reads!==1)return r.continue();
   const response=await r.fetch();const json=await response.json();snapshotReady(json);
   await snapshotAllowed;await r.fulfill({json}).catch(()=>{});snapshotDone();
  });
  await page.route('**/inspector/stream',async r=>{await streamAllowed;return r.continue().catch(()=>{});});
  try{
   await inspectorHarness(page,base,sid);const initial=await captured;assert.equal(initial.events.length,0);
   await postInspector(base,sid,Array.from({length:60},(_,i)=>({kind:'message',role:'assistant',text:`gap-${i}`})));
   releaseStream();await page.waitForFunction(()=>window.inspectorConnected===true);
   await page.waitForFunction(()=>document.querySelectorAll('.hd-ins__row').length===60,null,{timeout:5000});
   assert.ok(reads>=2,'a connected stream must resynchronize even while its initial snapshot is pending');
   releaseSnapshot();await fulfilled;await page.evaluate(()=>new Promise(requestAnimationFrame));
   assert.equal(await page.locator('.hd-ins__row').count(),60);
   assert.match(await page.locator('#ins-list').innerText(),/gap-59/);
  }finally{releaseSnapshot();releaseStream();}
 },{BURST:'0',INSPECTOR_SEED:'0'});
});

test('inspector HTTP cursor catches events received before its baseline even when newer stream rows arrive later',async()=>{
 await withApp(async({page,base})=>{
  const hash=await sessionHash(base),sid=hash.split('/').pop();
  await postInspector(base,sid,[{kind:'capabilities',reasoning:'none',streaming_tools:true}]);
  const gates=[];let baselineReady,deltaReady,baselineRevision,reads=0;
  const baselines=new Promise(resolve=>{baselineReady=resolve;});
  const delta=new Promise(resolve=>{deltaReady=resolve;});
  await page.addInitScript(()=>{
   const Native=window.EventSource;window.inspectorBuffered=[];
   window.EventSource=class extends Native{
    constructor(url,options){super(url,options);if(String(url).includes('/inspector/stream'))this.addEventListener('message',event=>{
     try{if(JSON.parse(event.data).type==='event'){event.stopImmediatePropagation();window.inspectorBuffered.push(()=>this.onmessage(event));}}catch{}
    });}
   };
  });
  await page.route('**/inspector?*',async r=>{
   const requestIndex=++reads;
   if(requestIndex>2){deltaReady(Number(new URL(r.request().url()).searchParams.get('since_rev')));return r.continue();}
   const response=await r.fetch();const json=await response.json();if(requestIndex===2)baselineRevision=json.rev;let release;
   const gate=new Promise(resolve=>{release=resolve;});gates[requestIndex-1]=release;
   if(gates.filter(Boolean).length===2)baselineReady();
   await gate;return r.fulfill({json}).catch(()=>{});
  });
  try{
   await page.clock.install();await inspectorHarness(page,base,sid);await baselines;
   await postInspector(base,sid,Array.from({length:60},(_,i)=>({kind:'message',role:'assistant',text:`cursor-${i}`})));
   await page.waitForFunction(()=>window.inspectorBuffered.length===60);
   await page.evaluate(()=>window.inspectorBuffered.slice(0,50).forEach(deliver=>deliver()));
   gates[1]();await page.clock.runFor(250);
   await page.waitForFunction(()=>document.querySelectorAll('#ins-bar .hd-ins__badge').length>0);
   await page.evaluate(()=>window.inspectorBuffered.slice(50).forEach(deliver=>deliver()));
   await page.clock.runFor(2200);const cursor=await delta;
   assert.equal(cursor,baselineRevision,'stream rows cannot advance the HTTP synchronization cursor past unseen events');
   await page.waitForFunction(()=>document.querySelectorAll('.hd-ins__row').length===60,null,{timeout:5000});
   assert.match(await page.locator('#ins-list').innerText(),/cursor-0/);
   assert.match(await page.locator('#ins-list').innerText(),/cursor-59/);
  }finally{gates.forEach(release=>release());}
 },{BURST:'0',INSPECTOR_SEED:'0'});
});

test('CI catalog keeps filters and step inspection through refresh and shows stale authentication failures',async()=>{
  await withApp(async({page,base})=>{
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    let reads=0,authError=false,logReads=0;
    const created=new Date(Date.now()-65000).toISOString();
    const running={repository:'lab/spectra',project_id:'spectra',run_number:42,state:'running',branch:'main',revision:'abcdef123456',created_at:created,url:'https://ci.example/lab/spectra/42'};
    const failed={...running,run_number:41,state:'failure',finished_at:new Date().toISOString()};
    await page.route('**/api/v1/ci',r=>{reads++;return r.fulfill({json:{available:!authError,status:authError?'auth_error':'connected',stale:authError,last_success_at:created,repos:[{repository:'lab/spectra'}],pipelines:{'lab/spectra':[running,failed]},agents:[]}});});
    await page.route('**/api/v1/ci/pipeline?*',r=>r.fulfill({json:{pipeline:{...running,tasks:[{id:1,name:'Compile',state:'success'},{id:2,name:'Test spectra',state:'running'}]}}}));
    await page.route('**/api/v1/ci/log?*',r=>{logReads++;return r.fulfill({json:{available:true,log:`<script>unsafe()</script>\nChecking spectrum ${logReads}`,checked_at:new Date().toISOString()}});});
    await page.goto(`${base}/#/ci`);
    await page.getByRole('button',{name:'Run #42',exact:true}).click();
    await page.getByRole('button',{name:'Test spectra running'}).click();
    await page.waitForFunction(()=>document.querySelector('.ig-log')?.textContent.includes('Checking spectrum'));
    assert.equal(await page.locator('.ig-log script').count(),0);
    assert.equal(await page.locator('[data-ci-action]').count(),0);
    const search=page.getByRole('searchbox',{name:'Search runs'});await search.fill('spectra');await search.focus();
    const before=logReads;authError=true;
    await page.waitForFunction(()=>document.body.innerText.includes('Woodpecker authentication failed'),{},{timeout:12000});
    assert.ok(reads>=2);assert.ok(logReads>before);
    assert.equal(await search.inputValue(),'spectra');assert.equal(await search.evaluate(e=>e===document.activeElement),true);
    assert.equal(await page.locator('[data-step="2"]').getAttribute('aria-pressed'),'true');
    assert.match(await bodyText(page),/Showing last received records/);
    await page.getByRole('tab',{name:/^Failed/}).click();
    await page.getByRole('button',{name:'Run #41',exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Run #42',exact:true}).count(),0);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    assert.deepEqual(errors,[]);
  },{BURST:'0'});
});

test('Reviews groups decisions and preserves plan inspection with safe upstream links',async()=>{
  await withApp(async({page,base})=>{
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    let reads=0,detailReads=0;
    const reviews=[
      {review_id:'plan-1',title:'Improve parser',kind:'plan',status:'awaiting_review',project_id:'spectra',repository:'lab/api',updated_at:'2026-10-20T12:00:00Z',review_url:'https://review.example/one',mission_id:'pi:mission'},
      {review_id:'code-1',title:'Revise decoder',kind:'code',status:'changes_requested',project_id:'spectra',repository:'lab/web',feedback:'Add empty-input validation',review_url:'javascript:alert(1)'},
      {review_id:'old-1',title:'Approved parser',kind:'plan',status:'approved',repository:'lab/api'},
    ];
    await page.route('**/api/v1/reviews',r=>{reads++;return r.fulfill({json:{available:true,reviews,checked_at:new Date().toISOString()}});});
    await page.route('**/api/v1/reviews/plan-1',r=>{detailReads++;return r.fulfill({json:{...reviews[0],plan:`## Plan\nRead <script>unsafe()</script> safely. Detail ${detailReads}`}});});
    await page.goto(`${base}/#/reviews`);
    await page.getByText('Improve parser',{exact:true}).waitFor();
    const external=page.getByRole('link',{name:'Open in Plannotator'});
    assert.equal(await external.getAttribute('href'),'https://review.example/one');
    assert.equal(await external.getAttribute('rel'),'noopener noreferrer');
    assert.equal(detailReads,0);
    await page.getByText('Plan and feedback',{exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.ig-log')?.textContent.includes('Detail 1'));
    const before=reads;
    await page.waitForTimeout(5500);
    assert.ok(reads>before);assert.equal(await page.locator('details[data-review-details]').evaluate(e=>e.open),true);
    assert.equal(await page.locator('.ig-log script').count(),0);assert.equal(detailReads,1);
    reviews[0].updated_at='2026-10-20T12:01:00Z';
    await page.waitForFunction(()=>document.querySelector('.ig-log')?.textContent.includes('Detail 2'),{},{timeout:12000});
    assert.equal(detailReads,2);
    await page.getByRole('tab',{name:/^Changes requested/}).click();
    await page.getByText('Revise decoder',{exact:true}).waitFor();
    assert.equal(await page.getByRole('link',{name:'Open in Plannotator'}).count(),0);
    await page.getByRole('tab',{name:/^Finished/}).click();
    await page.getByText('Approved parser',{exact:true}).waitFor();
    await page.getByRole('searchbox',{name:'Search reviews'}).fill('missing project');
    assert.match(await bodyText(page),/No reviews match this view/);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    assert.deepEqual(errors,[]);
  },{BURST:'0'});
});

test('failed CI run sends debugging context only to an explicitly chosen active Pi session',async()=>{
  await withApp(async({page,base})=>{
    const posts=[];
    const failed={provider_repo_id:'7',repository:'lab/spectra',run_number:42,state:'failure'};
    await page.route('**/api/v1/ci',r=>r.fulfill({json:{available:true,status:'connected',pipelines:{'lab/spectra':[failed]}}}));
    await page.route('**/api/v1/ci/pipeline?*',r=>r.fulfill({json:{pipeline:{repository:'7',number:42,status:'failure',commit:'a'.repeat(40),tasks:[{id:1,name:'Tests',state:'failure'}]}}}));
    await page.route('**/api/v1/sessions',r=>r.fulfill({json:{sessions:[{session_id:'chosen',display_name:'Amber Finch',agent_role:'pi',status:'active',title:'Parser agent',host_id:'beast',terminal_id:'term_1',herdr_session:'default',agent_session:{agent:'pi'}},{session_id:'closed',agent_role:'pi',status:'closed'},{session_id:'other',agent_role:'codex',status:'active'}]}}));
    await page.route('**/api/v1/ci/debug',r=>{posts.push(r.request().postDataJSON());if(posts.length===1)return r.abort('failed');return r.fulfill({json:{ok:true,status:'delivered'}});});
    await page.addInitScript(()=>{
      Object.defineProperty(crypto,'randomUUID',{value:undefined,configurable:true});
      const original=EventTarget.prototype.addEventListener;
      window.integrationListeners=[];
      EventTarget.prototype.addEventListener=function(type,callback,options){
        if(this.id==='view' && ['click','change','toggle'].includes(type))window.integrationListeners.push({type,signal:options?.signal});
        return original.call(this,type,callback,options);
      };
    });
    await page.goto(`${base}/#/ci?tab=failed`);
    await page.getByRole('button',{name:'Run #42',exact:true}).waitFor();
    await page.evaluate(()=>{window.location.hash='/reviews';});
    await page.getByRole('heading',{name:'Reviews',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>window.integrationListeners.slice(0,3).every(l=>l.signal?.aborted)),true);
    await page.evaluate(()=>{window.location.hash='/ci?tab=failed';});
    await page.getByRole('button',{name:'Run #42',exact:true}).click();
    await page.getByText('Debug with Pi',{exact:true}).click();
    const select=page.getByRole('combobox',{name:'Target Pi agent'});
    await select.waitFor();assert.equal(await select.locator('option').count(),2);assert.match(await select.innerText(),/Amber Finch/);
    assert.equal(await page.getByRole('button',{name:'Send to Pi',exact:true}).isDisabled(),true);
    assert.equal(posts.length,0);
    await select.selectOption('chosen');
    await page.getByRole('button',{name:'Send to Pi',exact:true}).click();
    await page.getByText('Delivery unconfirmed; retry checks the same operation.',{exact:true}).waitFor();
    assert.equal(posts.length,1);
    await page.getByRole('button',{name:'Send to Pi',exact:true}).click();
    await page.getByText('Debugging context delivered to the selected Pi agent.',{exact:true}).waitFor();
    assert.equal(posts.length,2);assert.equal(posts[1].operation_id,posts[0].operation_id);assert.equal(posts[0].session_id,'chosen');assert.equal(posts[0].repository,'7');
    assert.equal(posts[0].number,42);assert.match(posts[0].operation_id,/^[0-9a-f-]{36}$/);
    assert.match(await page.locator('.ig-inspector').innerText(),/Run #42/);
  },{BURST:'0'});
});

test('hosts page lists the fleet version matrix and flags the host that differs', async () => {
  await withApp(async ({ page, base }) => {
    await page.goto(`${base}/#/hosts`);
    await page.waitForSelector('.hd-fleet table');
    const text = await bodyText(page);
    assert.ok(/Fleet versions/.test(text), text);
    const drifted = await page.$$eval('.hd-fleet__cell--drifted', (cells) => cells.map((c) => c.innerText.trim()));
    assert.strictEqual(drifted.length, 1, drifted.join(','));
    assert.ok(drifted[0].startsWith('1.2.0'), drifted[0]);
    await page.goto(`${base}/#/hosts?drift=1`);
    await page.waitForSelector('.hd-fleet table');
    assert.ok(!/superpowers/.test(await bodyText(page)), 'in-sync rows hide under the drift filter');
  });
});
