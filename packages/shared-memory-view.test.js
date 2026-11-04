import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mountSharedMemory, memoryContent } from './shared-memory-view.js';

test('known Capture summary is readable; other JSON remains verbatim', () => {
  const content = JSON.stringify({ labels: ['derived', 'extractive', 'unreviewed'], summary: 'A summary', provenance: { session_id: 's' } });
  assert.equal(memoryContent(content), 'A summary');
  assert.equal(memoryContent('{"summary":"opaque"}'), '{"summary":"opaque"}');
  assert.equal(memoryContent('plain <note>'), 'plain <note>');
});

function host() {
  const listeners = {};
  return { innerHTML: '', addEventListener: (name, fn) => { listeners[name] = fn; }, removeEventListener: name => { delete listeners[name]; },
    fire(name, target) { listeners[name]?.({ target, preventDefault() {} }); } };
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const domains = [{ alias: '<project>', domain_type: 'project', domain_id: 'p', bank_ids: { fact: 'pf', summary: 'ps' }, counts: { active: 1, deleted: 0, pending: 1 } },
  { alias: 'personal', domain_type: 'personal', domain_id: 'me', bank_ids: { fact: 'uf', summary: 'us' }, status: { active: 0, deleted: 0, pending: 0 } }];
function select(el, value) { el.fire('change', { dataset: { domain: 'project' }, value }); }

test('registry does not select or merge banks; text is escaped and canonical excerpt rendered', async () => {
  const el = host(), calls = [];
  const view = mountSharedMemory(el, { listMemoryDomains: async () => ({ domains }), listMemoryDocuments: async (bank, opts) => {
    calls.push([bank, opts.offset]); return { documents: [{ document_id: '<id>', text: '<note>', revision: 2, metadata: { actor: '<actor>' } }], total: 1 };
  } });
  await flush(); assert.deepEqual(calls, []); assert.match(el.innerHTML, /Personal domain/); assert.match(el.innerHTML, /&lt;project&gt;/);
  select(el, '<project>'); await flush();
  assert.deepEqual(calls, [['pf', 0]]); assert.match(el.innerHTML, /&lt;note&gt;/); assert.doesNotMatch(el.innerHTML, /<note>/); assert.match(el.innerHTML, /&lt;actor&gt;/);
  view.dispose();
});

test('late responses cannot render across domain changes or disposal; previous request is aborted', async () => {
  const el = host(), pending = [];
  const view = mountSharedMemory(el, { listMemoryDomains: async () => ({ domains }), listMemoryDocuments: (bank, { signal }) => new Promise(resolve => pending.push({ bank, signal, resolve })) });
  await flush(); select(el, '<project>'); select(el, 'personal');
  assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve({ items: [{ document_id: 'mine', content: 'personal note' }], total: 1 }); await flush();
  pending[0].resolve({ items: [{ document_id: 'foreign', content: 'foreign note' }], total: 1 }); await flush();
  assert.match(el.innerHTML, /personal note/); assert.doesNotMatch(el.innerHTML, /foreign note/);
  view.refresh(); view.dispose(); const before = el.innerHTML;
  pending[2].resolve({ items: [{ content: 'after disposal' }], total: 1 }); await flush();
  assert.equal(el.innerHTML, before); assert.equal(pending[2].signal.aborted, true);
});

test('only registry 404 invokes legacy fallback; empty or forbidden registry does not', async () => {
  for (const status of [404, 403, 503, 0]) {
    let fallback = 0; const el = host();
    const view = mountSharedMemory(el, { listMemoryDomains: async () => { if (status) throw Object.assign(new Error('denied'), { status }); return { domains: [] }; } }, { onUnavailable: () => { fallback++; } });
    await flush(); assert.equal(fallback, status === 404 ? 1 : 0); view.dispose();
  }
});

const action = (el, value) => el.fire('click', { dataset: { action: value } });
const field = (el, name, value) => el.fire('input', { dataset: { field: name }, value });
const submit = (el, name) => el.fire('submit', { dataset: { form: name } });
const openNote = el => el.fire('click', { dataset: { open: '0' } });

test('pagination and search stay in selected bank; summary switches banks and resets pagination', async () => {
  const el = host(), calls = [];
  const view = mountSharedMemory(el, { listMemoryDomains: async () => ({ domains }),
    listMemoryDocuments: async (bank, opts) => { calls.push(['list', bank, opts.offset]); return { items: Array.from({ length: 20 }, (_, n) => ({ document_id: `n${n}`, content: 'note' })), total: 45 }; },
    recallMemoryDocuments: async (bank, query, opts) => { calls.push(['search', bank, query, opts.limit]); return { results: [{ document_id: 'found', text: 'found note', metadata: { revision: '8' } }] }; },
  });
  await flush(); select(el, '<project>'); await flush(); action(el, 'next'); await flush();
  assert.deepEqual(calls.at(-1), ['list', 'pf', 20]);
  field(el, 'query', 'tea'); submit(el, 'search'); await flush();
  assert.deepEqual(calls.at(-1), ['search', 'pf', 'tea', 100]); assert.match(el.innerHTML, /found note/);
  action(el, 'summary'); await flush(); assert.deepEqual(calls.at(-1), ['list', 'ps', 0]); view.dispose();
});

test('correction uses full document current revision and preserves references; delete receipt remains visible until refreshed', async () => {
  const el = host(), calls = []; let deleted = false;
  const document = { document_id: 'note/a', content: 'full note', revision: 7, metadata: { actor: 'origin', source_ref: 'source', evidence: ['e'] } };
  const view = mountSharedMemory(el, { listMemoryDomains: async () => ({ domains }),
    listMemoryDocuments: async () => ({ documents: deleted ? [] : [{ ...document, text: 'excerpt', revision: 3 }], total: deleted ? 0 : 1 }),
    getMemoryDocument: async (bank, id) => { calls.push(['get', bank, id]); return document; },
    getMemoryHistory: async () => ({ history: [{ revision: 7, operation: 'RETAIN' }] }),
    saveMemoryDocument: async (bank, body) => { calls.push(['save', bank, body]); return { status: 'pending', revision: 8, operation_id: 'retain' }; },
    deleteMemoryDocument: async (bank, id, opts) => { deleted = true; calls.push(['delete', bank, id, opts.base_revision]); return { status: 'pending', revision: 9, operation_id: 'delete' }; },
    getMemoryOperation: async (bank, op) => { calls.push(['operation', bank, op]); return { status: 'completed', revision: 9, operation_id: op }; },
  });
  await flush(); select(el, '<project>'); await flush(); openNote(el); await flush();
  assert.match(el.innerHTML, /full note/); assert.match(el.innerHTML, /Revision 7/);
  field(el, 'correction', 'corrected'); submit(el, 'correct'); await flush();
  assert.deepEqual(calls.find(c => c[0] === 'save'), ['save', 'pf', { document_id: 'note/a', content: 'corrected', base_revision: 7, metadata: { source_ref: 'source', evidence: ['e'] } }]);
  assert.match(el.innerHTML, /index pending/);
  openNote(el); await flush(); action(el, 'forget'); await flush();
  assert.deepEqual(calls.find(c => c[0] === 'delete'), ['delete', 'pf', 'note/a', 7]);
  assert.match(el.innerHTML, /Delete pending/); assert.doesNotMatch(el.innerHTML, /deletion completed/);
  await view.refresh(); assert.match(el.innerHTML, /deletion completed/); view.dispose();
});

test('new explicit save uses create revision; conflicts preserve draft without retrying or claiming success', async () => {
  const el = host(), calls = [];
  const view = mountSharedMemory(el, { listMemoryDomains: async () => ({ domains }), listMemoryDocuments: async () => ({ items: [], total: 0 }),
    saveMemoryDocument: async (bank, body) => { calls.push([bank, body]); throw Object.assign(new Error('conflict'), { status: 409 }); },
  });
  await flush(); select(el, '<project>'); await flush();
  field(el, 'newId', 'new/id'); field(el, 'draft', '<draft>'); submit(el, 'save'); await flush();
  assert.deepEqual(calls, [['pf', { document_id: 'new/id', content: '<draft>', base_revision: 0 }]]);
  assert.match(el.innerHTML, /Revision conflict/); assert.match(el.innerHTML, /&lt;draft&gt;/); assert.doesNotMatch(el.innerHTML, /index pending/); view.dispose();
});

test('late detail and mutation receipts cannot enter another domain', async () => {
  const el = host(); let resolveDetail, resolveSave;
  const view = mountSharedMemory(el, { listMemoryDomains: async () => ({ domains }), listMemoryDocuments: async () => ({ items: [{ document_id: 'one', text: 'excerpt' }], total: 1 }),
    getMemoryDocument: () => new Promise(resolve => { resolveDetail = resolve; }), getMemoryHistory: async () => ({ history: [] }),
    saveMemoryDocument: () => new Promise(resolve => { resolveSave = resolve; }),
  });
  await flush(); select(el, '<project>'); await flush(); openNote(el); select(el, 'personal'); await flush();
  resolveDetail({ document_id: 'foreign', content: 'foreign detail', revision: 6 }); await flush();
  assert.doesNotMatch(el.innerHTML, /foreign detail/);
  field(el, 'newId', 'personal-note'); field(el, 'draft', 'mine'); submit(el, 'save'); select(el, '<project>'); await flush();
  resolveSave({ status: 'pending', revision: 1, operation_id: 'personal-operation' }); await flush();
  assert.doesNotMatch(el.innerHTML, /personal-note|index pending/); view.dispose();
});

test('refresh replaces pending status rather than leaving a stale pending success message', async () => {
  const el = host();
  const view = mountSharedMemory(el, { listMemoryDomains: async () => ({ domains }), listMemoryDocuments: async () => ({ items: [], total: 0 }),
    saveMemoryDocument: async () => ({ status: 'pending', revision: 1, operation_id: 'op' }),
    getMemoryOperation: async () => ({ status: 'completed', revision: 1, operation_id: 'op' }),
  });
  await flush(); select(el, '<project>'); await flush(); field(el, 'newId', 'save'); field(el, 'draft', 'note'); submit(el, 'save'); await flush();
  assert.match(el.innerHTML, /index pending/); await view.refresh();
  assert.match(el.innerHTML, /indexing completed/); assert.doesNotMatch(el.innerHTML, /index pending/); view.dispose();
});

test('search requests full candidate budget despite stale index candidates being filtered', async () => {
  const el = host();
  const view = mountSharedMemory(el, { listMemoryDomains: async () => ({ domains }), listMemoryDocuments: async () => ({ items: [], total: 0 }),
    recallMemoryDocuments: async (bank, query, { limit }) => ({ results: limit === 100 ? [{ document_id: 'later', text: 'valid later result' }] : [] }),
  });
  await flush(); select(el, '<project>'); await flush(); field(el, 'query', 'search'); submit(el, 'search'); await flush();
  assert.match(el.innerHTML, /valid later result/); view.dispose();
});

for (const uuidAvailable of [false, true]) {
  test(`new note reaches API with randomUUID ${uuidAvailable ? 'available' : 'absent on private HTTP'}`, async () => {
    const originalCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
    let randomValuesCalls = 0, uuidCalls = 0;
    const crypto = {
      getRandomValues(bytes) {
        randomValuesCalls++;
        for (let i = 0; i < bytes.length; i++) bytes[i] = i;
        return bytes;
      },
      ...(uuidAvailable ? { randomUUID() { uuidCalls++; return 'test-uuid'; } } : {}),
    };
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: crypto });
    const el = host(), calls = [];
    let view;
    try {
      view = mountSharedMemory(el, {
        listMemoryDomains: async () => ({ domains }),
        listMemoryDocuments: async () => ({ items: [], total: 0 }),
        saveMemoryDocument: async (bank, body) => {
          calls.push([bank, body]); return { status: 'completed', revision: 1, operation_id: 'saved' };
        },
      });
      await flush(); select(el, '<project>'); await flush();
      field(el, 'draft', 'Remember this note');
      assert.doesNotThrow(() => submit(el, 'save'));
      await flush();
      assert.deepEqual(calls, [['pf', {
        document_id: uuidAvailable ? 'note-test-uuid' : 'note-00010203-0405-4607-8809-0a0b0c0d0e0f',
        content: 'Remember this note', base_revision: 0,
      }]]);
      assert.equal(uuidCalls, uuidAvailable ? 1 : 0);
      assert.equal(randomValuesCalls, uuidAvailable ? 0 : 1);
      assert.match(el.innerHTML, /Saved · indexing completed/);
    } finally {
      view?.dispose();
      if (originalCrypto) Object.defineProperty(globalThis, 'crypto', originalCrypto);
      else delete globalThis.crypto;
    }
  });
}
