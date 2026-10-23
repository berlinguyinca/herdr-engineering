// Shared session projections. Associations come from recorded identity, never path similarity.
export function pendingOperation(storage,key,text,generate) {
  try{const previous=JSON.parse(storage?.getItem(key) || 'null');if(previous?.text===text && typeof previous.id==='string' && previous.id)return previous.id;}catch{/* optional storage */}
  const id=generate();
  try{storage?.setItem(key,JSON.stringify({text,id}));}catch{/* optional storage */}
  return id;
}

export function selectEvents(events, {pane='conversation', agent='', type='', q='', errors=false}={}) {
  const needle=q.trim().toLowerCase();
  return events.filter(e=>{
    if(e.kind==='reasoning' || (pane==='conversation' ? e.kind!=='message' || !['user','assistant'].includes(e.data?.role) : e.kind==='message'))return false;
    if(agent && (e.agent_id || 'main')!==agent)return false;
    if(pane==='activity' && type && e.kind!==type)return false;
    if(pane==='activity' && errors && !['error','failed','interrupted'].includes(e.data?.status))return false;
    return !needle || JSON.stringify(e.data || {}).toLowerCase().includes(needle);
  }).sort((a,b)=>pane==='conversation' ? a.seq-b.seq : b.seq-a.seq);
}

export function linkedAgents(session, agents=[], sessions=[], collaborators=[]) {
  const result=agents.map(a=>({...a,...(a.agent_id==='main' && session.display_name?{display_name:session.display_name,agent_handle:session.agent_handle || a.agent_handle}:{}),relation:a.parent_agent_id ? 'helper' : 'main'}));
  for(const s of sessions) {
    if(s.session_id===session.session_id)continue;
    if(s.parent_session_id===session.session_id)result.push({...s,relation:'helper'});
  }
  const parent=session.parent_session_id || sessions.find(s=>s.session_id===session.session_id)?.parent_session_id;
  if(parent)result.push({...sessions.find(s=>s.session_id===parent),session_id:parent,relation:'parent'});
  const resolveInstance=instance=>{
    const candidates=sessions.filter(s=>s.session_id===instance || (s.host_id && s.pane_id && instance===`pane:${s.host_id}:${s.pane_id}`));
    return candidates.length===1?candidates[0]:null;
  };
  const roots=collaborators.filter(a=>a.instance_id===session.session_id || resolveInstance(a.instance_id)?.session_id===session.session_id);
  const ids=new Set(roots.map(a=>a.agent_id));
  // Bounded traversal supports grandchildren; recorded parent links are the only edges.
  for(let pass=0;pass<8;pass++)for(const a of collaborators)if(ids.has(a.parent_agent_id))ids.add(a.agent_id);
  for(const a of collaborators)if(ids.has(a.agent_id) || roots.some(r=>r.parent_agent_id===a.agent_id)){
    const target=resolveInstance(a.instance_id);
    result.push({collab_agent_id:a.agent_id,agent_role:a.name,display_name:target?.display_name,agent_handle:a.name,parent_agent_id:a.parent_agent_id,
      session_id:target?.session_id,relation:roots.some(r=>r.agent_id===a.agent_id)?'main':ids.has(a.agent_id)?'helper':'parent'});
  }
  return result;
}

function repository(value) {
  const match=String(value || '').match(/^(?:https:\/\/)?(?:github\.com\/)?([\w.-]+)\/([\w.-]+?)(?:\.git)?\/?$/);
  return match && ![match[1],match[2]].some(v=>v==='.' || v==='..') ? `${match[1]}/${match[2]}` : null;
}
export function githubLinks(session, association={}) {
  const result=[];
  const repo=repository(session.repository_id);
  if(repo && /^[a-f0-9]{40}(?:[a-f0-9]{24})?$/i.test(session.commit_sha || ''))result.push({label:`Commit ${session.commit_sha.slice(0,8)}`,href:`https://github.com/${repo}/commit/${session.commit_sha}`});
  for(const [kind, segment, label] of [['pr','pull','PR'],['issue','issues','Issue']]) {
    const item=association[kind];if(!item)continue;
    const identity=repository(item.repo) || repo;
    if(identity && Number.isSafeInteger(item.number) && item.number>0)result.push({label:`${label} #${item.number}`,href:`https://github.com/${identity}/${segment}/${item.number}`});
    else if(typeof item.url==='string' && /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(?:pull|issues)\/[1-9]\d*$/.test(item.url))result.push({label,href:item.url});
  }
  return result;
}

export function referenceSuggestions(session, data, sessions=[], collaborators=[]) {
  const summary=data?.summary || {};
  return [
    ...linkedAgents(session,summary.agents || [],sessions,collaborators).map(a=>({id:`agent:${a.collab_agent_id || a.agent_id || a.session_id}`,label:a.display_name || a.agent_id || a.agent_role || a.session_id,insertText:`@${a.agent_handle || a.agent_id || a.agent_role || a.session_id}`,detail:`${a.relation} agent reference`})),
    ...(summary.files || []).map(f=>({id:`file:${f.path}`,label:f.path,insertText:`@${f.path}`,detail:'Recorded file path'})),
    ...githubLinks(session,summary.association).map(l=>({id:l.href,label:l.label,insertText:l.href,trigger:'@',detail:'GitHub reference'})),
  ];
}
