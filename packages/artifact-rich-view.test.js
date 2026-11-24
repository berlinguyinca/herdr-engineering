import test from 'node:test';
import assert from 'node:assert/strict';
import { hasRichView, isHtmlType, isRichUrl, mountRichFrame, richViewHtml, RICH_NOTE, wireRichView } from './artifact-rich-view.js';

class Node {
  constructor(tag) { this.tag = tag; this.attrs = new Map(); this.children = []; this.className = ''; this.textContent = ''; this.listeners = {}; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  click() { this.listeners.click(); }
}
const doc = { createElement: tag => new Node(tag) };
const URL = '/api/v1/artifacts/r1/versions/v2/rich';

test('only reports with an html asset get a rich view', () => {
  assert.equal(hasRichView([{ media_type: 'text/csv' }, { media_type: 'text/html' }]), true);
  assert.equal(hasRichView([{ media_type: 'TEXT/HTML; charset=utf-8' }]), true);
  assert.equal(hasRichView([{ media_type: 'text/csv' }, null]), false);
  assert.equal(hasRichView([]), false);
  assert.equal(hasRichView(undefined), false);
  assert.equal(isHtmlType('text/htmlx'), false);
});

test('the frame has exactly the one allowed shape: empty sandbox, rich-route src, no scripts token, no srcdoc', () => {
  const slot = new Node('div');
  const frame = mountRichFrame(slot, { url: URL, liveUrl: '/api/v1/artifacts/r1/live', document: doc });
  assert.equal(frame.tag, 'iframe');
  assert.equal(frame.getAttribute('sandbox'), '');           // present and empty: every capability off
  assert.equal(frame.getAttribute('src'), URL);
  assert.equal(frame.getAttribute('loading'), 'lazy');
  assert.equal(frame.getAttribute('referrerpolicy'), 'no-referrer');
  assert.ok(frame.getAttribute('title'));
  assert.deepEqual([...frame.attrs.keys()].sort(), ['data-rich-frame', 'loading', 'referrerpolicy', 'sandbox', 'src', 'title']);
  assert.equal(frame.attrs.has('srcdoc'), false);
  const [, note, link] = slot.children;
  assert.equal(note.textContent, RICH_NOTE);
  assert.equal(link.getAttribute('href'), '/api/v1/artifacts/r1/live');
  assert.equal(link.getAttribute('target'), '_blank');
  assert.match(link.getAttribute('rel'), /noopener/);
});

test('any other source is refused', () => {
  for (const url of ['https://evil.example/x', 'javascript:alert(1)', 'data:text/html,x', '/api/v1/artifacts/r1/live',
    '/api/v1/artifacts/r1/versions/v2/rich/../../live', '/api/v1/artifacts/r1/versions/v2/rich?x=1', '//evil.example/api/v1/artifacts/r1/versions/v2/rich', '', undefined]) {
    assert.equal(isRichUrl(url), false, String(url));
    assert.throws(() => mountRichFrame(new Node('div'), { url, document: doc }), TypeError);
  }
  assert.equal(isRichUrl('http://127.0.0.1:8080/api/v1/artifacts/r1/versions/v2/rich'), true);
});

test('the preview stays the default: the panel starts closed and the frame exists only after the toggle', () => {
  assert.match(richViewHtml(), /aria-expanded="false"/);
  assert.doesNotMatch(richViewHtml(), /<iframe/i);
  const toggle = new Node('button'); toggle.setAttribute('aria-expanded', 'false');
  const slot = new Node('div');
  const root = { querySelector: selector => (selector === '[data-rich-toggle]' ? toggle : slot) };
  wireRichView(root, { url: URL, liveUrl: '/l', document: doc });
  assert.equal(slot.children.length, 0);
  toggle.click();
  assert.equal(slot.children[0].tag, 'iframe');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(toggle.textContent, 'Hide rich view');
  toggle.click();
  assert.equal(slot.children.length, 0);
  assert.equal(toggle.textContent, 'Show rich view');
});
