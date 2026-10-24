import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

// Real shared view/client and browser; only the HTTP service is replaced. New
// lifecycle routes are explicit frontend contracts pending server HTTP wiring.
async function withCollaboration(surface, fn, role = 'owner') {
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<link rel="stylesheet" href="/collab.css"><div id="view"></div>'); return; }
    try { res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(new URL(`../../packages/${req.url.slice(1)}`, import.meta.url))); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  const calls = [], holds = new Map();
  const room = { room_id: 'r1', name: 'Lab', role, members: [
    { user_id: 'owner', email: 'owner@example.org', role: 'owner', file_upload_permission: true },
    { user_id: 'member', email: 'member@example.org', role: 'contributor', room_membership_status: 'approved', file_upload_permission: false },
    { user_id: 'pending', email: 'pending@example.org', role: 'viewer', room_membership_status: 'pending' },
  ] };
  let agents = [{ agent_id: 'attached', name: 'attached-bot', owner_user_id: 'member', harness_type: 'codex', actions: ['read'] }];
  const requests = [{ request_id: 'req1', name: 'bot', owner_email: 'member@example.org', actions: ['read'] },
    { request_id: 'req2', name: 'second-bot', owner_email: 'member@example.org', actions: ['read'] }];
  const mine = [{ agent_id: 'a1', name: 'bot', status: 'active', rooms: [], pending: [] }];
  let users = [{ user_id: 'member', email: 'member@example.org', status: 'approved' }, { user_id: 'pending', email: 'pending@example.org', status: 'pending' }];
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined) });
    const page = await browser.newPage();
    page.setDefaultTimeout(2500);
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.route('**/api/v1/collab/**', async route => {
      const request = route.request(), path = new URL(request.url()).pathname.replace('/api/v1/collab', '');
      const body = request.postDataJSON();
      calls.push({ path, method: request.method(), body });
      let result;
      if (holds.has(path)) result = await holds.get(path);
      else if (path === '/config') result = { client_id: 'web', hosted_ui_domain: origin, public_url: origin, ...(surface === 'dashboard' ? { operator: true } : {}) };
      else if (path === '/me') result = { user_id: 'owner', status: 'approved', admin: role === 'owner' };
      else if (path === '/admin/users') result = { users: request.url().includes('?status=pending') ? users.filter(u => u.status === 'pending') : users, can_manage: true };
      else if (path === '/admin/users/member/delete') result = { status: 'complete' };
      else if (path === '/admin/users/pending/approve') { users.find(u => u.user_id === 'pending').status = 'approved'; result = {}; }
      else if (path === '/rooms') result = { rooms: [room] };
      else if (path === '/rooms/r1') result = room;
      else if (path === '/rooms/r1/invites') result = { invites: [] };
      else if (path === '/rooms/r1/state') { await new Promise(r => setTimeout(r, 60)); result = { changed: true, seq: 1, agents, tasks: [], requests }; }
      else if (path === '/rooms/r1/agents/attached/remove') { agents = []; result = {}; }
      else if (path === '/agents') result = { agents: mine };
      else if (path === '/rooms/r1/members/member/upload-permission') { room.members[1].file_upload_permission = body.file_upload_permission; result = room.members[1]; }
      else if (path === '/rooms/r1/members/pending/approve') { room.members[2].room_membership_status = 'approved'; result = room.members[2]; }
      else if (path === '/rooms/r1/members/member/remove') { room.members = room.members.filter(m => m.user_id !== 'member'); result = {}; }
      else if (path === '/rooms/r1/archive') result = { status: 'verified', archive_id: 'arc1', live_room_id: null };
      else result = {};
      if (path === '/admin/users/member/delete' && result.status === 'complete') users = users.filter(u => u.user_id !== 'member');
      await route.fulfill({ status: result.http || 200, contentType: 'application/json', body: JSON.stringify(result) }).catch(() => {});
    });
    await page.goto(origin);
    await page.evaluate(async surface => {
      sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: `h.${btoa(JSON.stringify({ exp: Date.now() / 1000 + 600 }))}.s`, id: 'id' }));
      const { mountCollab } = await import('/collab-view.js');
      window.view = mountCollab(document.querySelector('#view'), { surface });
      await window.view.ready;
    }, surface);
    const openRoom = async () => { await page.locator('[data-room="r1"]').click(); await page.getByRole('tab', { name: 'People', exact: true }).click(); };
    await fn({ page, calls, holds, openRoom, renameAgents: () => { agents.forEach(a => { a.name += '-renamed'; }); mine.forEach(a => { a.name += '-renamed'; }); requests.forEach(r => { r.name += '-renamed'; }); } });
    assert.deepEqual(errors, []);
  } finally { holds.clear(); await browser?.close(); await new Promise(resolve => server.close(resolve)); }
}

const flushUI = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

for (const surface of ['public', 'dashboard']) {
  for (const action of ['remove', 'upload']) for (const outcome of ['success', 'failure']) {
    test(`collaboration ${surface}: terminated room ignores pending member ${action} ${outcome}`, async () => withCollaboration(surface, async ({ page, calls, holds, openRoom }) => {
      await openRoom();
      const path = `/rooms/r1/members/member/${action === 'remove' ? 'remove' : 'upload-permission'}`;
      let release; holds.set(path, new Promise(r => { release = r; }));
      const control = page.locator(action === 'remove' ? '[data-rm="member"]' : '[data-upload="member"]');
      if (action === 'remove') { page.once('dialog', d => d.accept()); await control.click(); }
      else await control.check();
      await page.getByRole('status').filter({ hasText: 'Saving' }).waitFor();
      page.once('dialog', d => d.accept());
      await page.getByRole('button', { name: 'Archive and delete room' }).click();
      await page.getByRole('status').filter({ hasText: 'Archive verified. Room deleted.' }).waitFor();
      const before = await page.locator('#view').innerHTML(), count = calls.length;
      const response = page.waitForResponse(r => new URL(r.url()).pathname.endsWith(path));
      release(outcome === 'success' ? {} : { http: 503, error: 'Member action failed' });
      await (await response).finished();
      await flushUI(page);
      assert.equal(await control.isDisabled(), true, 'completion must not re-enable a terminated room');
      assert.equal(await page.locator('#view').innerHTML(), before, 'completion and rollback must leave terminated UI untouched');
      assert.equal(calls.length, count, 'completion must not refresh terminated membership');
      page.on('dialog', d => d.accept());
      await page.locator('[data-rm="member"]').dispatchEvent('click');
      await page.locator('[data-approve-member="pending"]').dispatchEvent('click');
      await page.locator('[data-upload="member"]').dispatchEvent('change');
      await page.locator('#hcn-delete-room').dispatchEvent('click');
      await flushUI(page);
      assert.equal(calls.length, count, 'terminated handlers must not start mutations');
      assert.equal(await page.locator('#view').innerHTML(), before);
    }));
  }

  for (const action of ['grant', 'detach', 'approve', 'deny', 'approve-all']) {
    test(`collaboration ${surface}: agent ${action} stays pending across navigation and rerender`, async () => withCollaboration(surface, async ({ page, calls, holds, openRoom, renameAgents }) => {
      await openRoom(); await page.getByRole('tab', { name: /Agents/ }).click();
      const path = { grant: '/rooms/r1/agents', detach: '/rooms/r1/agents/attached/remove',
        approve: '/rooms/r1/agent-requests/req1/decide', deny: '/rooms/r1/agent-requests/req1/decide',
        'approve-all': '/rooms/r1/agent-requests/approve-all' }[action];
      const selector = { grant: 'form[data-agent="a1"]', detach: '[data-rmagent="attached"]',
        approve: 'form[data-req="req1"]', deny: 'form[data-req="req1"] [data-deny]', 'approve-all': '#hcn-approve-all' }[action];
      const control = page.locator(selector);
      const button = ['grant', 'approve'].includes(action) ? control.locator('button[type="submit"]') : control;
      const submit = () => control.dispatchEvent(['grant', 'approve'].includes(action) ? 'submit' : 'click');
      const posts = () => calls.filter(c => c.method === 'POST' && c.path === path).length;
      let release; holds.set(path, new Promise(r => { release = r; }));
      const request = page.waitForRequest(r => new URL(r.url()).pathname.endsWith(path) && r.method() === 'POST');
      await button.click(); await request;
      await page.locator('[data-go="/rooms"]').click(); await openRoom();
      await page.getByRole('tab', { name: /Agents/ }).click(); await button.waitFor();
      await submit(); await flushUI(page);
      assert.equal(posts(), 1, 'navigation must not allow a second pending mutation');
      assert.equal(await button.isDisabled(), true, 'new controls must reflect pending state');
      // A live state update replaces all agent/request markup while the POST is held.
      renameAgents();
      await page.locator('#hcn-requests').getByText('@bot-renamed', { exact: true }).waitFor();
      await submit(); await flushUI(page);
      assert.equal(posts(), 1, 'rerender must not allow a second pending mutation');
      assert.equal(await button.isDisabled(), true);
      // Batch decisions and individual decisions share the affected request locks.
      if (['approve', 'deny', 'approve-all'].includes(action)) {
        const decisionCount = calls.filter(c => c.method === 'POST' && c.path.includes('/agent-requests/')).length;
        await page.locator('#hcn-approve-all').dispatchEvent('click');
        await page.locator('form[data-req="req1"] [data-deny]').dispatchEvent('click');
        await flushUI(page);
        assert.equal(calls.filter(c => c.method === 'POST' && c.path.includes('/agent-requests/')).length, decisionCount);
      }
      const response = page.waitForResponse(r => new URL(r.url()).pathname.endsWith(path));
      release({ http: 503, error: 'Agent mutation failed' });
      await (await response).finished();
      await page.waitForFunction(sel => { const el = document.querySelector(sel); return el && !(el.matches('form') ? el.querySelector('button[type="submit"]') : el).disabled; }, selector);
      page.once('dialog', d => d.dismiss());
      const retry = page.waitForRequest(r => new URL(r.url()).pathname.endsWith(path) && r.method() === 'POST');
      await button.click(); await retry;
      assert.equal(posts(), 2, 'settled operation must permit retry in the current view');
    }));
  }

  test(`collaboration ${surface}: pending approval and permanent deletion stay separate`, async () => withCollaboration(surface, async ({ page, calls, holds }) => {
    await page.locator('[data-go="/people"]').click();
    await page.getByRole('button', { name: 'Approve', exact: true }).click();
    await page.getByText('Nobody is waiting.').waitFor();
    await page.getByRole('tab', { name: 'Approved', exact: true }).click();
    const remove = page.locator('li').filter({ hasText: 'member@example.org' }).getByRole('button', { name: 'Delete person permanently' });
    page.once('dialog', d => d.dismiss()); await remove.click();
    assert.equal(calls.filter(c => c.path.endsWith('/delete')).length, 0);
    let release; holds.set('/admin/users/member/delete', new Promise(r => { release = r; }));
    page.once('dialog', async d => { assert.match(d.message(), /room-owned files.*remain/i); await d.accept(); });
    await remove.click();
    await page.getByRole('status').filter({ hasText: 'Deleting' }).waitFor();
    assert.equal(await remove.isDisabled(), true);
    await remove.dispatchEvent('click');
    assert.equal(calls.filter(c => c.path.endsWith('/delete')).length, 1);
    release({ status: 'complete' });
    await page.getByRole('status').filter({ hasText: 'deleted permanently' }).waitFor();
    assert.equal(await page.locator('#hcn-users li').filter({ hasText: 'member@example.org' }).count(), 0);
  }));

  test(`collaboration ${surface}: deletion failures remain visible and retryable`, async () => withCollaboration(surface, async ({ page, holds }) => {
    await page.locator('[data-go="/people"]').click();
    await page.getByRole('tab', { name: 'Approved', exact: true }).click();
    const button = page.locator('#hcn-users li').filter({ hasText: 'member@example.org' }).getByRole('button', { name: 'Delete person permanently' });
    for (const result of [{ http: 404, error: 'Deletion endpoint unavailable' }, { status: 'pending' }]) {
      holds.set('/admin/users/member/delete', Promise.resolve(result));
      page.once('dialog', d => d.accept()); await button.click();
      await page.getByRole('alert').filter({ hasText: result.error || 'not complete' }).waitFor();
      assert.equal(await button.isEnabled(), true);
      assert.equal(await page.locator('#hcn-users li').filter({ hasText: 'member@example.org' }).count(), 1);
    }
  }));

  test(`collaboration ${surface}: upload denial restores the server permission`, async () => withCollaboration(surface, async ({ page, holds, openRoom }) => {
    await openRoom();
    let release; holds.set('/rooms/r1/members/member/upload-permission', new Promise(r => { release = r; }));
    const checkbox = page.locator('[data-upload="member"]');
    await checkbox.check();
    await page.getByRole('status').filter({ hasText: 'Saving' }).waitFor();
    assert.equal(await checkbox.isChecked(), true);
    assert.equal(await checkbox.isDisabled(), true);
    release({ http: 403, error: 'Permission denied' });
    await page.getByRole('alert').filter({ hasText: 'Permission denied' }).waitFor();
    assert.equal(await checkbox.isChecked(), false);
    assert.equal(await checkbox.isEnabled(), true);
  }));

  test(`collaboration ${surface}: obsolete deletion results cannot change the new route`, async () => withCollaboration(surface, async ({ page, holds, openRoom }) => {
    await openRoom();
    let release; holds.set('/rooms/r1/archive', new Promise(r => { release = r; }));
    page.once('dialog', d => d.accept()); await page.getByRole('button', { name: 'Archive and delete room' }).click();
    await page.getByRole('status').filter({ hasText: 'Archiving' }).waitFor();
    await page.locator('[data-go="/people"]').click();
    await page.getByRole('heading', { name: 'People', exact: true }).waitFor();
    const before = await page.locator('#view').innerHTML();
    release({ status: 'verified', live_room_id: null });
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#view').innerHTML(), before);
  }));

  test(`collaboration ${surface}: returning to an in-flight archive does not trap the retry control`, async () => withCollaboration(surface, async ({ page, calls, holds, openRoom }) => {
    await openRoom();
    let release; holds.set('/rooms/r1/archive', new Promise(r => { release = r; }));
    page.once('dialog', d => d.accept()); await page.getByRole('button', { name: 'Archive and delete room' }).click();
    await page.getByRole('status').filter({ hasText: 'Archiving' }).waitFor();
    await page.locator('[data-go="/rooms"]').click(); await openRoom();
    const button = page.getByRole('button', { name: 'Archive and delete room' });
    assert.equal(await button.isDisabled(), true);
    release({ http: 503, error: 'Archive unavailable' });
    await page.waitForFunction(() => !document.getElementById('hcn-delete-room').disabled);
    assert.equal(calls.filter(c => c.path.endsWith('/archive')).length, 1);
  }));

  test(`collaboration ${surface}: membership and upload controls preserve room scope`, async () => withCollaboration(surface, async ({ page, calls, openRoom }) => {
    await openRoom();
    await page.getByRole('button', { name: 'Approve member' }).click();
    await page.getByRole('button', { name: 'Approve member' }).waitFor({ state: 'detached' });
    const member = page.locator('#hcn-people li').filter({ hasText: 'member@example.org' });
    await member.getByRole('checkbox', { name: 'Upload files' }).check();
    await page.getByRole('status').filter({ hasText: 'Upload permission saved' }).waitFor();
    assert.equal(calls.find(c => c.path.endsWith('/upload-permission')).body.file_upload_permission, true);
    page.once('dialog', async d => { assert.match(d.message(), /messages.*files.*remain/i); await d.accept(); });
    await member.getByRole('button', { name: 'Remove from room' }).click();
    await member.waitFor({ state: 'detached' });
    assert.equal(calls.some(c => c.path.endsWith('/delete')), false);
  }));

  test(`collaboration ${surface}: archive progress blocks duplicates and requires verification`, async () => withCollaboration(surface, async ({ page, calls, holds, openRoom }) => {
    await openRoom();
    let release; holds.set('/rooms/r1/archive', new Promise(r => { release = r; }));
    const button = page.getByRole('button', { name: 'Archive and delete room' });
    page.once('dialog', d => d.accept()); await button.click();
    await page.getByRole('status').filter({ hasText: 'Archiving' }).waitFor();
    assert.equal(await button.isDisabled(), true);
    await button.dispatchEvent('click');
    assert.equal(calls.filter(c => c.path.endsWith('/archive')).length, 1);
    release({ http: 503, error: 'Archive storage unavailable' });
    await page.getByRole('alert').filter({ hasText: 'Archive storage unavailable' }).waitFor();
    assert.equal(await button.isEnabled(), true);
    holds.set('/rooms/r1/archive', Promise.resolve({ status: 'verified', live_room_id: 'r1' }));
    page.once('dialog', d => d.accept()); await button.click();
    await page.getByRole('alert').filter({ hasText: 'not complete' }).waitFor();
    holds.delete('/rooms/r1/archive');
    page.once('dialog', d => d.accept()); await button.click();
    await page.getByRole('status').filter({ hasText: 'Archive verified. Room deleted.' }).waitFor();
    assert.equal(await button.isDisabled(), true);
    const before = calls.length;
    await page.getByRole('tab', { name: /Agents/ }).click();
    await page.waitForTimeout(150);
    assert.equal(calls.length, before, 'deleted room must not load agent administration');
  }));

  test(`collaboration ${surface}: agent owner approves only requested actions`, async () => withCollaboration(surface, async ({ page, calls, openRoom }) => {
    await openRoom(); await page.getByRole('tab', { name: /Agents/ }).click();
    const request = page.locator('form[data-req="req1"]');
    assert.equal(await request.getByRole('checkbox', { name: 'Write messages' }).isDisabled(), true);
    await request.getByRole('button', { name: 'Approve', exact: true }).click();
    assert.deepEqual(calls.find(c => c.path.endsWith('/req1/decide')).body.actions, ['read']);
    await request.getByRole('button', { name: 'Deny', exact: true }).click();
    assert.equal(calls.filter(c => c.path.endsWith('/req1/decide')).at(-1).body.decision, 'deny');
    await page.locator('#hcn-roomagents').getByRole('button', { name: 'Remove from room' }).click();
    await page.locator('#hcn-roomagents').getByText('No agents in this room yet.').waitFor();
    assert.equal(calls.filter(c => c.path.endsWith('/attached/remove')).length, 1);
  }));

  test(`collaboration ${surface}: contributors request agent access without owner administration`, async () => withCollaboration(surface, async ({ page, calls }) => {
    await page.locator('[data-room="r1"]').click();
    assert.equal(await page.getByRole('tab', { name: 'People', exact: true }).count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Archive and delete room' }).count(), 0);
    await page.getByRole('tab', { name: /Agents/ }).click();
    await page.getByText('The room owner approves what each agent may do here.').waitFor();
    await page.getByRole('button', { name: 'Request access', exact: true }).click();
    await page.getByRole('status').filter({ hasText: 'Request sent' }).waitFor();
    assert.deepEqual(calls.find(c => c.method === 'POST' && c.path === '/rooms/r1/agents').body.actions, ['read']);
  }, 'contributor'));
}
