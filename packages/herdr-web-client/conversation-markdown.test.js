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
  assert.ok(html.includes('rel="noopener noreferrer"'));
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
