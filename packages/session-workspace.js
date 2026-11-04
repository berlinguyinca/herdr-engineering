import {connectEvents} from './herdr-web-client/events.js';
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {mergeEntry, renderEvent, telemetryHealth} from './herdr-web-components/inspector.js';
import {mountComposer} from './conversation-composer.js';
import {renderMarkdown} from './conversation-markdown.js';
import {selectEvents, linkedAgents, githubLinks, referenceSuggestions, pendingOperation} from './session-workspace-model.js';
import {randomString} from './herdr-web-client/collab.js';
import {recordOutcome} from './record-state.js';

const MAX_EVENTS=2000;
const PAGE=200;

export function mountSessionWorkspace(host,client,session,{connect=connectEvents}={}) {
  if(!document.querySelector('link[data-session-workspace-style]')) {
    const link=document.createElement('link');link.rel='stylesheet';link.href=new URL('./session-workspace.css',import.meta.url).href;
    link.dataset.sessionWorkspaceStyle='';document.head.append(link);
  }
  let disposed=false, data=null, events=[], sessions=[], collaborators=[], epoch=null, request=0, loading=false, hasMore=false, error='', streamError=false;
  let renderTimer, refreshTimer, selection=null, split=60;
  let historyReset=false;
  const expanded=new Map();
  const state={agent:'',q:'',type:'',errors:false};
  host.innerHTML=`<section class="sw" data-session-workspace>
    <div class="sw-status" data-recording-status role="status">Loading recorded conversation…</div>
    <div class="sw-relations" data-session-relations></div>
    <div class="sw-filters"><label>Agent <select data-agent-filter><option value="">All agents</option></select></label>
      <label>Search <input type="search" data-search placeholder="Search conversation and activity"></label>
      <button type="button" data-load-older>Load older history</button><button type="button" data-refresh>Refresh</button></div>
    <div class="sw-mobile-tabs" role="tablist" aria-label="Session panes"><button type="button" role="tab" data-pane-tab="conversation" aria-selected="true">Conversation</button><button type="button" role="tab" data-pane-tab="activity" aria-selected="false">Activity</button></div>
    <div class="sw-panes" data-panes data-mobile-pane="conversation">
      <section class="sw-conversation" data-conversation-pane aria-label="Conversation"><h3>Conversation</h3>
        <div class="sw-log" data-conversation role="log" aria-label="Session messages" aria-live="polite"></div>
        <button type="button" data-new-messages hidden>↓ New messages</button>
        <div data-composer></div></section>
      <div class="sw-splitter" data-splitter role="separator" aria-label="Resize conversation pane" aria-orientation="vertical" tabindex="0" aria-valuemin="30" aria-valuemax="75" aria-valuenow="60"></div>
      <section class="sw-activity" data-activity-pane aria-label="Coding activity"><h3>Activity</h3>
        <div class="sw-filters"><label>Type <select data-type-filter><option value="">All activity</option>${['tool','file','test','usage','model','association','lifecycle'].map(t=>`<option value="${t}">${t}</option>`).join('')}</select></label><label><input type="checkbox" data-errors-filter> Errors only</label><button type="button" data-clear-selection hidden>Show all turns</button></div>
        <div class="sw-activity-log" data-activity></div></section>
    </div></section>`;
  const $=s=>host.querySelector(s);
  const conversation=$('[data-conversation]'), activity=$('[data-activity]'), panes=$('[data-panes]');
  const terminal=['finished','failed'].includes(recordOutcome(session));
  let operationId=null, operationText=null;
  let storage;
  try{storage=host.ownerDocument.defaultView.sessionStorage;}catch{/* optional storage */}
  const operationKey=`herdr.conversation.pending:${session.session_id}`;
  const composer=mountComposer($('[data-composer]'),{
    draftKey:`session:${session.session_id}`,disabled:terminal,
    placeholder:terminal?'This session has ended':'Message this session… type @ for references',
    hint:terminal?'Recorded history remains available.':'Enter sends · Shift+Enter adds a line · @ inserts a file, agent or GitHub reference.',
    suggestions:()=>referenceSuggestions(session,data,sessions,collaborators),
    onSend:async(text,attachments)=>{
      if(!operationId || operationText!==text){operationId=pendingOperation(storage,operationKey,text,()=>randomString(18));operationText=text;}
      const artifactIds=attachments.map(attachment=>attachment.artifact_id || attachment.artifactId || attachment.id).filter(Boolean);
      const receipt=await client.sendMessage(session.session_id,text,artifactIds,operationId);
      if(receipt.status!=='delivered')throw new Error(receipt.status==='queued'?'Message queued; delivery is not confirmed. Refresh before retrying.':receipt.error || 'Delivery was not acknowledged.');
      operationId=null;operationText=null;
      try{storage?.removeItem(operationKey);}catch{/* optional storage */}
      scheduleRefresh();return {...receipt,message:'Delivered to session. Waiting for the recorded response.'};
    },
  });

  function paint() {
    if(disposed)return;
    const health=telemetryHealth(data?.summary,data?.telemetry);
    const capture=session.transcript?.status;
    const captureNote=capture==='identity_unavailable'?' · Waiting for this session to identify its transcript.':capture==='unreadable'?' · The session transcript could not be read.':capture==='unsupported'?' · Recording is unavailable for this agent.':'';
    $('[data-recording-status]').textContent=error || (streamError?'Live connection interrupted; reconnecting. ': '')+health.text+(events.length?'':captureNote)+(historyReset?' · History refreshed after a recording gap. Load older history to inspect earlier turns.':'')+(data?.dropped?` · ${data.dropped} earlier events expired.`:'')+(events.length>=MAX_EVENTS?' · Display limited to the most recent 2,000 events.':'');
    const bottom=conversation.scrollHeight-conversation.scrollTop-conversation.clientHeight<60;
    const oldScroll=conversation.scrollTop,oldHeight=conversation.scrollHeight;
    const rows=selectEvents(events,{...state,pane:'conversation'});
    const html=rows.map(e=>`<article class="sw-message sw-message--${esc(e.data.role)}" data-message-seq="${e.seq}"><header><strong>${esc(e.data.role==='user'?'You':e.agent_id==='main' || !e.agent_id?'Assistant':e.agent_id)}</strong><time>${esc(e.ts || '')}</time><button type="button" data-related-turn="${e.seq}" aria-label="Show activity for this turn">Activity ↗</button></header>${e.data.withheld?'<p>Message withheld.</p>':renderMarkdown(e.data.text || '')}${e.data.text_more?`<button type="button" data-expand-message="${e.seq}">Show full message</button>`:''}${e.truncated?'<p class="sw-note">Message truncated by recording limits.</p>':''}</article>`).join('') || '<p class="sw-empty">No conversation messages recorded for these filters.</p>';
    if(conversation.innerHTML!==html){
      const previousNewest=Number(conversation.lastElementChild?.dataset.messageSeq || 0);
      conversation.innerHTML=html;
      if(bottom)conversation.scrollTop=conversation.scrollHeight;
      else {
        const prepended=rows.length && rows[rows.length-1].seq<=previousNewest;
        conversation.scrollTop=prepended?oldScroll+conversation.scrollHeight-oldHeight:oldScroll;
        $('[data-new-messages]').hidden=!(rows.at(-1)?.seq>previousNewest);
      }
    }
    const open=new Set([...activity.querySelectorAll('details[open]')].map(d=>d.dataset.activitySeq));
    const scroll=activity.scrollTop;
    let activityRows=selectEvents(events,{...state,pane:'activity'});
    if(selection!=null){
      const next=events.filter(e=>e.kind==='message' && e.seq>selection).sort((a,b)=>a.seq-b.seq)[0]?.seq ?? Infinity;
      activityRows=activityRows.filter(e=>e.seq>selection && e.seq<next);
    }
    const activityHtml=activityRows.map(e=>`<details class="sw-event" data-activity-seq="${e.seq}"${open.has(String(e.seq))?' open':''}><summary>${esc(e.data?.tool || e.data?.path || e.kind)} <span>${esc(e.agent_id || 'main')} · ${esc(e.data?.status || '')}</span></summary>${renderEvent(e,{show_tool_output:true,tool_output_lines:expanded.has(e.seq)?Number.MAX_SAFE_INTEGER:100,diff_max_lines:expanded.has(e.seq)?Number.MAX_SAFE_INTEGER:200})}</details>`).join('') || '<p class="sw-empty">No coding activity recorded for these filters.</p>';
    if(activity.innerHTML!==activityHtml){activity.innerHTML=activityHtml;activity.scrollTop=scroll;}
    const agents=linkedAgents({...session,parent_session_id:session.parent_session_id || data?.summary?.association?.parent_session_id},data?.summary?.agents || [],sessions,collaborators);
    const agentIds=[...new Set([...agents.map(a=>a.agent_id).filter(Boolean),...events.map(e=>e.agent_id || 'main')])];
    const options='<option value="">All agents</option>'+agentIds.map(id=>`<option value="${esc(id)}">${esc(id)}</option>`).join('');
    const filter=$('[data-agent-filter]');if(filter.innerHTML!==options){filter.innerHTML=options;filter.value=state.agent;}
    $('[data-session-relations]').innerHTML=`<div class="sw-links">${agents.map(a=>a.collab_agent_id?`${a.session_id && a.session_id!==session.session_id?`<a href="#/herd/session/${esc(encodeURIComponent(a.session_id))}">${esc(a.relation)}: @${esc(a.agent_role)}</a>`:''}<a href="#/collab?agent=${esc(encodeURIComponent(a.collab_agent_id))}">@${esc(a.agent_role)} in Collaboration</a>`:a.session_id?`<a href="#/herd/session/${esc(encodeURIComponent(a.session_id))}">${esc(a.relation)}: ${esc(a.agent_role || a.session_id)}</a>`:`<button type="button" data-select-agent="${esc(a.agent_id)}">${esc(a.agent_id)}${a.parent_agent_id?` ← ${esc(a.parent_agent_id)}`:''}${a.events!=null?` · ${esc(a.events)} events`:''}</button>`).join('')}${githubLinks(session,data?.summary?.association).map(l=>`<a href="${esc(l.href)}" target="_blank" rel="noopener noreferrer">${esc(l.label)}</a>`).join('')}</div>`;
    $('[data-load-older]').hidden=!hasMore || events.length>=MAX_EVENTS;
    $('[data-load-older]').disabled=loading;
    $('[data-clear-selection]').hidden=selection==null;
  }
  function schedulePaint(){if(!renderTimer)renderTimer=setTimeout(()=>{renderTimer=null;paint();},100);}
  function scheduleRefresh(){if(!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=null;load();},2000);}
  function merge(entry) {
    const full=expanded.get(entry.seq);
    if(full && full.rev!==entry.rev)expanded.delete(entry.seq);
    mergeEntry(events,full?.rev===entry.rev?full:entry,MAX_EVENTS);
    if(expanded.size>MAX_EVENTS)for(const seq of expanded.keys())if(!events.some(e=>e.seq===seq))expanded.delete(seq);
  }
  async function load(older=false) {
    const token=++request;loading=true;
    try {
      const result=await client.getInspector(session.session_id,{view:'all',limit:PAGE,...(older && events.length?{before_seq:Math.min(...events.map(e=>e.seq))}:{})});
      if(disposed || token!==request)return;
      if(epoch && result.epoch!==epoch){events=[];expanded.clear();selection=null;historyReset=true;}
      epoch=result.epoch;
      const incoming=result.events || [];
      if(!older && events.length && incoming.length && Math.min(...incoming.map(e=>e.seq))>Math.max(...events.map(e=>e.seq))+1){events=[];selection=null;historyReset=true;}
      if(result.oldest_available_seq!=null)events=events.filter(e=>e.seq>=result.oldest_available_seq);
      for(const entry of incoming)merge(entry);
      if(!older || !data)data=result;
      if(older || events.length<=PAGE)hasMore=Boolean(result.has_more);
      error='';
    }catch(e){if(!disposed && token===request)error=`Recorded history could not load: ${e.message}`;}
    finally{if(token===request){loading=false;schedulePaint();}}
  }
  const refresh=()=>load();
  $('[data-refresh]').addEventListener('click',refresh);
  $('[data-load-older]').addEventListener('click',()=>load(true));
  $('[data-search]').addEventListener('input',e=>{state.q=e.target.value;schedulePaint();});
  $('[data-agent-filter]').addEventListener('change',e=>{state.agent=e.target.value;selection=null;schedulePaint();});
  $('[data-type-filter]').addEventListener('change',e=>{state.type=e.target.value;schedulePaint();});
  $('[data-errors-filter]').addEventListener('change',e=>{state.errors=e.target.checked;schedulePaint();});
  $('[data-clear-selection]').addEventListener('click',()=>{selection=null;paint();});
  $('[data-new-messages]').addEventListener('click',()=>{conversation.scrollTop=conversation.scrollHeight;$('[data-new-messages]').hidden=true;});
  host.addEventListener('click',e=>{
    const target=e.target.closest('[data-select-agent],[data-related-turn],[data-pane-tab]');if(!target)return;
    if(target.dataset.selectAgent){state.agent=target.dataset.selectAgent;$('[data-agent-filter]').value=state.agent;paint();}
    if(target.dataset.relatedTurn){selection=Number(target.dataset.relatedTurn);panes.dataset.mobilePane='activity';paint();}
    if(target.dataset.paneTab){panes.dataset.mobilePane=target.dataset.paneTab;host.querySelectorAll('[data-pane-tab]').forEach(b=>b.setAttribute('aria-selected',String(b===target)));}
  });
  host.addEventListener('click',async e=>{
    const button=e.target.closest('[data-expand-message],.hd-ins__expand');if(!button)return;
    const seq=Number(button.dataset.expandMessage || button.dataset.seq);button.disabled=true;
    const currentEpoch=epoch;
    try{
      const full=await client.getInspectorEvent(session.session_id,seq);
      if(disposed || currentEpoch!==epoch)return;
      const current=events.find(entry=>entry.seq===seq);
      if(current && current.rev>full.rev)return;
      const entry={...full,data:{...full.data,text_more:false,output_more:false,diff_more:false}};
      expanded.set(seq,entry);merge(entry);paint();
    }catch(err){if(!disposed){button.disabled=false;button.textContent=`Could not expand: ${err.message}. Retry`;}}
  });
  const splitter=$('[data-splitter]');
  const resize=value=>{split=Math.min(75,Math.max(30,Math.round(value)));panes.style.setProperty('--sw-split',`${split}%`);splitter.setAttribute('aria-valuenow',String(split));};
  splitter.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();resize(e.key==='Home'?30:e.key==='End'?75:split+(e.key==='ArrowRight'?5:-5));}});
  splitter.addEventListener('pointerdown',e=>{splitter.setPointerCapture(e.pointerId);});
  splitter.addEventListener('pointermove',e=>{if(splitter.hasPointerCapture(e.pointerId)){const r=panes.getBoundingClientRect();resize((e.clientX-r.left)/r.width*100);}});
  const stop=connect({url:client.inspectorStreamUrl(session.session_id),onEvent:frame=>{
    if(disposed)return;
    streamError=false;
    if(frame.type==='connected' || frame.type==='gap'){if(events.length){events=[];selection=null;historyReset=true;}load();return;}
    if(frame.type==='event' && frame.event){merge(frame.event);schedulePaint();scheduleRefresh();}
  },onError:()=>{streamError=true;schedulePaint();}});
  client.listSessions().then(result=>{if(!disposed){sessions=result.sessions || [];schedulePaint();}}).catch(()=>{});
  client.listCollabAgentLinks?.().then(result=>{if(!disposed){collaborators=result.agents || [];schedulePaint();}}).catch(()=>{});
  load();
  return ()=>{disposed=true;request++;clearTimeout(renderTimer);clearTimeout(refreshTimer);stop();composer.dispose();};
}
