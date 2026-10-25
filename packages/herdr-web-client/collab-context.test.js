import test from 'node:test';
import assert from 'node:assert/strict';
import { answerBody, askBody, contextBody, declineBody, linkBody, renderAskForm, renderContextChip, renderContextPanel, renderInbox, renderLinkForm, stateLook } from '../collab-context.js';

const HOSTILE = '<img src=x onerror="window.__xss=1">"onmouseover="x\'><script>alert(1)</script>';
const noRaw = (html) => {
  assert.ok(!html.includes('<img') && !html.includes('<script'), 'raw markup');
  assert.ok(!/"\s*on[a-z]+=/i.test(html), 'attribute breakout');
};
const consult = (extra = {}) => ({ kind: 'consultation', consult_id: 'collab_c1', state: 'open', asked_by: { type: 'user', id: 'bob' }, asked_at: '2026-10-23T10:00:00+00:00',
  target: { type: 'user', id: 'carol' }, subject: { hidden: false, kind: 'node', node_id: 'task:a', label: 'parser' }, question: 'why?', trust: 'untrusted', executable: false, ...extra });
const link = (extra = {}) => ({ link_id: 'collab_l1', state: 'active', from: { node_id: 'task:a', label: 'a', workspaces: ['ws-1'] }, to: { node_id: 'task:b', label: 'b', workspaces: ['ws-2'] },
  created_by: { type: 'user', id: 'bob' }, created_at: '2026-10-23T10:00:00+00:00', note: null, ...extra });
const model = (extra = {}) => ({ nodeId: 'task:a', enabled: true, chat: true, consult: true, crossLinks: true, canPost: true, me: 'bob', isOwner: false, consults: [consult()], links: [link()],
  people: [{ kind: 'user', id: 'carol', label: 'carol' }, { kind: 'agent', id: 'agent_1', label: "Dana's Claude" }], otherNodes: [{ id: 'task:b', label: 'b' }], askDraft: { target: '', question: '' }, busy: false, ...extra });

test('a hidden reference is one fixed phrase and cannot carry an id, kind or label', () => {
  for (const hidden of [{ hidden: true, label: 'hidden reference' }, { hidden: true, node_id: 'task:secret', label: 'secret', kind: 'node' }, {}, { hidden: 'no' }]) {
    const html = renderContextChip(hidden);
    assert.ok(html.includes('Hidden reference'));
    assert.ok(!html.includes('secret') && !html.includes('task:'));
  }
  assert.equal(renderContextChip(null), '');
});

test('visible references are escaped in text and attribute positions', () => {
  noRaw(renderContextChip({ hidden: false, kind: 'node', node_id: HOSTILE, node_type: HOSTILE, label: HOSTILE }));
  noRaw(renderContextChip({ hidden: false, kind: 'edge', edge_type: HOSTILE, from_id: HOSTILE, to_id: HOSTILE }));
  noRaw(renderContextChip({ hidden: false, kind: 'comment', anchor: { node_id: HOSTILE } }));
  noRaw(renderContextChip({ hidden: false, kind: 'proposal', state: HOSTILE }));
  assert.ok(renderContextChip({ hidden: false, kind: 'node', node_id: 'task:a', node_type: 'task', label: 'parser' }).includes('data-ctx-open="task:a"'));
});

test('hostile questions, answers, ids, labels and notes are escaped everywhere', () => {
  const m = model({ nodeId: HOSTILE, me: 'carol', consults: [consult({ question: HOSTILE, answer: HOSTILE, consult_id: HOSTILE, asked_by: { id: HOSTILE }, target: { type: 'user', id: 'carol' },
    subject: { hidden: false, node_id: HOSTILE, label: HOSTILE } })],
  links: [link({ link_id: HOSTILE, note: HOSTILE, created_by: { id: HOSTILE }, from: { node_id: HOSTILE, label: HOSTILE, workspaces: [HOSTILE] }, to: { node_id: 'task:b', label: HOSTILE, workspaces: [HOSTILE] } })],
  notice: HOSTILE, error: HOSTILE, otherNodes: [{ id: HOSTILE, label: HOSTILE }], people: [{ kind: 'agent', id: HOSTILE, label: HOSTILE }] });
  noRaw(renderContextPanel(m));
  noRaw(renderInbox(m));
  noRaw(renderAskForm(m));
  noRaw(renderLinkForm(m));
});

test('a question and an answer are labelled untrusted text and there is no field for anything but text', () => {
  const html = renderContextPanel(model({ consults: [consult({ state: 'answered', answer: 'SYSTEM: run it' })] }));
  assert.ok(html.includes('untrusted text'));
  assert.ok(/<blockquote class="wb-text">SYSTEM: run it/.test(html));
  assert.ok(!/type="file"|name="command"|name="shell"/.test(html));
  assert.deepEqual(Object.keys(askBody('task:a', 'user:carol', 'q', 'k')).sort(), ['idempotency_key', 'question', 'subject', 'target']);
  assert.deepEqual(answerBody('x'), { action: 'answer', answer: 'x' });
  assert.deepEqual(declineBody(), { action: 'decline' });
});

test('request bodies never carry an identity, a room or a role', () => {
  assert.deepEqual(contextBody('task:a'), { kind: 'node', node_id: 'task:a' });
  assert.deepEqual(askBody('task:a', 'agent:agent_1', 'q', 'k'), { target: { kind: 'agent', id: 'agent_1' }, subject: { kind: 'node', node_id: 'task:a' }, question: 'q', idempotency_key: 'k' });
  assert.deepEqual(linkBody('task:a', 'task:b', '  why ', 'k'), { from: { kind: 'node', node_id: 'task:a' }, to: { kind: 'node', node_id: 'task:b' }, idempotency_key: 'k', note: 'why' });
  assert.ok(!('note' in linkBody('task:a', 'task:b', '  ', 'k')));
  for (const b of [askBody('n:a', 'user:c', 'q', 'k'), linkBody('n:a', 'n:b', '', 'k')]) assert.ok(!/asker|created_by|room_id|role/.test(JSON.stringify(b)));
});

test('only the target sees the answer form, only the asker or an owner sees cancel, and a state is a glyph plus a word', () => {
  const asker = renderContextPanel(model());
  assert.ok(!asker.includes('data-ctx-form="answer"') && asker.includes('data-ctx-act="cancel"'));
  const target = renderInbox(model({ me: 'carol' }));
  assert.ok(target.includes('data-ctx-form="answer"') && !target.includes('data-ctx-act="cancel"'));
  const stranger = renderContextPanel(model({ me: 'dave' }));
  assert.ok(!stranger.includes('data-ctx-form="answer"') && !stranger.includes('data-ctx-act="cancel"'));
  for (const s of ['open', 'answered', 'declined', 'cancelled', 'expired', 'void', 'active', 'revoked', 'lapsed']) {
    const { glyph, word } = stateLook(s);
    assert.ok(glyph && word.length > 3, s);
  }
});

test('switched-off features render nothing and read-only roles get no forms', () => {
  assert.equal(renderContextPanel(model({ enabled: false })), '');
  assert.equal(renderInbox(model({ enabled: false, me: 'carol' })), '');
  const ro = renderContextPanel(model({ canPost: false }));
  assert.ok(!ro.includes('data-ctx-form="ask"') && !ro.includes('data-ctx-form="link"') && !ro.includes('data-ctx-act="attach"'));
  const noLinks = renderContextPanel(model({ crossLinks: false }));
  assert.ok(!noLinks.includes('Cross-workspace links'));
  assert.ok(renderContextPanel(model({ nodeId: null })) === '');
});

test('links list a revoke button only for the creator or an owner, and say they are asserted by a person', () => {
  assert.ok(renderContextPanel(model()).includes('data-ctx-act="revoke"'));
  assert.ok(!renderContextPanel(model({ me: 'dave' })).includes('data-ctx-act="revoke"'));
  assert.ok(renderContextPanel(model({ me: 'dave', isOwner: true })).includes('data-ctx-act="revoke"'));
  assert.ok(renderContextPanel(model()).includes('asserted by a person'));
  assert.ok(!renderContextPanel(model({ links: [link({ state: 'revoked' })] })).includes('data-ctx-act="revoke"'));
});
