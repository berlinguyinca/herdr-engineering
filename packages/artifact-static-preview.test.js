import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { isKeptHref, liveLink, mountStaticHtml, PRINT_CSS, STATIC_NOTE } from './artifact-static-preview.js';

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
const themed = theme => ({ createElement: tag => new Node(tag), documentElement: { getAttribute: name => (name === 'data-theme' ? theme : null) } });
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

test('the dashboard theme drives the preview host; no or unknown theme leaves it to the OS', () => {
  for (const [theme, expected] of [['dark', 'dark'], ['light', 'light'], [null, null], ['sepia', null]]) {
    const wrapper = mountStaticHtml(new Node('div'), '<p>x</p>', { document: themed(theme) });
    assert.equal(wrapper.children[0].getAttribute('data-theme'), expected, String(theme));
  }
  assert.equal(mountStaticHtml(new Node('div'), '', { document: doc }).children[0].getAttribute('data-theme'), null);
  const explicit = mountStaticHtml(new Node('div'), '', { document: themed('light'), theme: 'dark' });
  assert.equal(explicit.children[0].getAttribute('data-theme'), 'dark');
});

test('mountStaticHtml leaves inline data-URI figure images in the copy untouched', () => {
  const html = '<article><h3>Fig</h3><img class="fig-img" alt="Fig" src="data:image/svg+xml;base64,PHN2Zz4="><p>cap</p></article>';
  const { shadow } = mount(html);
  assert.equal(shadow.innerHTML, html);
});

test('a print rule lifts the scroll box clip once per document and a document without a head is left alone', () => {
  const head = new Node('head');
  const withHead = { createElement: tag => new Node(tag), head, querySelector: () => head.children.find(child => child.tag === 'style') ?? null };
  mountStaticHtml(new Node('div'), '', { document: withHead });
  mountStaticHtml(new Node('div'), '', { document: withHead });
  assert.equal(head.children.length, 1, 'one style element for any number of mounts');
  assert.equal(head.children[0].textContent, PRINT_CSS);
  assert.match(PRINT_CSS, /^@media print\{\[data-static-preview\]\{max-height:none!important;overflow:visible!important/);
  assert.doesNotThrow(() => mountStaticHtml(new Node('div'), '', { document: doc }));
});

// A shadow stub that also knows ids and classes, so a fragment link can find its target and a focus id can be revealed.
function anchoredMount(html, ids, options = {}) {
  const targets = new Map();
  let divs = 0;
  const host = new Node('host');
  const attach = Node.prototype.attachShadow;
  host.attachShadow = init => {
    const root = attach.call(host, init);
    for (const id of ids) {
      const classes = new Set();
      const target = new Node('section');
      Object.assign(target, {
        classes, scrolled: 0, focused: 0,
        classList: { add: c => classes.add(c), remove: c => classes.delete(c) },
        hasAttribute: name => target.attrs.has(name),
        scrollIntoView() { this.scrolled += 1; },
        focus() { this.focused += 1; },
      });
      targets.set(id, target);
    }
    root.getElementById = id => targets.get(id) ?? null;
    const anchors = root.querySelectorAll;
    root.querySelectorAll = function find(selector) {
      return selector === '.is-target' ? [...targets.values()].filter(node => node.classes.has('is-target')) : anchors.call(this, selector);
    };
    root.appended = [];
    root.append = node => root.appended.push(node);
    Object.defineProperty(root, 'innerHTML', {
      set(value) {
        this._html = value;
        this.anchors = [...value.matchAll(/<a\b([^>]*)>(.*?)<\/a>/g)].map(match => {
          const anchor = new Node('a');
          const href = /href="([^"]*)"/.exec(match[1]);
          if (href) anchor.setAttribute('href', href[1]);
          const owner = /data-owner="([^"]*)"/.exec(match[1]);
          if (owner) { anchor.classList = { contains: c => c === 'anchor' }; anchor.closest = () => ({ getAttribute: () => owner[1] }); }
          anchor.childNodes = [match[2]];
          anchor.replaceWith = (...nodes) => { anchor.replacedWith = nodes; };
          anchor.addEventListener = (type, fn) => { anchor.listener = fn; };
          return anchor;
        });
      },
      get() { return this._html; },
    });
    return root;
  };
  const stub = { createElement: tag => ((tag === 'div' && (divs += 1) === 2) ? host : new Node(tag)) };
  const wrapper = mountStaticHtml(new Node('div'), html, { label: 'x', document: stub, ...options });
  return { shadow: host.shadowRoot, targets };
}

test('a fragment link whose target exists is kept, cancels the router, reveals and highlights its target', () => {
  let navigated;
  const { shadow, targets } = anchoredMount('<a href="#finding-f1">#</a><a href="#/settings">router</a>', ['finding-f1'], {
    permalink: id => `https://h.example/#/artifacts/a?at=${id}`, onNavigate: (id, url) => { navigated = [id, url]; },
  });
  const [link, router] = shadow.anchors;
  assert.deepEqual(router.replacedWith, router.childNodes, 'a fragment with no target stays unwrapped');
  assert.equal(link.replacedWith, undefined);
  assert.equal(link.getAttribute('href'), 'https://h.example/#/artifacts/a?at=finding-f1');
  assert.equal(link.getAttribute('target'), null, 'a permalink never opens a new tab');
  let prevented = false;
  link.listener({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true, 'the click never reaches the hash router');
  const target = targets.get('finding-f1');
  assert.ok(target.classes.has('is-target'));
  assert.equal(target.getAttribute('tabindex'), '-1');
  assert.equal(target.scrolled, 1);
  assert.equal(target.focused, 1);
  assert.deepEqual(navigated, ['finding-f1', 'https://h.example/#/artifacts/a?at=finding-f1']);
  assert.match(shadow.appended[0].textContent, /\.is-target\{/, 'the highlight rule is injected into the shadow root');
});

test('only one target is highlighted at a time and a focus id is revealed on mount', () => {
  const { shadow, targets } = anchoredMount('<a href="#a">a</a><a href="#b">b</a>', ['a', 'b'], { focus: 'a' });
  assert.ok(targets.get('a').classes.has('is-target'), 'the deep-link target is revealed on mount');
  shadow.anchors[1].listener({ preventDefault() {} });
  assert.ok(!targets.get('a').classes.has('is-target'));
  assert.ok(targets.get('b').classes.has('is-target'));
});

test('a hostile or malformed fragment never throws and never matches', () => {
  const { shadow } = anchoredMount('<a href="#%E0%A4%A">bad</a><a href="#">empty</a><a href="#nope&quot;&gt;">x</a>', ['a']);
  for (const anchor of shadow.anchors) assert.deepEqual(anchor.replacedWith, anchor.childNodes);
});

test('a permalink the server left without an href is wired from its nearest id and made keyboard-focusable', () => {
  const { shadow, targets } = anchoredMount('<a class="anchor" data-owner="finding-f1">#</a>', ['finding-f1']);
  const anchor = shadow.anchors[0];
  assert.equal(anchor.replacedWith, undefined, 'kept, not unwrapped');
  assert.equal(anchor.getAttribute('href'), '#finding-f1', 'a real href makes it focusable');
  anchor.listener({ preventDefault() {} });
  assert.ok(targets.get('finding-f1').classes.has('is-target'));
});
