import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { normalizeRecord, createTranscriptCollector } from './session-transcript.js';

const id = '12345678-1234-1234-1234-123456789abc';
function fixture(t, agent = 'codex') {
  const home = mkdtempSync(join(tmpdir(), 'transcript-'));
  t.after(() => rmSync(home, { recursive:true, force:true }));
  const root = join(home, ...({codex:['.codex','sessions'],pi:['.pi','agent','sessions'],claude:['.claude','projects']}[agent]));
  mkdirSync(root, {recursive:true});
  const path = join(root, `${id}.jsonl`);
  const pane = {agent, pane_id:'w1:p0', terminal_id:'term_0', agent_session:{kind:'path',value:path}};
  return {home,root,path,pane};
}
const message = text => ({type:'response_item',timestamp:'2026-10-22T10:00:00Z',payload:{type:'message',role:'assistant',channel:'final',content:[{type:'output_text',text}]}});
const lines = records => records.map(r=>JSON.stringify(r)+'\n').join('');

test('Codex captures public messages and tools without mirrored events or analysis', () => {
  assert.equal(normalizeRecord('codex',message('Hello'))[0].text,'Hello');
  assert.deepEqual(normalizeRecord('codex',{...message('secret'),payload:{...message('secret').payload,channel:'analysis'}}),[]);
  assert.deepEqual(normalizeRecord('codex',{type:'event_msg',payload:{type:'agent_message',message:'Hello'}}),[]);
  for (const role of ['system','developer']) assert.deepEqual(normalizeRecord('codex',{type:'response_item',payload:{type:'message',role,content:'hidden'}}),[]);
  assert.deepEqual(normalizeRecord('codex',{type:'response_item',payload:{type:'function_call',call_id:'call1',name:'exec',arguments:'pwd'}}),[{kind:'tool',call_id:'call1',phase:'start',tool:'exec',args_preview:'pwd'}]);
  assert.equal(normalizeRecord('codex',{type:'response_item',payload:{type:'function_call_output',call_id:'call1',output:'ok'}})[0].phase,'end');
});

test('Pi and Claude separate tool content and omit thinking blocks', () => {
  const pi = normalizeRecord('pi',{type:'message',message:{role:'assistant',content:[{type:'thinking',thinking:'private'},{type:'text',text:'Done'},{type:'toolCall',id:'c',name:'read',arguments:{path:'x'}}]}});
  assert.equal(pi.length,2); assert.equal(pi[0].text,'Done'); assert.equal(pi[1].call_id,'c');
  const claude = normalizeRecord('claude',{type:'user',message:{role:'user',content:[{type:'tool_result',tool_use_id:'c',content:'contents'},{type:'text',text:'Next'}]}});
  assert.equal(claude[0].text,'Next'); assert.equal(claude[1].output,'contents');
  assert.equal(normalizeRecord('pi',{type:'message',message:{role:'toolResult',toolCallId:'c',content:[{type:'text',text:'result'}]}})[0].output,'result');
});

test('collector retries identical events until acknowledged and skips partial writes', async t => {
  const f=fixture(t); writeFileSync(f.path,lines([message('first')])+JSON.stringify(message('partial')));
  const collect=createTranscriptCollector({home:f.home});
  const a=await collect('default',f.pane,'session');
  assert.equal(a.envelopes[0].payload.events.length,1);
  assert.deepEqual((await collect('default',f.pane,'session')).envelopes[0].payload,a.envelopes[0].payload);
  a.envelopes[0].onPublished();
  assert.equal((await collect('default',f.pane,'session')).envelopes.length,0);
  appendFileSync(f.path,'\n');
  const b=await collect('default',f.pane,'session');
  assert.equal(b.envelopes[0].payload.events[0].text,'partial');
  assert.notEqual(b.envelopes[0].payload.events[0].event_id,a.envelopes[0].payload.events[0].event_id);
  assert.equal(JSON.stringify(b.envelopes[0]).includes('onPublished'),false);
});

test('collector only follows explicit identity and rejects paths outside native roots or symlinks', async t=>{
  const f=fixture(t); writeFileSync(f.path,lines([message('hi')]));
  const collect=createTranscriptCollector({home:f.home});
  assert.equal((await collect('default',{...f.pane,agent_session:null,cwd:f.root},'s')).status,'identity_unavailable');
  const outside=join(f.home,'outside.jsonl'); writeFileSync(outside,lines([message('secret')]));
  symlinkSync(outside,join(f.root,'link.jsonl'));
  for(const path of [outside,join(f.root,'link.jsonl')]) assert.equal((await collect('default',{...f.pane,agent_session:{kind:'path',value:path}},'s')).envelopes.length,0);
  const resolved=await collect('default',{...f.pane,agent_session:{kind:'id',value:id}},'s');
  assert.equal(resolved.envelopes[0].payload.events[0].text,'hi');
});

test('bounded batches advance in order after acknowledgement and maintain replay IDs across restarts',async t=>{
  const f=fixture(t); writeFileSync(f.path,lines(Array.from({length:7},(_,i)=>message(String(i)))));
  const collect=createTranscriptCollector({home:f.home,maxEvents:2});
  const a=await collect('d',f.pane,'s'); assert.equal(a.truncated,true); assert.equal(a.envelopes[0].payload.events.length,2);
  const restarted=await createTranscriptCollector({home:f.home,maxEvents:2})('d',f.pane,'s');
  assert.deepEqual(restarted.envelopes[0].payload,a.envelopes[0].payload);
  a.envelopes[0].onPublished();
  const b=await collect('d',f.pane,'s'); assert.equal(b.envelopes[0].payload.events[0].text,'2');
});

test('Pi PID descriptor resolves exact log without cwd matching',async t=>{
  const f=fixture(t,'pi'); writeFileSync(f.path,lines([{type:'message',message:{role:'user',content:'hello'}}]));
  const control=join(f.home,'control'); mkdirSync(control); writeFileSync(join(control,'live.json'),JSON.stringify({pid:42,sessionId:id}));
  const collect=createTranscriptCollector({home:f.home,env:{PI_ENGINEERING_CONTROL_DIR:control},queryProcess:async()=>({result:{process_info:{foreground_processes:[{pid:42}]}}})});
  assert.equal((await collect('d',{...f.pane,agent_session:null},'s')).envelopes[0].payload.events[0].text,'hello');
});

test('acknowledged offsets survive collector restart without replaying old history',async t=>{
  const f=fixture(t);writeFileSync(f.path,lines([message('first'),message('second')]));
  const a=await createTranscriptCollector({home:f.home,maxEvents:1})('d',f.pane,'s'); a.envelopes[0].onPublished();
  const b=await createTranscriptCollector({home:f.home,maxEvents:1})('d',f.pane,'s');
  assert.equal(b.envelopes[0].payload.events[0].text,'second');
});

test('Codex fd discovery requires exact process and a valid native header',async t=>{
  const f=fixture(t);writeFileSync(f.path,lines([{type:'session_meta',payload:{id}},message('known')]));
  const procRoot=join(f.home,'proc');mkdirSync(join(procRoot,'42','fd'),{recursive:true});symlinkSync(f.path,join(procRoot,'42','fd','7'));
  const collect=createTranscriptCollector({home:f.home,procRoot,queryProcess:async()=>({result:{process_info:{foreground_processes:[{pid:42}]}}})});
  assert.equal((await collect('d',{...f.pane,agent_session:null},'s')).envelopes[0].payload.events[0].text,'known');
});

test('oversized records are discarded through newline and later complete records are captured',async t=>{
  const f=fixture(t);writeFileSync(f.path,lines([message('x'.repeat(1000)),message('ok')]));
  const collect=createTranscriptCollector({home:f.home,maxBytes:256});
  let result;for(let i=0;i<8;i++){result=await collect('d',f.pane,'s');if(result.envelopes.length)break;}
  assert.equal(result.envelopes[0].payload.events[0].text,'ok');
});

test('Codex analysis-channel tools remain visible while private messages and reasoning are excluded', () => {
  for (const type of ['function_call', 'custom_tool_call']) {
    const events = normalizeRecord('codex', {type:'response_item', payload:{type,channel:'analysis',call_id:'call-analysis',name:'exec',arguments:'pwd',input:'pwd'}});
    assert.equal(events.length,1);
    assert.equal(events[0].kind,'tool');
    assert.equal(events[0].phase,'start');
    assert.equal(events[0].args_preview,'pwd');
  }
  for (const type of ['function_call_output', 'custom_tool_call_output']) {
    const events = normalizeRecord('codex', {type:'response_item', payload:{type,channel:'analysis',call_id:'call-analysis',output:'done'}});
    assert.equal(events.length,1);
    assert.equal(events[0].phase,'end');
    assert.equal(events[0].output,'done');
  }
  assert.deepEqual(normalizeRecord('codex',{type:'response_item',payload:{type:'message',role:'assistant',channel:'analysis',content:[{type:'output_text',text:'private'}]}}),[]);
  assert.deepEqual(normalizeRecord('codex',{type:'response_item',payload:{type:'reasoning',channel:'analysis',summary:[{type:'summary_text',text:'private'}]}}),[]);
});
