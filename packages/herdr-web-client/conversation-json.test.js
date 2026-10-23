import test from 'node:test';
import assert from 'node:assert/strict';
import { renderJson, parseJson } from '../conversation-json.js';

test('JSON renders hostile keys and values as escaped collapsible data', () => {
  const html = renderJson('{"__proto__":{"<script>":"<img src=x onerror=alert(1)>"},"ok":true,"nothing":null}');
  for (const part of ['<details', '__proto__', '&lt;script&gt;', '&lt;img', 'conversation-json-boolean', 'conversation-json-null']) assert.ok(html.includes(part), part);
  assert.ok(!html.includes('<img'));
});
test('JSON parser reports invalid input and bounds huge and deep output', () => {
  assert.equal(parseJson('{bad').ok, false);
  assert.equal(parseJson(' '.repeat(200001)).ok, false);
  let deep = 0; for (let i = 0; i < 100; i++) deep = { child: deep };
  assert.ok(renderJson(deep).includes('Display limit'));
  assert.ok(renderJson(Array.from({length: 20000}, (_, i) => i)).length < 150000);
  assert.ok(renderJson('{bad').includes('Invalid JSON'));
});
