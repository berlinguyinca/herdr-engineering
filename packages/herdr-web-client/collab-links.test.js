import test from 'node:test';
import assert from 'node:assert/strict';
import { grantSummary, renderLinks } from '../collab-links.js';

test('links render escaped, with the primary marker and without a terminal control', () => {
  const html = renderLinks([
    { workspace_id: '<img onerror=x>', host: 'bender', is_primary: true, grants: ['status', 'tabs'], can_terminal: false },
    { workspace_id: 'ws-2', host: 'beast', is_primary: false, grants: [], can_terminal: true },
  ], [{ mission_id: 'm-<1>' }]);
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img onerror=x&gt;') && html.includes('m-&lt;1&gt;'));
  assert.ok(html.includes('Primary room') && html.includes('You may open the terminal'));
  assert.ok(html.includes('nothing shared'));
  assert.ok(!/<button|<a /.test(html), 'read-only: nothing to click, no terminal link');
});

test('missing links (guest or agent) say so, empty lists have a message', () => {
  assert.match(renderLinks(null, null), /not shown to your role/);
  const html = renderLinks([], []);
  assert.match(html, /No workspaces are linked/);
  assert.match(html, /No missions are linked/);
  assert.equal(grantSummary(['terminal']), 'terminal (named people only)');
});
