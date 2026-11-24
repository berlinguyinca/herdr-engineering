import {test} from 'node:test';
import assert from 'node:assert/strict';
import {plotModel, plotHtml, formatCount, formatSeconds} from './summary-snapshot-plot.js';

const snap = (i, extra = {}) => ({prompts: 5 + i, events: 40 + i * 20, tool_failures: i, worked_s: 600 * (i + 1), fresh: 1000 * (i + 1), drift: false, ...extra});

test('fewer than two snapshots draws nothing', () => {
  assert.equal(plotModel(null), null);
  assert.equal(plotModel([snap(0)]), null);
  assert.equal(plotHtml([snap(0)]), '');
});

test('one row per known metric, first to last, oldest on the left', () => {
  const m = plotModel([snap(0), snap(1), snap(2)]);
  assert.equal(m.count, 3);
  const prompts = m.rows.find(r => r.key === 'prompts');
  assert.equal(prompts.text, '5 → 7');
  assert.ok(prompts.points[0][0] < prompts.points[2][0]);
  assert.ok(prompts.points[2][1] < prompts.points[0][1], 'larger value is higher (smaller y)');
  assert.equal(m.rows.find(r => r.key === 'worked_s').text, '10m → 30m');
  assert.equal(m.rows.find(r => r.key === 'fresh').text, '1k → 3k');
});

test('a metric with no values is skipped and a gap breaks the line', () => {
  const m = plotModel([snap(0, {fresh: null}), snap(1, {fresh: null}), snap(2, {fresh: null}), snap(3)]);
  assert.equal(m.rows.find(r => r.key === 'fresh').points.filter(Boolean).length, 1);
  assert.equal(plotModel([{prompts: 1, events: 2}, {prompts: 2, events: 3}]).rows.some(r => r.key === 'worked_s'), false);
  const html = plotHtml([snap(0), snap(1, {events: null}), snap(2)]);
  assert.match(html, /data-plot-row="events"/);
});

test('a flat series is drawn mid-height, not divided by zero', () => {
  const m = plotModel([snap(0, {prompts: 3}), snap(1, {prompts: 3})]);
  assert.deepEqual(m.rows[0].points.map(p => p[1]), [11, 11]);
});

test('drift snapshots are marked and noted', () => {
  const html = plotHtml([snap(0), snap(1, {drift: true}), snap(2)]);
  assert.match(html, /sw-plot-drift/);
  assert.match(html, /Snapshot history \(3 snapshots\)/);
  assert.doesNotMatch(plotHtml([snap(0), snap(1)]), /sw-plot-drift/);
});

test('output is built from numbers only and escapes through the caller', () => {
  const evil = [snap(0, {prompts: '<img onerror=x>'}), snap(1, {prompts: '<b>'})];
  assert.doesNotMatch(plotHtml(evil), /<img|<b>/);
  const esc = s => String(s).replace(/</g, '&lt;');
  assert.ok(plotHtml([snap(0), snap(1)], esc).includes('Prompts'));
});

test('number formats', () => {
  assert.equal(formatCount(950), '950');
  assert.equal(formatCount(12500), '12.5k');
  assert.equal(formatCount(2_000_000), '2M');
  assert.equal(formatSeconds(45), '45s');
  assert.equal(formatSeconds(4800), '1h20m');
  assert.equal(formatCount(null), '–');
});
