import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { normalizeRecord, createTranscriptCollector, resolveTranscript } from './session-transcript.js';

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


test('UUIDv7 Codex identity resolves its date bucket despite more than 2048 old logs', async t => {
  const f=fixture(t);
  const stamp=Date.parse('2026-10-22T10:00:00Z').toString(16).padStart(12,'0');
  const nativeId=`${stamp.slice(0,8)}-${stamp.slice(8)}-7000-8000-123456789abc`;
  const old=join(f.root,'2025','01','01'); mkdirSync(old,{recursive:true});
  for(let i=0;i<2100;i++)writeFileSync(join(old,`old-${i}.jsonl`),'');
  const current=join(f.root,'2026','10','22'); mkdirSync(current,{recursive:true});
  const path=join(current,`rollout-2026-10-22T10-00-00-${nativeId}.jsonl`);
  writeFileSync(path,lines([{type:'session_meta',payload:{id:nativeId,instructions:'x'.repeat(24000)}},message('current')]));
  assert.equal(await resolveTranscript('codex',{kind:'id',value:nativeId},{home:f.home}),path);
});

test('fallback identity scans resume past 2048 entries and reject conflicting copies', async t => {
  const f=fixture(t,'claude');
  for(let i=0;i<2100;i++)writeFileSync(join(f.root,`irrelevant-${i}.jsonl`),'');
  const project=join(f.root,'project'); mkdirSync(project);
  const path=join(project,`${id}.jsonl`);writeFileSync(path,lines([{type:'user',sessionId:id,message:{role:'user',content:'found'}}]));
  let found=null;
  for(let attempt=0;attempt<4&&!found;attempt++)found=await resolveTranscript('claude',{kind:'id',value:id},{home:f.home});
  assert.equal(found,path);
  const duplicate=join(f.root,'duplicate');mkdirSync(duplicate);writeFileSync(join(duplicate,`${id}.jsonl`),'');
  for(let attempt=0;attempt<4;attempt++)assert.equal(await resolveTranscript('claude',{kind:'id',value:id},{home:f.home}),null);
});


test('Codex tool results retain explicit process failures without guessing from prose', () => {
  const result=output=>normalizeRecord('codex',{type:'response_item',payload:{type:'function_call_output',call_id:'c',output}})[0];
  assert.equal(result('Process exited with code 2\nOutput: failed').is_error,true);
  assert.equal(result('Process exited with code 0\nOutput: error example').is_error,false);
  assert.equal(result('An error example in documentation').is_error,undefined);
  assert.equal(result({exit_code:7,output:'bad'}).exit_code,7);
});

test('only successful explicit edit tool results produce file activity across restarts', async t => {
  const f=fixture(t);
  const call=(call_id,input)=>({type:'response_item',payload:{type:'custom_tool_call',call_id,name:'apply_patch',input}});
  const output=(call_id,output)=>({type:'response_item',payload:{type:'custom_tool_call_output',call_id,output}});
  writeFileSync(f.path,lines([call('good','*** Begin Patch\n*** Update File: src/a.js\n@@\n-old\n+new\n*** End Patch')]));
  const first=await createTranscriptCollector({home:f.home})('d',f.pane,'s');
  assert.equal(first.envelopes[0].payload.events.filter(e=>e.kind==='file').length,0);
  first.envelopes[0].onPublished();
  appendFileSync(f.path,lines([output('good','Success. Updated the following files:\nM src/a.js'),call('bad','*** Begin Patch\n*** Delete File: src/b.js\n*** End Patch'),output('bad','Process exited with code 1\nfailed')]));
  const next=await createTranscriptCollector({home:f.home})('d',f.pane,'s');
  const files=next.envelopes[0].payload.events.filter(e=>e.kind==='file');
  assert.equal(files.length,1);assert.equal(files[0].path,'src/a.js');assert.equal(files[0].op,'edit');assert.match(files[0].diff,/\+new/);
});

test('native usage capture uses deltas and preserves model without private reasoning', async t => {
  const f=fixture(t);
  const usage=n=>({type:'event_msg',payload:{type:'token_count',info:{total_token_usage:{input_tokens:n,output_tokens:2,reasoning_output_tokens:1,cached_input_tokens:3,total_tokens:n+2},model_context_window:1000}}});
  writeFileSync(f.path,lines([{type:'turn_context',payload:{model:'gpt-test'}},usage(10),usage(10),usage(15)]));
  const a=await createTranscriptCollector({home:f.home})('d',f.pane,'s');
  const events=a.envelopes[0].payload.events;
  assert.equal(events.find(e=>e.kind==='model').model,'gpt-test');
  assert.equal(events.filter(e=>e.kind==='usage').reduce((sum,e)=>sum+(e.input_tokens||0),0),15);
  a.envelopes[0].onPublished();appendFileSync(f.path,lines([usage(18)]));
  const b=await createTranscriptCollector({home:f.home})('d',f.pane,'s');
  assert.equal(b.envelopes[0].payload.events[0].input_tokens,3);
});


test('private resolved identity and helper attribution survive pending retry',async t=>{
  const f=fixture(t);writeFileSync(f.path,lines([{type:'session_meta',payload:{id}},message('helper')]));
  const collect=createTranscriptCollector({home:f.home});
  const attribution={agentId:'child',parentAgentId:'main'};
  const result=await collect('d',f.pane,'s',attribution);
  assert.equal(result.identity.value,f.path);assert.equal(result.identity.nativeId,id);
  assert.equal(JSON.stringify(result).includes(f.path),false);
  assert.equal(result.envelopes[0].payload.events[0].agent_id,'child');
  assert.equal(result.envelopes[0].payload.events[0].parent_agent_id,'main');
  assert.equal((await collect('d',f.pane,'s',attribution)).identity.nativeId,id);
});

test('a successful edit and its tool result stay together with one-event batch preference',async t=>{
  const f=fixture(t,'pi');
  writeFileSync(f.path,lines([{type:'message',message:{role:'assistant',content:[{type:'toolCall',id:'edit',name:'edit',arguments:{path:'a',oldText:'old',newText:'new'}}]}},{type:'message',message:{role:'toolResult',toolCallId:'edit',content:[{type:'text',text:'ok'}]}}]));
  const collect=createTranscriptCollector({home:f.home,maxEvents:1});
  const first=await collect('d',f.pane,'s');first.envelopes[0].onPublished();
  const second=await collect('d',f.pane,'s');
  assert.deepEqual(second.envelopes[0].payload.events.map(e=>e.kind),['tool','file']);
});


test('Codex descendant environment resolves only exact pane-bound primary identity',async t=>{
  const f=fixture(t);writeFileSync(f.path,lines([{type:'session_meta',payload:{id,source:'cli'}},message('known')]));
  const procRoot=join(f.home,'proc');
  const process=(pid,children,environment='')=>{mkdirSync(join(procRoot,String(pid),'task',String(pid)),{recursive:true});writeFileSync(join(procRoot,String(pid),'task',String(pid),'children'),children);writeFileSync(join(procRoot,String(pid),'environ'),environment);};
  process(42,'43',`CODEX_THREAD_ID=${id}\0HERDR_PANE_ID=w1:p0\0`);
  process(43,'',`CODEX_THREAD_ID=${id}\0HERDR_PANE_ID=w1:p0\0`);
  const options={home:f.home,procRoot,pids:[42],paneId:'w1:p0'};
  assert.equal(await resolveTranscript('codex',null,options),f.path);
  assert.equal(await resolveTranscript('codex',null,{...options,paneId:'other'}),null);
  writeFileSync(join(procRoot,'43','environ'),'');
  assert.equal(await resolveTranscript('codex',null,options),null,'root inherited environment is insufficient');
  writeFileSync(join(procRoot,'43','environ'),`CODEX_THREAD_ID=${id}\0HERDR_PANE_ID=w1:p0\0`);
  writeFileSync(f.path,lines([{type:'session_meta',payload:{id,source:{subagent:{thread_spawn:{parent_thread_id:'other'}}}}},message('helper')]));
  assert.equal(await resolveTranscript('codex',null,options),null,'helper identity cannot become the parent pane');
});


test('large edit arguments still produce bounded confirmed file evidence',()=>{
  const context={};
  const call={type:'message',message:{role:'assistant',content:[{type:'toolCall',id:'large',name:'edit',arguments:{path:'large.txt',oldText:'a'.repeat(20000),newText:'b'.repeat(20000)}}]}};
  normalizeRecord('pi',call,context);
  const output=normalizeRecord('pi',{type:'message',message:{role:'toolResult',toolCallId:'large',content:'ok'}},context);
  const file=output.find(e=>e.kind==='file');assert.equal(file.path,'large.txt');assert.ok(Buffer.byteLength(file.diff)<=16384);
});

test('repeated Claude assistant records do not inflate usage',()=>{
  const context={};
  const record={type:'assistant',message:{id:'same-message',role:'assistant',content:[{type:'text',text:'answer'}],usage:{input_tokens:10,output_tokens:2}}};
  const first=normalizeRecord('claude',record,context);
  const second=normalizeRecord('claude',record,context);
  assert.equal([...first,...second].filter(e=>e.kind==='usage').reduce((n,e)=>n+e.input_tokens,0),10);
});

test('Claude PID descriptors require current process lifetime and boot namespace proof',async t=>{
  const f=fixture(t,'claude');
  writeFileSync(f.path,lines([{type:'user',sessionId:id,message:{role:'user',content:'recovered'}}]));
  const procRoot=join(f.home,'proc');
  mkdirSync(join(procRoot,'42','ns'),{recursive:true});
  mkdirSync(join(procRoot,'sys','kernel','random'),{recursive:true});
  writeFileSync(join(procRoot,'sys','kernel','random','boot_id'),'ebf8189c-f339-41b4-b420-32bf1a8ee69f\n');
  symlinkSync('pid:[1234]',join(procRoot,'42','ns','pid'));
  const fields=['S',...Array(18).fill('0'),'440839879','0'];
  writeFileSync(join(procRoot,'42','stat'),`42 (claude name with ) parens) ${fields.join(' ')}\n`);
  const descriptors=join(f.home,'.claude','sessions');mkdirSync(descriptors,{recursive:true});
  const descriptor=join(descriptors,'42.json');
  const evidence={pid:42,sessionId:id,procStart:'440839879',pidDomain:'linux:ebf8189cf33941b4b42032bf1a8ee69f:pid:[1234]'};
  const options={home:f.home,procRoot,pids:[42],platform:'linux'};
  writeFileSync(descriptor,JSON.stringify(evidence));
  assert.equal(await resolveTranscript('claude',null,options),f.path);
  for(const override of [{pid:43},{procStart:'440839878'},{procStart:null},{pidDomain:'linux:old-boot:pid:[1234]'},{pidDomain:'linux:ebf8189cf33941b4b42032bf1a8ee69f:pid:[9999]'}]) {
    writeFileSync(descriptor,JSON.stringify({...evidence,...override}));
    assert.equal(await resolveTranscript('claude',null,options),null,JSON.stringify(override));
  }
  writeFileSync(descriptor,JSON.stringify(evidence));
  assert.equal(await resolveTranscript('claude',null,{...options,platform:'darwin'}),null);
  assert.equal(await resolveTranscript('claude',null,{...options,pids:[43]}),null);
  const collect=createTranscriptCollector({home:f.home,procRoot,queryProcess:async()=>({result:{process_info:{foreground_processes:[{pid:42}]}}})});
  const captured=await collect('d',{...f.pane,agent_session:null},'s');
  assert.equal(captured.envelopes[0].payload.events[0].text,'recovered');
});


test('large native metadata headers resolve fd, descendant, and private identity without exposing instructions',async t=>{
  const f=fixture(t);writeFileSync(f.path,lines([{type:'session_meta',payload:{id,source:'cli',instructions:'private'.repeat(4000)}},message('public')]));
  const procRoot=join(f.home,'proc');mkdirSync(join(procRoot,'42','fd'),{recursive:true});
  symlinkSync(f.path,join(procRoot,'42','fd','7'));
  assert.equal(await resolveTranscript('codex',null,{home:f.home,procRoot,pids:[42]}),f.path);
  const captured=await createTranscriptCollector({home:f.home})('d',f.pane,'s');
  assert.equal(captured.identity.nativeId,id);
  assert.equal(JSON.stringify(captured.envelopes).includes('private'),false);
  for(const [pid,children] of [[43,'44'],[44,'']]) {
    mkdirSync(join(procRoot,String(pid),'task',String(pid)),{recursive:true});
    writeFileSync(join(procRoot,String(pid),'task',String(pid),'children'),children);
  }
  writeFileSync(join(procRoot,'44','environ'),`CODEX_THREAD_ID=${id}\0HERDR_PANE_ID=w1:p0\0`);
  assert.equal(await resolveTranscript('codex',null,{home:f.home,procRoot,pids:[43],paneId:'w1:p0'}),f.path);
});
