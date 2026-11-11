// WC-004: Work Context model/render for the session page. Real code, no mocks.
import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionContextModel, renderSessionContext } from './session-context.js';

const now = Date.parse('2026-11-04T12:00:00Z'), iso = ms => new Date(ms).toISOString();
const projection = (id, title, activity = {health: 'active'}) => ({mission_key: `pi:${id}`, activity_snapshot_at: iso(now - 2000), host_id: 'beast',
  mission: {id, title, status: 'EXECUTING'}, freshness: {state: 'live', age_seconds: 2}, activity});
const sess = (id, o = {}) => ({session_id: id, host_id: 'beast', status: 'running', updated_at: iso(now - 1000), ...o});
const model = (session, data = {}) => sessionContextModel({session, projections: [], recorded: [], sessions: [session], received: now, now, ...data});

test('a session with an explicit mission key links to its parent mission through entityHref', () => {
  const s = sess('s-1', {mission_key: 'pi:MSN-1'});
  const m = model(s, {projections: [projection('MSN-1', 'Repair parser')]});
  assert.equal(m.state, 'linked');
  assert.equal(m.mission.title, 'Repair parser');
  assert.equal(m.mission.href, '#/missions/pi%3AMSN-1');
  assert.match(renderSessionContext(m), /href="#\/missions\/pi%3AMSN-1"/);
});

test('hostile titles are escaped everywhere', () => {
  const evil = '<img src=x onerror="window.__xss=1">';
  const s = sess('s-1', {mission_key: 'pi:MSN-1'}), sib = sess('s-2', {mission_key: 'pi:MSN-1', display_name: evil});
  const html = renderSessionContext(model(s, {projections: [projection('MSN-1', evil)], sessions: [s, sib]}));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
});

test('an ambiguous parent (two missions claim the bare id) is shown as not linked, never guessed', () => {
  const s = sess('s-1', {mission_id: 'M'});
  const a = {mission_id: 'M', mission_key: 'a:M', title: 'A', sessions: ['s-1']}, b = {mission_id: 'M', mission_key: 'b:M', title: 'B', sessions: ['s-1']};
  const m = model(s, {recorded: [a, b]});
  assert.equal(m.state, 'unlinked');
  assert.match(renderSessionContext(m), /Not linked to a mission/);
  assert.match(renderSessionContext(m), /could not be confirmed/);
});

test('a stale or unknown mission reference is unlinked, and a title match alone never links', () => {
  const s = sess('s-1', {mission_key: 'pi:GONE', display_name: 'Repair parser'});
  const m = model(s, {projections: [projection('MSN-1', 'Repair parser')]});
  assert.equal(m.state, 'unlinked');
  assert.equal(m.mission, undefined);
  const none = model(sess('s-9'), {projections: [projection('MSN-1', 'Repair parser')]});
  assert.equal(none.state, 'unlinked');
  assert.match(renderSessionContext(none), /does not report a mission/);
});

test('progress only with measured evidence', () => {
  const s = sess('s-1', {mission_key: 'pi:MSN-1'});
  const without = renderSessionContext(model(s, {projections: [projection('MSN-1', 'T')]}));
  assert.ok(!without.includes('<progress'));
  const withEv = renderSessionContext(model(s, {projections: [projection('MSN-1', 'T', {health: 'active', acceptanceCoverage: {completed: 1, total: 4}})]}));
  assert.match(withEv, /<progress[^>]*max="100"[^>]*value="25"[^>]*aria-label="[^"]*1 \/ 4 acceptance criteria verified/);
});

test('no next action is shown without a real source', () => {
  const html = renderSessionContext(model(sess('s-1', {mission_key: 'pi:MSN-1'}), {projections: [projection('MSN-1', 'T')]}));
  assert.ok(!/next action/i.test(html));
});

test('sibling list excludes the session itself and links the others', () => {
  const s = sess('s-1', {mission_key: 'pi:MSN-1'}), b = sess('s-2', {mission_key: 'pi:MSN-1', display_name: 'Sibling B'}), c = sess('s-3', {display_name: 'Elsewhere'});
  const m = model(s, {projections: [projection('MSN-1', 'T')], sessions: [s, b, c]});
  assert.deepEqual(m.siblings.map(x => x.id), ['s-2']);
  const html = renderSessionContext(m);
  assert.match(html, /href="#\/herd\/session\/s-2"/);
  assert.ok(!html.includes('Elsewhere'));
  assert.ok(!html.includes('href="#/herd/session/s-1"'));
});

test('loading and failed-source states do not claim "not linked"', () => {
  assert.match(renderSessionContext(null), /Loading work context/);
  const html = renderSessionContext(model(sess('s-1', {mission_key: 'pi:X'}), {failed: ['missions', 'projections']}));
  assert.match(html, /could not be read/);
  assert.ok(!html.includes('Not linked to a mission'));
});

// G8-A: session work context shows the Pi-reported next action, escaped, only for a linked mission.
import { renderSessionContext as renderG8, sessionContextModel as modelG8 } from './session-context.js';
const g8now = Date.parse('2026-11-04T12:00:00Z');
const g8proj = activity => ({mission_key: 'pi:MSN-1', activity_snapshot_at: new Date(g8now - 2000).toISOString(), host_id: 'beast',
  mission: {id: 'MSN-1', title: 'Repair parser', status: 'EXECUTING'}, freshness: {state: 'live', age_seconds: 2}, activity});
const g8session = {session_id: 'a1', host_id: 'beast', status: 'running', updated_at: new Date(g8now - 1000).toISOString(), mission_key: 'pi:MSN-1'};
test('G8 linked context shows a Pi-reported next action with hostile text escaped', () => {
  const model = modelG8({session: g8session, projections: [g8proj({nextAction: '<img src=x onerror=alert(1)> then "ship"', nextActionAt: '2026-11-04T13:00:00Z'})], sessions: [g8session], received: g8now, now: g8now});
  const html = renderG8(model);
  assert.match(html, /Pi-reported next action/);
  assert.ok(!html.includes('<img'));
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});
test('G8 linked context omits the row for the literal fallback and absent text', () => {
  for (const nextAction of ['No further action is scheduled', undefined, '']) {
    const model = modelG8({session: g8session, projections: [g8proj({nextAction})], sessions: [g8session], received: g8now, now: g8now});
    assert.ok(!renderG8(model).includes('Pi-reported next action'));
  }
});
test('G8 an unlinked session never shows a next action', () => {
  const lone = {...g8session, mission_key: undefined};
  const html = renderG8(modelG8({session: lone, projections: [g8proj({nextAction: 'Do it'})], sessions: [lone], received: g8now, now: g8now}));
  assert.ok(!html.includes('Pi-reported next action'));
});
