import test from 'node:test';
import assert from 'node:assert/strict';
import {compactSessionRow, runtimeLine} from '../../packages/sessions-view.js';

test('runtime line names the runtime, engine and fallback', () => {
  assert.equal(runtimeLine({runtime_kind: 'pi-bolt', js_engine: 'bun', js_engine_version: '1.4.3'}), 'Pi-Bolt · bun 1.4.3');
  assert.match(runtimeLine({runtime_kind: 'standard-pi', fallback_reason: 'binary changed'}), /fell back: binary changed/);
});

test('unknown or missing runtime renders nothing', () => {
  assert.equal(runtimeLine({runtime_kind: 'evil'}), '');
  assert.equal(runtimeLine(null), '');
});

test('session row shows the runtime and escapes reported text', () => {
  const html = compactSessionRow({id: 's1', role: 'R', project: 'p', host: 'h',
    record: {session_id: 's1', runtime: {runtime_kind: 'pi-bolt', fallback_reason: '<img onerror=x>'}}});
  assert.ok(html.includes('data-runtime'));
  assert.ok(!html.includes('<img'));
});
