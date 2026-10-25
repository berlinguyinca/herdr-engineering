import test from 'node:test';
import assert from 'node:assert/strict';
import { renderCiFix, latestFix, fixElapsed, fixError, fixIsActive, fixRunKey } from '../ci-fix-view.js';
import { HerdrClient } from '../herdr-web-client/client.js';

const base = { id: 'cifix_1', repository: '7', number: 42, requested_at: '2026-10-24T10:00:00Z', cancel_requested: false };
const NOW = Date.parse('2026-10-24T10:05:00Z');

test('disabled feature renders nothing; no fix renders the button and the plain-language note', () => {
  assert.equal(renderCiFix({ available: false }), '');
  const html = renderCiFix({});
  assert.match(html, /data-fix-sandbox[^>]*>Fix in sandbox</);
  assert.match(html, /never merges/);
  assert.ok(!html.includes('data-fix-cancel'));
});

test('every state shows its label and reason; reasons are escaped', () => {
  const cases = {
    queued: /Queued/, claimed: /Starting/, preparing: /Preparing the sandbox/, running: /Pi is working/,
    finishing: /Checking the result/, succeeded: /Draft pull request opened/, no_change: /made no change/,
    failed: /Failed/, cancelled: /Cancelled/, expired: /Expired/,
  };
  for (const [state, label] of Object.entries(cases)) {
    const html = renderCiFix({ fix: { ...base, state, reason: 'because <b>x</b>' }, now: NOW });
    assert.match(html, label, state);
    assert.match(html, /because &lt;b&gt;x&lt;\/b&gt;/, state);
    assert.ok(!html.includes('<b>x'), state);
    assert.equal(html.includes('data-fix-cancel'), fixIsActive({ state }), state);
    // A finished fix that did not succeed can be started again; an active or successful one cannot.
    assert.equal(html.includes('Fix in sandbox again'), ['no_change', 'failed', 'cancelled', 'expired'].includes(state), state);
  }
});

test('running card shows host, model and elapsed time, the agent link and a Terminal button', () => {
  const html = renderCiFix({ fix: { ...base, state: 'running', host: 'beast', model: 'qwen', started_at: '2026-10-24T10:01:00Z', session_id: 'herdr:beast:default:wK%3ApH' }, now: NOW });
  assert.match(html, /Host beast · Model qwen · 4m/);
  assert.match(html, /href="#\/herd\/session\/herdr%3Abeast%3Adefault%3AwK%253ApH"/);
  assert.match(html, /data-fix-terminal/);
  assert.equal(fixElapsed({ ...base, state: 'running' }, NOW), '5m 0s');
  assert.equal(fixElapsed({ state: 'running' }, NOW), '');
});

test('terminal commands render escaped, with the suggested form first and a copy button each', () => {
  const attach = { recommended: 'local', commands: [{ form: 'remote', label: 'ssh', command: 'ssh -t beast "x"' }, { form: 'local', label: 'here', command: 'herdr <attach>' }] };
  const html = renderCiFix({ fix: { ...base, state: 'running', session_id: 's' }, terminal: attach, now: NOW });
  assert.ok(html.indexOf('herdr &lt;attach&gt;') < html.indexOf('ssh -t beast'));
  assert.equal((html.match(/data-fix-copy/g) || []).length, 2);
  assert.ok(!html.includes('<attach>'));
  assert.match(renderCiFix({ fix: { ...base, state: 'running', session_id: 's' }, terminalError: 'not running' }), /not running/);
  assert.ok(!renderCiFix({ fix: { ...base, state: 'queued' }, now: NOW }).includes('data-fix-terminal'));
});

test('a succeeded fix links the draft PR, diff stat, flags and summary; unsafe links are dropped', () => {
  const result = { pr_url: 'https://github.com/a/b/pull/9', pr_number: 9, files: 2, insertions: 10, deletions: 3, commits: 1, flags: ['touches <ci>'], summary: 'Fixed the import.' };
  const html = renderCiFix({ fix: { ...base, state: 'succeeded', result }, now: NOW });
  assert.match(html, /href="https:\/\/github.com\/a\/b\/pull\/9"[^>]*rel="noopener noreferrer"/);
  assert.match(html, /#9/);
  assert.match(html, /2 files · \+10 −3 · 1 commit/);
  assert.match(html, /touches &lt;ci&gt;/);
  assert.match(html, /Fixed the import\./);
  for (const bad of ['javascript:alert(1)', 'http://github.com/x', 'https://u:p@github.com/x']) {
    assert.ok(!renderCiFix({ fix: { ...base, state: 'succeeded', result: { pr_url: bad } }, now: NOW }).includes('data-fix-pr'), bad);
  }
});

test('cancel requested is shown and hides the cancel button', () => {
  const html = renderCiFix({ fix: { ...base, state: 'running', cancel_requested: true }, now: NOW });
  assert.match(html, /cancel requested/);
  assert.ok(!html.includes('data-fix-cancel'));
});

test('helpers: newest fix, run key, error sentences', () => {
  assert.equal(latestFix({ fixes: [{ id: 'b' }, { id: 'a' }] }).id, 'b');
  assert.equal(latestFix({ fixes: [] }), null);
  assert.equal(latestFix(null), null);
  assert.equal(fixRunKey('7', 42), '7:42');
  assert.match(fixError({ status: 404 }), /not enabled/);
  assert.match(fixError({ status: 403 }), /operator/);
  assert.match(fixError(new Error('network')), /same operation/);
});

test('client methods hit the ci/fix routes', async () => {
  const calls = [];
  const client = new HerdrClient('', { fetchImpl: async (url, init) => { calls.push([url, init.method, init.body]); return { ok: true, status: 200, text: async () => '{}' }; } });
  await client.ciFix('7', 42, 'op-1', { providerUrl: 'https://ci.example' });
  await client.listCiFix({ run: '7:42' });
  await client.getCiFix('cifix_1');
  await client.cancelCiFix('cifix_1');
  assert.deepEqual(calls.map((c) => [c[0], c[1]]), [
    ['/api/v1/ci/fix', 'POST'], ['/api/v1/ci/fix?run=7%3A42', 'GET'], ['/api/v1/ci/fix/cifix_1', 'GET'], ['/api/v1/ci/fix/cifix_1/cancel', 'POST'],
  ]);
  assert.deepEqual(JSON.parse(calls[0][2]), { repository: '7', number: 42, operation_id: 'op-1', provider_url: 'https://ci.example' });
});
