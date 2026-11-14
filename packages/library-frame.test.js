import test from 'node:test';
import assert from 'node:assert/strict';
import { isContentUrl, mountLibraryFrame } from './library-frame.js';

class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; }
  setAttribute(k, v) { this.attrs[k] = v; }
  replaceChildren(...c) { this.children = c; }
}
const doc = { createElement: (t) => new El(t) };

test('only a library content route can be framed', () => {
  assert.ok(isContentUrl('/api/v1/library/content/a1/v1/index.html'));
  assert.ok(isContentUrl('/api/v1/library/content/a1/v1/data/x%20y.html'));
  for (const bad of ['', null, 'https://evil.example/', '/api/v1/library/items/a1', '/api/v1/artifacts/a1/versions/v1/rich',
    '/api/v1/library/content/a1/v1/', '/api/v1/library/content/a1/v1/../x', '/api/v1/library/content/a1/v1/%2e%2e/x',
    '/api/v1/library/content/a1/v1/x?y#z', 'javascript:alert(1)', 'http://h/api/v1/library/content/a1/v1/index.html', 'https://evil.example/api/v1/library/content/a1/v1/x.html',
    '//evil.example/api/v1/library/content/a1/v1/x.html', 'file:///api/v1/library/content/a1/v1/x', '\\evil/api/v1/library/content/a1/v1/x', 'data:text/html,<p>']) {
    assert.equal(isContentUrl(bad), false, String(bad));
  }
});

test('the frame is sandboxed with no tokens and no srcdoc', () => {
  const slot = new El('div');
  const frame = mountLibraryFrame(slot, { url: '/api/v1/library/content/a1/v1/index.html', document: doc });
  assert.equal(frame.attrs.sandbox, '');
  assert.equal(frame.attrs.src, '/api/v1/library/content/a1/v1/index.html');
  assert.equal('srcdoc' in frame.attrs, false);
  assert.equal(frame.attrs.referrerpolicy, 'no-referrer');
  assert.deepEqual(slot.children, [frame]);
  assert.throws(() => mountLibraryFrame(slot, { url: 'https://evil.example/', document: doc }), TypeError);
});
