// Bounded native transcript capture. Only explicit native identity or process
// evidence may associate a log with a pane; cwd and file age are never evidence.
import { closeSync, fstatSync, statSync, openSync, readSync, realpathSync, opendirSync, readlinkSync, mkdirSync, writeFileSync, renameSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { homedir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { controlDir } from './session-model.js';

const MAX_BYTES = 256 * 1024;
const BODY_BYTES = 16 * 1024;
const CHECKPOINT_BYTES = 2 * 1024 * 1024;
const ID = /^[a-zA-Z0-9][a-zA-Z0-9-]{7,79}$/;
const hash = value => createHash('sha256').update(value).digest('hex');
const bounded = value => Buffer.from(typeof value === 'string' ? value : JSON.stringify(value ?? '')).subarray(0,BODY_BYTES).toString('utf8');
function text(content) {
  if (typeof content === 'string') return bounded(content);
  if (!Array.isArray(content)) return '';
  return bounded(content.filter(b => ['text','input_text','output_text'].includes(b?.type)).map(b => b.text || '').join('\n'));
}

function toolStatus(output) {
  let value=output;
  if(typeof output==='string') {try {value=JSON.parse(output);} catch { /* ordinary text output */ }}
  const explicit=value && typeof value==='object' ? value : {};
  const match=typeof output==='string' ? output.match(/(?:^|\n)Process exited with code (-?\d+)(?:\r?\n|$)/) : null;
  const code=Number.isInteger(explicit.exit_code) ? explicit.exit_code : match ? Number(match[1]) : null;
  if(code!==null) return {exit_code:code,is_error:code!==0};
  if(typeof explicit.is_error==='boolean') return {is_error:explicit.is_error};
  return {};
}
function fileOperations(tool, input, call_id) {
  const name=tool?.split('.').at(-1)?.toLowerCase();
  let args=input;
  if(typeof input==='string') {try {args=JSON.parse(input);} catch { /* freeform patch */ }}
  if(name==='apply_patch') {
    const patch=typeof args==='string' ? args : args?.patch ?? args?.input;
    if(typeof patch!=='string') return [];
    return [...patch.matchAll(/^\*\*\* (Update|Add|Delete) File: ([^\r\n]+)\r?\n([\s\S]*?)(?=^\*\*\* (?:Update|Add|Delete) File:|^\*\*\* End Patch|$(?![\s\S]))/gm)]
      .slice(0,32).map(m=>({kind:'file',call_id,path:m[2],op:({Update:'edit',Add:'write',Delete:'delete'})[m[1]],diff:bounded(m[3])}));
  }
  const path=args?.file_path ?? args?.path;
  if(typeof path!=='string' || !['write','edit'].includes(name)) return [];
  const event={kind:'file',call_id,path,op:name};
  const oldText=args.old_string ?? args.oldText, newText=args.new_string ?? args.newText;
  if(name==='edit' && typeof oldText==='string' && typeof newText==='string') event.diff=bounded(oldText.split('\n').map(l=>'-'+l).join('\n')+'\n'+newText.split('\n').map(l=>'+'+l).join('\n'));
  return [event];
}
function recordTools(events, context, inputs={}) {
  context.pendingTools ??= {};
  const files=[];
  for(const event of events) {
    if(event.kind!=='tool') continue;
    if(event.phase==='start') {
      const operations=fileOperations(event.tool,inputs[event.call_id] ?? event.args_preview,event.call_id);
      if(operations.length) context.pendingTools[event.call_id]=operations;
      const keys=Object.keys(context.pendingTools);
      for(const key of keys.slice(0,Math.max(0,keys.length-64))) delete context.pendingTools[key];
      while(Buffer.byteLength(JSON.stringify(context.pendingTools))>64*1024) delete context.pendingTools[Object.keys(context.pendingTools)[0]];
    } else if(event.phase==='end') {
      const operations=context.pendingTools[event.call_id];
      if(operations && (event.is_error===false || (event.is_error!==true && /^Success\. Updated the following files:/m.test(event.output)))) files.push(...operations);
      delete context.pendingTools[event.call_id];
    }
  }
  return [...events,...files];
}
export function normalizeRecord(agent, record, context={}) {
  if (!record || typeof record !== 'object') return [];
  let message;
  if (agent === 'codex') {
    const p = record.payload;
    if (!p) return [];
    if(record.type==='turn_context' && typeof p.model==='string') return [{kind:'model',model:p.model}];
    if(record.type==='event_msg' && p.type==='token_count' && p.info?.total_token_usage) {
      const totals=p.info.total_token_usage, previous=context.usageTotals || {};
      const event={kind:'usage'};
      for(const [target,source] of Object.entries({input_tokens:'input_tokens',output_tokens:'output_tokens',reasoning_tokens:'reasoning_output_tokens',cache_read_tokens:'cached_input_tokens'})) {
        if(Number.isSafeInteger(totals[source]) && totals[source]>=0) event[target]=Math.max(0,totals[source]-(previous[source] || 0));
      }
      if(Number.isSafeInteger(p.info.last_token_usage?.total_tokens)) event.context_tokens=p.info.last_token_usage.total_tokens;
      if(Number.isSafeInteger(p.info.model_context_window)) event.context_window=p.info.model_context_window;
      context.usageTotals=totals;
      return [event];
    }
    // event_msg repeats response_item messages and may include private reasoning.
    if (record.type !== 'response_item') return [];
    if (['function_call','custom_tool_call'].includes(p.type) && p.call_id) return recordTools([{kind:'tool',call_id:p.call_id,phase:'start',tool:p.name,args_preview:bounded(p.arguments ?? p.input)}],context,{[p.call_id]:p.arguments ?? p.input});
    if (['function_call_output','custom_tool_call_output'].includes(p.type) && p.call_id) return recordTools([{kind:'tool',call_id:p.call_id,phase:'end',output:bounded(p.output),...toolStatus(p.output)}],context);
    // Execution activity is public even when dispatched from the analysis channel.
    if (p.type !== 'message' || p.channel === 'analysis') return [];
    message = p;
  } else if (agent === 'pi' && record.type === 'message') message = record.message;
  else if (agent === 'claude' && ['user','assistant'].includes(record.type)) message = record.message;
  if (!message || !['user','assistant','toolResult'].includes(message.role)) return [];
  if (message.role === 'toolResult') return message.toolCallId ? recordTools([{kind:'tool',call_id:message.toolCallId,phase:'end',tool:message.toolName,output:text(message.content),is_error:!!message.isError}],context) : [];
  const result = [], inputs={};
  const body = text(message.content);
  if (body) result.push({kind:'message',role:message.role,text:body});
  for (const block of Array.isArray(message.content) ? message.content : []) {
    if (['toolCall','tool_use'].includes(block?.type) && block.id) {inputs[block.id]=block.arguments ?? block.input;result.push({kind:'tool',call_id:block.id,phase:'start',tool:block.name,args_preview:bounded(inputs[block.id])});}
    if (block?.type === 'tool_result' && block.tool_use_id) result.push({kind:'tool',call_id:block.tool_use_id,phase:'end',output:text(block.content),is_error:!!block.is_error});
  }
  if(message.role==='assistant' && message.usage) {
    const usage=message.usage, event={kind:'usage'};
    for(const [key,value] of Object.entries({input_tokens:usage.input_tokens ?? usage.input,output_tokens:usage.output_tokens ?? usage.output,cache_read_tokens:usage.cache_read_input_tokens ?? usage.cacheRead})) if(Number.isSafeInteger(value)&&value>=0)event[key]=value;
    if(typeof message.model==='string')event.model=bounded(message.model);
    if(typeof message.id==='string') {
      context.messageUsage ??= {};
      const usageKey=hash(message.id), previous=context.messageUsage[usageKey] || {};
      const current={...event};
      for(const key of ['input_tokens','output_tokens','cache_read_tokens'])if(Number.isSafeInteger(event[key]))event[key]=Math.max(0,event[key]-(previous[key]||0));
      context.messageUsage[usageKey]=current;
      for(const key of Object.keys(context.messageUsage).slice(0,-64))delete context.messageUsage[key];
    }
    result.push(event);
  }
  return recordTools(result,context,inputs);
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
const identityWalks = new Map();
const identityName = (name,id) => name===`${id}.jsonl` || name.endsWith(`_${id}.jsonl`) || name.endsWith(`-${id}.jsonl`);
function nativeHeader(path) {
  try { return JSON.parse(readRange(path,0,64*1024)?.buffer.toString().split('\n')[0]); } catch { return null; }
}
function dateBucket(root,id) {
  if(!/^[a-f0-9]{8}-[a-f0-9]{4}-7[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(id)) return null;
  const date=new Date(parseInt(id.slice(0,8)+id.slice(9,13),16));
  if(!Number.isFinite(date.getTime())) return null;
  return join(root,...date.toISOString().slice(0,10).split('-'));
}
function findIdentity(root, id, agent) {
  // Codex UUIDv7 IDs encode their creation date. Exact filenames plus native
  // metadata provide identity without scanning years of unrelated history.
  const bucket=agent==='codex' ? dateBucket(root,id) : null;
  if(bucket) {
    const children=entries(bucket,2049);
    if(children.length<2049) {
      const matches=children.filter(e=>e.isFile() && identityName(e.name,id))
        .map(e=>confined(join(bucket,e.name),root)).filter(Boolean)
        .filter(path=>{const header=nativeHeader(path);return header?.type==='session_meta' && header.payload?.id===id;});
      if(matches.length===1) return matches[0];
      if(matches.length>1) return null;
    }
  }
  // Retain the open directory cursor across polls; restarting the first 2048
  // entries can otherwise make older UUIDs and busy Claude/Pi roots unreachable.
  const key=`${root}:${id}`;
  let walk=identityWalks.get(key);
  if(!walk) walk={queue:[{path:root,depth:0}],matches:[],dir:null,current:null};
  identityWalks.delete(key);identityWalks.set(key,walk);
  while(identityWalks.size>256) {
    const oldest=identityWalks.keys().next().value;
    identityWalks.get(oldest).dir?.closeSync();identityWalks.delete(oldest);
  }
  let remaining=2048;
  while(remaining>0 && (walk.dir || walk.queue.length)) {
    if(!walk.dir) {
      walk.current=walk.queue.shift();
      try {walk.dir=opendirSync(walk.current.path);} catch {continue;}
    }
    let e;
    try {e=walk.dir.readSync();} catch {e=null;}
    if(!e) {walk.dir.closeSync();walk.dir=null;continue;}
    remaining--;
    const path=join(walk.current.path,e.name);
    if(e.isDirectory() && walk.current.depth<4) walk.queue.push({path,depth:walk.current.depth+1});
    else if(e.isFile() && identityName(e.name,id)) walk.matches.push(path);
  }
  if(walk.dir || walk.queue.length) return null;
  identityWalks.delete(key);
  return walk.matches.length===1 ? confined(walk.matches[0],root) : null;
}

function procText(path,limit=8192) {
  let fd;
  try {
    fd=openSync(path,'r');const buffer=Buffer.alloc(Math.min(limit,64*1024));
    return buffer.subarray(0,readSync(fd,buffer,0,buffer.length,0)).toString();
  } catch { return ''; } finally {if(fd!==undefined)closeSync(fd);}
}
function descendantIdentities(processIds,procRoot,paneId) {
  if(typeof paneId!=='string' || !paneId || typeof process.getuid!=='function')return [];
  const queue=[...processIds].map(pid=>({pid,depth:0})),visited=new Set(),identities=new Set();
  while(queue.length && visited.size<64) {
    const {pid,depth}=queue.shift();
    if(visited.has(pid))continue;visited.add(pid);
    const path=join(procRoot,String(pid));
    try {if(statSync(path).uid!==process.getuid())continue;} catch {continue;}
    // Parent processes can inherit another session's identity. Only tool
    // descendants with an explicit matching pane marker establish a binding.
    if(depth>0) {
      const values=Object.fromEntries(procText(join(path,'environ'),64*1024).split('\0').slice(0,-1).filter(v=>v.includes('=')).map(v=>{const i=v.indexOf('=');return [v.slice(0,i),v.slice(i+1)];}));
      if(values.HERDR_PANE_ID===paneId) {
        const ids=[values.CODEX_THREAD_ID,values.CODEX_SESSION_ID].filter(v=>ID.test(v || ''));
        if(new Set(ids).size===1)identities.add(ids[0]);
      }
    }
    if(depth<6)for(const raw of procText(join(path,'task',String(pid),'children')).trim().split(/\s+/).slice(0,64)) {
      const child=Number(raw);if(Number.isSafeInteger(child)&&child>0&&queue.length<64)queue.push({pid:child,depth:depth+1});
    }
  }
  return [...identities];
}

function currentClaudeProcess(descriptor,pid,procRoot,platform,machineIdPath) {
  // A PID alone is reusable; Claude records the native process lifetime and
  // domain so stale descriptors cannot attach an unrelated conversation.
  if(platform!=='linux' || descriptor?.pid!==pid || !/^\d+$/.test(descriptor?.procStart || ''))return false;
  const path=join(procRoot,String(pid));
  try {if(typeof process.getuid==='function' && statSync(path).uid!==process.getuid())return false;} catch {return false;}
  const stat=procText(join(path,'stat'));
  if(!stat.startsWith(`${pid} (`))return false;
  // comm may itself contain spaces and parentheses; fields after its closing
  // parenthesis begin at field 3, so starttime (field 22) is index 19.
  const fields=stat.slice(stat.lastIndexOf(')')+1).trim().split(/\s+/);
  if(fields[19]!==String(descriptor.procStart))return false;
  if(descriptor.pidDomain!==undefined) {
    // Claude's linux domain identifies the machine, not the kernel boot.
    const machine=procText(machineIdPath).trim();
    let namespace;try {namespace=readlinkSync(join(path,'ns','pid'));} catch {return false;}
    const domain=typeof descriptor.pidDomain==='string' ? descriptor.pidDomain.match(/^linux:([a-fA-F0-9-]+):(pid:\[\d+\])$/) : null;
    const canonicalMachine=value=>value.replaceAll('-','').toLowerCase();
    if(!domain || !/^[a-f0-9]{32}$/.test(canonicalMachine(machine)) || canonicalMachine(domain[1])!==canonicalMachine(machine) || domain[2]!==namespace)return false;
  }
  return true;
}

export async function resolveTranscript(agent, evidence, {home=homedir(),env=process.env,pids=[],procRoot='/proc',paneId,platform=process.platform,machineIdPath='/etc/machine-id'}={}) {
  const root=rootFor(agent,home);
  if(!root) return null;
  if(evidence?.agent && evidence.agent!==agent) return null;
  if(evidence?.kind==='path' && typeof evidence.value==='string') return confined(resolve(evidence.value),root);
  if(evidence?.kind==='id' && ID.test(evidence.value || '')) return findIdentity(root,evidence.value,agent);
  const candidates=new Set();
  const processIds=new Set(pids.filter(p=>Number.isInteger(p)&&p>0).slice(0,32));
  if(agent==='pi') {
    for(const entry of entries(controlDir(env),256)) {
      if(!entry.isFile() || !entry.name.endsWith('.json')) continue;
      const d=jsonFile(join(controlDir(env),entry.name));
      if(processIds.has(d?.pid) && ID.test(d?.sessionId || '')) { const file=findIdentity(root,d.sessionId,agent); if(file) candidates.add(file); }
    }
  } else if(agent==='claude') {
    for(const pid of processIds) { const d=jsonFile(join(home,'.claude','sessions',`${pid}.json`)); if(ID.test(d?.sessionId || '') && currentClaudeProcess(d,pid,procRoot,platform,machineIdPath)) {const file=findIdentity(root,d.sessionId,agent); if(file)candidates.add(file);} }
  } else if(agent==='codex') {
    for(const pid of processIds) for(const entry of entries(join(procRoot,String(pid),'fd'),256)) {
      let target; try { target=readlinkSync(join(procRoot,String(pid),'fd',entry.name)); } catch {continue;}
      const file=confined(target,root);
      if(!file) continue;
      const header=nativeHeader(file);
      if(header?.type==='session_meta' && ID.test(header.payload?.id || '')) candidates.add(file);
    }
  }
  if(agent==='codex')for(const id of descendantIdentities(processIds,procRoot,paneId)) {
    const file=findIdentity(root,id,agent);if(!file)continue;
    const header=nativeHeader(file);
    if(header?.type==='session_meta' && header.payload?.id===id && typeof header.payload?.source==='string')candidates.add(file);
  }
  return candidates.size===1 ? [...candidates][0] : null;
}

export function createTranscriptCollector({home=homedir(),env=process.env,queryProcess,procRoot='/proc',machineIdPath='/etc/machine-id',maxBytes=MAX_BYTES,maxEvents=100,maxEntries=256}={}) {
  const states=new Map();
  const checkpoint=join(home,'.cache','herdr-engineering','transcript-offsets.json');
  const saved=new Map();
  try {
    const data=readRange(checkpoint,0,CHECKPOINT_BYTES);
    if(data && data.size<=CHECKPOINT_BYTES) for(const [key,value] of Object.entries(JSON.parse(data.buffer.toString())).slice(-maxEntries)) {
      if(/^[a-f0-9]{64}$/.test(key) && Number.isSafeInteger(value?.offset) && value.offset>=0 && typeof value.identity==='string') saved.set(key,value);
    }
  } catch { /* first run or invalid checkpoint: bounded replay with stable IDs */ }
  function save(key,state) {
    saved.delete(key); saved.set(key,{identity:state.identity,offset:state.offset,skipLine:state.skipLine,dropped:state.dropped,context:state.context});
    while(saved.size>Math.max(1,maxEntries)) saved.delete(saved.keys().next().value);
    while(saved.size>1 && Buffer.byteLength(JSON.stringify(Object.fromEntries(saved)))>CHECKPOINT_BYTES) saved.delete(saved.keys().next().value);
    const temporary=`${checkpoint}.${process.pid}.tmp`;
    try {
      mkdirSync(dirname(checkpoint),{recursive:true,mode:0o700});
      writeFileSync(temporary,JSON.stringify(Object.fromEntries(saved)),{mode:0o600});
      renameSync(temporary,checkpoint);
    } catch { try {unlinkSync(temporary);} catch { /* best effort */ } }
  }
  maxBytes=Math.max(128,Math.min(MAX_BYTES,maxBytes));
  maxEvents=Math.max(1,Math.min(100,maxEvents));
  return async function collect(server,pane,sessionId,{agentId='main',parentAgentId}={}) {
    const empty=status=>({status,envelopes:[],truncated:false});
    if(!rootFor(pane.agent,home)) return empty('unsupported');
    let pids=[];
    if(!pane.agent_session && queryProcess) {
      const info=await queryProcess(server,pane.pane_id).catch(()=>null);
      pids=(info?.result?.process_info?.foreground_processes ?? []).map(p=>p?.pid);
    }
    const path=await resolveTranscript(pane.agent,pane.agent_session,{home,env,pids,procRoot,machineIdPath,paneId:pane.pane_id});
    if(!path) return empty('identity_unavailable');
    const key=hash(`${sessionId}:${path}${agentId==='main'?'':`:${agentId}`}`);
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
    let context=structuredClone(state.context || {});
    let cursor=0, skipLine=state.skipLine, dropped=state.dropped, eventBytes=0;
    while(cursor<read.buffer.length) {
      const end=read.buffer.indexOf(10,cursor);
      if(end<0) break; // A concurrently written partial record is retried.
      const start=cursor;
      if(skipLine) {skipLine=false; cursor=end+1; continue;}
      const raw=read.buffer.subarray(start,end).toString('utf8');
      let record, normalized=[];
      const nextContext=structuredClone(context);
      try {record=JSON.parse(raw); normalized=normalizeRecord(pane.agent,record,nextContext);} catch { /* malformed native line */ }
      const size=Buffer.byteLength(JSON.stringify(normalized));
      if(events.length && (events.length+normalized.length>maxEvents || eventBytes+size>192*1024)) break;
      context=nextContext;
      eventBytes+=size;
      if(normalized.length>100) dropped=true;
      cursor=end+1;
      for(const [index,event] of normalized.slice(0,100).entries()) {
        const eventId=hash(`${sessionId}:${path}:${state.offset+start}:${index}:${raw}${agentId==='main'?'':`:${agentId}`}`);
        events.push({...event,...(Buffer.byteLength(raw)>BODY_BYTES?{truncated:true}:{}),event_id:eventId,...(event.kind==='message'?{message_id:eventId}:{}),agent_id:agentId,...(parentAgentId?{parent_agent_id:parentAgentId}:{}),...(record.timestamp?{ts:record.timestamp}:{})});
      }
      if(events.length>=maxEvents) break;
    }
    // Oversized records cannot pin the reader forever. Discard through their
    // next newline and expose truncation; never parse a cut line as a message.
    if(cursor===0 && read.buffer.length===maxBytes && read.buffer.indexOf(10)<0) {cursor=read.buffer.length;skipLine=true;dropped=true;}
    const truncated=dropped || skipLine || state.offset+cursor<read.size;
    const commit=()=>{state.offset+=cursor;state.skipLine=skipLine;state.dropped=dropped;state.context=context;state.pending=null;if(cursor)save(key,state);};
    const result={status:'recording',truncated,envelopes:[]};
    const header=nativeHeader(path);
    const nativeId=header?.type==='session_meta' ? header.payload?.id : header?.sessionId ?? (header?.type==='session' ? header.id : undefined);
    Object.defineProperty(result,'identity',{value:{agent:pane.agent,kind:'path',value:path,nativeId:ID.test(nativeId || '')?nativeId:null}});
    if(!events.length) {commit();return result;}
    const envelope={source:'herdr',entity_type:'inspector',entity_id:sessionId,event_type:'inspector.transcript',payload:{events}};
    Object.defineProperty(envelope,'onPublished',{value:()=>{if(state.pending===result)commit();}});
    result.envelopes.push(envelope);state.pending=result;
    return result;
  };
}
