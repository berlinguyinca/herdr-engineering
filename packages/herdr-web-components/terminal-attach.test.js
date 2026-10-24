import test from 'node:test';
import assert from 'node:assert/strict';
import { attachForms, attachError, attachHtml, mountTerminalAttach } from '../terminal-attach.js';
import { HerdrClient } from '../herdr-web-client/client.js';

const RESULT = {
  viewer_host: 'bender', recommended: 'remote', note: 'Run it locally.',
  commands: [
    { form: 'remote', label: 'From any other machine (ssh to beast)', command: `ssh -t beast "bash -lc 'herdr --session default agent attach wK:pH'"` },
    { form: 'local', label: 'On beast itself', command: 'herdr --session default agent attach wK:pH' },
  ],
};

test('the recommended form comes first and both forms are always offered', () => {
  const forms = attachForms({ ...RESULT, recommended: 'local' });
  assert.deepEqual(forms.map((f) => f.form), ['local', 'remote']);
  assert.equal(forms[0].recommended, true);
  assert.equal(attachForms({ commands: 'nope' }).length, 0);
});

test('commands are escaped, never rendered as markup', () => {
  const html = attachHtml({ ...RESULT, viewer_host: '<img src=x>', commands: [{ form: 'local', label: '<b>', command: `a"<script>` }] });
  assert.ok(!html.includes('<script>') && !html.includes('<img') && !html.includes('<b>'));
  assert.match(html, /&quot;&lt;script&gt;/);
  assert.match(attachHtml(RESULT), /ssh -t beast &quot;bash -lc &#39;herdr --session default agent attach wK:pH&#39;&quot;/);
  assert.match(attachHtml({ ...RESULT, viewer_host: null }), /could not be recognised/);
});

test('refusals read as plain sentences', () => {
  assert.match(attachError({ status: 409, body: { reason: 'not_live' } }), /not running/);
  assert.match(attachError({ status: 409, body: { reason: 'unsafe_identity' } }), /cannot be put in a command safely/);
  assert.equal(attachError({ status: 404 }), 'Agent not found.');
  assert.match(attachError({ status: 403 }), /Only the operator/);
});

test('client posts only the session id to the terminal route', async () => {
  const calls = [];
  const client = new HerdrClient('', { fetchImpl: async (url, opts) => {
    calls.push({ url, method: opts.method, body: JSON.parse(opts.body) });
    return { ok: true, status: 200, text: async () => JSON.stringify(RESULT) };
  } });
  await client.getTerminalAttach('herdr:beast:default:wK%3ApH');
  assert.deepEqual(calls, [{ url: '/api/v1/sessions/herdr%3Abeast%3Adefault%3AwK%253ApH/terminal', method: 'POST', body: {} }]);
});

function fakeHost() {
  const listeners = new Map(), nodes = {};
  const node = (name) => (nodes[name] ||= { innerHTML: '', disabled: false, listeners: new Map(),
    addEventListener(t, f) { this.listeners.set(t, f); }, removeEventListener(t) { this.listeners.delete(t); } });
  return { nodes, listeners, innerHTML: '', replaceChildren() { this.innerHTML = ''; },
    querySelector(sel) { return node(sel); } };
}

test('no button without the operator capability; with it, the command is fetched and copied', async () => {
  const hidden = fakeHost();
  mountTerminalAttach(hidden, {}, { session_id: 's', capabilities: { attach_terminal: false } }, { copyText() {} });
  assert.equal(hidden.innerHTML, '');

  const host = fakeHost(), copied = [];
  const client = { getTerminalAttach: async (id) => { assert.equal(id, 's'); return RESULT; } };
  const dispose = mountTerminalAttach(host, client, { session_id: 's', capabilities: { attach_terminal: true } }, { copyText: (t) => copied.push(t) });
  assert.match(host.innerHTML, />Terminal</);
  await host.nodes['[data-terminal]'].listeners.get('click')();
  const out = host.nodes['[data-terminal-out]'];
  assert.match(out.innerHTML, /Copy command/);
  out.listeners.get('click')({ target: { closest: () => ({ dataset: { copy: '0' } }) } });
  assert.deepEqual(copied, [RESULT.commands[0].command]);
  dispose();
  assert.equal(out.listeners.size, 0);
});
