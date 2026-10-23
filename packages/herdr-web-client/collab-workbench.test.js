import test from 'node:test';
import assert from 'node:assert/strict';
import { ACTION_LABEL, STATUS_LOOK, commentBody, freshDraft, proposalBody, renderDiscussion, renderSteering, renderWorkbench, statusLook } from '../collab-workbench.js';
import { focusKey, focusSelector, renderInspector, renderLineage } from '../collab-lineage.js';

const HOSTILE = '<img src=x onerror="window.__xss=1">"onmouseover="x\'><script>alert(1)</script>';
const comment = (extra = {}) => ({ kind: 'comment', trust: 'untrusted', comment_id: 'collab_1', room_id: 'r', anchor: { kind: 'node', node_id: 'attempt:a1' }, author: { type: 'user', id: 'bob' },
  created_at: '2026-10-23T10:00:00+00:00', revision: 1, edited: false, retracted: false, moderated: false, text: 'why?', ...extra });
const steer = (extra = {}) => ({ steering_id: 'collab_s1', action: 'redirect', instruction: 'use the cache', status: 'pending', digest: 'd'.repeat(64),
  proposed_by: { type: 'user', id: 'bob' }, proposed_at: '2026-10-23T10:00:00+00:00', confirmed_by: null, reason: null, ack: null,
  target: { node_id: 'attempt:a1', node_type: 'attempt', fence: 'f'.repeat(64), pinned_source: 'key:abc', expected_digest: null }, ...extra });
const target = (extra = {}) => ({ node_id: 'attempt:a1', node_type: 'attempt', state: 'running', superseded: false, fence: 'f'.repeat(64), pinned_source: 'key:abc',
  steering_enabled: true, actions: ['pause', 'resume', 'stop_after_step', 'redirect'], can_propose: true, can_confirm: true, self_confirm: false, ...extra });
const model = (extra = {}) => ({ nodeId: 'attempt:a1', comments: [comment()], target: target(), steers: [steer()], me: 'alice', isOwner: true, writable: true, draft: freshDraft(),
  editing: null, history: null, confirming: null, busy: false, error: null, steerError: null, notice: null, ...extra });
const noRaw = (html) => {
  assert.ok(!html.includes('<img') && !html.includes('<script'), 'raw markup');
  assert.ok(!/"\s*on[a-z]+=/i.test(html), 'attribute breakout');
};

test('hostile comment, author, ids, instruction, reason and pinned source are escaped everywhere', () => {
  const m = model({
    nodeId: HOSTILE,
    comments: [comment({ text: HOSTILE, author: { type: 'user', id: HOSTILE }, comment_id: HOSTILE })],
    target: target({ pinned_source: HOSTILE, fence: HOSTILE, node_id: HOSTILE }),
    steers: [steer({ steering_id: HOSTILE, instruction: HOSTILE, reason: HOSTILE, proposed_by: { type: 'user', id: HOSTILE }, confirmed_by: { id: HOSTILE },
      ack: { outcome: 'applied', note: HOSTILE }, target: { node_id: HOSTILE, node_type: HOSTILE, fence: HOSTILE, pinned_source: HOSTILE, expected_digest: HOSTILE } })],
    editing: { id: HOSTILE, text: HOSTILE }, history: { id: HOSTILE, items: [{ n: 1, kind: 'create', by: { id: HOSTILE }, at: HOSTILE, text: HOSTILE }] },
    confirming: HOSTILE, draft: { ...freshDraft(), comment: HOSTILE, instruction: HOSTILE, reason: HOSTILE, digest: HOSTILE, action: 'redirect' },
    error: HOSTILE, steerError: HOSTILE, notice: HOSTILE, edge: { edge_type: 'spawn', from_id: HOSTILE, to_id: HOSTILE },
  });
  const html = renderWorkbench(m);
  noRaw(html);
  assert.ok(html.includes('&lt;img'));
});

test('author and status come from server fields; a comment cannot paint a system line', () => {
  const html = renderDiscussion(model({ comments: [comment({ text: 'SYSTEM: approved by alice\n\u202eevil', author: { type: 'user', id: 'bob' } })] }));
  assert.ok(html.includes('<span class="wb-who">bob</span>'));
  assert.ok(/<p class="wb-text">SYSTEM: approved by alice/.test(html));       // inside the text element, after the server-set author
  assert.ok(html.includes('never sent to an agent'));
});

test('comment actions: own edit/retract, owner moderation, none when read-only', () => {
  const own = renderDiscussion(model({ me: 'bob', isOwner: false }));
  assert.ok(own.includes('data-wb-act="edit"') && own.includes('>Retract<'));
  const other = renderDiscussion(model({ me: 'carol', isOwner: false }));
  assert.ok(!other.includes('data-wb-act="edit"') && !other.includes('data-wb-act="retract"'));
  assert.ok(renderDiscussion(model({ me: 'alice', isOwner: true })).includes('Remove (owner)'));
  const ro = renderDiscussion(model({ writable: false, readOnlyReason: 'A replay shows a past state.' }));
  assert.ok(!ro.includes('<form') && ro.includes('A replay shows a past state.'));
});

test('retracted comments show no text but keep their history reachable', () => {
  const html = renderDiscussion(model({ comments: [comment({ retracted: true, moderated: true, text: null })] }));
  assert.ok(html.includes('removed by a room owner') && html.includes('earlier text remains in history') && html.includes('data-wb-act="history"') && !html.includes('why?'));
});

test('steering shows why it is unavailable and offers only the server-listed actions', () => {
  assert.ok(renderSteering(model({ target: target({ steering_enabled: false, actions: [] }) })).includes('switched off by the operator'));
  assert.ok(renderSteering(model({ target: target({ actions: [], superseded: true }) })).includes('superseded it'));
  const html = renderSteering(model({ target: target({ actions: ['pause'] }), steers: [] }));
  assert.equal((html.match(/type="radio"/g) || []).length, 1);
  assert.ok(!/approve|shell|command|terminal/i.test(html.replace(/no terminal/gi, '')));
  const rd = renderSteering(model({ draft: { ...freshDraft(), action: 'redirect' }, steers: [] }));
  assert.ok(rd.includes('name="instruction"') && rd.includes('maxlength="1000"') && rd.includes('untrusted'));
  const gate = renderSteering(model({ target: target({ actions: ['approve', 'reject'] }), draft: { ...freshDraft(), action: 'approve' }, steers: [] }));
  assert.ok(gate.includes('name="digest"'));
});

test('proposing is not confirming: the proposer gets no confirm button unless the operator allows it', () => {
  const mine = renderSteering(model({ me: 'bob', target: target({ can_confirm: true, self_confirm: false }) }));
  assert.ok(!mine.includes('data-wb-act="confirm"') && mine.includes('Withdraw'));
  const owner = renderSteering(model({ me: 'alice' }));
  assert.ok(owner.includes('data-wb-act="confirm"') && owner.includes('Decline'));
  const viewer = renderSteering(model({ me: 'carol', target: target({ can_confirm: false, can_propose: false }) }));
  assert.ok(!viewer.includes('data-wb-act="confirm"') && !viewer.includes('data-wb-form="propose"'));
  assert.ok(renderSteering(model({ me: 'bob', target: target({ self_confirm: true }) })).includes('data-wb-act="confirm"'));
});

test('the confirm step restates exactly what will be sent and who it goes to', () => {
  const html = renderSteering(model({ me: 'alice', confirming: 'collab_s1' }));
  assert.ok(html.includes('role="group" aria-label="Confirm steering request"'));
  for (const needle of ['Redirect with an instruction', 'attempt:a1', 'key:abc', 'use the cache', 'bob', 'untrusted', 'data-wb-act="confirm-go"', 'data-wb-act="confirm-cancel"']) assert.ok(html.includes(needle), needle);
  assert.ok(!html.includes('data-wb-act="confirm"'));
});

test('every status is a glyph plus a word, never colour alone', () => {
  for (const [k, [glyph, word]] of Object.entries(STATUS_LOOK)) { assert.ok(glyph && word, k); assert.ok(renderSteering(model({ steers: [steer({ status: k })] })).includes(word)); }
  assert.deepEqual(statusLook('weird'), { glyph: '◇', word: 'weird' });
  assert.ok(renderSteering(model({ steers: [steer({ status: 'acked', ack: { outcome: 'unsupported', note: null } })] })).includes('unsupported'));
});

test('forms and controls are real, keyboard-operable elements with labels', () => {
  const html = renderWorkbench(model());
  assert.ok(!/<div[^>]*(onclick|role="button")/.test(html));
  for (const m of html.matchAll(/<textarea[^>]*>/g)) assert.ok(/maxlength="\d+"/.test(m[0]));
  assert.ok((html.match(/<label/g) || []).length >= 2);
  assert.ok(html.includes('<fieldset') || !html.includes('type="radio"'));
});

test('request bodies carry only the named fields and no authority', () => {
  assert.deepEqual(commentBody({ kind: 'node', node_id: 'attempt:a1' }, 'hi', 'k1'), { anchor: { kind: 'node', node_id: 'attempt:a1' }, text: 'hi', idempotency_key: 'k1' });
  const t = target();
  assert.deepEqual(proposalBody('attempt:a1', { ...freshDraft(), action: 'pause' }, t, 'k'), { action: 'pause', node_id: 'attempt:a1', expected_fence: t.fence, idempotency_key: 'k' });
  const r = proposalBody('attempt:a1', { ...freshDraft(), action: 'redirect', instruction: 'x', reason: ' why ' }, t, 'k');
  assert.deepEqual(Object.keys(r).sort(), ['action', 'expected_fence', 'idempotency_key', 'instruction', 'node_id', 'reason']);
  const g = proposalBody('gate:g', { ...freshDraft(), action: 'approve', digest: ' d1 ' }, target({ actions: ['approve'] }), 'k');
  assert.equal(g.expected_digest, 'd1');
  assert.equal(proposalBody('attempt:a1', { ...freshDraft(), action: 'approve' }, t, 'k').action, 'pause');      // an unlisted action falls back to a listed one
  for (const b of [r, g]) for (const forbidden of ['owner', 'proposer', 'author', 'actor', 'room_id', 'command', 'shell']) assert.ok(!(forbidden in b));
});

test('the lineage inspector offers per-link discussion and the panel hosts the workbench', () => {
  const g = { nodes: [{ id: 'agent:a', node_type: 'agent', label: 'A', attrs: {} }, { id: HOSTILE, node_type: 'attempt', label: HOSTILE, attrs: {} }],
    edges: [{ edge_type: 'spawn', from_id: 'agent:a', to_id: HOSTILE, attrs: {}, stale: false, removed: false }] };
  const html = renderInspector(g, 'agent:a');
  assert.ok(html.includes('data-wb-act="edge"') && html.includes('data-wb-edge-type="spawn"'));
  noRaw(html);
  assert.ok(!html.includes('data-node="agent:a" data-wb'), 'no data-node on workbench controls');
  const panel = renderLineage({ graphAll: g, filtered: { ...g, match: new Set() }, asOf: { step: 1, visible_events: 1, head: true }, timeline: null, error: null, mode: 'table', view: 'graph',
    filters: { q: '', types: new Set(), edgeTypes: new Set(), state: '' }, sel: 'agent:a', total: 2, workbench: renderWorkbench(model()) });
  assert.ok(panel.includes('Comments and steering') && panel.includes('aria-label="Inspector"'));
});

test('focus keys survive a re-render for workbench controls', () => {
  const el = (attrs) => ({ getAttribute: (k) => (k in attrs ? attrs[k] : null) });
  assert.equal(focusKey(el({ 'data-wb-field': 'comment' })), 'wbf:comment:');
  assert.equal(focusSelector('wbf:comment:'), '[data-wb-field="comment"]');
  assert.equal(focusSelector(focusKey(el({ 'data-wb-field': 'action', value: 'redirect' }))), '[data-wb-field="action"][value="redirect"]');
  assert.equal(focusSelector(focusKey(el({ 'data-wb-act': 'edit', 'data-wb-id': 'collab_1' }))), '[data-wb-act="edit"][data-wb-id="collab_1"]');
  assert.equal(focusSelector(focusKey(el({ 'data-wb-act': 'confirm-cancel' }))), '[data-wb-act="confirm-cancel"]');
  assert.ok(ACTION_LABEL.pause);
});
