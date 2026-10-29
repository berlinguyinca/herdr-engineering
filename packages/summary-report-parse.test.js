import test from 'node:test';
import assert from 'node:assert/strict';
import {parseReport, staleness, elapsedLabel, nearestTurn} from './summary-report-parse.js';

const REPORT = `# Session summary
**Session:** claude · **Repository:** x
**Overall status:** in progress

1. **Original objective** — build it
2. **Starting point**
Clean.
3. **Timeline of work**
- a
10. **Resume point**
Continue in file.py.

**Evidence coverage**: partial — one gap`;

test('parses status, coverage, sections and resume point', () => {
  const p = parseReport(REPORT);
  assert.equal(p.status, 'in progress');
  assert.equal(p.coverage, 'partial');
  assert.deepEqual(p.sections.map(s => s.title).slice(0, 3), ['Original objective', 'Starting point', 'Timeline of work']);
  assert.equal(p.sections.at(-1).title, 'Evidence coverage');
});

test('sections must be numbered in order; others fall back to plain rendering', () => {
  assert.deepEqual(parseReport('hello\n\n1. only\n').sections, []);
  assert.equal(parseReport('').coverage, '');
});

test('staleness counts newer messages', () => {
  const ev = [{seq: 5, kind: 'message'}, {seq: 9, kind: 'tool'}, {seq: 10, kind: 'message'}];
  assert.deepEqual(staleness(5, 10, ev), {stale: true, count: 1});
  assert.equal(staleness(10, 10, ev).stale, false);
  assert.equal(staleness(undefined, 10, ev).stale, false);
});

test('elapsed label', () => {
  assert.equal(elapsedLabel(5400), '5s');
  assert.equal(elapsedLabel(125000), '2m 05s');
});

test('nearestTurn picks the closest turn within a day', () => {
  const turns = [{seq: 1, ts: '2026-10-28T10:00:00Z'}, {seq: 2, ts: '2026-10-28T11:00:00Z'}];
  assert.equal(nearestTurn('2026-10-28 10:55', turns), 2);
  assert.equal(nearestTurn('2026-12-01 10:55', turns), null);
});
