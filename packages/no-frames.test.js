// No frames, objects or embeds in the member UI or the share viewer scripts (operator Decision 1).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const shareStatic = join(here, '..', 'herdr_engineering', 'control_plane', 'share_static');
const PLAN_FILES = ['member-artifacts.html', 'member-artifacts-app.js', 'member-artifacts-view.js', 'member-api.js',
  'member-upload.js', 'sha256-stream.js'];
const TAG = /<\s*(iframe|frame|frameset|object|embed)(?=[\s/>])/i;
const API = /createElement\(\s*['"`](iframe|frame|object|embed)/i;

function scannable() {
  const files = PLAN_FILES.map((f) => join(here, f));
  for (const name of readdirSync(shareStatic)) {
    const p = join(shareStatic, name);
    if (statSync(p).isFile() && /\.(js|html|css)$/.test(name)) files.push(p);
  }
  return files;
}

test('the plan files and share_static contain no frame, object or embed markup or creation', () => {
  const files = scannable();
  assert.ok(files.length > 8);
  for (const file of files) {
    const text = readFileSync(file, 'utf8');
    assert.doesNotMatch(text, TAG, file);
    assert.doesNotMatch(text, API, file);
  }
});

test('the scanner itself catches the hostile forms', () => {
  assert.match('<IFRAME src=x>', TAG);
  assert.match('<embed\nsrc=x>', TAG);
  assert.match("document.createElement('iframe')", API);
  assert.match('document.createElement( "OBJECT" )', API);
  assert.doesNotMatch('the iframe element is banned', TAG);
});
