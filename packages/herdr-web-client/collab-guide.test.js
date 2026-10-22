import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { mdToHtml } from '../collab-guide.js';

test('headings, lists, tables and code render; everything is escaped', () => {
  const html = mdToHtml('# Title\n\nSome **bold** and `a<b>` text <script>alert(1)</script>\n\n* one\n* two\n  more\n\n| A | B |\n|---|---|\n| 1 | `x` |\n\n```bash\ncurl <x>\n```\n');
  assert.match(html, /<h1 id="title">Title<\/h1>/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<code>a&lt;b&gt;<\/code>/);
  assert.ok(!html.includes('<script>'));
  assert.match(html, /<li>two more<\/li>/);
  assert.match(html, /<th>A<\/th>/);
  assert.match(html, /<pre><code>curl &lt;x&gt;<\/code><\/pre>/);
});

test('the shipped guide renders and names no private infrastructure', () => {
  const md = fs.readFileSync(new URL('../collab-guide.md', import.meta.url), 'utf8');
  const html = mdToHtml(md);
  assert.ok(html.includes('<h2'));
  for (const bad of ['100.104', 'tailnet', '192.168', 'journalctl', 'systemctl', 'bender', 'cubert']) assert.ok(!md.includes(bad), bad);
});
