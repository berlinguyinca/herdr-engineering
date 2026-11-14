// Guard: every explicit wait budget in the UI suite must go through scaled() so it stretches with host load
// (ui-gates-lib loadScale). A bare `timeout: 5000` is what turned a loaded CI box into a red build.
import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';

const FILES = ['ui.test.mjs'];

test('ui.test.mjs has no unscaled numeric timeouts', () => {
  const bare = [];
  for (const f of FILES) {
    fs.readFileSync(path.join(import.meta.dirname, f), 'utf8').split('\n').forEach((line, i) => {
      if (/timeout: ?\d/.test(line)) bare.push(`${f}:${i + 1}: ${line.trim().slice(0, 120)}`);
    });
  }
  assert.deepEqual(bare, [], `wrap these in scaled(): \n${bare.join('\n')}`);
});
