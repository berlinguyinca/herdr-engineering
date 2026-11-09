import test from 'node:test';
import assert from 'node:assert/strict';
import { renderSettings } from './settings-view.js';

const row = (html, capability) => html.split('<li ').find((chunk) => chunk.includes(`data-service="${capability}"`)) || '';

test('the page lists every service with its state and the config key for the off ones', () => {
  const html = renderSettings(['share_v1', 'collab_v1']);
  assert.match(row(html, 'share_v1'), /data-state="on"/);
  assert.doesNotMatch(row(html, 'share_v1'), /control_plane\.share\.enabled/);
  assert.match(row(html, 'members_v1'), /data-state="off"/);
  assert.match(row(html, 'members_v1'), /control_plane\.collab\.members\.enabled/);
});

test('with no capabilities every optional service is off and nothing links elsewhere', () => {
  const html = renderSettings([]);
  assert.doesNotMatch(html, /<a /);
  assert.match(row(html, 'share_v1'), /data-state="off"/);
  assert.match(row(html, 'share_v1'), /control_plane\.share\.enabled/);
});

test('a probe failure is stated instead of listing everything as off', () => {
  assert.match(renderSettings(null), /Could not read which services are enabled/);
});

test('the page points operators at the settings CLI', () => {
  assert.match(renderSettings([]), /herdr-eng settings enable/);
});
