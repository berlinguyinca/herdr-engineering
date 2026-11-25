// Collaboration sign-in/out, blocked storage, listener hygiene and the nav badge label, in real Chromium against the
// static packages/ server (tests/playwright/helpers/fixture.mjs); the collab API is answered by page.route.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withPage } from './helpers/fixture.mjs';

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
const jwt = () => `${b64({ alg: 'none' })}.${b64({ exp: Math.floor(Date.now() / 1000) + 3600 })}.x`;

async function collabApi(page, base, { operator }) {
  await page.route('**/api/v1/collab/**', async (route) => {
    const where = new URL(route.request().url()).pathname.replace('/api/v1/collab', '');
    let body = {};
    if (where === '/config') body = { client_id: 'cid', hosted_ui_domain: `${base}/idp`, public_url: base, ...(operator ? { operator: { user_id: 'owner', email: 'o@example.test' } } : {}) };
    else if (where === '/me') body = { user_id: 'u1', email: 'u@example.test', status: 'approved' };
    else if (where === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
    else if (where === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
    else if (where === '/rooms/room-one/state') body = { changed: false, seq: 1, next_cursor: 1, agents: [], people: [], tasks: [], events: [] };
    await route.fulfill({ json: body });
  });
}

test('blocked site storage does not stop the collaboration module from loading or rendering', async () => {
  await withPage(async (page, base) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.addInitScript(() => {
      Object.defineProperty(window, 'sessionStorage', { configurable: true, get() { throw new DOMException('The operation is insecure.', 'SecurityError'); } });
    });
    await collabApi(page, base, { operator: true });
    await page.goto(`${base}/__blank`);
    await page.evaluate(() => { document.body.innerHTML = '<div id="host"></div>'; });
    await page.evaluate(async () => {
      const { mountCollab } = await import('/collab-view.js');
      window.collab = mountCollab(document.querySelector('#host'));
    });
    await page.locator('[data-room="room-one"]').waitFor();
    await page.evaluate(() => window.collab.dispose());
    assert.deepEqual(errors, []);
  });
});

test('Sign out also ends the Hosted-UI session', async () => {
  await withPage(async (page, base) => {
    await collabApi(page, base, { operator: false });
    const logouts = [];
    await page.route('**/idp/logout**', (route) => { logouts.push(new URL(route.request().url())); return route.fulfill({ contentType: 'text/html', body: '<p>signed out</p>' }); });
    await page.goto(`${base}/__blank`);
    await page.evaluate((t) => {
      sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: t, id: t }));
      document.body.innerHTML = '<div id="host" data-standalone="1"></div>';
    }, jwt());
    await page.evaluate(async () => {
      const { mountCollab } = await import('/collab-view.js');
      window.collab = mountCollab(document.querySelector('#host'));
    });
    await page.locator('#hcn-out').click();
    await page.getByText('signed out').waitFor();
    assert.equal(logouts.length, 1);
    assert.equal(logouts[0].searchParams.get('client_id'), 'cid');
    assert.equal(logouts[0].searchParams.get('logout_uri'), base);
    assert.equal(await page.evaluate(() => sessionStorage.getItem('hcn.tokens')), null);
  });
});

test('opening rooms repeatedly leaves one focusout listener on the mount root, not one per visit', async () => {
  await withPage(async (page, base) => {
    await collabApi(page, base, { operator: true });
    await page.goto(`${base}/__blank`);
    await page.evaluate(() => { document.body.innerHTML = '<div id="host"></div>'; });
    await page.evaluate(async () => {
      const { mountCollab } = await import('/collab-view.js');
      window.collab = mountCollab(document.querySelector('#host'));
    });
    const cdp = await page.context().newCDPSession(page);
    const focusouts = async () => {
      const { result } = await cdp.send('Runtime.evaluate', { expression: "document.querySelector('#host')" });
      const { listeners } = await cdp.send('DOMDebugger.getEventListeners', { objectId: result.objectId });
      return listeners.filter((l) => l.type === 'focusout').length;
    };
    for (let i = 0; i < 4; i += 1) {
      await page.locator('[data-room="room-one"]').click();
      await page.getByRole('combobox', { name: 'Message' }).waitFor();   // the room is fully wired once its composer is up
      await page.locator('.cx-rtab--new').click();
      await page.locator('[data-room="room-one"]').waitFor();
    }
    await page.locator('[data-room="room-one"]').click();
    await page.getByRole('combobox', { name: 'Message' }).waitFor();   // the room is fully wired once its composer is up
    assert.equal(await focusouts(), 1);
    await page.locator('.cx-rtab--new').click();
    await page.locator('[data-room="room-one"]').waitFor();
    assert.equal(await focusouts(), 0);
    await page.evaluate(() => window.collab.dispose());
  });
});

test('the nav badge is announced with its critical count, not "true"', async () => {
  await withPage(async (page, base) => {
    await page.goto(`${base}/__blank`);
    await page.evaluate(async () => {
      const { defineComponents } = await import('/herdr-web-components/index.js');
      defineComponents();
      document.body.innerHTML = '<herdr-app-shell id="s" routes=\'[{"path":"/attention","label":"Attention"}]\'></herdr-app-shell>';
    });
    const label = () => page.locator('herdr-app-shell .hd-shell__badge').getAttribute('aria-label');
    await page.evaluate(() => document.getElementById('s').setBadge('/attention', { count: 3, critical: 2 }));
    assert.equal(await label(), '3 open, 2 critical');
    assert.equal(await page.locator('herdr-app-shell .hd-shell__badge').getAttribute('data-tone'), 'critical');
    await page.evaluate(() => document.getElementById('s').setBadge('/attention', { count: 3, critical: 0 }));
    assert.equal(await label(), '3 open');
    assert.equal(await page.locator('herdr-app-shell .hd-shell__badge').getAttribute('data-tone'), 'normal');
  });
});

test('leaving a room whose wiring failed part-way still runs the whole teardown (no unbindHelpers TDZ error)', async () => {
  await withPage(async (page, base) => {
    await collabApi(page, base, { operator: true });
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.goto(`${base}/__blank`);
    await page.evaluate(() => { document.body.innerHTML = '<div id="host"></div>'; });
    await page.evaluate(async () => {
      const { mountCollab } = await import('/collab-view.js');
      window.collab = mountCollab(document.querySelector('#host'));
    });
    await page.locator('[data-room="room-one"]').waitFor();
    // The browser refuses the room's focusout listener once: showRoom stops after it has installed its teardown but before the
    // helper handlers are wired. Leaving the room must still run the whole teardown.
    await page.evaluate(() => {
      const host = document.querySelector('#host');
      const add = host.addEventListener.bind(host);
      host.addEventListener = (type, ...rest) => {
        if (type === 'focusout') { host.addEventListener = add; throw new Error('injected: focusout refused'); }
        return add(type, ...rest);
      };
    });
    await page.locator('[data-room="room-one"]').click();
    await page.locator('.cx-rtab--new').waitFor();
    await page.locator('.cx-rtab--new').click();
    await page.locator('[data-room="room-one"]').waitFor();
    assert.deepEqual(errors.filter((m) => !m.includes('injected')), []);
    await page.evaluate(() => window.collab.dispose());
  });
});
