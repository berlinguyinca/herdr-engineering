// The assistant's Connectors tab in a real browser: the Apps cards, the one-time Google client form, the blocked-calendars
// checklist, Drafts and Reminders. It mounts the real view (packages/personal-assistant-view.js) against an in-page fake of the
// /api/v1 routes (no control-plane process), the same way assistant-proposals.test.mjs does. The fake keeps the OAuth client secret
// on its side only, like the server; the tests then look for that string in everything the page rendered and received.
// Run: npm run test:ui
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { extname } from 'node:path';
import { chromium } from 'playwright';

const SECRET = 'GOCSPX-fixture-secret-9f3a7c';
const GOOGLE_URL = 'https://accounts.google.com/o/oauth2/v2/auth?client_id=fixture&state=s1';
const TYPES = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' };
const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Assistant connectors</title>
<link rel="stylesheet" href="/herdr-design-system/theme.css"><link rel="stylesheet" href="/herdr-design-system/base.css">
<link rel="stylesheet" href="/herdr-web-components/components.css"><link rel="stylesheet" href="/personal-assistant.css"></head>
<body><main id="stage"></main></body></html>`;

async function withPage(scenario, fn, { width = 1100, path = '/', colorScheme = 'light' } = {}) {
  const server = createServer(async (req, res) => {
    const p = req.url.split('?')[0];
    if (p === '/') { res.setHeader('Content-Type', 'text/html'); res.end(PAGE); return; }
    try {
      res.setHeader('Content-Type', TYPES[extname(p)] || 'application/octet-stream');
      res.end(await readFile(new URL(`../../packages${p}`, import.meta.url)));
    } catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: existsSync(fallback) ? fallback : undefined });
    const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme });
    const page = await ctx.newPage();
    // Google is never contacted: the sign-in address is answered here so the navigation can be seen.
    await page.route('https://accounts.google.com/**', (route) => route.fulfill({ contentType: 'text/html', body: '<title>Google sign-in stub</title><p>stub</p>' }));
    const recorded = [];       // calls seen by the fake, kept outside the page so they survive the navigation to Google
    await page.exposeFunction('__record', (c) => { recorded.push(c); });
    await page.goto(`http://127.0.0.1:${server.address().port}${path}`);
    await page.evaluate(FAKE_BACKEND, { scenario, secret: SECRET, googleUrl: GOOGLE_URL });
    await fn(page, recorded);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

// Runs in the page: the fake API, the real view; exposes window.__calls, window.__db, window.__responses (every body the page received).
const FAKE_BACKEND = async ({ scenario, secret, googleUrl }) => {
  const { HerdrClient } = await import('/herdr-web-client/client.js');
  const { defineComponents } = await import('/herdr-web-components/index.js');
  const { mountPersonalAssistant } = await import('/personal-assistant-view.js');
  defineComponents();
  const now = '2026-10-29T10:00:00Z';
  const db = {
    ready: scenario !== 'notready', clientSaved: scenario !== 'unconfigured' && scenario !== 'notready', secret: '',
    connection: { connected: scenario === 'connected' || scenario === 'needs', account: 'ann@example.com', status: scenario === 'needs' ? 'needs_attention' : 'connected' },
    blocked: ['secret@example.com'],
    calendars: [{ id: 'ann@example.com', summary: 'Ann', primary: true, blocked: false }, { id: 'team@example.com', summary: 'Team', primary: false, blocked: false },
      { id: 'secret@example.com', summary: 'Private', primary: false, blocked: true }],
    drafts: [{ id: 'r-1', to: 'dan@example.com', subject: 'Lunch on Friday', snippet: 'Are you free at noon?', gmail_url: 'https://mail.google.com/mail/u/0/#drafts/m1' },
      { id: 'r-2', to: '<img src=x onerror="window.pwned=1">', subject: 'Hostile', snippet: 'x', gmail_url: 'javascript:window.pwned=2' }],
    reminders: [{ id: 'rem_1', text: 'Call the dentist', fire_at: '2026-10-31T09:00:00-07:00', state: 'pending' }, { id: 'rem_2', text: 'Sent already', fire_at: '2026-10-28T09:00:00-07:00', state: 'fired' }],
  };
  const calls = [], responses = [];
  const providers = () => ['gmail', 'calendar'].map((id) => ({ id, name: id === 'gmail' ? 'Gmail' : 'Google Calendar', kind: 'oauth', provider: 'google',
    connected: db.connection.connected, account: db.connection.connected ? db.connection.account : '', status: db.connection.connected ? db.connection.status : 'not_connected',
    scopes: [], client_configured: db.clientSaved })).concat([{ id: 'github', name: 'GitHub', kind: 'status', provider: 'github', connected: true, account: 'berlinguyinca', status: 'connected', scopes: [], client_configured: false }]);
  const reply = (status, json) => { const text = JSON.stringify(json); responses.push(text); return { ok: status < 400, status, headers: { get: () => null }, text: async () => text }; };
  const fetchImpl = async (url, opts = {}) => {
    const method = opts.method || 'GET', path = url.split('?')[0], body = opts.body ? JSON.parse(opts.body) : {};
    calls.push({ method, path, body });
    window.__record({ method, path, body });
    let m;
    if (method === 'GET' && path === '/api/v1/assistants') return reply(200, { assistants: [{ assistant_id: 'asst_1', name: 'Personal Assistant', persona: 'helpful', status: 'ready', instructions: '', model_policy: 'auto', enabled: true, created_at: now, updated_at: now }] });
    if (method === 'GET' && path === '/api/v1/assistant-approvals') return reply(200, { approvals: [] });
    if (method === 'GET' && path === '/api/v1/assistant-connectors') return reply(200, { connectors: [{ connector_id: 'conn_1', name: 'Weather', kind: 'http', base_url: 'https://w.example.com', auth_present: false, enabled: true }] });
    if (method === 'GET' && /\/threads$/.test(path)) return reply(200, { threads: [] });
    if (method === 'GET' && path === '/api/v1/assistant-apps') {
      return reply(200, db.ready ? { ready: true, redirect_uri: 'https://cp.example/api/v1/assistant-apps/google/callback', providers: providers(), blocked_calendars: db.blocked }
        : { ready: false, redirect_uri: '', providers: [], blocked_calendars: [] });
    }
    if (method === 'PUT' && path === '/api/v1/assistant-apps/google/client') { db.secret = body.client_secret; db.clientSaved = true; return reply(200, { ok: true }); }
    if (method === 'POST' && path === '/api/v1/assistant-apps/google/connect') return db.clientSaved ? reply(200, { url: db.connectUrl || googleUrl }) : reply(409, { error: 'no_client' });
    if (method === 'DELETE' && path === '/api/v1/assistant-apps/google') { db.connection.connected = false; return reply(200, { ok: true }); }
    if (method === 'GET' && path === '/api/v1/assistant-apps/google/calendars') return reply(200, { calendars: db.calendars });
    if (method === 'PUT' && path === '/api/v1/assistant-apps/google/blocked-calendars') { db.blocked = body.ids; return reply(200, { ids: body.ids }); }
    if (method === 'GET' && path === '/api/v1/assistant-apps/google/drafts') return reply(200, { drafts: db.drafts });
    if (method === 'DELETE' && (m = /^\/api\/v1\/assistant-apps\/google\/drafts\/([^/]+)$/.exec(path))) { db.drafts = db.drafts.filter((d) => d.id !== m[1]); return reply(200, { ok: true, id: m[1] }); }
    if (method === 'GET' && path === '/api/v1/assistant-reminders') return reply(200, { reminders: db.reminders, timezone: 'America/Los_Angeles' });
    if (method === 'DELETE' && (m = /^\/api\/v1\/assistant-reminders\/([^/]+)$/.exec(path))) { db.reminders = db.reminders.map((r) => (r.id === m[1] ? { ...r, state: 'cancelled' } : r)); return reply(200, { ok: true, id: m[1] }); }
    return reply(404, { error: 'not found' });
  };
  window.__calls = calls; window.__db = db; window.__responses = responses;
  mountPersonalAssistant(document.getElementById('stage'), new HerdrClient('', { fetchImpl }));
  await new Promise((r) => setTimeout(r, 50));
  if (document.querySelector('[data-config]') && !document.querySelector('#pa-panel')) document.querySelector('[data-config]').click();
  const tab = [...document.querySelectorAll('[data-tab]')].find((b) => b.dataset.tab === 'Connectors');
  if (tab && !tab.classList.contains('pa-tab--active')) tab.click();
};

const app = (page, name) => page.locator(`[data-app="${name}"]`);
const calls = (page) => page.evaluate(() => window.__calls);

test('not set up: the cards say so and offer no form and no Enable', () => withPage('notready', async (page) => {
  await page.locator('[data-app-notready]').waitFor();
  assert.match(await page.locator('[data-app-notready]').textContent(), /Not set up — ask the operator to create the encryption key/);
  assert.equal(await page.locator('[data-client-form]').count(), 0);
  assert.equal(await page.getByRole('button', { name: /^Enable/ }).count(), 0);
  assert.equal(await page.getByRole('button', { name: 'Save client' }).count(), 0);
}));

test('no client saved yet: cards say not set up, Enable is disabled, the form is open', () => withPage('unconfigured', async (page) => {
  await page.locator('[data-client-form]').waitFor();
  assert.match(await app(page, 'gmail').locator('[data-app-state]').textContent(), /Not set up/i);
  assert.equal(await page.getByRole('button', { name: 'Enable Gmail' }).isDisabled(), true);
  assert.equal(await page.locator('[data-client-secret]').getAttribute('type'), 'password');
  assert.equal(await page.locator('[data-client-secret]').inputValue(), '');
  assert.ok(await page.locator('[data-redirect-uri]').textContent());
}));

test('saving the Google client shows Client saved and the redirect address, and the secret never comes back', () => withPage('unconfigured', async (page) => {
  await page.locator('[data-client-id]').fill('cid.apps.googleusercontent.com');
  await page.locator('[data-client-secret]').fill(SECRET);
  await page.getByRole('button', { name: 'Save client' }).click();
  await page.locator('[data-client-saved]').waitFor();
  assert.equal(await page.locator('[data-client-saved]').textContent(), 'Client saved');
  assert.match(await page.locator('[data-redirect-uri]').textContent(), /\/api\/v1\/assistant-apps\/google\/callback$/);
  assert.ok(await page.getByRole('button', { name: 'Copy redirect address' }).isVisible());
  const put = (await calls(page)).find((c) => c.method === 'PUT' && c.path.endsWith('/google/client'));
  assert.deepEqual(put.body, { client_id: 'cid.apps.googleusercontent.com', client_secret: SECRET });
  assert.equal(await page.getByRole('button', { name: 'Enable Gmail' }).isEnabled(), true);
  // the secret is nowhere: not in the markup, not in any field, not in any response the page received
  const seen = await page.evaluate(() => ({ html: document.documentElement.outerHTML, values: [...document.querySelectorAll('input,textarea')].map((i) => i.value).join('|'),
    responses: window.__responses.join('\n'), text: document.body.innerText }));
  for (const [where, text] of Object.entries(seen)) assert.ok(!text.includes(SECRET), `the client secret appears in ${where}`);
}));

test('a not-connected card offers Enable; Enable POSTs connect and navigates to the returned Google URL', () => withPage('disconnected', async (page) => {
  assert.match(await app(page, 'gmail').locator('[data-app-state]').textContent(), /Not connected/);
  await page.getByRole('button', { name: 'Enable Gmail' }).click();
  await page.waitForURL(GOOGLE_URL);
}));

test('Enable (on either card) sends POST connect with a JSON body before navigating', () => withPage('disconnected', async (page, recorded) => {
  await page.getByRole('button', { name: 'Enable Google Calendar' }).click();
  await page.waitForURL(GOOGLE_URL);
  const c = recorded.filter((x) => x.path.endsWith('/google/connect'));
  assert.equal(c.length, 1);
  assert.equal(c[0].method, 'POST'); assert.deepEqual(c[0].body, {});
}));

test('Enable refuses any sign-in address that is not https://accounts.google.com and does not navigate', () => withPage('disconnected', async (page) => {
  const before = page.url();
  for (const bad of ['https://evil.example/', 'http://accounts.google.com/x', 'https://accounts.google.com.evil.example/', 'javascript:window.pwned=1', '//accounts.google.com/x', 'not a url']) {
    await page.evaluate((u) => { window.__db.connectUrl = u; }, bad);
    await page.getByRole('button', { name: 'Enable Gmail' }).click();
    await page.locator('[data-apps-flash] [role="alert"]').filter({ hasText: 'sign-in address was not valid' }).waitFor();
    await page.waitForTimeout(100);
    assert.equal(page.url(), before, `navigated for ${bad}`);
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    await page.evaluate(() => { document.querySelector('[data-apps-flash]').innerHTML = ''; });
  }
}));

test('connected: the card shows the account and Disconnect; the blocked list is loaded', () => withPage('connected', async (page) => {
  await app(page, 'gmail').getByText('Connected as ann@example.com').waitFor();
  await app(page, 'calendar').getByText('Connected as ann@example.com').waitFor();
  assert.ok(await page.getByRole('button', { name: 'Disconnect Gmail' }).isVisible());
  assert.equal(await page.getByRole('button', { name: /^Enable/ }).count(), 0);
  assert.match(await app(page, 'github').textContent(), /berlinguyinca/);
  assert.equal(await app(page, 'github').getByRole('button').count(), 0);                 // GitHub is status only
  assert.ok(await page.getByText('Slack, Notion …').isVisible());
}));

test('needs attention: the card says so and offers Sign in again', () => withPage('needs', async (page) => {
  await app(page, 'gmail').getByText(/Needs attention/).waitFor();
  assert.ok(await page.getByRole('button', { name: 'Sign in again for Gmail' }).isVisible());
  assert.equal(await page.locator('[data-blocked-form]').count(), 0);                      // no Google calls for a connection that needs attention
  assert.equal((await calls(page)).some((c) => c.path.endsWith('/calendars') || c.path.endsWith('/drafts')), false);
  await page.getByRole('button', { name: 'Sign in again for Gmail' }).click();
  await page.waitForURL(GOOGLE_URL);
}));

test('Disconnect calls DELETE and the cards turn to Not connected', () => withPage('connected', async (page) => {
  await page.getByRole('button', { name: 'Disconnect Gmail' }).click();
  await app(page, 'gmail').getByText('Not connected').waitFor();
  assert.ok((await calls(page)).some((c) => c.method === 'DELETE' && c.path === '/api/v1/assistant-apps/google'));
  assert.equal(await page.locator('#pa-drafts').isHidden(), true);
}));

test('blocked calendars: the checklist shows blocked ones checked and Save sends the chosen ids', () => withPage('connected', async (page) => {
  await page.locator('[data-blocked-form]').waitFor();
  const boxes = page.locator('[data-blocked-id]');
  assert.equal(await boxes.count(), 3);
  assert.equal(await page.locator('[data-blocked-id="secret@example.com"]').isChecked(), true);
  assert.equal(await page.locator('[data-blocked-id="team@example.com"]').isChecked(), false);
  await page.getByRole('checkbox', { name: /Team/ }).check();
  await page.locator('[data-blocked-id="secret@example.com"]').uncheck();
  await page.getByRole('button', { name: 'Save blocked calendars' }).click();
  await page.getByText(/^Saved: 1 calendar hidden/).waitFor();
  const put = (await calls(page)).find((c) => c.method === 'PUT' && c.path.endsWith('/blocked-calendars'));
  assert.deepEqual(put.body, { ids: ['team@example.com'] });
}));

test('drafts: listed with Open in Gmail and Discard; Send is visibly disabled; hostile text and links are inert', () => withPage('connected', async (page) => {
  await page.locator('[data-draft="r-1"]').waitFor();
  assert.ok(await page.getByText('Lunch on Friday').isVisible());
  assert.ok(await page.getByText('Are you free at noon?').isVisible());
  const open = page.getByRole('link', { name: 'Open draft Lunch on Friday in Gmail' });
  assert.equal(await open.getAttribute('href'), 'https://mail.google.com/mail/u/0/#drafts/m1');
  assert.equal(await open.getAttribute('target'), '_blank');
  assert.match(await open.getAttribute('rel'), /noopener/);
  const send = page.getByRole('button', { name: 'Send (coming soon)' }).first();
  assert.equal(await send.isDisabled(), true);
  assert.equal(await page.locator('#pa-drafts').getByRole('button', { name: /^Send/ }).count(), 2);
  assert.equal(await page.locator('#pa-drafts').getByRole('button', { name: /^Send/ }).evaluateAll((bs) => bs.every((b) => b.disabled)), true);
  assert.equal(await page.evaluate(() => window.pwned), undefined);
  assert.equal(await page.locator('#pa-drafts img').count(), 0);
  assert.equal(await page.locator('[data-draft="r-2"] a').count(), 0);                      // a javascript: address gets no link
  const dialogs = [];
  page.once('dialog', (d) => { dialogs.push(d.message()); d.accept(); });
  await page.getByRole('button', { name: 'Discard draft Lunch on Friday' }).click();
  await page.locator('[data-draft="r-1"]').waitFor({ state: 'detached' });
  assert.ok((await calls(page)).some((c) => c.method === 'DELETE' && c.path === '/api/v1/assistant-apps/google/drafts/r-1'));
  assert.equal(dialogs.length, 1);
}));

test('drafts: dismissing the confirmation discards nothing', () => withPage('connected', async (page) => {
  await page.locator('[data-draft="r-1"]').waitFor();
  page.once('dialog', (d) => d.dismiss());
  await page.getByRole('button', { name: 'Discard draft Lunch on Friday' }).click();
  assert.equal(await page.locator('[data-draft="r-1"]').count(), 1);
  assert.equal((await calls(page)).some((c) => c.method === 'DELETE'), false);
}));

test('reminders: pending ones can be cancelled', () => withPage('connected', async (page) => {
  await page.locator('[data-reminder="rem_1"]').waitFor();
  assert.ok(await page.getByText('Call the dentist').isVisible());
  assert.equal(await page.getByRole('button', { name: /^Cancel reminder/ }).count(), 1);   // the fired one has no Cancel
  await page.getByRole('button', { name: 'Cancel reminder Call the dentist' }).click();
  await page.getByText('Reminder cancelled.').waitFor();
  assert.ok((await calls(page)).some((c) => c.method === 'DELETE' && c.path === '/api/v1/assistant-reminders/rem_1'));
  assert.equal(await page.locator('[data-reminder="rem_1"]').count(), 0);
}));

test('returning from Google: ?connected= and ?error= show fixed text and are removed from the address', async () => {
  await withPage('connected', async (page) => {
    await page.getByText('Google connected.').waitFor();
    assert.equal(new URL(page.url()).search, '');
  }, { path: '/?connected=google' });
  await withPage('disconnected', async (page) => {
    await page.getByText('Google sign-in was cancelled, so nothing was connected.').waitFor();
    assert.equal(await page.locator('[data-apps-flash] [role="alert"]').count(), 1);
    assert.equal(new URL(page.url()).search, '');
  }, { path: '/?error=denied' });
  await withPage('disconnected', async (page) => {                                           // an unknown reason never reaches the page as text
    await page.getByText('Google sign-in did not finish. Choose Enable to try again.').waitFor();
    assert.equal(await page.getByText('<b>x</b>').count(), 0);
    assert.equal(new URL(page.url()).search, '');
  }, { path: '/?error=%3Cb%3Ex%3C%2Fb%3E' });
  for (const name of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {         // names from Object.prototype are not reasons
    await withPage('disconnected', async (page) => {
      await page.getByText('Google sign-in did not finish. Choose Enable to try again.').waitFor();
      assert.equal(new URL(page.url()).search, '');
    }, { path: `/?error=${name}` });
  }
  await withPage('connected', async (page) => {                                              // the dashboard's own address form: #/route?query
    await page.getByText('Google connected.').waitFor();
    assert.equal(new URL(page.url()).hash, '#/personal-assistant');
  }, { path: '/#/personal-assistant?connected=google' });
});

test('every control has an accessible name, nothing overflows at 375px, in light and dark', async () => {
  for (const colorScheme of ['light', 'dark']) {
    await withPage('connected', async (page) => {
      await page.locator('[data-draft="r-1"]').waitFor();
      await page.locator('[data-reminder="rem_1"]').waitFor();
      const nameless = await page.evaluate(() => [...document.querySelectorAll('#pa-connectors button, #pa-connectors a, #pa-connectors input')]
        .filter((e) => !(e.getAttribute('aria-label') || e.textContent || (e.labels && e.labels.length) || '').toString().trim()).map((e) => e.outerHTML.slice(0, 120)));
      assert.deepEqual(nameless, []);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 0, `overflows by ${overflow}px`);
    }, { width: 375, colorScheme });
  }
});
