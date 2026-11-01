import test from 'node:test';
import assert from 'node:assert/strict';
import { createCommand, createBody, loadTypes, parentChoices, renderCreateForm } from './assistant-create-form.js';

const TYPES = [{ type: 'rakazo', kind: 'assistant', can_be_parent: true },
  { type: 'pi-secretary', kind: 'specialist', requires: ['internet'], resources: { cpus: 2 } }];
const ASSISTANTS = [{ assistant_id: 'a1', name: 'Fleet PA', enabled: true, type: 'rakazo' },
  { assistant_id: 'a2', name: 'Old', enabled: false, type: 'rakazo' },
  { assistant_id: 'a3', name: 'Spec', enabled: true, type: 'pi-secretary' }];

test('the command quotes what a person typed', () => {
  assert.equal(createCommand({ name: "O'Neil; rm -rf /", type: 'rakazo' }),
    `herdr-eng assistant create 'O'\\''Neil; rm -rf /' --type 'rakazo'`);
  assert.equal(createCommand({ name: 'S', type: 'pi-secretary', parent: 'Fleet PA' }),
    `herdr-eng assistant create 'S' --type 'pi-secretary' --parent 'Fleet PA'`);
});

test('only enabled rakazo assistants can be parents', () => {
  assert.deepEqual(parentChoices(ASSISTANTS).map(a => a.assistant_id), ['a1']);
});

test('a specialist shows a parent picker, what it needs, and an escaped command; rakazo shows no picker', () => {
  const html = renderCreateForm({ types: TYPES, assistants: ASSISTANTS, values: { name: '<b>x</b>', type: 'pi-secretary', parent_id: 'a1' } });
  assert.match(html, /data-create-parent/);
  assert.match(html, /internet, cpus 2/);
  assert.match(html, /--parent &#39;Fleet PA&#39;|--parent &#x27;Fleet PA&#x27;|--parent 'Fleet PA'/);
  assert.ok(!html.includes('<b>x</b>'));
  assert.ok(!renderCreateForm({ types: TYPES, assistants: ASSISTANTS }).includes('data-create-parent'));
});

test('the body matches the CLI fields and omits an empty parent', () => {
  assert.deepEqual(createBody({ name: ' A ', type: 'pi-secretary', parent_id: 'a1' }), { name: 'A', type: 'pi-secretary', parent_id: 'a1' });
  assert.deepEqual(createBody({ name: '', type: '' }), { name: 'Personal Assistant', type: 'rakazo' });
});

test('an idempotency key rides along only when given', () => {
  assert.equal(createBody({ name: 'A', type: 'rakazo', idempotency_key: 'k1' }).idempotency_key, 'k1');
  assert.ok(!('idempotency_key' in createBody({ name: 'A', type: 'rakazo' })));
});

test('types fall back to rakazo when the server is unreachable', async () => {
  const out = await loadTypes({ assistantTypes: async () => { throw new Error('boom'); } });
  assert.deepEqual(out.types.map(t => t.type), ['rakazo']);
  assert.equal(out.error, 'boom');
});
