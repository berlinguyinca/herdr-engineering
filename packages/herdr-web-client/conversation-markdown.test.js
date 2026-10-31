import test from 'node:test';
import assert from 'node:assert/strict';
import { renderMarkdown } from '../conversation-markdown.js';

test('conversation Markdown renders paragraphs, headings, lists, inline and fenced code', () => {
  const html = renderMarkdown('# Result\n\n**Ready** and *tested* with `npm test`.\n\n- one\n- two\n\n1. first\n2. second\n\n```js\nconst x = "<tag>";\n```');
  for (const fragment of ['<h1>Result</h1>', '<strong>Ready</strong>', '<em>tested</em>', '<code>npm test</code>', '<ul><li>one</li><li>two</li></ul>', '<ol><li>first</li><li>second</li></ol>', '<pre><code>const x = &quot;&lt;tag&gt;&quot;;</code></pre>']) assert.ok(html.includes(fragment), fragment);
});

test('untrusted HTML and unsafe URLs never become executable markup', () => {
  const html = renderMarkdown('<img src=x onerror=alert(1)>\n\n[x](javascript:alert) [x](data:text/html,hi) [x](//evil.test) [x](jav&#97;script:alert)');
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('href='));
  assert.equal(renderMarkdown('`**plain** <svg>`'), '<p><code>**plain** &lt;svg&gt;</code></p>');
});

test('safe explicit links are escaped and open with isolated browsing context', () => {
  const html = renderMarkdown('[issue](https://github.com/org/repo/issues/12?a=1&b=2) [session](#/herd/session/one)');
  assert.ok(html.includes('href="https://github.com/org/repo/issues/12?a=1&amp;b=2"'));
  assert.ok(html.includes('rel="noopener noreferrer nofollow"'));
  assert.ok(html.includes('href="#/herd/session/one"'));
});

test('unclosed fences remain escaped code and large messages stay bounded', () => {
  assert.equal(renderMarkdown('```\n<script>'), '<pre><code>&lt;script&gt;</code></pre>');
  assert.ok(renderMarkdown('a'.repeat(210000)).length < 201000);
});

test('GFM tables, checked tasks, strikethrough and rules stay escaped', () => {
  const html = renderMarkdown('| Name | Value |\n| :--- | ---: |\n| **one** | <script>x</script> |\n\n- [x] done\n- [ ] next\n\n~~old~~\n\n---');
  for (const part of ['<table>', '<th', '<td', '<strong>one</strong>', '&lt;script&gt;', 'type="checkbox" disabled checked', '<del>old</del>', '<hr>']) assert.ok(html.includes(part), part);
  assert.ok(!html.includes('<script>'));
});

const hrefs = (html) => [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);

test('bare http(s) URLs become isolated anchors', () => {
  const html = renderMarkdown('see https://example.com/a?b=1&c=2 and http://example.org now');
  assert.deepEqual(hrefs(html), ['https://example.com/a?b=1&amp;c=2', 'http://example.org/']);
  assert.ok(html.includes('target="_blank"'));
  assert.ok(html.includes('rel="noopener noreferrer nofollow"'));
});

test('trailing punctuation and unbalanced parentheses are not part of the link', () => {
  for (const [src, want] of [
    ['go to https://example.com/x.', 'https://example.com/x'],
    ['(https://example.com/x)', 'https://example.com/x'],
    ['https://example.com/x, then', 'https://example.com/x'],
    ['[https://example.com/x]', 'https://example.com/x'],
    ['"https://example.com/x";', 'https://example.com/x'],
    ["'https://example.com/x'", 'https://example.com/x'],
    ['https://example.com/x?!', 'https://example.com/x'],
    ['https://en.wikipedia.org/wiki/Foo_(bar)', 'https://en.wikipedia.org/wiki/Foo_(bar)'],
    ['(see https://en.wikipedia.org/wiki/Foo_(bar))', 'https://en.wikipedia.org/wiki/Foo_(bar)'],
  ]) assert.deepEqual(hrefs(renderMarkdown(src)), [want], src);
  assert.ok(renderMarkdown('(https://example.com/x).').includes('</a>).'));
});

test('dangerous schemes are never linked, bare or markdown', () => {
  for (const src of ['javascript:alert(1)', 'data:text/html,<b>x</b>', 'vbscript:x', '[a](javascript:alert(1))', '[a](data:text/html,hi)', '[a](vbscript:x)', '[a](JaVa\tScript:x)', '[a](file:///etc/passwd)', 'https://user:pw@evil.test/', 'https://google.com@evil.test/'])
    assert.ok(!renderMarkdown(src).includes('href='), src);
});

test('HTML injection attempts stay escaped text', () => {
  const html = renderMarkdown('https://example.com/"onmouseover="alert(1) <a href="https://x.test">x</a> https://e.com/<script>alert(1)</script>');
  assert.ok(!html.includes('<script'));
  const tags = [...html.matchAll(/<[^>]*>/g)].map((m) => m[0]);
  assert.ok(tags.every((t) => /^<\/?(p|a|span|br)\b/.test(t)));
  assert.ok(tags.every((t) => !/onmouseover/.test(t)));
  for (const h of hrefs(html)) assert.ok(!/[<>"\s]/.test(h), h);
});

test('markdown links with a web target show the real host', () => {
  const html = renderMarkdown('[https://github.com/safe](https://evil.example/phish) [docs](https://docs.example.com/x)');
  assert.ok(html.includes('(evil.example)'));
  assert.ok(html.includes('(docs.example.com)'));
  assert.ok(html.includes('title="https://evil.example/phish"'));
  assert.deepEqual(hrefs(html), ['https://evil.example/phish', 'https://docs.example.com/x']);
  assert.ok(!renderMarkdown('[a](javascript:x "t")').includes('href='));
});

test('URLs inside code spans and fenced blocks stay plain text', () => {
  assert.equal(renderMarkdown('`https://example.com/x`'), '<p><code>https://example.com/x</code></p>');
  assert.ok(!renderMarkdown('```\nhttps://example.com/x\n```').includes('<a '));
  assert.ok(!renderMarkdown('[https://a.test](javascript:x)').includes('<a '));
});

test('link text inside a markdown link is not linkified again', () => {
  const html = renderMarkdown('[see https://inner.test here](https://outer.test/)');
  assert.equal((html.match(/<a /g) || []).length, 1);
});

test('displayed URL text is capped but href is complete', () => {
  const long = `https://example.com/${'a'.repeat(500)}`;
  const html = renderMarkdown(long);
  assert.deepEqual(hrefs(html), [long]);
  assert.ok(html.includes('…</a>'));
  assert.ok(html.length < 2000);
  assert.ok(!renderMarkdown(`https://example.com/${'a'.repeat(5000)}`).includes('href='));
});

test('IDN and lookalike hosts reveal their punycode host', () => {
  const html = renderMarkdown('https://аpple.com/login [paypal.com](https://xn--pypal-4ve.com/)');
  assert.ok(html.includes('(xn--pple-43d.com)'));
  assert.ok(html.includes('(xn--pypal-4ve.com)'));
});

test('adversarial long input renders quickly', () => {
  const inputs = ['['.repeat(100000), '[a]('.repeat(40000), 'http://'.repeat(30000), `https://a.com/${'('.repeat(100000)}`, `https://a.com/${')'.repeat(100000)}`, '`'.repeat(100000), '*a '.repeat(60000), `${'https://a.com/x.'.repeat(10000)}`, 'a'.repeat(199999)];
  for (const src of inputs) {
    const start = process.hrtime.bigint();
    renderMarkdown(src);
    assert.ok(Number(process.hrtime.bigint() - start) / 1e6 < 1500, src.slice(0, 12));
  }
});

// ---- entity reference tokens (cross-linking) -----------------------------------------
import { createEntityIndex } from '../entity-refs.js';

const refs = {
  resolve(kind, id) {
    const known = { 'session:herdr:beast:eng:p1': ['#/herd/session/herdr%3Abeast%3Aeng%3Ap1', 'Amber Swift'], 'mission:m1': ['#/missions/m1', 'Fix parser'], 'host:beast': ['#/hosts/beast', 'Host beast'] };
    const hit = known[`${kind}:${id}`];
    return hit ? { href: hit[0], label: hit[1] } : null;
  },
};

test('reference tokens link only entities that exist, and only when the caller opts in', () => {
  const text = 'See session:herdr:beast:eng:p1, mission:m1 and host:beast.';
  const html = renderMarkdown(text, { references: refs });
  assert.match(html, /<a class="md-entity-ref" href="#\/herd\/session\/herdr%3Abeast%3Aeng%3Ap1" title="session:herdr:beast:eng:p1">Amber Swift<\/a>,/);
  assert.match(html, /href="#\/missions\/m1"[^>]*>Fix parser<\/a> and/);
  assert.match(html, /href="#\/hosts\/beast"[^>]*>Host beast<\/a>\./);
  // the public collaboration site passes no resolver: the text stays text
  assert.doesNotMatch(renderMarkdown(text), /md-entity-ref|<a /);
});

test('look-alike and unknown references stay plain text', () => {
  for (const bad of ['session:nope', 'xsession:herdr:beast:eng:p1', 'mission:m1x', 'session: m1', 'session:javascript:alert(1)', 'host:beast/../x', 'pr:', 'http://x/session:herdr:beast:eng:p1']) {
    const html = renderMarkdown(bad, { references: refs });
    assert.doesNotMatch(html, /md-entity-ref/, bad);
    assert.doesNotMatch(html, /href="javascript/i, bad);
  }
});

test('a resolver cannot inject an external or scripted target', () => {
  const evil = { resolve: () => ({ href: 'javascript:alert(1)', label: '<img src=x onerror=1>' }) };
  assert.doesNotMatch(renderMarkdown('session:abc', { references: evil }), /<a |javascript:/);
  const throwing = { resolve: () => { throw new Error('x'); } };
  assert.equal(renderMarkdown('host:beast', { references: throwing }), '<p>host:beast</p>');
  const external = { resolve: () => ({ href: 'https://evil.example', label: 'x' }) };
  assert.doesNotMatch(renderMarkdown('host:beast', { references: external }), /<a /);
});

test('references work inside lists, tables and bold but not inside code or link labels', () => {
  const html = renderMarkdown('- **mission:m1**\n\n`host:beast`\n\n[session:herdr:beast:eng:p1](https://example.com)', { references: refs });
  assert.match(html, /<li><strong><a class="md-entity-ref"/);
  assert.match(html, /<code>host:beast<\/code>/);
  assert.doesNotMatch(html, /<a [^>]*md-entity-ref[^>]*>[^<]*<\/a><\/a>/);
});

test('a hostile reference flood stays linear', () => {
  const started = Date.now();
  renderMarkdown('session:'.repeat(20000) + ' ' + 'mission:'.repeat(20000), { references: refs });
  renderMarkdown(('a' + ':').repeat(80000), { references: refs });
  assert.ok(Date.now() - started < 2000, `took ${Date.now() - started} ms`);
});

test('the dashboard index resolves loaded entities and filters by id shape', async () => {
  const client = {
    listSessions: async () => ({ sessions: [{ session_id: 's 1', display_name: 'Amber' }] }),
    listMissions: async () => ({ missions: [{ mission_id: 'm1', mission_key: 'pi:m1', title: 'Fix' }] }),
    listHosts: async () => ({ hosts: [{ host_id: 'beast' }] }),
  };
  const index = createEntityIndex(client);
  assert.equal(index.resolver.resolve('session', 's%201'), null);       // not loaded yet
  await index.load();
  assert.deepEqual(index.resolver.resolve('session', 's%201'), { href: '#/herd/session/s%201', label: 'Amber' });
  assert.equal(index.resolver.resolve('mission', 'pi:m1').href, '#/missions/pi%3Am1');
  assert.equal(index.resolver.resolve('host', 'beast').label, 'Host beast');
  assert.equal(index.resolver.resolve('ci', 'org/app#12').href, '#/ci?search=12&repository=org%2Fapp');
  assert.equal(index.resolver.resolve('ci', 'org/app#x'), null);
  assert.equal(index.resolver.resolve('host', 'ghost'), null);
  assert.equal(index.resolver.resolve('pr', 'javascript:1#2'), null);
});
