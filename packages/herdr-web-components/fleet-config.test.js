import test from 'node:test';
import assert from 'node:assert/strict';
import { configBadge, configNotice, indexConfigStatus } from '../fleet-config-badge.js';
import { HerdrClient, ApiError } from '../herdr-web-client/client.js';

const REV = 'abcdef0123456789';
const status = {
  revision: REV,
  last_error: null,
  hosts: [
    { id: 'bender', roles: ['dev'], applied_revision: REV },
    { id: 'Mac', roles: [], applied_revision: '1234567890abcdef' },
    { id: 'beast', roles: [], applied_revision: null },
  ],
};

test('current revision shows Config current', () => {
  const html = configBadge(indexConfigStatus(status), { host_id: 'x', host_name: 'bender' });
  assert.match(html, /Config current/);
  assert.ok(!html.includes('behind'));
});

test('behind shows warning with shortened revisions in title', () => {
  const html = configBadge(indexConfigStatus(status), { host_id: 'mac', host_name: 'other' });
  assert.match(html, /Config behind/);
  assert.match(html, /title="applied 1234567 of abcdef0"/);
  assert.match(html, /hd-ins__badge--warn/);
  assert.ok(!html.includes(REV));
});

test('null applied revision shows No config reported', () => {
  const html = configBadge(indexConfigStatus(status), { host_id: 'beast', host_name: 'beast' });
  assert.match(html, /Config behind/);
  assert.match(html, /No config reported/);
});

test('case-insensitive match on host_id or host_name; unknown host gets nothing', () => {
  const idx = indexConfigStatus(status);
  assert.match(configBadge(idx, { host_id: 'BENDER' }), /Config current/);
  assert.match(configBadge(idx, { host_id: 'zzz', host_name: 'MAC' }), /Config behind/);
  assert.equal(configBadge(idx, { host_id: 'zzz', host_name: 'nope' }), '');
});

test('no status (404) renders no badge and no notice', () => {
  const idx = indexConfigStatus(null);
  assert.equal(configBadge(idx, { host_id: 'bender' }), '');
  assert.equal(configNotice(idx), '');
});

test('last_error shows fixed muted line, never the error text', () => {
  const idx = indexConfigStatus({ ...status, last_error: 'secret <b>boom</b> at line 3' });
  const html = configNotice(idx);
  assert.match(html, /Fleet config file has an error; hosts keep the last good revision/);
  assert.ok(!html.includes('boom'));
  assert.equal(configNotice(indexConfigStatus(status)), '');
});

test('escapes interpolated revisions and renders no other fields', () => {
  const evil = { revision: '<img onerror=x>', hosts: [{ id: 'h', roles: ['token'], token: 's3', api_key: 'k', applied_revision: '"><script>' }] };
  const html = configBadge(indexConfigStatus(evil), { host_id: 'h' });
  assert.ok(!html.includes('<img') && !html.includes('<script'));
  for (const bad of ['token', 'api_key', 's3', 'dev']) assert.ok(!html.includes(bad), bad);
});

test('client.getFleetConfigStatus returns body, null on 404, throws otherwise', async () => {
  const mk = (status, body) => new HerdrClient('', { fetchImpl: async () => ({ ok: status < 400, status, text: async () => JSON.stringify(body) }) });
  assert.deepEqual(await mk(200, status).getFleetConfigStatus(), status);
  assert.equal(await mk(404, { error: 'no fleet config' }).getFleetConfigStatus(), null);
  await assert.rejects(mk(500, { error: 'x' }).getFleetConfigStatus(), ApiError);
});

test('client.getFleetConfigStatus gives up after its timeout', async () => {
  let seen = null;
  const hang = new HerdrClient('', { fetchImpl: (_url, init) => new Promise((_resolve, reject) => {
    seen = init.signal;
    init.signal.addEventListener('abort', () => reject(init.signal.reason));
  }) });
  const started = Date.now();
  await assert.rejects(hang.getFleetConfigStatus({ timeoutMs: 50 }));
  assert.ok(seen && seen.aborted);
  assert.ok(Date.now() - started < 2000);
});
