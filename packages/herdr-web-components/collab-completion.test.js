import test from 'node:test';
import assert from 'node:assert/strict';
import { completionAt, providers, applyCompletion, ROOM_COMMANDS, unresolvedMentions, mentionChips, filterItems, resolvesTo, resolveToken, findAgent, ambiguousMentions, mentionTokens } from '../collab-mention.js';

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
  assert.deepEqual(typeAll('@org/repo#12'), { sigil: '@', query: 'org/repo#12', start: 0 });
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

test('mentionChips wraps only server-resolved names, outside code and links, keeping the literal text', () => {
  const mentions = [{ kind: 'person', id: 'u1', name: 'gert' }, { kind: 'agent', id: 'a1', name: 'gert.pi' }];
  const html = mentionChips('<p>hi @gert. ask @lead, <code>@gert</code> <a href="#">@gert</a> @zzz a@gert</p>', mentions, ctx);
  assert.equal((html.match(/class="cx-mention /g) || []).length, 2);
  assert.match(html, /data-mention-kind="person"[^>]*><span[^>]*data-icon="person"[^>]*>[^<]*<\/span>@gert<\/span>\./);
  assert.match(html, /data-mention-id="a1"[^>]*><span[^>]*data-icon="robot"[^>]*>[^<]*<\/span>@lead<\/span>,/);
  assert.match(html, /<code>@gert<\/code> <a href="#">@gert<\/a> @zzz a@gert/);
  assert.equal(mentionChips('<p>@gert</p>', [], ctx), '<p>@gert</p>');
  assert.equal(mentionChips('<p>@x</p>', [{ kind: 'person', id: '"><img>', name: 'x' }], {}).includes('<img>'), false);
});

test('filterItems ranks generic references by label and insert text', () => {
  const items = [{ insert: '@src/app.js', label: 'src/app.js', section: 'Files' }, { insert: 'https://github.com/o/r/issues/12', label: 'Issue #12', section: 'Issues' }];
  assert.deepEqual(filterItems(items, 'app').map((i) => i.label), ['src/app.js']);
  assert.deepEqual(filterItems(items, '12').map((i) => i.label), ['Issue #12']);
});

const clash = {
  people: [{ user_id: 'u1', handle: 'gert' }, { user_id: 'u3', handle: 'coder' }],
  agents: [
    { agent_id: 'a1', address: 'gert.pi', name: 'p1', aliases: ['lead'], skills: ['skill:review'] },
    { agent_id: 'a2', address: 'ana.claude', name: 'p2', aliases: ['lead'], skills: ['deploy'] },
    { agent_id: 'a3', address: 'ana.codex', name: 'coder', aliases: [], skills: [] },
  ],
};

test('a name shared at one level addresses nobody, as on the server, and says which forms to use', () => {
  assert.equal(resolvesTo('lead', clash), null);
  assert.deepEqual(resolveToken('lead', clash), { ambiguous: ['@ana.claude', '@gert.pi'] });
  assert.equal(resolvesTo('coder', clash), null);                                   // person handle = agent name
  assert.deepEqual(resolveToken('coder', clash), { ambiguous: ['@ana.codex'] });
  assert.deepEqual(resolvesTo('gert.pi', clash), { kind: 'agent', id: 'a1', name: 'gert.pi' });
  assert.equal(findAgent(clash.agents, 'lead'), null);
  assert.deepEqual(providers['/']('', { ...clash, agent: 'lead' }), []);
  assert.deepEqual(unresolvedMentions('@lead @coder @gert', clash), []);              // not unknown: ambiguous
  assert.deepEqual(ambiguousMentions('@lead and @coder, @gert @lead.', clash),
    [{ name: 'lead', forms: ['@ana.claude', '@gert.pi'] }, { name: 'coder', forms: ['@ana.codex'] }]);
  assert.equal(mentionChips('<p>@lead</p>', [{ kind: 'agent', id: 'a1', name: 'gert.pi' }], clash), '<p>@lead</p>');
});

test('names inside ~~~ fences are not mentions either', () => {
  assert.deepEqual(mentionTokens('~~~\n@gert\n~~~ @ana ~~~py\n@x'), ['ana']);
});
