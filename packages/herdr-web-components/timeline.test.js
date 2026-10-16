import test from 'node:test';
import assert from 'node:assert/strict';

import { renderTimeline } from './timeline.js';

const EMPTY =
  '<herdr-empty message="No timeline events yet"></herdr-empty>';

test('renderTimeline: empty array renders herdr-empty markup', () => {
  assert.equal(renderTimeline([]), EMPTY);
});

test('renderTimeline: non-array input renders herdr-empty markup', () => {
  assert.equal(renderTimeline(null), EMPTY);
  assert.equal(renderTimeline(undefined), EMPTY);
  assert.equal(renderTimeline({ items: [] }), EMPTY);
  assert.equal(renderTimeline('nope'), EMPTY);
});

test('renderTimeline: one item per kind renders data-kind and title', () => {
  const html = renderTimeline([
    { kind: 'mission', title: 'Start mission', ts: '2025-01-01T00:00:00Z' },
    { kind: 'worker', title: 'Worker finished', ts: '2025-01-01T00:05:00Z' },
  ]);
  assert.ok(html.startsWith('<ol class="hd-timeline">'));
  assert.ok(html.includes('data-kind="mission"'));
  assert.ok(html.includes('data-kind="worker"'));
  assert.ok(html.includes('<span class="hd-timeline__title">Start mission</span>'));
  assert.ok(html.includes('<span class="hd-timeline__title">Worker finished</span>'));
});

test('renderTimeline: title containing <script> is escaped', () => {
  const html = renderTimeline([
    { kind: 'note', title: '<script>alert(1)</script>', ts: '2025-01-01T00:00:00Z' },
  ]);
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
});

test('renderTimeline: javascript: ref yields no <a>; https: ref does', () => {
  const unsafe = renderTimeline([
    { kind: 'note', title: 't', ts: '2025-01-01T00:00:00Z', ref: 'javascript:alert(1)' },
  ]);
  assert.ok(!unsafe.includes('<a '));
  assert.ok(!unsafe.includes('javascript:'));

  const safe = renderTimeline([
    { kind: 'note', title: 't', ts: '2025-01-01T00:00:00Z', ref: 'https://example.com/a?b=c' },
  ]);
  assert.ok(safe.includes('<a href="https://example.com/a?b=c" target="_blank" rel="noopener">link</a>'));
});

test('renderTimeline: status null renders no herdr-status; non-empty status does', () => {
  const noStatus = renderTimeline([
    { kind: 'note', title: 't', ts: '2025-01-01T00:00:00Z', status: null },
  ]);
  assert.ok(!noStatus.includes('herdr-status'));

  const withStatus = renderTimeline([
    { kind: 'note', title: 't', ts: '2025-01-01T00:00:00Z', status: 'implementation' },
  ]);
  assert.ok(withStatus.includes('<herdr-status stage="implementation"></herdr-status>'));

  const emptyStatus = renderTimeline([
    { kind: 'note', title: 't', ts: '2025-01-01T00:00:00Z', status: '' },
  ]);
  assert.ok(!emptyStatus.includes('herdr-status'));
});

test('renderTimeline: time element uses raw ts as datetime and formatted text', () => {
  const html = renderTimeline([
    { kind: 'note', title: 't', ts: '2025-06-15T12:30:00Z' },
  ]);
  assert.ok(html.includes('<time datetime="2025-06-15T12:30:00Z">'));
});

test('renderTimeline: multiple items render in the given order', () => {
  const html = renderTimeline([
    { kind: 'a', title: 'first', ts: '2025-01-01T00:00:00Z' },
    { kind: 'b', title: 'second', ts: '2025-01-01T00:01:00Z' },
    { kind: 'c', title: 'third', ts: '2025-01-01T00:02:00Z' },
  ]);
  const i1 = html.indexOf('data-kind="a"');
  const i2 = html.indexOf('data-kind="b"');
  const i3 = html.indexOf('data-kind="c"');
  assert.ok(i1 > -1 && i2 > -1 && i3 > -1);
  assert.ok(i1 < i2 && i2 < i3);
});

test('renderTimeline: dynamic values are HTML-escaped', () => {
  const html = renderTimeline([
    { kind: 'k&"', title: 'a<b>c"d', ts: '2025-01-01T00:00:00Z' },
  ]);
  assert.ok(html.includes('data-kind="k&amp;&quot;"'));
  assert.ok(html.includes('a&lt;b&gt;c&quot;d'));
});
