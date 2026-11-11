import test from 'node:test';
import assert from 'node:assert/strict';
import { WORK_CENTER_CAPABILITY, workCenterEnabled } from './work-center-flag.js';
import { overviewCards, sanitizeOverviewPreferences, renderOverviewSection } from './overview-view.js';
import { renderProjectCard } from './github-projects-view.js';

test('the flag is the exact capability string and anything else is off', () => {
  assert.equal(WORK_CENTER_CAPABILITY, 'work_center_v1');
  assert.equal(workCenterEnabled(['ci_v1', 'work_center_v1']), true);
  for (const bad of [undefined, null, [], ['ci_v1'], 'work_center_v1', { work_center_v1: true }, ['work_center_v1x']]) {
    assert.equal(workCenterEnabled(bad), false, JSON.stringify(bad));
  }
});

test('flag off: the overview has exactly the pre-WC cards, in the pre-WC order, even with saved layouts', () => {
  const before = ['sessions', 'missions', 'fleet', 'ci', 'reviews', 'services', 'hosts', 'activity'];
  assert.deepEqual(overviewCards(false), before);
  assert.deepEqual(overviewCards(true), ['week', ...before]);
  assert.deepEqual(sanitizeOverviewPreferences(null, overviewCards(false)).order, before);
  const saved = sanitizeOverviewPreferences({ order: ['week', 'hosts'], hidden: ['week', 'ci'] }, overviewCards(false));
  assert.deepEqual(saved.order, ['hosts', 'sessions', 'missions', 'fleet', 'ci', 'reviews', 'services', 'hosts', 'activity'].filter((k, i, a) => a.indexOf(k) === i));
  assert.deepEqual(saved.hidden, ['ci']);
});

test('flag off: the week section renders nothing', () => {
  assert.equal(renderOverviewSection('week', {}, {}, 0, 0, { workCenter: false }), '');
});

test('board card never shows Open in Herdr when the caller passes no resolution', () => {
  const html = renderProjectCard({ title: 'x', type: 'ISSUE', url: 'https://github.com/o/r/issues/1' }, null);
  assert.doesNotMatch(html, /Open in Herdr|data-herdr/);
});
