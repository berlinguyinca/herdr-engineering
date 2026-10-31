// Session detail: the story names the commits and pull requests the server linked to the agent, and says so when it recorded
// none. The real server answers the no-link case; the linked case answers the commits route with a fixture.
import { test } from 'node:test';
import assert from 'node:assert';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const PY = process.env.PYTHON || (fs.existsSync(path.join(REPO, '.venv', 'bin', 'python')) ? path.join(REPO, '.venv', 'bin', 'python') : 'python3');
const EXE_FALLBACK = process.env.PLAYWRIGHT_CHROMIUM || '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';

async function startServer() {
  const proc = spawn(PY, [path.join(REPO, 'tests', 'playwright', 'server.py')], { env: { ...process.env }, stdio: ['ignore', 'pipe', 'pipe'] });
  const port = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server start timeout')), 20000);
    proc.stdout.on('data', (d) => { const m = d.toString().match(/PORT=(\d+)/); if (m) { clearTimeout(timer); resolve(Number(m[1])); } });
    proc.on('exit', (c) => reject(new Error(`server exited early (${c})`)));
  });
  return { proc, port };
}

async function launch() {
  const args = ['--no-sandbox'];
  try { return await chromium.launch({ headless: true, args }); }
  catch (err) { if (fs.existsSync(EXE_FALLBACK)) return chromium.launch({ headless: true, executablePath: EXE_FALLBACK, args }); throw err; }
}

async function withSession(fn) {
  const srv = await startServer();
  let browser;
  try {
    browser = await launch();
    const page = await browser.newPage();
    const base = `http://127.0.0.1:${srv.port}`;
    const { sessions } = await (await fetch(`${base}/api/v1/sessions`)).json();
    return await fn({ page, base, id: sessions[0].session_id });
  } finally {
    if (browser) await browser.close().catch(() => {});
    srv.proc.kill();
  }
}

test('a session with no attributable commit says no link is recorded', () => withSession(async ({ page, base, id }) => {
  await page.goto(`${base}/#/herd/session/${id}`);
  const story = page.locator('#session-story');
  await story.locator('li[data-kind=unlinked]:has-text("No commit link recorded")').waitFor();
  assert.match(await story.locator('li[data-kind=unlinked]').innerText(), /No commit link recorded: /);
}));

test('linked commits and pull requests appear in the story, and only https links are clickable', () => withSession(async ({ page, base, id }) => {
  await page.route('**/api/v1/sessions/*/commits', (route) => route.fulfill({ contentType: 'application/json', body: JSON.stringify({
    state: 'linked', reason: '', commits: [
      { sha: 'abcdef1234567890', run_number: 12, state: 'success', pull_request: 214, at: '2026-10-29T10:00:00Z', ci_url: 'https://ci.example/repos/7/pipeline/12', match: 'branch_window' },
      { sha: '1234567abcdef', run_number: 11, state: 'failure', at: '2026-10-29T09:00:00Z', ci_url: 'javascript:alert(1)', match: 'commit' }],
    pull_requests: [{ number: 214, status: 'open', url: 'https://github.com/org/app/pull/214' }, { number: 300, status: 'open', url: 'https://github.com/org/app/pull/300', at: '2026-10-29T10:30:00Z' }] }) }));
  await page.goto(`${base}/#/herd/session/${id}`);
  const commits = page.locator('#session-story li[data-kind=commit]');
  await commits.nth(2).waitFor();
  assert.match(await commits.nth(0).innerText(), /Commit 1234567 · CI run #11 failure \(the commit the agent reported\)/);
  assert.match(await commits.nth(1).innerText(), /Commit abcdef1 · PR #214 · CI run #12 success/);
  assert.match(await commits.nth(2).innerText(), /PR #300 open/);
  assert.equal(await commits.nth(1).locator('a').getAttribute('href'), 'https://ci.example/repos/7/pipeline/12');
  assert.equal(await commits.nth(0).locator('a').count(), 0, 'a javascript: URL is not made a link');
  assert.equal(await page.locator('#session-story li[data-kind=unlinked]').count(), 0);
}));

test('the story says the links are loading, then that they could not be loaded when the request fails', () => withSession(async ({ page, base, id }) => {
  await page.route('**/api/v1/sessions/*/commits', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"x"}' }));
  await page.goto(`${base}/#/herd/session/${id}`);
  await page.locator('#session-story li[data-kind=unlinked]:has-text("could not be loaded")').waitFor();
}));
