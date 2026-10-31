import test from 'node:test';
import assert from 'node:assert/strict';

import {mountSupervisionCatalog, selectCatalogRooms} from '../supervision-catalog.js';

const projection = room => ({room_id: room, workspace_id: `workspace-${room}`, jobs: [], unassigned: []});

function fixture(responses, options = {}) {
  const elements = {
    '[data-supervision-room]': {value: '', innerHTML: '', disabled: false},
    '[data-supervision-status]': {textContent: '', dataset: {}},
    '[data-supervision-tree]': {innerHTML: ''},
  };
  const listeners = new Map();
  const container = {
    innerHTML: '',
    querySelector: selector => elements[selector] || null,
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: name => listeners.delete(name),
    fire(name, target) { listeners.get(name)?.({target}); },
  };
  const documentListeners = new Map();
  const documentImpl = {
    hidden: false,
    addEventListener: (name, handler) => documentListeners.set(name, handler),
    removeEventListener: name => documentListeners.delete(name),
  };
  const calls = [];
  const client = {request: async (path, request) => {
    calls.push([path, request]);
    const next = responses.shift();
    if (next instanceof Error) throw next;
    return structuredClone(next);
  }};
  const timers = [];
  const schedule = fn => { timers.push(fn); return timers.length; };
  const cancelSchedule = () => {};
  const trees = [];
  const treeMount = (host, data, state) => {
    const tree = {host, data, initialState: state, disposed: false, updates: [],
      update(next) { this.data = next; this.updates.push(next); },
      state() { return this.saved || {expanded: new Set([`open-${data.room_id}`]), selectedId: `agent-${data.room_id}`, query: ''}; },
      dispose() { this.disposed = true; },
    };
    trees.push(tree); return tree;
  };
  const catalog = mountSupervisionCatalog(container, client, {
    documentImpl, schedule, cancelSchedule, treeMount, pollMs: 7000, ...options,
  });
  return {catalog, container, elements, listeners, documentImpl, documentListeners, calls, timers, trees};
}

test('room selection includes authorized non-guests only and escapes labels', () => {
  assert.deepEqual(selectCatalogRooms({rooms: [
    {room_id: 'r2', name: '<b>Two</b>', role: 'viewer'},
    {room_id: 'r1', name: 'One', role: 'owner'},
    {room_id: 'r3', name: 'Guest', role: 'guest'},
    {room_id: 'r1', name: 'Duplicate', role: 'owner'},
    {room_id: 3, name: 'Bad', role: 'owner'},
  ]}).map(room => room.room_id).sort(), ['r1', 'r2']);
});

test('loads rooms then only the selected authorized room supervision', async () => {
  const f = fixture([
    {rooms: [{room_id: 'r1', name: '<Room>', role: 'owner'}, {room_id: 'guest', name: 'Guest', role: 'guest'}]},
    projection('r1'),
  ], {title: '<Trees>'});
  await f.catalog.ready;
  assert.deepEqual(f.calls.map(call => call[0]), ['/api/v1/collab/rooms', '/api/v1/collab/rooms/r1/supervision']);
  assert.match(f.container.innerHTML, /&lt;Trees&gt;/);
  assert.match(f.container.innerHTML, /Workspace\/room → job tabs → agents/);
  assert.match(f.elements['[data-supervision-room]'].innerHTML, /&lt;Room&gt;/);
  assert.equal(f.trees[0].data.room_id, 'r1');
  assert.equal(f.elements['[data-supervision-status]'].dataset.state, 'ready');
  assert.equal(f.timers.length, 1);
});

test('empty and room-list error states clear any tree data', async () => {
  const empty = fixture([{rooms: []}]);
  await empty.catalog.ready;
  assert.equal(empty.elements['[data-supervision-status]'].dataset.state, 'empty');
  assert.equal(empty.elements['[data-supervision-tree]'].innerHTML, '');

  const failed = fixture([new Error('<private>')]);
  await failed.catalog.ready;
  assert.equal(failed.elements['[data-supervision-status]'].dataset.state, 'error');
  assert.equal(failed.elements['[data-supervision-status]'].textContent, 'Could not load collaboration rooms.');
  assert.doesNotMatch(failed.container.innerHTML, /private/);
});

test('switching rooms preserves separate tree state without cross-room data', async () => {
  const f = fixture([
    {rooms: [{room_id: 'r1', name: 'One', role: 'owner'}, {room_id: 'r2', name: 'Two', role: 'viewer'}]},
    projection('r1'), projection('r2'), projection('r1'),
  ]);
  await f.catalog.ready;
  f.elements['[data-supervision-room]'].value = 'r2';
  f.container.fire('change', f.elements['[data-supervision-room]']);
  await f.catalog.settled();
  assert.equal(f.trees[0].disposed, true);
  assert.equal(f.trees[1].data.room_id, 'r2');
  assert.equal(f.trees[1].initialState.selectedId, undefined);
  f.elements['[data-supervision-room]'].value = 'r1';
  f.container.fire('change', f.elements['[data-supervision-room]']);
  await f.catalog.settled();
  assert.equal(f.trees[2].data.room_id, 'r1');
  assert.equal(f.trees[2].initialState.selectedId, 'agent-r1');
});

test('a supervision error disposes and clears stale prior room data and view state', async () => {
  const f = fixture([{rooms: [{room_id: 'r1', name: 'One', role: 'owner'}]}, projection('r1'), new Error('denied'), projection('r1')]);
  await f.catalog.ready;
  f.elements['[data-supervision-tree]'].innerHTML = 'stale';
  await f.catalog.refresh();
  assert.equal(f.trees[0].disposed, true);
  assert.equal(f.elements['[data-supervision-tree]'].innerHTML, '');
  assert.equal(f.elements['[data-supervision-status]'].dataset.state, 'error');
  await f.catalog.refresh();
  assert.equal(f.trees[1].initialState.selectedId, undefined);
});

test('polling is visible-only and dispose aborts work and unbinds', async () => {
  const f = fixture([{rooms: [{room_id: 'r1', name: 'One', role: 'owner'}]}, projection('r1')]);
  await f.catalog.ready;
  f.documentImpl.hidden = true;
  f.documentListeners.get('visibilitychange')();
  const before = f.calls.length;
  f.timers.at(-1)?.();
  await Promise.resolve();
  assert.equal(f.calls.length, before);
  f.catalog.dispose();
  assert.equal(f.listeners.size, 0);
  assert.equal(f.documentListeners.size, 0);
  assert.equal(f.trees[0].disposed, true);
});
