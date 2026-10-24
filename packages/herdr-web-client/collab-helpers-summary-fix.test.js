import test from 'node:test';
import assert from 'node:assert/strict';
import { createHelpersPanel, renderHelpersPanel, renderSummaryFixResults } from '../collab-helpers.js';

const XSS = '<img src=x onerror=alert(1)>';

test('summary-fix results are escaped, marked untrusted and never actionable beyond Cancel', () => {
  const html = renderSummaryFixResults([
    { helper_id: 'h1', status: 'done', created_epoch: 100, result: `apply this ${XSS}`, untrusted: true },
    { helper_id: 'h2', status: 'running', created_epoch: 190, result: null },
  ], 200);
  assert.ok(!html.includes(XSS) && html.includes('&lt;img'));
  assert.match(html, /Result \(untrusted text, not applied\)/);
  assert.equal((html.match(/data-cancel-helper/g) || []).length, 1);   // only the live one
  assert.match(renderSummaryFixResults([], 0), /No summary-fix helpers yet/);
});

test('the panel shows the section only when the server has the route and a room or results', () => {
  const base = { paused: false, requests: [], helpers: [], allow: {}, rooms: [] };
  assert.doesNotMatch(renderHelpersPanel({ ...base, fixes: null }, { operator: true }), /Summary fixes/);
  assert.match(renderHelpersPanel({ ...base, fixes: [] }, { operator: true }), /Summary fixes/);
});

test('the panel loads summary-fix results and tolerates an older server', async () => {
  const mk = (summaryFixHelpers) => ({
    egressRequests: async () => ({ requests: [] }), helpers: async () => ({ helpers: [] }), egressAllowlist: async () => ({ allowlist: [] }),
    ...(summaryFixHelpers ? { summaryFixHelpers } : {}),
  });
  const container = { innerHTML: '', addEventListener() {}, removeEventListener() {} };
  const p = createHelpersPanel({ container, api: mk(async () => ({ configured: true, helpers: [{ helper_id: 'h9', status: 'done', result: 'fix it', created_epoch: 1 }] })), operator: true, getRooms: async () => [] });
  await p.refresh();
  assert.match(container.innerHTML, /Summary fixes/);
  assert.match(container.innerHTML, /fix it/);
  p.destroy();
  const old = { innerHTML: '', addEventListener() {}, removeEventListener() {} };
  const q = createHelpersPanel({ container: old, api: mk(async () => { throw new Error('404'); }), operator: true, getRooms: async () => [] });
  await q.refresh();
  assert.doesNotMatch(old.innerHTML, /Summary fixes/);
  assert.doesNotMatch(old.innerHTML, /hd-error/);
  q.destroy();
});
