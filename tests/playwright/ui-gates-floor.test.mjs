// The focus-audit floors in ui-gates-states.test.mjs may only go up. This reads the source text (no browser), so it runs anywhere;
// the numbers it protects are what the audit counted, and only the ui-gates CI run proves the page still reaches them.
// Run: node --test tests/playwright/ui-gates-floor.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('./ui-gates-states.test.mjs', import.meta.url), 'utf8');
const m = /const FLOOR = \{ owner: \{ controls: (\d+), focused: (\d+) \}, viewer: \{ controls: (\d+), focused: (\d+) \} \};/.exec(src);

test('the focus-audit floor line can be parsed', () => assert.ok(m, 'FLOOR line changed shape: update this test with it, keeping the minimums'));

test('no focus-audit floor is below its recorded minimum', () => {
  const [oc, of, vc, vf] = m.slice(1).map(Number);
  assert.ok(oc >= 540, `owner controls floor ${oc} < 540`);
  assert.ok(of >= 480, `owner focused floor ${of} < 480`);
  assert.ok(vc >= 37, `viewer controls floor ${vc} < 37`);
  assert.ok(vf >= 30, `viewer focused floor ${vf} < 30`);
});
