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
// The one allowed share frame: an empty-sandbox iframe whose src is the fixed bundled author page (opt-in Rich view).
// Its exact shape is pinned by tests/control_plane/test_no_frames.py (share_rich_shape_problems), so it is skipped here only.
const ALLOWED = new Set(['rich-toggle.js']);
const TAG = /<\s*(iframe|frame|frameset|object|embed)(?=[\s/>])/i;
const API = /createElement\(\s*['"`](iframe|frameset|frame|object|embed)/i;

function walk(dir, out) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(js|mjs|html|css)$/.test(name)) out.push(p);
  }
  return out;
}

function scannable() {
  return [...PLAN_FILES.map((f) => join(here, f)), ...walk(shareStatic, [])];     // share_static includes vendor/pdfjs
}

test('the plan files and share_static contain no frame, object or embed markup or creation', () => {
  const files = scannable();
  assert.ok(files.length > 8);
  assert.ok(files.some((f) => f.includes('vendor')), 'the vendored pdf.js is scanned too');
  for (const file of files) {
    if (ALLOWED.has(file.split('/').pop()) && file.includes('share_static')) continue;
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
