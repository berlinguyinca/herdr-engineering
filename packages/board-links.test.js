// WC-005: board item -> Herdr target resolver and card render. Real code, no mocks.
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveBoardLink, refsFromMissionOutcomes } from './board-links.js';
import { renderProjectCard, renderProjectRow } from './github-projects-view.js';

const issue = (o = {}) => ({item_id: 'I1', type: 'ISSUE', repository: 'acme/app', number: 7, title: 'Fix parser', url: 'https://github.com/acme/app/issues/7', ...o});
const pr = (o = {}) => issue({item_id: 'P1', type: 'PULL_REQUEST', url: 'https://github.com/acme/app/pull/7', ...o});
const ref = (o = {}) => ({kind: 'mission', id: 'pi:MSN-1', type: 'issue', repository: 'acme/app', number: 7, ...o});

test('exactly one explicit repo+number+type match is verified and the href comes from entityHref', () => {
  const r = resolveBoardLink(issue(), {refs: [ref(), ref({id: 'pi:OTHER', number: 8})]});
  assert.equal(r.state, 'verified');
  assert.deepEqual(r.target, {kind: 'mission', id: 'pi:MSN-1', href: '#/missions/pi%3AMSN-1'});
  const s = resolveBoardLink(pr(), {refs: [ref({kind: 'session', id: 's-1', type: 'pr'})]});
  assert.equal(s.target.href, '#/herd/session/s-1');
});

test('no refs, an empty source, or no matching ref gives none and no target', () => {
  assert.deepEqual(resolveBoardLink(issue()), {state: 'none'});
  assert.deepEqual(resolveBoardLink(issue(), {refs: []}), {state: 'none'});
  assert.deepEqual(resolveBoardLink(issue(), {refs: [ref({number: 9})]}), {state: 'none'});
  assert.deepEqual(resolveBoardLink(issue({type: 'DRAFT_ISSUE', repository: null, number: null}), {refs: [ref()]}), {state: 'none'});
});

test('two different missions or sessions for one item is ambiguous with no target; duplicates of one ref are not', () => {
  assert.deepEqual(resolveBoardLink(issue(), {refs: [ref(), ref({id: 'pi:MSN-2'})]}), {state: 'ambiguous'});
  assert.deepEqual(resolveBoardLink(issue(), {refs: [ref(), ref({kind: 'session', id: 's-1'})]}), {state: 'ambiguous'});
  assert.equal(resolveBoardLink(issue(), {refs: [ref(), ref()]}).state, 'verified');
});

test('unavailable repository, number or source is partial with no target', () => {
  assert.deepEqual(resolveBoardLink(issue({repository: null}), {refs: [ref()]}), {state: 'partial'});
  assert.deepEqual(resolveBoardLink(issue({number: null}), {refs: [ref()]}), {state: 'partial'});
  assert.deepEqual(resolveBoardLink({item_id: 'R', type: 'REDACTED'}, {refs: [ref()]}), {state: 'partial'});
  assert.deepEqual(resolveBoardLink(issue(), {refs: [ref()], refsFailed: true}), {state: 'partial'});
});

test('the same number in a different repository, or issue vs pull request, never joins', () => {
  assert.equal(resolveBoardLink(issue({repository: 'acme/other'}), {refs: [ref()]}).state, 'none');
  assert.equal(resolveBoardLink(pr(), {refs: [ref()]}).state, 'none');
  assert.equal(resolveBoardLink(issue({repository: 'ACME/App'}), {refs: [ref()]}).state, 'verified', 'GitHub names are case-insensitive');
  assert.equal(resolveBoardLink(issue(), {refs: [ref({repository: 'git.example.com/acme/app'})]}).state, 'none', 'another forge host never joins');
});

test('a title or free text never links', () => {
  assert.equal(resolveBoardLink(issue({title: 'pi:MSN-1 Fix parser'}), {refs: [ref({number: 99, title: 'Fix parser'})]}).state, 'none');
});

test('hostile title and URL stay inert and an unlinkable id yields no href', () => {
  const evil = '<img src=x onerror="window.__xss=1">';
  const item = issue({title: evil, url: 'javascript:alert(1)'});
  const r = resolveBoardLink(item, {refs: [ref({id: 'x'.repeat(400)})]});
  assert.equal(r.state, 'none', 'entityHref refuses an over-long id');
  const html = renderProjectCard(item, resolveBoardLink(item, {refs: [ref()]}));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('javascript:'));
  assert.match(html, /href="#\/missions\/pi%3AMSN-1"/);
  const odd = resolveBoardLink(issue(), {refs: [ref({id: '"><script>x</script>'})]});
  assert.ok(!renderProjectCard(issue(), odd).includes('<script>'));
});

test('card and row keep the external GitHub link; Open in Herdr only when verified; partial shows a quiet note', () => {
  const verified = {state: 'verified', target: {kind: 'mission', id: 'pi:MSN-1', href: '#/missions/pi%3AMSN-1'}};
  for (const render of [renderProjectCard, renderProjectRow]) {
    const v = render(issue(), verified);
    assert.match(v, /href="https:\/\/github.com\/acme\/app\/issues\/7"[^>]*target="_blank"/);
    assert.match(v, /data-herdr-link href="#\/missions\/pi%3AMSN-1"[^>]*>Open in Herdr</);
    for (const state of [{state: 'none'}, {state: 'ambiguous'}, undefined]) {
      const n = render(issue(), state);
      assert.ok(!n.includes('Open in Herdr') && !n.includes('data-herdr'), 'no dead link');
      assert.match(n, /href="https:\/\/github.com\/acme\/app\/issues\/7"/);
    }
    const p = render(issue(), {state: 'partial'});
    assert.ok(!p.includes('Open in Herdr'));
    assert.match(p, /data-herdr-partial[^>]*>Herdr data partial</);
  }
});

test('refsFromMissionOutcomes keeps the served mission key and verifies a PR card end to end', () => {
  const {refs, failed} = refsFromMissionOutcomes({outcomes: {
    'pi:MSN-1': {prs: [{repository: 'acme/app', number: 7}]},
    'MSN-2': {prs: []}, 'MSN-3': {}, 'MSN-4': {prs: [{repository: 5, number: 1}, {repository: 'a/b', number: '2'}, null]},
  }});
  assert.equal(failed, false);
  assert.deepEqual(refs, [{kind: 'mission', id: 'pi:MSN-1', type: 'pr', repository: 'acme/app', number: 7}]);
  assert.equal(resolveBoardLink(pr(), {refs}).state, 'verified');
  assert.equal(resolveBoardLink(issue(), {refs}).state, 'none', 'a PR ref never verifies an issue');
});

test('refsFromMissionOutcomes flags malformed payloads as failed', () => {
  for (const bad of [null, undefined, {}, {outcomes: null}, {outcomes: []}, 'x'])
    assert.deepEqual(refsFromMissionOutcomes(bad), {refs: [], failed: true});
});

test('G2: issue cards stay unlinked; free text and unrecorded issue fields are never relationship sources', () => {
  const body = {outcomes: {
    'pi:MSN-1': {prs: [{repository: 'acme/app', number: 7}], title: 'Closes #7', last_event: 'Fixes acme/app#7', issue_number: 7, issue: {repository: 'acme/app', number: 7}},
    'pi:MSN-2': {prs: [], issues: [{repository: 'acme/app', number: 7}]},
  }};
  const {refs, failed} = refsFromMissionOutcomes(body);
  assert.equal(failed, false);
  assert.ok(refs.every(r => r.type === 'pr'), 'only recorded PRs become refs');
  assert.equal(resolveBoardLink(issue(), {refs}).state, 'none');
});
