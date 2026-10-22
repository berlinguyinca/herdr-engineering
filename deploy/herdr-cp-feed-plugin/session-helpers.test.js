import {test} from 'node:test';
import assert from 'node:assert/strict';
import {appendFileSync, mkdirSync, mkdtempSync, readdirSync, rmSync, statSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {createSessionTracker, recordSessionBinding} from './session-helpers.js';
import {createTranscriptCollector} from './session-transcript.js';

const parent='019a0950-0000-7000-8000-000000000001';
const child='019a0950-0000-7000-8000-000000000002';
const grandchild='019a0950-0000-7000-8000-000000000003';
const sid='herdr:test:default:w1%3Ap0';
function fixture(t) {
  const home=mkdtempSync(join(tmpdir(),'herdr-helper-'));
  t.after(()=>rmSync(home,{recursive:true,force:true}));
  const log=(id,parentId,date='2026/10/22',extra=[])=>{
    const path=join(home,'.codex','sessions',date,`rollout-${id}.jsonl`);
    mkdirSync(dirname(path),{recursive:true});
    writeFileSync(path,[{type:'session_meta',payload:{id,source:parentId?{subagent:{thread_spawn:{parent_thread_id:parentId}}}:'cli'}},...extra].map(JSON.stringify).join('\n')+'\n');
    return path;
  };
  return {home,log};
}
const message=text=>({type:'response_item',payload:{type:'message',role:'assistant',channel:'final',content:[{type:'output_text',text}]}});
function binding(path,id=parent) {return {agent:'codex',kind:'path',value:path,nativeId:id};}
function remember(tracker,path) {tracker.remember({sessionId:sid,server:'default',paneId:'w1:p0',identity:binding(path)});}
function events(result) {return result.envelopes.flatMap(e=>e.payload.events);}
function ack(result) {for(const e of result.envelopes)e.onPublished?.();}

test('helpers require explicit parent headers, retain nested parentage, and exclude hidden reasoning',async t=>{
  const f=fixture(t), path=f.log(parent,null);
  f.log(child,parent,'2026/10/22',[message('helper answer'),{type:'response_item',payload:{type:'message',role:'assistant',channel:'analysis',content:'secret'}}]);
  f.log(grandchild,child,'2026/10/22',[message('nested answer')]);
  f.log('019a0950-0000-7000-8000-000000000004','019a0950-0000-7000-8000-000000000099','2026/10/22',[message('unrelated')]);
  const tracker=createSessionTracker({home:f.home,collectTranscript:createTranscriptCollector({home:f.home}),now:()=>Date.parse('2026-10-22')});
  remember(tracker,path);
  const result=await tracker.collect(new Set([sid]));
  assert.deepEqual(events(result).map(e=>[e.text,e.agent_id,e.parent_agent_id]).sort(),[['helper answer',child,'main'],['nested answer',grandchild,child]].sort());
  ack(result);
  assert.equal(events(await tracker.collect(new Set([sid]))).length,0);
});

test('exact known bindings drain final output after pane disappears and across tracker restarts',async t=>{
  const f=fixture(t), path=f.log(parent,null,'2026/10/22',[message('first')]);
  const collector=createTranscriptCollector({home:f.home});
  const initial=await collector('default',{agent:'codex',agent_session:binding(path)},sid);ack(initial);
  let tracker=createSessionTracker({home:f.home,collectTranscript:collector});
  remember(tracker,path);
  appendFileSync(path,JSON.stringify(message('last after exit'))+'\n');
  tracker=createSessionTracker({home:f.home,collectTranscript:createTranscriptCollector({home:f.home})});
  const result=await tracker.collect(new Set());
  assert.deepEqual(events(result).map(e=>e.text),['last after exit']);
  ack(result);
  assert.equal(events(await tracker.collect(new Set())).length,0);
  assert.equal(statSync(join(f.home,'.cache','herdr-engineering','transcript-bindings',readdirSync(join(f.home,'.cache','herdr-engineering','transcript-bindings'))[0])).mode & 0o777,0o600);
});

test('bounded header scan resumes backlog and finds newly created helpers',async t=>{
  const f=fixture(t), path=f.log(parent,null);
  for(let i=0;i<20;i++)f.log(`019a0950-0000-7000-8000-${String(i+10).padStart(12,'0')}`,null,'2025/01/01');
  f.log(child,parent,'2025/01/01',[message('older helper')]);
  const tracker=createSessionTracker({home:f.home,collectTranscript:createTranscriptCollector({home:f.home}),now:()=>Date.parse('2026-10-22'),scanEntries:4});
  remember(tracker,path);
  const found=[];
  for(let i=0;i<40;i++){const r=await tracker.collect(new Set([sid]));found.push(...events(r));ack(r);}
  assert.equal(found.filter(e=>e.text==='older helper').length,1);
  f.log(grandchild,parent,'2026/10/22',[message('new helper')]);
  for(let i=0;i<40;i++){const r=await tracker.collect(new Set([sid]));found.push(...events(r));ack(r);}
  assert.equal(found.filter(e=>e.text==='new helper').length,1);
});

test('invalid and outside-root bindings never persist or collect',async t=>{
  const f=fixture(t), calls=[];
  const tracker=createSessionTracker({home:f.home,collectTranscript:async(...args)=>{calls.push(args);return {envelopes:[]};}});
  remember(tracker,join(f.home,'unrelated.jsonl'));
  await tracker.collect(new Set());
  assert.equal(calls.length,0);
  assert.throws(()=>readdirSync(join(f.home,'.cache','herdr-engineering','transcript-bindings')));
});

test('event handoff captures a short session never present in a snapshot',async t=>{
  const f=fixture(t), path=f.log(parent,null,'2026/10/22',[message('short session')]);
  const tracker=createSessionTracker({home:f.home,collectTranscript:createTranscriptCollector({home:f.home})});
  const envelope={entity_type:'session',entity_id:sid,payload:{herdr_session:'default',pane_id:'w1:p0',agent_role:'codex',agent_session:binding(path)}};
  assert.equal(recordSessionBinding(envelope,{home:f.home}),true);
  const result=await tracker.collect(new Set());
  assert.deepEqual(events(result).map(e=>e.text),['short session']);
  assert.equal(recordSessionBinding({...envelope,payload:{...envelope.payload,agent_session:null}},{home:f.home}),false);
});

test('a reused pane drains its previous exact transcript alongside the new native session',async t=>{
  const f=fixture(t), oldPath=f.log(parent,null),newPath=f.log(child,null);
  const tracker=createSessionTracker({home:f.home,collectTranscript:createTranscriptCollector({home:f.home})});
  remember(tracker,oldPath);
  tracker.remember({sessionId:sid,server:'default',paneId:'w1:p0',identity:binding(newPath,child)});
  appendFileSync(oldPath,JSON.stringify(message('previous final'))+'\n');
  const result=await tracker.collect(new Set([sid]));
  assert.deepEqual(events(result).map(e=>e.text),['previous final']);
});


test('native ID handoffs from independent event hooks survive restart without overwriting other bindings',async t=>{
  const f=fixture(t);
  f.log(parent,null,'2026/10/22',[message('first session')]);
  f.log(child,null,'2026/10/22',[message('second session')]);
  for(const id of [parent,child]) {
    assert.equal(recordSessionBinding({entity_type:'session',entity_id:sid,payload:{herdr_session:'default',pane_id:'w1:p0',agent_role:'codex',agent_session:{kind:'id',value:id}}},{home:f.home}),true);
  }
  const tracker=createSessionTracker({home:f.home,collectTranscript:createTranscriptCollector({home:f.home})});
  const result=await tracker.collect(new Set());
  assert.deepEqual(events(result).map(e=>e.text).sort(),['first session','second session']);
});

test('duplicate helper UUID claims are ambiguous and never assigned to a parent',async t=>{
  const f=fixture(t),path=f.log(parent,null);
  f.log(child,parent,'2026/10/22',[message('ambiguous one')]);
  f.log(child,parent,'2026/10/21',[message('ambiguous two')]);
  const tracker=createSessionTracker({home:f.home,collectTranscript:createTranscriptCollector({home:f.home}),now:()=>Date.parse('2026-10-22')});
  remember(tracker,path);
  assert.deepEqual(events(await tracker.collect(new Set([sid]))),[]);
});
