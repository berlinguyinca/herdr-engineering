import test from 'node:test';
import assert from 'node:assert/strict';
import { decisionBody, delegationBody, renderDelegations, renderProposalList, renderReview } from '../collab-proposals.js';

const evil = '<img src=x onerror="window.pwned=1"><script>alert(1)</script>"\'&';
const prop = (extra = {}) => ({ proposal_id: 'prp_1', task_id: 'grant_1', agent_id: 'agent_1', delegate_label: 'Dana', kind: 'doc', title: 'T', state: 'quarantined',
  digest: 'ab'.repeat(32), size: 12, content: 'hello', url: null, refs: { paths: ['src/a.py'], questions: ['q1'] }, flags: [], promoted: false, ...extra });

test('the delegation request is built from the form and carries no identity or room', () => {
  const b = delegationBody({ target: 'agent_1', text: 'look', paths: 'src/\n\n docs/a.md ', nodes: 'commit:abc', questions: 'q1: why?\nplain question',
    kinds: ['patch', 'doc', 'shell'], maxProposals: '2', maxSteps: 'x', maxBytes: '4096', maxTokens: '', expires: '600' });
  assert.deepEqual(b.scope.paths, ['src/', 'docs/a.md']);
  assert.deepEqual(b.scope.questions, [{ id: 'q1', text: 'why?' }, { id: 'q2', text: 'plain question' }]);
  assert.deepEqual(b.output_kinds, ['patch', 'doc']);                       // unknown kinds are dropped, order is the canonical one
  assert.deepEqual(b.budget, { max_proposals: 2, max_steps: 20, max_artifact_bytes: 4096, max_tokens: 100000 });
  assert.equal(b.expires_in_seconds, 600);
  assert.deepEqual(Object.keys(b).sort(), ['budget', 'expires_in_seconds', 'output_kinds', 'scope', 'target_agent_id', 'text']);
});

test('hostile proposal text is escaped everywhere and is never markup', () => {
  const html = renderReview(prop({ title: evil, content: evil, delegate_label: evil, refs: { paths: [evil], questions: [evil] }, reason: evil }));
  for (const bad of ['<script', '<img', 'onerror="', '<a ']) assert.ok(!html.includes(bad), bad);
  assert.ok(html.includes('&lt;script&gt;'));
  const list = renderProposalList({ proposals: [{ proposal_id: evil, title: evil, kind: evil, size: 1, state: evil, flags: [] }] }, null);
  assert.ok(!list.includes('<script') && !list.includes('<img'));
  const d = renderDelegations({ delegations: [{ task_id: evil, target_name: evil, text: evil, status: evil, version: 1, bounds: { output_kinds: [evil], scope: {}, budget: {} }, usage: {} }] });
  assert.ok(!d.includes('<script') && !d.includes('<img'));
});

test('a link proposal is shown as text, never an anchor, and says it was not opened', () => {
  const html = renderReview(prop({ kind: 'link', content: null, url: 'https://example.org/x?a=<b>' }));
  assert.ok(!/<a[\s>]/.test(html) && !html.includes('href=') && html.includes('https://example.org/x?a=&lt;b&gt;'));
  assert.match(html, /has not been opened/);
});

test('a patch is shown line by line with classes, escaped', () => {
  const html = renderReview(prop({ kind: 'patch', content: '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-<old>\n+<new>\n ctx' }));
  assert.match(html, /cx-diff--d">-&lt;old&gt;/);
  assert.match(html, /cx-diff--a">\+&lt;new&gt;/);
  assert.match(html, /cx-diff--h">--- a\/x/);
});

test('accept and reject are offered only while quarantined and carry the digest that was shown', () => {
  const open = renderReview(prop());
  assert.match(open, /data-decide="accept"[^>]*data-digest="abab/);
  assert.match(open, /data-decide="reject"/);
  for (const state of ['rejected', 'superseded', 'expired', 'withdrawn']) assert.ok(!renderReview(prop({ state })).includes('data-decide'), state);
  assert.ok(!renderReview(prop({ state: 'accepted', promoted: true })).includes('data-decide'));
  assert.match(renderReview(prop({ state: 'accepted', promoted: false })), /Finish recording in the graph/);   // the retry after a graph failure
  assert.deepEqual(decisionBody('accept', 'd', ''), { decision: 'accept', expected_digest: 'd' });
  assert.deepEqual(decisionBody('reject', 'd', 'no'), { decision: 'reject', expected_digest: 'd', reason: 'no' });
});

test('the page never offers apply, run, a terminal or a shell for a proposal and says nothing is applied', () => {
  const html = renderReview(prop({ kind: 'patch', content: '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n' }));
  assert.ok(!/apply|terminal|shell|execute/i.test(html.replace(/it does not apply, merge or run it/i, '')), html);
  assert.match(html, /UNTRUSTED/);
  assert.match(html, /does not apply, merge or run it/);
});

test('instruction-like text gets a warning for the reviewer and nothing else changes', () => {
  const html = renderReview(prop({ flags: ['instruction_like_text'] }));
  assert.match(html, /role="alert"/);
  assert.match(renderProposalList({ proposals: [prop({ flags: ['instruction_like_text'] })] }, null), /looks like instructions to an AI/);
});

test('empty states are explicit', () => {
  assert.match(renderProposalList({ proposals: [] }), /Nothing in quarantine/);
  assert.match(renderDelegations({ delegations: [] }), /No bounded tasks/);
  assert.match(renderReview(null), /Choose a proposal/);
});
