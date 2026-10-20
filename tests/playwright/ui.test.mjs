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
  ['/missions', 'Alpha'],
  ['/herd', 'Sessions'],
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
    // The list must render the pi mission card before we can click it.
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
    const streamText = await page.locator('#mission-stream').innerText();
    assert.ok(streamText.includes('MissionStageChanged'),
      `stage event missing from stream:\n${streamText.slice(0, 400)}`);
  }, { BURST: '0' });
});

test('mission detail renders the timeline', async () => {
  await withApp(async ({ page, base }) => {
    // Legacy plain-colon bookmarks remain valid; the app encodes the API path.
    await page.goto(`${base}/#/missions/pi:MSN-PI1`);
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
    await page.waitForSelector('.hd-ins__tab');
    await page.click('[data-view=reasoning]');
    await page.waitForFunction(() => /not report|Reasoning/.test(document.querySelector('#ins-root')?.innerText || ''));
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
    await page.waitForSelector('.hd-ins__tab');
    await page.waitForFunction(() => document.querySelector('#ins-root')?.innerText.includes('make the parser faster'));
    await page.click('[data-view=tools]');
    await page.click('[data-view=conversation]');
    await sleep(2500);
    const text = await page.locator('#ins-list').innerText();
    assert.ok(text.includes('make the parser faster') && !text.includes('make build'), text.slice(0, 300));
  }, { BURST: '0' });
});

test('mission report ages advance without events or notification requests', async () => {
  await withApp(async ({ page, base }) => {
    const writes = [];
    const requests = [];
    page.on('request', (req) => {
      requests.push(req.url());
      if (req.method() !== 'GET') writes.push(req.url());
    });
    await page.goto(`${base}/#/missions`);
    const activity = page.locator('herdr-mission-activity[source="pi"]').first();
    await activity.waitFor({ timeout: 10000 });
    // Let the initial SSE replay finish its existing trailing refresh.
    await page.waitForTimeout(600);
    await activity.evaluate((el) => el.setAttribute('updated-at', new Date(Date.now() - 20000).toISOString()));
    const before = await activity.innerText();
    const requestCount = requests.length;
    await page.waitForFunction((text) => document.querySelector('herdr-mission-activity[source="pi"]')?.innerText !== text, before);
    assert.equal(requests.length, requestCount, 'clock refreshes do not poll the backend');
    assert.match(await activity.innerText(), /Last reported change: 2\ds ago/);
    assert.match(await activity.innerText(), /Finished, crashed, or stalled updates only/);
    assert.doesNotMatch(await activity.innerText(), /stalled for|No activity/);
    await page.locator('herdr-status').first().evaluate((el) => el.setAttribute('stage', 'stalled'));
    assert.match(await page.locator('herdr-status').first().innerText(), /Stalled/);
    await page.locator('a[href="#/missions/pi%3AMSN-PI1"]').click();
    await page.locator('herdr-mission-activity[source="pi"]').waitFor();
    assert.deepEqual(writes, [], 'local clocks never emit notifications');
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

test('live Pi missions lead the list and details inspect workers without guessing session links', async () => {
  await withApp(async ({page, base}) => {
    let bulk = 0, detail = 0;
    await page.clock.install();
    await page.route('**/api/v1/mission-activity', r => { bulk++; return r.fulfill({json: {activities: [liveProjection()], server_time: new Date().toISOString()}}); });
    await page.route('**/api/v1/missions/pi%3AMSN-live/activity', r => { detail++; return r.fulfill({json: {activity: liveProjection()}}); });
    await page.goto(`${base}/#/missions`);
    await page.waitForSelector('[data-mission-key="pi:MSN-live"]');
    assert.match(await bodyText(page), /Live telemetry · stalled/);
    assert.match(await bodyText(page), /flash → metabolomics\/qwen3.8/);
    const headings = await page.locator('#view h2').allTextContents();
    assert.deepEqual(headings, ['Live Pi missions', 'Recorded missions']);
    await page.getByRole('link', {name: 'Live Pi mission', exact: true}).click();
    await page.waitForSelector('[data-worker="reviewer"]');
    await page.locator('[data-worker="reviewer"] summary').click();
    assert.match(await bodyText(page), /4 tests passed/);
    assert.equal(await page.evaluate(() => window.outputExecuted), undefined);
    assert.equal(await page.locator('#view a[href*="/herd/session/"]').count(), 0);
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
    await page.route('**/api/v1/mission-activity', r => { calls++; return r.fulfill({json: {activities: [liveProjection()], server_time: new Date().toISOString()}}); });
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
    const elapsed = page.locator('.ml-card [data-clock]').first();
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
    await page.evaluate(() => { window.livePaneBefore = document.querySelector('#live-mission-detail'); });
    const response = await fetch(`${base}/api/v1/ingest`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      source: 'pi', entity_type: 'mission', entity_id: 'MSN-PI1', event_type: 'MissionStopped', payload: {title: 'Pi mission', stage: 'complete'},
    })});
    assert.equal(response.status, 200);
    await page.waitForFunction(() => document.querySelector('#mission-history herdr-status')?.getAttribute('stage') === 'complete', null, {timeout: 12000});
    assert.equal(await page.evaluate(() => window.livePaneBefore === document.querySelector('#live-mission-detail')), true);
    assert.equal(await page.locator('[data-worker="reviewer"]').evaluate(el => el.open), true);
    assert.match(await page.locator('#mission-stream').innerText(), /complete|stopped/i);
    await page.waitForFunction(() => /complete|stopped/i.test(document.querySelector('#mission-timeline')?.innerText || ''), null, {timeout: 5000});
    assert.match(await page.locator('#mission-timeline').innerText(), /complete|stopped/i);
  }, {BURST: '0'});
});
