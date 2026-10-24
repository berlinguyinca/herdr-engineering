import test from 'node:test';
import assert from 'node:assert/strict';
import { completionAt, providers, applyCompletion, ROOM_COMMANDS, unresolvedMentions } from '../collab-mention.js';

const ctx = {
  people: [{ user_id: 'u1', handle: 'gert', online: true }, { user_id: 'u2', handle: 'ana' }],
  agents: [
    { agent_id: 'a1', address: 'gert.pi', name: 'lead', aliases: ['old-lead'], harness_type: 'pi', skills: ['skill:review', 'skill:pdf-tools', 'deploy'] },
    { agent_id: 'a2', address: 'ana.claude', name: 'claude-code-x1y2', aliases: [], harness_type: 'claude-code', skills: [] },
  ],
  files: [{ path: 'notes/gemini.md' }],
  commands: ROOM_COMMANDS,
};
const typeAll = (text) => completionAt(text, text.length);

test('completionAt finds @, # and a leading / with the query typed so far', () => {
  assert.deepEqual(typeAll('@ge'), { sigil: '@', query: 'ge', start: 0 });
  assert.deepEqual(typeAll('hi @Gert.p'), { sigil: '@', query: 'gert.p', start: 3 });
  assert.deepEqual(typeAll('see @src/main.js'), { sigil: '@', query: 'src/main.js', start: 4 });
  assert.deepEqual(typeAll('fixes #binv'), { sigil: '#', query: 'binv', start: 6 });
  assert.deepEqual(typeAll('/hand'), { sigil: '/', query: 'hand', start: 0 });
  assert.deepEqual(completionAt('hi @ge there', 6), { sigil: '@', query: 'ge', start: 3 });
});

test('mail addresses, Windows paths, finished words and a mid-sentence / open no menu', () => {
  for (const text of ['mail a@b.com', 'a@b.com', 'C:\\path', 'C:\\x\\@y', 'open C:\\path\\to', 'and/or', 'see /etc/hosts',
    'please run /deploy', '@gert done', '!ls', '$HOME', 'x <@y', '']) {
    assert.equal(typeAll(text), null, text);
  }
});

test('after "@agent " a / lists that agent\'s reported skills, and is passed literally', () => {
  assert.deepEqual(typeAll('@gert.pi /sk'), { sigil: '/', query: 'sk', start: 9, agent: 'gert.pi' });
  const items = providers['/']('sk', { ...ctx, agent: 'gert.pi' });
  assert.deepEqual(items.map((i) => i.insert), ['/skill:pdf-tools', '/skill:review']);
  assert.ok(items.every((i) => i.section === 'Skills'));
  assert.deepEqual(providers['/']('', { ...ctx, agent: 'old-lead' }).map((i) => i.insert).sort(),
    ['/deploy', '/skill:pdf-tools', '/skill:review']);                           // an alias finds the same agent
  assert.deepEqual(providers['/']('', { ...ctx, agent: 'ana.claude' }), []);       // no skills reported
  assert.deepEqual(providers['/']('', { ...ctx, agent: 'nobody' }), []);
});

test('a leading / offers room commands only', () => {
  const items = providers['/']('', ctx);
  assert.deepEqual(items.map((i) => i.insert), ['/handoff', '/decide', '/summarize', '/issue', '/assign']);
  assert.ok(items.every((i) => i.section === 'Commands'));
  assert.deepEqual(providers['/']('dec', ctx).map((i) => i.insert), ['/decide']);
  assert.deepEqual(providers['/']('', { ...ctx, commands: undefined }), []);
});

test('@ ranks people before their agents and groups People, Agents and Files', () => {
  const items = providers['@']('ge', ctx);
  assert.deepEqual(items.map((i) => i.insert), ['@gert', '@gert.pi', '@notes/gemini.md']);
  assert.deepEqual(items.map((i) => [i.section, i.icon]), [['People', 'person'], ['Agents', 'robot'], ['Files', 'file']]);
  for (const item of items) for (const key of ['insert', 'label', 'section', 'icon', 'detail']) assert.ok(key in item, key);
  assert.deepEqual(providers['@']('lead', ctx).map((i) => i.insert), ['@gert.pi']);      // name and old names find the address
  assert.deepEqual(providers['@']('old', ctx).map((i) => i.insert), ['@gert.pi']);
  assert.deepEqual(providers['@']('he', ctx).map((i) => i.insert), ['@here']);
  assert.deepEqual(providers['@']('', ctx).filter((i) => i.icon === 'group').map((i) => i.insert), ['@agents', '@all', '@here']);
  assert.deepEqual(providers['#']('x', ctx), []);
});

test('applyCompletion inserts the literal text only, equal to typing it out', () => {
  let typed = applyCompletion('@ge', 3, 0, { insert: '@gert.pi' });
  assert.deepEqual(typed, { text: '@gert.pi ', caret: 9 });
  typed = applyCompletion(`${typed.text}/sk`, typed.text.length + 3, 9, { insert: '/skill:review' });
  assert.deepEqual(typed, { text: '@gert.pi /skill:review ', caret: 23 });
  assert.equal(`${typed.text}src/`, '@gert.pi /skill:review src/');
  assert.deepEqual(applyCompletion('hi @ge and more', 6, 3, { insert: '@gert' }), { text: 'hi @gert  and more', caret: 9 });
});

test('unresolvedMentions flags names that address nobody, as the server would', () => {
  assert.deepEqual(unresolvedMentions('@gert @gert.pi @lead @old-lead @all @zzz and @Gert. `@code` a@b.com', ctx), ['zzz']);
  assert.deepEqual(unresolvedMentions('@zzz @zzz @yy', ctx), ['zzz', 'yy']);
});
