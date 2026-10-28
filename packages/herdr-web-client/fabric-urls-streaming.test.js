import test from 'node:test';
import assert from 'node:assert/strict';
import { renderMarkdown, osc8ToMarkdown, trailingOpenUrl } from '../conversation-markdown.js';
import { candidateUrls } from '../fabric-urls.js';

const FULL = 'http://localhost:3188/benchmarks';
const urls = (spy = []) => ({
  resolve: (u) => { spy.push(u); return u === FULL ? { status: 'resolved', canonical: 'https://dev.lan:3188/benchmarks', original: u }
    : { status: 'unmapped', canonical: null, original: u }; },
});

test('every prefix of a streamed URL renders as plain text, never as an unreachable service', () => {
  const text = `open ${FULL} now`;
  const start = text.indexOf('http');
  for (let i = start + 1; i < start + FULL.length; i++) {
    const html = renderMarkdown(text.slice(0, i), { urls: urls(), openEnd: true });
    assert.ok(!html.includes('md-link-unreachable'), `prefix ${text.slice(0, i)}`);
    assert.ok(!html.includes('dev.lan'), `prefix ${text.slice(0, i)}`);
  }
});

test('once the stream settles the complete URL resolves', () => {
  const html = renderMarkdown(`open ${FULL}`, { urls: urls(), openEnd: false });
  assert.ok(html.includes('href="https://dev.lan:3188/benchmarks"'));
});

test('a complete URL earlier in the message resolves even while the tail is still streaming', () => {
  const html = renderMarkdown(`${FULL} and then http://localhost:31`, { urls: urls(), openEnd: true });
  assert.ok(html.includes('href="https://dev.lan:3188/benchmarks"'));
  assert.ok(!html.includes('md-link-unreachable'));
});

test('a URL followed by more text is complete even while streaming', () => {
  assert.ok(renderMarkdown(`${FULL} next`, { urls: urls(), openEnd: true }).includes('dev.lan:3188'));
});

test('streaming prefetch never asks the server about the unfinished tail', () => {
  assert.deepEqual(candidateUrls('see http://localhost:31', { openEnd: true }), []);
  assert.deepEqual(candidateUrls('see http://localhost:31 ', { openEnd: true }), ['http://localhost:31']);
  assert.deepEqual(candidateUrls('see http://localhost:31'), ['http://localhost:31']);
  assert.equal(trailingOpenUrl('no url here'), null);
});

test('raw logs, commands and code are never linkified or rewritten', () => {
  const html = renderMarkdown(`\`\`\`\n$ curl ${FULL}\nlistening on ${FULL}\n\`\`\`\n\nrun \`curl ${FULL}\``, { urls: urls() });
  assert.ok(!html.includes('dev.lan') && !html.includes('<a '));
  assert.ok(html.includes(`curl ${FULL}`));
});

test('markdown links, bare URLs and OSC 8 terminal hyperlinks resolve the same way', () => {
  const osc = `\u001b]8;;${FULL}\u0007the dashboard\u001b]8;;\u0007`;
  assert.equal(osc8ToMarkdown(osc), `[the dashboard](${FULL})`);
  for (const input of [`[the dashboard](${FULL})`, FULL, osc]) {
    const html = renderMarkdown(input, { urls: urls() });
    assert.ok(html.includes('href="https://dev.lan:3188/benchmarks"'), input);
  }
  assert.ok(renderMarkdown(osc, { urls: urls() }).includes('the dashboard'));
});

test('OSC 8 with an unsafe target or other escapes yields no link and no escape bytes', () => {
  const bad = '\u001b]8;;javascript:alert(1)\u0007click\u001b]8;;\u0007';
  const html = renderMarkdown(bad, { urls: urls() });
  assert.ok(!html.includes('<a ') && !html.includes('\u001b') && html.includes('click'));
  assert.ok(!renderMarkdown('\u001b]0;evil title\u0007hi').includes('\u001b'));
});

test('the original text passed in is never mutated', () => {
  const original = `see ${FULL}`;
  const copy = String(original);
  renderMarkdown(original, { urls: urls() });
  assert.equal(original, copy);
});
