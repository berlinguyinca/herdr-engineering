import test from 'node:test';
import assert from 'node:assert/strict';
import { nodeActions, renderInactive, deleteConfirmed } from './assistant-manage-controls.js';

test('a linked, enabled assistant offers rename, remove from network and archive; an unlinked one no unlink', () => {
  const html = nodeActions({ id: 'a1', enabled: true, parent_id: 'p1' });
  assert.match(html, /data-node-rename="a1"/); assert.match(html, /data-node-unlink="a1"/); assert.match(html, /data-node-archive="a1"/);
  assert.ok(!nodeActions({ id: 'a1', enabled: true }).includes('data-node-unlink'));
  assert.equal(nodeActions({ id: 'a1', enabled: false }), '');
});

test('only inactive assistants are listed, names are escaped, and delete shows its form only when opened', () => {
  const rows = [{ assistant_id: 'a1', name: '<b>Old</b>', enabled: false, type: 'pi-secretary' }, { assistant_id: 'a2', name: 'Live', enabled: true }];
  const closed = renderInactive(rows);
  assert.match(closed, /data-inactive-id="a1"/); assert.ok(!closed.includes('a2')); assert.ok(!closed.includes('<b>Old</b>'));
  assert.ok(!closed.includes('data-delete-form'));
  const open = renderInactive(rows, { openDelete: 'a1', error: 'nope <x>' });
  assert.match(open, /data-delete-form="a1"/); assert.match(open, /data-delete-go" disabled|data-delete-go disabled/);
  assert.ok(!open.includes('<x>'));
  assert.match(renderInactive([{ assistant_id: 'a2', enabled: true }]), /data-inactive-empty/);
});

test('delete is confirmed only by the exact name', () => {
  const a = { name: 'Doomed' };
  assert.equal(deleteConfirmed(a, 'Doomed'), true);
  for (const bad of ['doomed', 'Doomed ', '', undefined]) assert.equal(deleteConfirmed(a, bad), false);
  assert.equal(deleteConfirmed(null, 'x'), false);
});
