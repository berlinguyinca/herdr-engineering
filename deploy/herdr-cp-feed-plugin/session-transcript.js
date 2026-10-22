// Bounded native transcript capture. Only explicit native identity or process
// evidence may associate a log with a pane; cwd and file age are never evidence.
import { closeSync, fstatSync, openSync, readSync, realpathSync, opendirSync, readlinkSync, mkdirSync, writeFileSync, renameSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { homedir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { controlDir } from './session-model.js';

const MAX_BYTES = 256 * 1024;
const BODY_BYTES = 16 * 1024;
const ID = /^[a-zA-Z0-9][a-zA-Z0-9-]{7,79}$/;
const hash = value => createHash('sha256').update(value).digest('hex');
const bounded = value => Buffer.from(typeof value === 'string' ? value : JSON.stringify(value ?? '')).subarray(0,BODY_BYTES).toString('utf8');
function text(content) {
  if (typeof content === 'string') return bounded(content);
  if (!Array.isArray(content)) return '';
  return bounded(content.filter(b => ['text','input_text','output_text'].includes(b?.type)).map(b => b.text || '').join('\n'));
}

export function normalizeRecord(agent, record) {
  if (!record || typeof record !== 'object') return [];
  let message;
  if (agent === 'codex') {
    // event_msg repeats response_item messages and may include private reasoning.
    if (record.type !== 'response_item') return [];
    const p = record.payload;
    if (!p) return [];
    if (['function_call','custom_tool_call'].includes(p.type) && p.call_id) return [{kind:'tool',call_id:p.call_id,phase:'start',tool:p.name,args_preview:bounded(p.arguments ?? p.input)}];
    if (['function_call_output','custom_tool_call_output'].includes(p.type) && p.call_id) return [{kind:'tool',call_id:p.call_id,phase:'end',output:bounded(p.output)}];
    // Execution activity is public even when dispatched from the analysis channel.
    if (p.type !== 'message' || p.channel === 'analysis') return [];
    message = p;
  } else if (agent === 'pi' && record.type === 'message') message = record.message;
  else if (agent === 'claude' && ['user','assistant'].includes(record.type)) message = record.message;
  if (!message || !['user','assistant','toolResult'].includes(message.role)) return [];
  if (message.role === 'toolResult') return message.toolCallId ? [{kind:'tool',call_id:message.toolCallId,phase:'end',tool:message.toolName,output:text(message.content),is_error:!!message.isError}] : [];
  const result = [];
  const body = text(message.content);
  if (body) result.push({kind:'message',role:message.role,text:body});
  for (const block of Array.isArray(message.content) ? message.content : []) {
    if (['toolCall','tool_use'].includes(block?.type) && block.id) result.push({kind:'tool',call_id:block.id,phase:'start',tool:block.name,args_preview:bounded(block.arguments ?? block.input)});
    if (block?.type === 'tool_result' && block.tool_use_id) result.push({kind:'tool',call_id:block.tool_use_id,phase:'end',output:text(block.content),is_error:!!block.is_error});
  }
  return result;
}

function entries(path, limit = 512) {
  let dir;
  const result=[];
  try { dir=opendirSync(path); for(let e; result.length<limit && (e=dir.readSync());) result.push(e); }
  catch { /* optional native state */ }
  finally { dir?.closeSync(); }
  return result;
}
function readRange(path, offset = 0, limit = MAX_BYTES) {
  let fd;
  try {
    fd=openSync(path,'r'); const stat=fstatSync(fd);
    if(!stat.isFile()) return null;
    const buffer=Buffer.alloc(Math.min(limit,Math.max(0,stat.size-offset)));
    const count=readSync(fd,buffer,0,buffer.length,offset);
    return {buffer:buffer.subarray(0,count),size:stat.size,identity:`${stat.dev}:${stat.ino}`};
  } catch { return null; } finally { if(fd!==undefined) closeSync(fd); }
}
function jsonFile(path) {
  const r=readRange(path,0,8192);
  try { return r && r.size<=8192 ? JSON.parse(r.buffer.toString()) : null; } catch { return null; }
}
function rootFor(agent, home) {
  return ({codex:join(home,'.codex','sessions'),claude:join(home,'.claude','projects'),pi:join(home,'.pi','agent','sessions')})[agent];
}
function confined(path, root) {
  try { const file=realpathSync(path), base=realpathSync(root)+sep; return file.startsWith(base) && file.endsWith('.jsonl') ? file : null; } catch { return null; }
}
function findIdentity(root, id) {
  // A finite tree walk, exact ID matches only; ambiguous copies are refused.
  const queue=[{path:root,depth:0}], matches=[];
  let remaining=2048;
  while(queue.length && remaining>0) {
    const current=queue.shift();
    for(const e of entries(current.path,remaining)) {
      remaining--;
      if(e.isDirectory() && current.depth<4) queue.push({path:join(current.path,e.name),depth:current.depth+1});
      else if(e.isFile() && (e.name===`${id}.jsonl` || e.name.endsWith(`_${id}.jsonl`) || e.name.endsWith(`-${id}.jsonl`))) matches.push(join(current.path,e.name));
    }
  }
  return remaining>0 && matches.length===1 ? confined(matches[0],root) : null;
}

export async function resolveTranscript(agent, evidence, {home=homedir(),env=process.env,pids=[],procRoot='/proc'}={}) {
  const root=rootFor(agent,home);
  if(!root) return null;
  if(evidence?.agent && evidence.agent!==agent) return null;
  if(evidence?.kind==='path' && typeof evidence.value==='string') return confined(resolve(evidence.value),root);
  if(evidence?.kind==='id' && ID.test(evidence.value || '')) return findIdentity(root,evidence.value);
  const candidates=new Set();
  const processIds=new Set(pids.filter(p=>Number.isInteger(p)&&p>0).slice(0,32));
  if(agent==='pi') {
    for(const entry of entries(controlDir(env),256)) {
      if(!entry.isFile() || !entry.name.endsWith('.json')) continue;
      const d=jsonFile(join(controlDir(env),entry.name));
      if(processIds.has(d?.pid) && ID.test(d?.sessionId || '')) { const file=findIdentity(root,d.sessionId); if(file) candidates.add(file); }
    }
  } else if(agent==='claude') {
    for(const pid of processIds) { const d=jsonFile(join(home,'.claude','sessions',`${pid}.json`)); if(ID.test(d?.sessionId || '')) {const file=findIdentity(root,d.sessionId); if(file)candidates.add(file);} }
  } else if(agent==='codex') {
    for(const pid of processIds) for(const entry of entries(join(procRoot,String(pid),'fd'),256)) {
      let target; try { target=readlinkSync(join(procRoot,String(pid),'fd',entry.name)); } catch {continue;}
      const file=confined(target,root);
      if(!file) continue;
      const head=readRange(file,0,8192)?.buffer.toString().split('\n')[0];
      try { const r=JSON.parse(head); if(r.type==='session_meta' && ID.test(r.payload?.id || '')) candidates.add(file); } catch { /* no trustworthy header */ }
    }
  }
  return candidates.size===1 ? [...candidates][0] : null;
}

export function createTranscriptCollector({home=homedir(),env=process.env,queryProcess,procRoot='/proc',maxBytes=MAX_BYTES,maxEvents=100,maxEntries=256}={}) {
  const states=new Map();
  const checkpoint=join(home,'.cache','herdr-engineering','transcript-offsets.json');
  const saved=new Map();
  try {
    const data=readRange(checkpoint,0,128*1024);
    if(data && data.size<=128*1024) for(const [key,value] of Object.entries(JSON.parse(data.buffer.toString())).slice(-maxEntries)) {
      if(/^[a-f0-9]{64}$/.test(key) && Number.isSafeInteger(value?.offset) && value.offset>=0 && typeof value.identity==='string') saved.set(key,value);
    }
  } catch { /* first run or invalid checkpoint: bounded replay with stable IDs */ }
  function save(key,state) {
    saved.delete(key); saved.set(key,{identity:state.identity,offset:state.offset,skipLine:state.skipLine,dropped:state.dropped});
    while(saved.size>Math.max(1,maxEntries)) saved.delete(saved.keys().next().value);
    const temporary=`${checkpoint}.${process.pid}.tmp`;
    try {
      mkdirSync(dirname(checkpoint),{recursive:true,mode:0o700});
      writeFileSync(temporary,JSON.stringify(Object.fromEntries(saved)),{mode:0o600});
      renameSync(temporary,checkpoint);
    } catch { try {unlinkSync(temporary);} catch { /* best effort */ } }
  }
  maxBytes=Math.max(128,Math.min(MAX_BYTES,maxBytes));
  maxEvents=Math.max(1,Math.min(100,maxEvents));
  return async function collect(server,pane,sessionId) {
    const empty=status=>({status,envelopes:[],truncated:false});
    if(!rootFor(pane.agent,home)) return empty('unsupported');
    let pids=[];
    if(!pane.agent_session && queryProcess) {
      const info=await queryProcess(server,pane.pane_id).catch(()=>null);
      pids=(info?.result?.process_info?.foreground_processes ?? []).map(p=>p?.pid);
    }
    const path=await resolveTranscript(pane.agent,pane.agent_session,{home,env,pids,procRoot});
    if(!path) return empty('identity_unavailable');
    const key=hash(`${sessionId}:${path}`);
    let state=states.get(key) || saved.get(key);
    if(state?.pending) return state.pending;
    const probe=readRange(path,0,0);
    if(!probe) return empty('unreadable');
    if(!state || state.identity!==probe.identity || probe.size<state.offset) state={identity:probe.identity,offset:0,skipLine:false,dropped:false};
    states.delete(key); states.set(key,state);
    while(states.size>Math.max(1,maxEntries)) states.delete(states.keys().next().value);
    const read=readRange(path,state.offset,maxBytes);
    if(!read) return empty('unreadable');
    const events=[];
    let cursor=0, skipLine=state.skipLine, dropped=state.dropped, eventBytes=0;
    while(cursor<read.buffer.length) {
      const end=read.buffer.indexOf(10,cursor);
      if(end<0) break; // A concurrently written partial record is retried.
      const start=cursor;
      if(skipLine) {skipLine=false; cursor=end+1; continue;}
      const raw=read.buffer.subarray(start,end).toString('utf8');
      let record, normalized=[];
      try {record=JSON.parse(raw); normalized=normalizeRecord(pane.agent,record);} catch { /* malformed native line */ }
      const size=Buffer.byteLength(JSON.stringify(normalized));
      if(events.length && (events.length+normalized.length>maxEvents || eventBytes+size>192*1024)) break;
      eventBytes+=size;
      if(normalized.length>maxEvents) dropped=true;
      cursor=end+1;
      for(const [index,event] of normalized.slice(0,maxEvents).entries()) {
        const eventId=hash(`${sessionId}:${path}:${state.offset+start}:${index}:${raw}`);
        events.push({...event,...(Buffer.byteLength(raw)>BODY_BYTES?{truncated:true}:{}),event_id:eventId,...(event.kind==='message'?{message_id:eventId}:{}),agent_id:'main',...(record.timestamp?{ts:record.timestamp}:{})});
      }
      if(events.length>=maxEvents) break;
    }
    // Oversized records cannot pin the reader forever. Discard through their
    // next newline and expose truncation; never parse a cut line as a message.
    if(cursor===0 && read.buffer.length===maxBytes && read.buffer.indexOf(10)<0) {cursor=read.buffer.length;skipLine=true;dropped=true;}
    const truncated=dropped || skipLine || state.offset+cursor<read.size;
    const commit=()=>{state.offset+=cursor;state.skipLine=skipLine;state.dropped=dropped;state.pending=null;if(cursor)save(key,state);};
    const result={status:'recording',truncated,envelopes:[]};
    if(!events.length) {commit();return result;}
    const envelope={source:'herdr',entity_type:'inspector',entity_id:sessionId,event_type:'inspector.transcript',payload:{events}};
    Object.defineProperty(envelope,'onPublished',{value:()=>{if(state.pending===result)commit();}});
    result.envelopes.push(envelope);state.pending=result;
    return result;
  };
}
