// Pure tests of the annotation view's anchor maths and fixed messages (no browser): share_static/anchor.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  LIMITS, annotationUrls, cleanRow, cleanText, clipHead, clipTail, describeAnchor, errorMessage, findQuote, isRawHref,
  statusLabel, textQuote, textQuoteFromString, unitBox,
} from '../../herdr_engineering/control_plane/share_static/anchor.js';

const ID = 'ann_' + 'A'.repeat(22);

test('cleanText: LF only, no control characters but tab, no lone surrogate', () => {
  assert.equal(cleanText('a\r\nb\rc\nd'), 'a\nb\nc\nd');
  assert.equal(cleanText('a\u0000b\u0007c\u007fd\u000be\tf'), 'a b c d e\tf');
  assert.equal(cleanText('x\ud83dy'), 'x�y');
  assert.equal(cleanText('x\ude00y'), 'x�y');
  assert.equal(cleanText('ok 😀 שלום'), 'ok 😀 שלום');
  assert.ok(!/[\r\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(cleanText('\r\n\u0001\u001f\u007f')));
});

test('clipHead and clipTail never split a surrogate pair', () => {
  const smile = '😀';
  assert.equal(clipHead('ab' + smile, 3), 'ab');
  assert.equal(clipHead('ab' + smile, 4), 'ab' + smile);
  assert.equal(clipTail(smile + 'ab', 3), 'ab');
  assert.equal(clipTail(smile + 'ab', 4), smile + 'ab');
  assert.equal(clipHead('abc', 10), 'abc');
});

test('textQuote: trimmed exact with 64 characters of context each side', () => {
  const full = 'x'.repeat(100) + '  The quick fox.  ' + 'y'.repeat(100);
  const start = 100;
  const end = full.length - 100;
  const quote = textQuote(full, start, end);
  assert.equal(quote.exact, 'The quick fox.');
  assert.equal(quote.prefix, 'x'.repeat(62) + '  ');
  assert.equal(quote.prefix.length, 64);
  assert.equal(quote.suffix, '  ' + 'y'.repeat(62));
  assert.deepEqual(textQuote(full, 5, 5), null);
  assert.deepEqual(textQuote('   \n\t  ', 0, 7), null);
  assert.deepEqual(textQuote(full, end, start), textQuote(full, start, end), 'a backwards selection is the same quote');
});

test('textQuote: clips a huge selection to 1000 characters and the context follows the clip', () => {
  const full = 'a'.repeat(50) + 'b'.repeat(5000) + 'c'.repeat(50);
  const quote = textQuote(full, 50, 5050);
  assert.equal(quote.exact.length, LIMITS.exact);
  assert.equal(quote.suffix, 'b'.repeat(64), 'the suffix is what follows the clipped exact');
  assert.equal(quote.prefix, 'a'.repeat(50));
});

test('textQuote: emoji and RTL survive; CRLF becomes LF; the quote is server-safe', () => {
  const full = 'pre 🎉😀 مرحبا line1\r\nline2 post';
  const quote = textQuote(full, 4, full.length - 5);
  assert.ok(quote.exact.includes('🎉😀'));
  assert.ok(quote.exact.includes('مرحبا'));
  assert.ok(quote.exact.includes('line1\nline2'));
  for (const text of [quote.exact, quote.prefix, quote.suffix]) assert.ok(!text.includes('\r'));
  const cut = textQuote('😀'.repeat(600), 0, 1200);
  assert.ok(cut.exact.length <= LIMITS.exact);
  assert.equal(cut.exact, cleanText(cut.exact), 'no lone surrogate after clipping');
});

test('textQuoteFromString finds the first occurrence', () => {
  const quote = textQuoteFromString('one two three two', ' two ');
  assert.equal(quote.exact, 'two');
  assert.equal(quote.prefix, 'one ');
  assert.equal(textQuoteFromString('abc', 'zzz'), null);
  assert.equal(textQuoteFromString('abc', '  '), null);
});

test('findQuote prefers the occurrence whose context matches', () => {
  const full = 'cat sat. dog sat. cow sat.';
  assert.equal(findQuote(full, { exact: 'sat', prefix: 'dog ', suffix: '. cow' }), full.indexOf('sat', 9));
  assert.equal(findQuote(full, { exact: 'sat', prefix: 'cow ', suffix: '.' }), full.lastIndexOf('sat'));
  assert.equal(findQuote(full, { exact: 'missing' }), -1);
  assert.equal(findQuote(full, { exact: '' }), -1);
  assert.equal(findQuote(full, null), -1);
});

test('unitBox: normalised to [0, 1], x+w and y+h never above 1, tiny drags refused', () => {
  assert.deepEqual(unitBox(10, 20, 110, 70, 200, 100), { x: 0.05, y: 0.2, w: 0.5, h: 0.5 });
  assert.deepEqual(unitBox(110, 70, 10, 20, 200, 100), { x: 0.05, y: 0.2, w: 0.5, h: 0.5 }, 'drag direction does not matter');
  const out = unitBox(-50, -50, 500, 500, 200, 100);
  assert.deepEqual(out, { x: 0, y: 0, w: 1, h: 1 }, 'a drag outside the image is clamped to it');
  assert.equal(unitBox(10, 10, 10.2, 90, 200, 100), null);
  assert.equal(unitBox(0, 0, 10, 10, 0, 0), null);
  assert.equal(unitBox(NaN, 0, 10, 10, 100, 100), null);
  for (let i = 0; i < 2000; i += 1) {
    const [w, h] = [1 + Math.random() * 3000, 1 + Math.random() * 3000];
    const box = unitBox(Math.random() * w, Math.random() * h, Math.random() * w, Math.random() * h, w, h);
    if (!box) continue;
    for (const key of ['x', 'y', 'w', 'h']) assert.ok(box[key] >= 0 && box[key] <= 1, key);
    assert.ok(box.x + box.w <= 1 && box.y + box.h <= 1, JSON.stringify(box));
  }
});

test('describeAnchor and statusLabel give plain strings only', () => {
  assert.deepEqual(describeAnchor({ kind: 'text_quote', exact: '<b>x</b>' }, 'f.html'), { title: 'Selected text', quote: '<b>x</b>' });
  assert.equal(describeAnchor({ kind: 'pdf_page', page: 3, exact: 'q' }, 'a.pdf').title, 'Page 3');
  assert.match(describeAnchor({ kind: 'pdf_page', page: 2, region: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } }, 'a.pdf').title, /region x 10%/);
  assert.match(describeAnchor({ kind: 'image_region', x: 0.1, y: 0.1, w: 0.5, h: 0.5 }, 'p.png').title, /Image region/);
  assert.equal(describeAnchor(null, 'f.bin').title, 'Whole file: f.bin');
  assert.equal(describeAnchor({ kind: 'text_quote', exact: 5 }, 'f').title, 'Whole file: f');
  assert.equal(statusLabel({ status: 'open' }), 'Open');
  assert.equal(statusLabel({ status: 'addressed', addressed_in_version_number: 4 }), 'Addressed in v4');
  assert.equal(statusLabel({ status: 'dismissed' }), 'Dismissed');
  assert.equal(statusLabel({ status: 'submitted' }), 'Submitted');
  assert.equal(statusLabel({ status: 'edited' }), 'Edited');
  assert.equal(statusLabel({ status: '<script>' }), 'Unknown');
});

test('errorMessage is fixed text per outcome and never echoes the server', () => {
  assert.match(errorMessage(429, 'rate_limited'), /Too many annotations/);
  assert.match(errorMessage(503, 'unavailable'), /unavailable right now/);
  assert.match(errorMessage(404, ''), /session ended/);
  assert.equal(errorMessage(409, 'not_editable', false), 'This annotation can no longer be changed');
  assert.match(errorMessage(409, 'not_editable', true), /Still saving/);
  assert.equal(errorMessage(400, '<img src=x onerror=1>'), 'The server did not accept that.');
  assert.match(errorMessage(400, 'empty'), /Write a comment/);
  assert.match(errorMessage(0, ''), /Could not reach/);
  assert.match(errorMessage(500, ''), /Something went wrong/);
  assert.equal(errorMessage(400, 'constructor'), 'The server did not accept that.');
});

test('isRawHref accepts only our relative raw-file paths', () => {
  assert.ok(isRawHref('../../f/sub/a.png'));
  assert.ok(isRawHref('../f/a%20b.pdf'));
  for (const bad of ['javascript:alert(1)', 'https://evil/f/x', '//evil/f/x', '/f/x', '../x/y', '../f/x?y', '../f/x#y', '../f\\x', '', null, 5]) assert.ok(!isRawHref(bad), String(bad));
});

test('annotationUrls come from the page path and validate ids', () => {
  const token = 'T'.repeat(43);
  const urls = annotationUrls(`/s/${token}/k/abc.def-1_2/a/sub/x.html`);
  assert.equal(urls.list, `/s/${token}/k/abc.def-1_2/annotations`);
  assert.equal(urls.create, urls.list);
  assert.equal(urls.edit(ID), `${urls.list}/${ID}/edit`);
  assert.equal(urls.remove(ID), `${urls.list}/${ID}/delete`);
  assert.equal(urls.edit('../../x'), null);
  assert.equal(urls.remove('ann_short'), null);
  for (const bad of ['/', '/s/short/k/x/a/y', `/s/${token}/k/x/f/y`, `/s/${token}/k/x/a/`, `/s/${token}/k/a b/a/y`, `/x/${token}/k/x/a/y`]) assert.equal(annotationUrls(bad), null, bad);
});

test('cleanRow keeps projection rows and drops anything else', () => {
  assert.equal(cleanRow({ annotation_id: ID, body: 'b', status: 'open', file: 'f', anchor: { kind: 'document' } }).status, 'open');
  assert.equal(cleanRow({ annotation_id: ID, body: 'b', status: 'open' }).anchor, null);
  for (const bad of [null, 5, {}, { annotation_id: 'x', body: 'b', status: 'open' }, { annotation_id: ID, body: 5, status: 'open' }, { annotation_id: ID, body: 'b' }]) assert.equal(cleanRow(bad), null);
});
