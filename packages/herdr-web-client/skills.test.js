import test from 'node:test';
import assert from 'node:assert/strict';
import { CollabApi, collabRouteState } from './collab.js';
import * as view from '../collab-view.js';

const manifest = (scope = 'private') => ({ skill_id: 'skill/1', name: 'CSV summary', version: '1.2.0',
  creator_id: 'alice', scope, room_id: scope === 'room' ? 'lab' : null,
  capabilities: ['files:read'], files: { 'SKILL.md': { size_bytes: 42, sha256: 'b'.repeat(64) },
    'requirements.lock': { size_bytes: 9, sha256: 'c'.repeat(64) } }, dependency_lock: 'requirements.lock',
  provenance: { artifact_id: 'artifact-1', file_ids: ['file-1'], parameters: { delimiter: ',' }, runtime: 'python-3.12' },
  input_schema: {}, output_schema: {}, tests: ['test.py'], fixtures: [], trust: 'untrusted', execution: 'harness_only' });
const published = scope => ({ skill_id: 'skill/1', version: '1.2.0', status: 'published', manifest: manifest(scope), sha256: 'a'.repeat(64) });
const draft = (publish_scopes = []) => ({ ...published('private'), status: 'draft', draft_id: 'draft/1', target_room_id: 'lab', publish_scopes });
const identity = { user_id: 'alice', status: 'approved' };

// Break caught: routing Skills back to Rooms in either shell.
test('skill route is shared by public and dashboard shells', () => {
  for (const surface of ['public', 'dashboard']) assert.equal(collabRouteState({ surface, route: '#/skills' }).route, '/skills');
});

// Break caught: wrong scope, draft ID, version or authentication on browser requests.
test('shared client pins publication and download to encoded registry identities', async () => {
  const calls = [];
  const api = new CollabApi(() => ({ access: 'token' }), async (url, init) => {
    calls.push({ url, ...init });
    return { ok: true, status: 200, json: async () => ({ skill_id: 'skill/1', version: '1.2.0',
      url: 'https://objects.example/package', expires_at: Date.now() / 1000 + 60 }) };
  });
  await api.skills('room');
  await api.skillDraft('draft/1');
  await api.publishSkill('draft/1', 'shared');
  await api.downloadSkill('skill/1', '1.2.0');
  assert.deepEqual(calls.map(c => [c.method, c.url]), [
    ['GET', '/api/v1/collab/skills?scope=room'], ['GET', '/api/v1/collab/skills/drafts/draft%2F1'],
    ['POST', '/api/v1/collab/skills/drafts/draft%2F1/publish'],
    ['POST', '/api/v1/collab/skills/skill%2F1/versions/1.2.0/download'],
  ]);
  assert.deepEqual(JSON.parse(calls[2].body), { scope: 'shared' });
  for (const call of calls) assert.equal(call.headers.Authorization, 'Bearer token');
});

test('download rejects unsafe, expired, mismatched grants and denied authorization', async () => {
  for (const change of [{ url: 'javascript:alert(1)' }, { url: 'https://user:password@objects.example/p' },
    { expires_at: 1 }, { expires_at: null }, { version: '2.0.0' }, { skill_id: 'other' }]) {
    const api = new CollabApi(() => ({ access: 'token' }), async () => ({ ok: true, status: 200,
      json: async () => ({ skill_id: 'skill/1', version: '1.2.0', url: 'https://objects.example/p', expires_at: Date.now() / 1000 + 60, ...change }) }));
    await assert.rejects(() => api.downloadSkill('skill/1', '1.2.0'), /download|expired|secure|identity/i);
  }
  const api = new CollabApi(() => ({ access: 'token' }), async () => ({ ok: false, status: 403, json: async () => ({ error: 'Access revoked' }) }));
  await assert.rejects(() => api.publishSkill('draft/1', 'shared'), /Access revoked/);
});

// Break caught: mixing private drafts or versions into another visibility tab.
test('library separates private, room, shared and unpublished versions', () => {
  for (const scope of ['private', 'room', 'shared']) {
    const skills = ['private', 'room', 'shared'].map(s => ({ ...published(s), manifest: { ...manifest(s), name: `${s}-only` } }));
    const html = view.renderSkillLibrary({ skills: [...skills, { ...published(scope), status: 'revoked', manifest: { ...manifest(scope), name: 'revoked-only' } }],
      drafts: [draft(['private'])], identity, scope });
    assert.match(html, new RegExp(`${scope}-only`));
    for (const other of ['private', 'room', 'shared'].filter(s => s !== scope)) assert.doesNotMatch(html, new RegExp(`${other}-only`));
    assert.doesNotMatch(html, /revoked-only/);
    assert.equal(html.includes('data-review-draft'), scope === 'private');
    assert.match(html, /1\.2\.0/);
    assert.match(html, /data-download-skill/);
    assert.match(html, /data-install-skill/);
  }
  assert.doesNotMatch(view.renderSkillLibrary({ drafts: [draft(['private'])], scope: 'private', identity: { user_id: 'bob' } }), /data-review-draft/);
});

// The HTTP adapter must supply publish_scopes from the registry authority checks.
// Break caught: inferring shared authority from room role or dashboard/operator state.
test('draft review uses explicit server publication scopes and fails closed without them', () => {
  for (const [role, scopes] of [['contributor', ['private']], ['owner', ['private', 'room']], ['maintainer', ['private', 'shared']]]) {
    const html = view.renderSkillReview({ ...draft(scopes), role });
    for (const scope of ['private', 'room', 'shared']) assert.equal(html.includes(`value="${scope}"`), scopes.includes(scope));
    assert.match(html, /data-confirm-publication/);
  }
  assert.doesNotMatch(view.renderSkillReview({ ...draft(), role: 'owner', admin: true }), /data-confirm-publication/);
  assert.doesNotMatch(view.renderSkillReview(published('shared')), /data-confirm-publication/);
});

test('review shows escaped capabilities, package inventory, dependency lock and complete provenance', () => {
  const skill = draft(['private', 'room']); skill.manifest.name = '<img src=x onerror=bad()>';
  skill.manifest.provenance.prompt = '</pre><script>bad()</script>';
  const html = view.renderSkillReview(skill);
  for (const value of ['files:read', 'SKILL.md', 'requirements.lock', 'artifact-1', 'file-1', 'python-3.12', '1.2.0', 'lab', 'a'.repeat(64), 'b'.repeat(64)]) assert.ok(html.includes(value), value);
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /untrusted/i);
  assert.match(html, /signature.*safety/is);
});

test('library distinguishes empty, loading and failed responses', () => {
  assert.match(view.renderSkillLibrary({ scope: 'shared', status: 'loading' }), /Loading/);
  assert.match(view.renderSkillLibrary({ scope: 'shared', error: 'Unavailable' }), /role="alert"[^>]*>Unavailable/);
  assert.doesNotMatch(view.renderSkillLibrary({ scope: 'shared', error: 'Unavailable' }), /No published skills/);
  assert.match(view.renderSkillLibrary({ scope: 'shared', skills: [] }), /No published skills/);
});

test('malformed publication scopes cannot enable publication controls', () => {
  for (const scopes of ['private room shared', { private: true }, null]) {
    assert.doesNotMatch(view.renderSkillReview(draft(scopes)), /data-confirm-publication/);
  }
});
