import test from 'node:test';
import assert from 'node:assert/strict';
import { commandCard, isCommandEvent, presenceLabel, unreadBadge } from '../collab-cards.js';
import { CollabApi } from './collab.js';

const at = '2026-10-24T10:00:00Z';
const evil = '<img src=x onerror=alert(1)>';

test('command events are recognised; messages and tasks are not', () => {
  for (const t of ['handoff', 'handoff_updated', 'decision', 'summary_request', 'issue_draft', 'assignment', 'github_written']) assert.ok(isCommandEvent(t), t);
  for (const t of ['message', 'task_created', 'member_added']) assert.ok(!isCommandEvent(t), t);
});

test('a handoff card names both parties and its note, all escaped', () => {
  const c = commandCard({ event_type: 'handoff', created_at: at, sender: { type: 'user', id: 'bob' },
    handoff: { handoff_id: 'h1', from: { kind: 'agent', name: 'alice.pi' }, to: { kind: 'person', name: `x${evil}` }, note: evil, status: 'pending', can_answer: false } });
  assert.match(c.html, /@alice\.pi/);
  assert.ok(!c.html.includes('<img'), c.html);
  assert.match(c.html, /&lt;img/);
  assert.equal(c.attrs['data-handoff'], 'h1');
  assert.match(c.html, /pending/);
  assert.ok(!c.html.includes('data-handoff-answer'));
});

test('a decision, a summary request and a GitHub result render as cards', () => {
  assert.match(commandCard({ event_type: 'decision', created_at: at, text: `ship ${evil}` }).html, /Decision[\s\S]*ship &lt;img/);
  assert.match(commandCard({ event_type: 'summary_request', created_at: at, text: '' }, { who: () => '@bob' }).html, /@bob asked for a summary/);
  const done = commandCard({ event_type: 'github_written', created_at: at,
    github: { kind: 'issue', owner: 'acme', repo: 'app', number: 7, url: 'javascript:alert(1)' } });
  assert.match(done.html, /acme\/app#7/);
  assert.ok(!done.html.includes('javascript:'), 'only https://github.com links');
});

test('an issue draft shows its body escaped and offers confirm only when the server allows it', () => {
  const draft = { draft_id: 'd1', kind: 'issue', owner: 'acme', repo: 'app', title: evil, body: `> ${evil}`, status: 'draft', can_confirm: true };
  const c = commandCard({ event_type: 'issue_draft', created_at: at, draft });
  assert.ok(!c.html.includes('<img'));
  assert.match(c.html, /data-draft-confirm="d1"/);
  assert.equal(c.attrs['data-draft'], 'd1');
  const no = commandCard({ event_type: 'issue_draft', created_at: at, draft: { ...draft, can_confirm: false } });
  assert.ok(!no.html.includes('data-draft-confirm'));
  const made = commandCard({ event_type: 'issue_draft', created_at: at, draft: { ...draft, status: 'created', number: 9, can_confirm: false } });
  assert.match(made.html, /created/);
});

test('an assignment card names the person and the issue', () => {
  const c = commandCard({ event_type: 'assignment', created_at: at,
    draft: { draft_id: 'd2', kind: 'assignment', owner: 'acme', repo: 'app', number: 3, person: { kind: 'person', name: 'bob' }, login: 'bob-gh', status: 'draft', can_confirm: true } });
  assert.match(c.html, /acme\/app#3/);
  assert.match(c.html, /@bob/);
  assert.match(c.html, /data-draft-confirm="d2"/);
});

test('the api confirms a draft and lists decisions; a command carries its selection', async () => {
  const seen = [];
  const api = new CollabApi(() => ({ access: 'T' }), async (url, init) => { seen.push({ url, ...init }); return { ok: true, status: 200, json: async () => ({}) }; });
  await api.confirmDraft('r 1', 'd 1');
  assert.equal(seen[0].url, '/api/v1/collab/rooms/r%201/drafts/d%201/confirm');
  assert.equal(seen[0].method, 'POST');
  await api.decisions('r1');
  assert.equal(seen[1].url, '/api/v1/collab/rooms/r1/decisions');
  await api.say('r1', '/issue acme/app', undefined, [], { selection: ['e1', 'e2'] });
  assert.deepEqual(JSON.parse(seen[2].body).selection, ['e1', 'e2']);
  await api.say('r1', 'hi');
  assert.ok(!('selection' in JSON.parse(seen[3].body)));
});

test('the recipient of a pending handoff gets accept and decline; an answered one shows its state', () => {
  const base = { event_type: 'handoff', created_at: at, handoff: { handoff_id: 'h"1', from: { kind: 'person', name: 'bob' }, to: { kind: 'person', name: 'alice' }, note: '', status: 'pending', can_answer: true } };
  const c = commandCard(base);
  assert.match(c.html, /data-handoff-answer="accept" data-handoff-id="h&quot;1"/);
  assert.match(c.html, /data-handoff-answer="decline"/);
  const up = commandCard({ event_type: 'handoff_updated', created_at: at, handoff: { handoff_id: 'h1', status: 'declined', from: { kind: 'person', name: 'bob' }, to: { kind: 'person', name: 'alice' } } });
  assert.match(up.html, /@alice declined the handoff from @bob/);
  const old = commandCard({ ...base, handoff: { ...base.handoff, status: 'expired', can_answer: false } });
  assert.match(old.html, /expired/);
  assert.ok(!old.html.includes('data-handoff-answer'));
});

test('presence and unread helpers: working beats online; a count shows only when there is one', () => {
  assert.equal(presenceLabel({ presence: 'working' }), 'working…');
  assert.equal(presenceLabel({ presence: 'online' }), 'online');
  assert.equal(presenceLabel({ online: true }), 'online');
  assert.equal(presenceLabel({}), '');
  assert.equal(unreadBadge({ unread_mentions: 0 }), '');
  assert.equal(unreadBadge({ unread_mentions: null }), '');
  assert.match(unreadBadge({ unread_mentions: 3 }), /3/);
  assert.match(unreadBadge({ unread_mentions: 99 }), /99\+/);
});

test('the api answers a handoff', async () => {
  const seen = [];
  const api = new CollabApi(() => ({ access: 'T' }), async (url, init) => { seen.push({ url, ...init }); return { ok: true, status: 200, json: async () => ({}) }; });
  await api.answerHandoff('r1', 'h 1', 'accept');
  assert.equal(seen[0].url, '/api/v1/collab/rooms/r1/handoffs/h%201/accept');
  await assert.rejects(() => api.answerHandoff('r1', 'h1', 'maybe'));
});

test('the confirm button carries the title and a body summary for the confirm dialog', () => {
  const body = `> ${'x'.repeat(600)}`;
  const c = commandCard({ event_type: 'issue_draft', created_at: at, draft: { draft_id: 'd1', kind: 'issue', owner: 'acme', repo: 'app', title: `T "${evil}"`, body, status: 'draft', can_confirm: true } });
  assert.match(c.html, /data-draft-title="T &quot;&lt;img/);
  const summary = /data-draft-summary="([^"]*)"/.exec(c.html)[1];
  assert.ok(summary.length > 100 && summary.length <= 420, String(summary.length));
});
