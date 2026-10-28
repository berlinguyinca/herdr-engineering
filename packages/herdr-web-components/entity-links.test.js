import test from 'node:test';
import assert from 'node:assert/strict';
import { ENTITY_KINDS, entityHref, entityLabel, forgePrUrl } from '../entity-links.js';

test('every kind builds the expected hash', () => {
  assert.equal(entityHref('mission', 'm1'), '#/missions/m1');
  assert.equal(entityHref('session', 'herdr:beast:eng:p1'), '#/herd/session/herdr%3Abeast%3Aeng%3Ap1');
  assert.equal(entityHref('host', 'beast'), '#/hosts/beast');
  assert.equal(entityHref('service', 's'), '#/services/s');
  assert.equal(entityHref('ci', '12', { repository: 'o/r' }), '#/ci?search=12&repository=o%2Fr');
  assert.equal(entityHref('review', 'r1'), '#/reviews?search=r1');
  assert.equal(entityHref('project', 'o/r'), '#/github?repository=o%2Fr');
  assert.equal(entityHref('llm-session', 'x y'), '#/llm/session/x%20y');
  assert.equal(entityHref('intel-project', 'p-binbase', { event: 'e 1' }), '#/intelligence?project=p-binbase&event=e+1');
  assert.equal(ENTITY_KINDS.length, 10);
});

test('unknown kind, empty or overlong id give null', () => {
  for (const bad of [['nope', 'a'], ['mission', ''], ['mission', null], ['mission', undefined], ['__proto__', 'a'], ['toString', 'a'], ['mission', 'x'.repeat(301)]]) {
    assert.equal(entityHref(...bad), null, String(bad));
  }
});

test('hostile ids are encoded and round-trip through decodeURIComponent', () => {
  for (const id of ['a/b', '100%', 'a:b', `q"uote'`, 'ünï/😀', '../../x', 'a?b#c', '<script>']) {
    const href = entityHref('mission', id);
    assert.match(href, /^#\/missions\/[^/?#"'<>\s]+$/);
    assert.equal(decodeURIComponent(href.slice('#/missions/'.length)), id);
  }
});

test('labels prefer names and never throw', () => {
  assert.equal(entityLabel('mission', { title: 'Fix parser', id: 'm1' }), 'Fix parser');
  assert.equal(entityLabel('ci', { repository: 'o/r', run_number: 4 }), 'o/r · run #4');
  assert.equal(entityLabel('pr', { repository: 'o/r', number: 9 }), 'o/r #9');
  assert.equal(entityLabel('host', { id: 'beast' }), 'Host beast');
  assert.equal(entityLabel('host', null), 'Host');
  assert.equal(entityLabel('zzz', {}), 'Item');
  assert.equal(entityLabel('mission', { label: 'x'.repeat(500) }).length, 120);
});

test('forge urls come from structured fields only', () => {
  assert.equal(forgePrUrl('o/r', 5), 'https://github.com/o/r/pull/5');
  for (const bad of [['o/r/x', 1], ['javascript:alert(1)//x', 1], ['o/r', 0], ['o/r', '5'], [null, 1], ['o r/x', 1]]) assert.equal(forgePrUrl(...bad), null);
});
