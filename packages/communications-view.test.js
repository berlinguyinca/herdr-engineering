import {test} from 'node:test';
import assert from 'node:assert/strict';
import {renderCommunications, parsePage} from './communications-view.js';

test('communication text is escaped and delivery is distinct from completion', () => {
  const html = renderCommunications([{operation_id:'one', status:'delivered',
    text:'<script>alert("unsafe")</script>', created_at:'2026-11-13T22:00:00Z',
    sender:{name:'Claude',session_id:'herdr:a'}, recipient:{name:'Codex',session_id:'herdr:b'},
    evidence_kind:'host_observation', truncated:false}]);
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('#/herd/session/herdr%3Aa'));
  assert.ok(html.includes('#/herd/session/herdr%3Ab'));
  assert.ok(html.includes('Delivered'));
  assert.ok(html.includes('Host observation'));
  assert.ok(!html.includes('Completed'));
});

test('unknown sender, truncated body, and empty history are explicit', () => {
  assert.ok(renderCommunications([]).includes('No recorded communications'));
  const html = renderCommunications([{operation_id:'one', status:'unknown', text:'partial',
    created_at:'',sender:{},recipient:{pane_id:'w1:p1'},evidence_kind:'server_receipt',truncated:true}]);
  assert.ok(html.includes('Unknown sender'));
  assert.ok(html.includes('Outcome unknown'));
  assert.ok(html.includes('Message truncated'));
});

test('offline messages show the reported time separately from the server record time', () => {
  const html = renderCommunications([{operation_id:'offline', status:'queued', text:'hello',
    created_at:'2026-11-13T22:00:00Z',reported_at:0,recorded_at:'2026-11-13T22:00:00Z',
    sender:{},recipient:{},evidence_kind:'host_observation',truncated:false}]);
  assert.ok(html.includes('datetime="1970-01-01T00:00:00.000Z"'));
  assert.ok(html.includes('Reported'));
  assert.ok(html.includes('Recorded'));
  assert.ok(html.includes('Delivery unconfirmed'));
});

test('invalid server data is rejected rather than presented as a communication', () => {
  assert.throws(()=>parsePage({communications:[{status:'completed'}]}));
  assert.throws(()=>parsePage({communications:'wrong'}));
  assert.deepEqual(parsePage({communications:[],has_more:false,next_cursor:null}),
    {communications:[],has_more:false,next_cursor:null});
});
