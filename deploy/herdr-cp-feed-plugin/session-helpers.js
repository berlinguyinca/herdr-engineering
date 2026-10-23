// Private transcript bindings survive pane exit. Codex helper membership comes
// exclusively from its session_meta parent_thread_id, never cwd or file age.
import {closeSync, fstatSync, mkdirSync, openSync, opendirSync, readFileSync, readSync, realpathSync, renameSync, statSync, unlinkSync, writeFileSync} from 'node:fs';
import {createHash, randomUUID} from 'node:crypto';
import {homedir} from 'node:os';
import {dirname, join, sep} from 'node:path';

const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const roots=(home,agent)=>({codex:join(home,'.codex','sessions'),claude:join(home,'.claude','projects'),pi:join(home,'.pi','agent','sessions')})[agent];
function confined(home,identity) {
  const root=roots(home,identity?.agent);
  if(!root || identity.kind!=='path' || typeof identity.value!=='string')return null;
  try {
    const path=realpathSync(identity.value);
    return path.startsWith(realpathSync(root)+sep) && path.endsWith('.jsonl') && statSync(path).isFile()?path:null;
  }catch{return null;}
}
function header(path) {
  let fd;
  try {
    fd=openSync(path,'r');
    if(!fstatSync(fd).isFile())return null;
    // Read only the first record, bounded even if a corrupt file has no newline.
    const chunks=[];
    for(let offset=0;offset<65536;offset+=4096) {
      const buffer=Buffer.alloc(4096),count=readSync(fd,buffer,0,buffer.length,offset);
      const end=buffer.subarray(0,count).indexOf(10);
      chunks.push(buffer.subarray(0,end<0?count:end));
      if(end>=0 || count<4096)return JSON.parse(Buffer.concat(chunks).toString('utf8'));
    }
  }catch{/* Missing or incomplete metadata is not parent evidence. */}
  finally {if(fd!==undefined)closeSync(fd);}
  return null;
}
function walker(paths) {return {queue:paths.map(path=>({path,depth:0})),dir:null,current:null};}
function walk(scan,limit,visit) {
  let remaining=limit;
  while(remaining>0 && (scan.dir || scan.queue.length)) {
    if(!scan.dir) {
      scan.current=scan.queue.shift();remaining--;
      try{scan.dir=opendirSync(scan.current.path);}catch{continue;}
    }
    let entry;
    try{entry=scan.dir.readSync();}catch{entry=null;}
    if(!entry){scan.dir.closeSync();scan.dir=null;continue;}
    remaining--;
    const path=join(scan.current.path,entry.name);
    if(entry.isDirectory() && scan.current.depth<3 && scan.queue.length<4096)scan.queue.push({path,depth:scan.current.depth+1});
    else if(entry.isFile() && entry.name.endsWith('.jsonl'))visit(path);
  }
  return !!(scan.dir || scan.queue.length);
}

function validateBinding(home,value) {
  if(!value || typeof value.sessionId!=='string' || !/^herdr:[A-Za-z0-9_.:%-]{1,500}$/.test(value.sessionId)
    || typeof value.server!=='string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.server)
    || typeof value.paneId!=='string' || value.paneId.length===0 || value.paneId.length>200)return null;
  const evidence=value.identity;
  if(!evidence || !roots(home,evidence.agent))return null;
  let identity;
  if(evidence.kind==='id' && /^[a-zA-Z0-9][a-zA-Z0-9-]{7,79}$/.test(evidence.value || '')) {
    identity={agent:evidence.agent,kind:'id',value:evidence.value,nativeId:evidence.value};
  }else {
    const path=confined(home,evidence);
    if(!path)return null;
    const record=evidence.agent==='codex'?header(path):null;
    const nativeId=record?.type==='session_meta' && UUID.test(record.payload?.id || '')?record.payload.id:evidence.nativeId || null;
    identity={agent:evidence.agent,kind:'path',value:path,nativeId};
  }
  return {sessionId:value.sessionId,server:value.server,paneId:value.paneId,identity};
}
const bindingKey=binding=>`${binding.sessionId}:${binding.identity.agent}:${binding.identity.nativeId || binding.identity.value}`;
function storeBinding(home,binding) {
  const directory=join(home,'.cache','herdr-engineering','transcript-bindings');
  const path=join(directory,createHash('sha256').update(bindingKey(binding)).digest('hex')+'.json');
  const temporary=`${path}.${process.pid}.${randomUUID()}.tmp`;
  try {
    mkdirSync(directory,{recursive:true,mode:0o700});
    writeFileSync(temporary,JSON.stringify(binding),{mode:0o600});
    renameSync(temporary,path);
    return true;
  }catch{try{unlinkSync(temporary);}catch{/* best effort */}return false;}
}
// Native event hooks can hand off exact identities before a short pane exits.
// Separate atomic files prevent concurrent hook processes overwriting siblings.
export function recordSessionBinding(envelope,{home=homedir()}={}) {
  if(envelope?.entity_type!=='session')return false;
  const payload=envelope.payload;
  if(!payload?.agent_session || (payload.agent_session.agent && payload.agent_session.agent!==payload.agent_role))return false;
  const binding=validateBinding(home,{sessionId:envelope.entity_id,server:payload.herdr_session,paneId:payload.pane_id,
    identity:{...payload.agent_session,agent:payload.agent_role}});
  return binding?storeBinding(home,binding):false;
}

export function createSessionTracker({home=homedir(),collectTranscript,now=Date.now,scanEntries=256,maxBindings=256,maxCollect=64}={}) {
  const bindings=new Map(), active=new Map(), helpers=new Map(), inspected=new Map(),loaded=new Map();
  const checkpoint=join(home,'.cache','herdr-engineering','transcript-bindings');
  const root=roots(home,'codex');
  let backlog=null,recent=null,bindingScan=null,rotation=0;
  function add(binding) {
    const key=bindingKey(binding);
    bindings.delete(key);bindings.set(key,binding);
    while(bindings.size>maxBindings)bindings.delete(bindings.keys().next().value);
  }
  function loadBindings() {
    try{bindingScan??=opendirSync(checkpoint);}catch{return;}
    for(let i=0;i<128;i++) {
      let entry;try{entry=bindingScan.readSync();}catch{entry=null;}
      if(!entry){bindingScan.closeSync();bindingScan=null;break;}
      if(!entry.isFile() || !/^[a-f0-9]{64}\.json$/.test(entry.name))continue;
      const path=join(checkpoint,entry.name);
      try {
        const stat=statSync(path),key=`${stat.ino}:${stat.mtimeMs}:${stat.size}`;
        if(stat.size>8192 || loaded.get(path)===key)continue;
        const binding=validateBinding(home,JSON.parse(readFileSync(path,'utf8')));
        if(binding)add(binding);
        loaded.delete(path);loaded.set(path,key);
        while(loaded.size>4096)loaded.delete(loaded.keys().next().value);
      }catch{/* Invalid event handoffs establish no identity. */}
    }
  }
  function remember(value) {
    const binding=validateBinding(home,value);
    if(!binding)return;
    const key=bindingKey(binding);active.set(binding.sessionId,key);
    while(active.size>maxBindings)active.delete(active.keys().next().value);
    if(JSON.stringify(bindings.get(key))===JSON.stringify(binding))return;
    add(binding);storeBinding(home,binding);
  }
  function inspect(path) {
    let stat;try{stat=statSync(path);}catch{return;}
    const key=`${stat.dev}:${stat.ino}`;
    const previous=inspected.get(path);
    if(previous?.key===key && (previous.complete || previous.size===stat.size))return;
    const record=header(path),id=record?.payload?.id,parent=record?.payload?.source?.subagent?.thread_spawn?.parent_thread_id;
    inspected.delete(path);inspected.set(path,{key,size:stat.size,complete:record?.type==='session_meta'});
    while(inspected.size>8192)inspected.delete(inspected.keys().next().value);
    if(record?.type!=='session_meta' || !UUID.test(id || '') || !UUID.test(parent || '') || id===parent)return;
    const identity={agent:'codex',kind:'path',value:path,nativeId:id};
    if(!confined(home,identity))return;
    // Multiple different files claiming one UUID are ambiguous, not a choice.
    const prior=helpers.get(id);
    helpers.set(id,prior && (prior.path!==path || prior.parent!==parent)?{ambiguous:true}:{id,parent,path,identity});
    while(helpers.size>8192)helpers.delete(helpers.keys().next().value);
  }
  function scan() {
    if(!recent)recent=walker(Array.from({length:3},(_,i)=>join(root,...new Date(now()-i*86400000).toISOString().slice(0,10).split('-'))));
    if(!backlog)backlog=walker([root]);
    const budget=Math.max(2,Math.min(2048,scanEntries));
    if(!walk(recent,Math.ceil(budget/2),inspect))recent=null;
    if(!walk(backlog,Math.floor(budget/2),inspect))backlog=null;
  }
  async function collect(seen=new Set()) {
    loadBindings();
    scan();
    const work=[];
    for(const binding of bindings.values()) {
      if(!seen.has(binding.sessionId) || active.get(binding.sessionId)!==bindingKey(binding))work.push({...binding,agentId:'main'});
      if(binding.identity.agent!=='codex' || !UUID.test(binding.identity.nativeId || ''))continue;
      const parents=new Map([[binding.identity.nativeId,'main']]);
      // Bounded transitive expansion supports helpers spawning their own helpers.
      let changed=true;
      while(changed && parents.size<=256) {
        changed=false;
        for(const helper of helpers.values()) {
          if(helper.ambiguous || parents.has(helper.id) || !parents.has(helper.parent))continue;
          parents.set(helper.id,helper.id);changed=true;
          work.push({...binding,identity:helper.identity,agentId:helper.id,parentAgentId:parents.get(helper.parent)});
          if(parents.size>256)break;
        }
      }
    }
    const envelopes=[];
    const count=Math.min(maxCollect,work.length);
    for(let i=0;i<count;i++) {
      const item=work[(rotation+i)%work.length];
      const pane={pane_id:item.paneId,agent:item.identity.agent,agent_session:item.identity};
      const result=await collectTranscript(item.server,pane,item.sessionId,{agentId:item.agentId,parentAgentId:item.parentAgentId}).catch(()=>({envelopes:[]}));
      envelopes.push(...result.envelopes);
    }
    rotation=work.length?(rotation+count)%work.length:0;
    return {envelopes,truncated:work.length>count};
  }
  return {remember,collect};
}
