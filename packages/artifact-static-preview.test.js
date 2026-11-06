import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { isKeptHref, liveLink, mountStaticHtml, STATIC_NOTE } from './artifact-static-preview.js';

// A document stub with just enough DOM for mountStaticHtml: the shadow root "parses" the anchors of its html.
class Node {
  constructor(tag) { this.tag = tag; this.attrs = new Map(); this.children = []; this.childNodes = []; this.textContent = ''; this.className = ''; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  attachShadow(init) {
    this.shadowInit = init;
    const root = {
      anchors: [],
      set innerHTML(html) {
        this._html = html;
        this.anchors = [...html.matchAll(/<a\b([^>]*)>(.*?)<\/a>/g)].map(match => {
          const anchor = new Node('a');
          const href = /href="([^"]*)"/.exec(match[1]);
          if (href) anchor.setAttribute('href', href[1]);
          anchor.childNodes = [match[2]];
          anchor.replaceWith = (...nodes) => { anchor.replacedWith = nodes; };
          return anchor;
        });
      },
      get innerHTML() { return this._html; },
      querySelectorAll(selector) { assert.equal(selector, 'a'); return this.anchors; },
    };
    this.shadowRoot = root;
    return root;
  }
}
const doc = { createElement: tag => new Node(tag) };
const mount = html => {
  const container = new Node('div');
  const wrapper = mountStaticHtml(container, html, { label: 'Report preview', document: doc });
  return { container, wrapper, shadow: wrapper.children[0].shadowRoot, host: wrapper.children[0] };
};

test('relative, fragment and script links are unwrapped; absolute http(s) and mailto links are kept', () => {
  const { shadow } = mount('<a href="#/settings">frag</a><a href="page.html">rel</a><a href="javascript:alert(1)">js</a>'
    + '<a href="//evil.example/x">proto</a><a href="https://example.org/a">abs</a><a href="mailto:a@b.c">mail</a><a>none</a>');
  const [frag, rel, js, proto, abs, mail, none] = shadow.anchors;
  for (const unwrapped of [frag, rel, js, proto, none]) assert.deepEqual(unwrapped.replacedWith, unwrapped.childNodes);
  for (const kept of [abs, mail]) {
    assert.equal(kept.replacedWith, undefined);
    assert.equal(kept.getAttribute('rel'), 'noopener noreferrer');
    assert.equal(kept.getAttribute('target'), '_blank');
  }
});

test('the wrapper is paint-contained, labelled, shows the static note and holds an open shadow root', () => {
  const { container, wrapper, host } = mount('<p>hi</p>');
  assert.equal(wrapper.getAttribute('style'), 'contain:paint;overflow:auto;max-height:640px');
  assert.equal(wrapper.getAttribute('role'), 'document');
  assert.equal(wrapper.getAttribute('aria-label'), 'Report preview');
  assert.ok(wrapper.attrs.has('data-static-preview'));
  assert.deepEqual(host.shadowInit, { mode: 'open' });
  assert.equal(container.children[0].textContent, STATIC_NOTE);
  assert.equal(container.children[1], wrapper);
  assert.equal(host.shadowRoot.innerHTML, '<p>hi</p>');
});

test('isKeptHref accepts only absolute http(s) and mailto', () => {
  for (const ok of ['http://a.b/c', 'https://a.b', 'HTTPS://A.B', 'mailto:x@y.z']) assert.ok(isKeptHref(ok), ok);
  for (const bad of ['', null, undefined, '#x', '/a', 'a.html', 'javascript:1', 'data:text/html,x', 'https:', 'https:\\\\x', 'ftp://x']) assert.ok(!isKeptHref(bad), String(bad));
});

test('liveLink escapes the url and opens a new tab without opener or referrer', () => {
  const html = liveLink('/api/v1/artifacts/a"><script>x</script>/live');
  assert.doesNotMatch(html, /<script/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /target="_blank"/);
  assert.match(html, /data-open-live/);
  assert.match(html, /class="hd-btn"/);
});

test('every pdf.js getDocument call disables eval and XFA, and no frame element is built', () => {
  const source = readFileSync(new URL('./artifact-static-preview.js', import.meta.url), 'utf8');
  const calls = source.match(/getDocument\([^)]*\)/g) ?? [];
  assert.ok(calls.length >= 1);
  for (const call of calls) assert.match(call, /isEvalSupported: false[^)]*enableXfa: false/);
  assert.match(source, /AnnotationMode\.DISABLE/);
  assert.doesNotMatch(source, /iframe|srcdoc|createElement\('(?:frame|object|embed)'\)/i);
});

test('the static preview container follows the theme tokens instead of forcing white', () => {
  const css = readFileSync(new URL('./artifact-static-preview.css', import.meta.url), 'utf8');
  const rule = css.split('\n').find(line => line.startsWith('[data-static-preview]'));
  assert.match(rule, /background: var\(--surface/);
  assert.match(rule, /color: var\(--text/);
  assert.doesNotMatch(rule, /#fff;|#111;/);
});
