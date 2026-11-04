import test from 'node:test';
import assert from 'node:assert/strict';
import { groupAssistants, renderTabs, tabOrder } from './assistant-tabs.js';

const A = [
  { assistant_id: 'p1', name: 'Fleet PA', type: 'rakazo', status: 'ready' },
  { assistant_id: 's1', name: 'Helper', type: 'pi', parent_id: 'p1', status: 'needs_setup' },
  { assistant_id: 'p2', name: 'Mail <b>', type: 'rakazo', status: 'ready' },
  { assistant_id: 's2', name: 'Scout', type: 'scout', status: 'needs_setup' },
  { assistant_id: 's3', name: 'Orphan', type: 'pi', parent_id: 'gone', status: 'ready' },
];

test('each Rakazo assistant leads a group with its linked specialists; the rest are Unlinked', () => {
  const groups = groupAssistants(A);
  assert.deepEqual(groups.map((g) => [g.label, g.items.map((a) => a.assistant_id)]),
    [['Fleet PA', ['p1', 's1']], ['Mail <b>', ['p2']], ['Unlinked', ['s2', 's3']]]);
  assert.deepEqual(tabOrder(groups), ['p1', 's1', 'p2', 's2', 's3']);
});

test('tabs are an accessible tablist with one tab stop, names escaped, and the status on the tab', () => {
  const html = renderTabs(A, 's1', (s) => `status ${s}`);
  assert.match(html, /role="tablist"/);
  assert.equal((html.match(/tabindex="0"/g) || []).length, 1);
  assert.match(html, /data-atab="s1"[^>]*aria-selected="true"/);
  assert.match(html, /data-atab="p1"[^>]*aria-selected="false"/);
  assert.match(html, /role="group" aria-label="Fleet PA"/);
  assert.ok(!html.includes('Mail <b>') && html.includes('Mail &lt;b&gt;'));
  assert.match(html, /title="status needs_setup"/);
});

test('nothing renders without assistants, and bad rows are skipped', () => {
  assert.equal(renderTabs([], null), '');
  assert.equal(renderTabs([null, {}, { name: 'x' }], null), '');
});
