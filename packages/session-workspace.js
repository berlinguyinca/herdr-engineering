import {connectEvents} from './herdr-web-client/events.js';
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {mergeEntry, renderEvent, telemetryHealth} from './herdr-web-components/inspector.js';
import {mountComposer} from './conversation-composer.js';
import {renderMarkdown} from './conversation-markdown.js';
import {createEntityIndex} from './entity-refs.js';
import {parseJson,renderJson} from './conversation-json.js';
import {selectEvents, linkedAgents, githubLinks, referenceSuggestions, pendingOperation} from './session-workspace-model.js';
import {randomString} from './herdr-web-client/collab.js';
import {recordOutcome} from './record-state.js';
import {fixOutcome, fixError, FIX_LABELS, FIX_LOCKED} from './summary-fix-status.js';
import {morph} from './patch.js';
import {enter,skeleton} from './motion.js';

// Rows carry data-key="<seq>". morph keeps every shown row's node (open disclosures, focus and
// text selection survive) and writes only what changed. A row enters with the shared transition
// (not under calm) only when the stream brought it: its seq is above the newest record received
// before this paint, and the filters did not change. A filter change, an older page or the first
// paint show their rows without motion, even rows that are new to this pane.
function patchRows(container,html,newestBefore){
  morph(container,html);
  for(const row of container.children){
    const key=row.getAttribute('data-key');
    if(key && Number(key)>newestBefore)enter(row);
  }
}

const MAX_EVENTS=2000;
const PAGE=200;

export function mountSessionWorkspace(host,client,session,{connect=connectEvents,onRename=()=>{}}={}) {
  if(!document.querySelector('link[data-session-workspace-style]')) {
    const link=document.createElement('link');link.rel='stylesheet';link.href=new URL('./session-workspace.css',import.meta.url).href;
    link.dataset.sessionWorkspaceStyle='';document.head.append(link);
  }
  // session:/mission:/host: references link only entities the dashboard knows now (cross-linking).
  const entityIndex=createEntityIndex(client),markdown={references:entityIndex.resolver};
  let disposed=false, data=null, events=[], conversationEvents=[], sessions=[], collaborators=[], epoch=null, request=0, loading=false, error='', streamError=false;
  let renderTimer, refreshTimer, selection=null, split=60;
  let historyGeneration=0,historyReset=false,fullscreen=false,fullscreenFocus=null,agentSummary=null,summaryBusy=false,summaryError='',summarySignature='';
  const expanded=new Map();
  // "Let the agent fix this" state per exact item text, kept outside paint so polling keeps it.
  const fixStates=new Map();
  const fixKey=(list,text)=>`${list}\u0000${text}`;
  const canFix=()=>Boolean(session.capabilities?.fix_summary_item);
  const pages={all:{before:null,more:false},conversation:{before:null,more:false}};
  const canLoadOlder=view=>pages[view].more && (view==='conversation'?conversationEvents:events).length<MAX_EVENTS;
  const state={agent:'',q:'',type:'',errors:false};
  host.innerHTML=`<section class="sw" data-session-workspace>
    <header class="sw-workspace-heading"><strong data-agent-name>${esc(session.display_name || session.agent_role || 'Agent')}</strong><button type="button" data-rename-agent${session.capabilities?.rename_agent?'':' hidden'}>Rename agent</button><a class="sw-llm-link" data-llm-link href="#/llm/session/${esc(encodeURIComponent(session.session_id))}">LLM usage</a><button type="button" data-fullscreen aria-expanded="false">Fullscreen workspace</button></header>
    <form data-rename-form hidden><label>Agent name <input aria-label="Agent name" name="agent_name" maxlength="64" required></label><button type="submit">Save name</button><button type="button" data-cancel-rename>Cancel</button><p data-rename-status role="status"></p></form>
    <section class="sw-summary-card" aria-label="Agent summary"><div class="sw-summary-heading"><h3>AI-generated summary</h3><button type="button" data-regenerate-summary${session.capabilities?.regenerate_summary?'':' hidden'}>Regenerate summary</button></div><div data-agent-summary role="status">${skeleton('summary',{rows:2,height:18,inStatus:true})}</div></section>
    <div class="sw-status" data-recording-status role="status">${skeleton('recorded conversation',{rows:1,height:16,inStatus:true})}</div>
    <div class="sw-relations" data-session-relations></div>
    <div class="sw-filters"><label>Agent <select data-agent-filter><option value="">All agents</option></select></label>
      <label>Search <input type="search" data-search placeholder="Search conversation and activity"></label>
      <button type="button" data-load-older>Load older history</button><button type="button" data-refresh>Refresh</button></div>
    <div class="sw-mobile-tabs" role="tablist" aria-label="Agent panes"><button type="button" role="tab" data-pane-tab="conversation" aria-selected="true">Conversation</button><button type="button" role="tab" data-pane-tab="activity" aria-selected="false">Activity</button></div>
    <div class="sw-panes" data-panes data-mobile-pane="conversation">
      <section class="sw-conversation" data-conversation-pane aria-label="Conversation"><h3>Conversation</h3>
        <div class="sw-log" data-conversation role="log" aria-label="Agent messages" aria-live="polite"></div>
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
    placeholder:terminal?'This agent has ended':'Message this agent… type @ for references',
    hint:terminal?'Recorded history remains available.':'Enter sends · Shift+Enter adds a line · @ inserts a file, agent or GitHub reference.',
    suggestions:()=>referenceSuggestions(session,data,sessions,collaborators),
    onSend:async(text,attachments)=>{
      if(attachments.length)throw new Error('This agent transport cannot deliver files. Remove attachments before sending.');
      if(!operationId || operationText!==text){operationId=pendingOperation(storage,operationKey,text,()=>randomString(18));operationText=text;}
      const receipt=await client.sendMessage(session.session_id,text,[],operationId);
      if(receipt.status!=='delivered')throw new Error(receipt.status==='queued'?'Message queued; delivery is not confirmed. Refresh before retrying.':receipt.error || 'Delivery was not acknowledged.');
      operationId=null;operationText=null;
      try{storage?.removeItem(operationKey);}catch{/* optional storage */}
      scheduleRefresh();return {...receipt,message:'Delivered to agent. Waiting for the recorded response.'};
    },
  });

  let shownConversation=null,shownActivity=null,paintedNewest=Infinity,paintedFilters=null;
  function paint() {
    if(disposed)return;
    // Newest record received so far; rows above the previous paint's value arrived by stream.
    const newest=Math.max(-Infinity,...events.map(e=>e.seq),...conversationEvents.map(e=>e.seq));
    const filters=JSON.stringify([state,selection]);
    const streamNewest=filters===paintedFilters?paintedNewest:Infinity;
    paintedFilters=filters;
    const health=telemetryHealth(data?.summary,data?.telemetry);
    const capture=session.transcript?.status;
    const captureNote=capture==='identity_unavailable'?' · Waiting for this agent to identify its transcript.':capture==='unreadable'?' · The agent transcript could not be read.':capture==='unsupported'?' · Recording is unavailable for this agent.':'';
    $('[data-recording-status]').textContent=error || (streamError?'Live connection interrupted; reconnecting. ': '')+health.text+(events.length || conversationEvents.length?'':captureNote)+(historyReset?' · History refreshed after a recording gap. Load older history to inspect earlier turns.':'')+(data?.dropped?` · ${data.dropped} earlier events expired.`:'')+(events.length>=MAX_EVENTS || conversationEvents.length>=MAX_EVENTS?' · Display limited to the most recent 2,000 records per pane.':'');
    const bottom=conversation.scrollHeight-conversation.scrollTop-conversation.clientHeight<60;
    const oldScroll=conversation.scrollTop,oldHeight=conversation.scrollHeight;
    const agentNames=new Map((data?.summary?.agents || []).map(a=>[a.agent_id,a.display_name || a.agent_id]));
    agentNames.set('main',session.display_name || agentNames.get('main') || 'Assistant');
    const rows=selectEvents(conversationEvents,{...state,pane:'conversation'});
    const html=rows.map(e=>`<article class="sw-message conversation-markdown sw-message--${esc(e.data.role)}" data-message-seq="${e.seq}" data-key="${e.seq}"><header><strong>${esc(e.data.role==='user'?'You':agentNames.get(e.agent_id || 'main') || e.agent_id)}</strong><time>${esc(e.ts || '')}</time><button type="button" data-related-turn="${e.seq}" aria-label="Show activity for this turn">Activity ↗</button></header>${e.data.withheld?'<p>Message withheld.</p>':renderMarkdown(e.data.text || '',markdown)}${e.data.text_more?`<button type="button" data-expand-message="${e.seq}">Show full message</button>`:''}${e.truncated?'<p class="sw-note">Message truncated by recording limits.</p>':''}</article>`).join('') || '<p class="sw-empty">No conversation messages recorded for these filters.</p>';
    if(shownConversation!==html){
      shownConversation=html;
      const previousNewest=Math.max(0,...[...conversation.querySelectorAll(':scope > [data-message-seq]')].map(row=>Number(row.dataset.messageSeq)));
      patchRows(conversation,html,streamNewest);
      if(bottom)conversation.scrollTop=conversation.scrollHeight;
      else {
        const prepended=rows.length && rows[rows.length-1].seq<=previousNewest;
        conversation.scrollTop=prepended?oldScroll+conversation.scrollHeight-oldHeight:oldScroll;
        $('[data-new-messages]').hidden=!(rows.at(-1)?.seq>previousNewest);
      }
    }
    const open=new Set([...activity.querySelectorAll('details[open]')].map(d=>d.dataset.activitySeq));
    let activityRows=selectEvents(events,{...state,pane:'activity'});
    if(selection!=null){
      const next=conversationEvents.filter(e=>e.seq>selection).sort((a,b)=>a.seq-b.seq)[0]?.seq ?? Infinity;
      activityRows=activityRows.filter(e=>e.seq>selection && e.seq<next);
    }
    const activityHtml=activityRows.map(e=>`<details class="sw-event" data-activity-seq="${e.seq}" data-key="${e.seq}"${open.has(String(e.seq))?' open':''}><summary>${esc(e.data?.tool || e.data?.path || e.kind)} <span>${esc(agentNames.get(e.agent_id || 'main') || e.agent_id || 'main')} · ${esc(e.data?.status || '')}</span></summary>${activityEvent(e)}</details>`).join('') || '<p class="sw-empty">No coding activity recorded for these filters.</p>';
    // Rows keep their nodes, so open disclosures (and nested ones) and the scroll position stay as they are.
    if(shownActivity!==activityHtml){shownActivity=activityHtml;patchRows(activity,activityHtml,streamNewest);}
    paintedNewest=Number.isFinite(newest)?newest:Infinity;// nothing received yet: the first history is not "new"
    const agents=linkedAgents({...session,parent_session_id:session.parent_session_id || data?.summary?.association?.parent_session_id},data?.summary?.agents || [],sessions,collaborators);
    const agentIds=[...new Set([...agents.map(a=>a.agent_id).filter(Boolean),...[...events,...conversationEvents].map(e=>e.agent_id || 'main')])];
    const options='<option value="">All agents</option>'+agentIds.map(id=>`<option value="${esc(id)}">${esc(agentNames.get(id) || id)}</option>`).join('');
    const filter=$('[data-agent-filter]');if(filter.innerHTML!==options){filter.innerHTML=options;filter.value=state.agent;}
    $('[data-session-relations]').innerHTML=`<div class="sw-links">${agents.map(a=>a.collab_agent_id?`${a.session_id && a.session_id!==session.session_id?`<a href="#/herd/session/${esc(encodeURIComponent(a.session_id))}">${esc(a.relation)}: @${esc(a.display_name || a.agent_role)}</a>`:''}<a href="#/collab?agent=${esc(encodeURIComponent(a.collab_agent_id))}">@${esc(a.display_name || a.agent_role)} in Collaboration</a>`:a.session_id?`<a href="#/herd/session/${esc(encodeURIComponent(a.session_id))}">${esc(a.relation)}: ${esc(a.display_name || a.agent_role || a.session_id)}</a>`:`<button type="button" data-select-agent="${esc(a.agent_id)}">${esc(a.display_name || a.agent_id)}${a.parent_agent_id?` ← ${esc(agentNames.get(a.parent_agent_id) || a.parent_agent_id)}`:''}${a.events!=null?` · ${esc(a.events)} events`:''}</button>`).join('')}${githubLinks(session,data?.summary?.association).map(l=>`<a href="${esc(l.href)}" target="_blank" rel="noopener noreferrer">${esc(l.label)}</a>`).join('')}</div>`;
    $('[data-load-older]').hidden=!canLoadOlder('all') && !canLoadOlder('conversation');
    $('[data-load-older]').disabled=loading;
    $('[data-clear-selection]').hidden=selection==null;
  }
  function activityEvent(entry) {
    entry={...entry,display_name:data?.summary?.agents?.find(agent=>agent.agent_id===entry.agent_id)?.display_name};
    const prefs={show_tool_output:true,tool_output_lines:expanded.has(entry.seq)?Number.MAX_SAFE_INTEGER:100,diff_max_lines:expanded.has(entry.seq)?Number.MAX_SAFE_INTEGER:200};
    const output=entry.data?.output;
    if(entry.kind==='tool' && !entry.data?.withheld && typeof output==='string' && output.trim() && parseJson(output).ok){
      const html=renderEvent({...entry,data:{...entry.data,output:''}},prefs);
      return html+renderJson(output)+(entry.data.output_more || entry.truncated?`<button type="button" class="hd-ins__expand" data-seq="${entry.seq}">Show full output</button>`:'');
    }
    return renderEvent(entry,prefs);
  }
  function paintSummary(){
    if(disposed)return;
    const value=agentSummary || {state:summaryError?'failed':'pending'};
    const signature=JSON.stringify([value,summaryError,[...fixStates]]);if(signature===summarySignature)return;summarySignature=signature;
    const states={disabled:'Automatic summaries are not enabled.',pending:'Summary update pending.',ready:'Up to date with recorded conversation.',stale:'Newer conversation or unavailable history: showing the last saved summary.',failed:'Summary update failed. The last saved summary is retained.',no_data:'No recorded conversation is available to summarize.'};
    const fields=[['discussed','Discussed'],['decisions','Decisions'],['completed','Completed'],['next_steps','Next steps'],['blockers','Blockers']];
    const coverage=value.coverage;
    const provenance=coverage ? `${coverage.status==='partial'?'Partial recording':'Recorded window only'} · ${Number(coverage.message_count ?? value.source_message_count ?? 0)} messages${coverage.reasons?.length?` · ${coverage.reasons.join(', ')}`:''}` : 'Recording coverage is unavailable.';
    const fixable=new Set(canFix()?['next_steps','blockers']:[]);
    const item=(key,text,index)=>{
      if(!fixable.has(key))return `<li class="conversation-markdown">${renderMarkdown(String(text),markdown)}</li>`;
      const fix=fixStates.get(fixKey(key,text));
      const label=FIX_LABELS[fix?.state] || 'Let the agent fix this';
      const locked=FIX_LOCKED.has(fix?.state);
      const link=fix?.link?` <a href="${esc(fix.link.href)}">${esc(fix.link.label)}</a>`:'';
      return `<li class="conversation-markdown">${renderMarkdown(String(text),markdown)} <button type="button" class="sw-summary-fix" data-fix-summary data-list="${key}" data-index="${index}"${locked?' disabled':''}>${label}</button>${fix?.message?`<span class="sw-summary-fix-status" ${fix.state==='error'?'role="alert"':'role="status"'}>${esc(fix.message)}${link}</span>`:''}</li>`;
    };
    const summaryHost=$('[data-agent-summary]');
    const detailsOpen=summaryHost.querySelector('details')?.open;
    summaryHost.innerHTML=`<p class="sw-summary-state">${esc(summaryError || states[value.state] || 'Summary status unknown.')}</p>${value.overview?`<div class="conversation-markdown">${renderMarkdown(value.overview,markdown)}</div>`:''}${value.where_left_off?`<h4>Where we left off</h4><div class="conversation-markdown">${renderMarkdown(value.where_left_off,markdown)}</div>`:''}${fields.some(([key])=>value[key]?.length)?`<details><summary>Discussion, decisions and next steps</summary>${fields.filter(([key])=>Array.isArray(value[key]) && value[key].length).map(([key,label])=>`<h4>${label}</h4><ul>${value[key].map((text,index)=>item(key,text,index)).join('')}</ul>`).join('')}</details>`:''}<p class="sw-summary-provenance">${esc(provenance)}${value.generated_at?` · Generated ${esc(value.generated_at)}`:''}${value.model?` · ${esc(value.model)}`:''}</p>`;
    const details=summaryHost.querySelector('details');if(details && detailsOpen)details.open=true;
  }
  $('[data-agent-summary]').addEventListener('click',async event=>{
    const button=event.target.closest('[data-fix-summary]');if(!button || button.disabled || !canFix())return;
    const list=button.dataset.list,index=Number(button.dataset.index);
    // Send the text the summary holds, never text read back from the page.
    const text=agentSummary?.[list]?.[index];
    if(typeof text!=='string' || typeof client.fixSummaryItem!=='function')return;
    const key=fixKey(list,text);
    if(FIX_LOCKED.has(fixStates.get(key)?.state))return;
    fixStates.set(key,{state:'sending',message:''});paintSummary();
    try{
      const receipt=await client.fixSummaryItem(session.session_id,list,index,text);
      if(disposed)return;
      fixStates.set(key,fixOutcome(receipt));
    }catch(err){
      if(disposed)return;
      const outcome=fixError(err);
      fixStates.set(key,outcome);
      if(outcome.reload)loadSummary();
    }finally{if(!disposed)paintSummary();}
  });
  async function loadSummary(){
    if(disposed || document.hidden || summaryBusy)return;
    if(typeof client.getAgentSummary!=='function'){agentSummary={state:'disabled'};paintSummary();return;}
    summaryBusy=true;
    try{
      const value=await client.getAgentSummary(session.session_id);if(disposed)return;
      // Older valid text keeps its original provenance when an update has no result yet.
      agentSummary=agentSummary?.generated_at && !value.generated_at ? {...agentSummary,state:value.state,error:value.error} : value;
      summaryError='';
    }catch(err){if(!disposed)summaryError=`Summary unavailable: ${err.message}${agentSummary?.generated_at?' Showing the last saved summary.':''}`;}
    finally{summaryBusy=false;paintSummary();}
  }
  const fullscreenButton=$('[data-fullscreen]'), workspace=$('[data-session-workspace]');
  function setFullscreen(value){
    if(value===fullscreen)return;
    if(value)fullscreenFocus=document.activeElement;
    fullscreen=value;workspace.classList.toggle('sw--fullscreen',value);fullscreenButton.setAttribute('aria-expanded',String(value));fullscreenButton.textContent=value?'Exit fullscreen workspace':'Fullscreen workspace';
    if(value){workspace.setAttribute('role','dialog');workspace.setAttribute('aria-label','Agent workspace');workspace.setAttribute('aria-modal','true');fullscreenButton.focus();}
    else{workspace.removeAttribute('role');workspace.removeAttribute('aria-label');workspace.removeAttribute('aria-modal');if(fullscreenFocus?.isConnected)fullscreenFocus.focus();}
  }
  fullscreenButton.addEventListener('click',()=>setFullscreen(!fullscreen));
  workspace.addEventListener('keydown',event=>{
    if(event.defaultPrevented || event.isComposing || event.keyCode===229)return;
    if(event.key==='Escape' && fullscreen && !workspace.querySelector('.conversation-composer--expanded')){event.preventDefault();event.stopPropagation();setFullscreen(false);}
    if(event.key==='Tab' && fullscreen && !workspace.querySelector('.conversation-composer--expanded')){
      const controls=[...workspace.querySelectorAll('a,button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex="0"]')].filter(el=>el.getClientRects().length);
      if(event.shiftKey && document.activeElement===controls[0]){event.preventDefault();controls.at(-1)?.focus();}
      else if(!event.shiftKey && document.activeElement===controls.at(-1)){event.preventDefault();controls[0]?.focus();}
    }
  });
  const renameForm=$('[data-rename-form]'),renameInput=renameForm.querySelector('input'),renameStatus=$('[data-rename-status]');
  $('[data-rename-agent]').addEventListener('click',()=>{renameInput.value=session.display_name || session.agent_role || '';renameStatus.textContent='';renameForm.hidden=false;renameInput.focus();});
  $('[data-cancel-rename]').addEventListener('click',()=>{renameForm.hidden=true;$('[data-rename-agent]').focus();});
  renameForm.addEventListener('submit',async event=>{
    event.preventDefault();const submit=renameForm.querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;
    try{
      const profile=await client.renameAgent(session.session_id,renameInput.value,session.name_version);
      if(disposed)return;session.display_name=profile.display_name;session.agent_handle=profile.handle;session.name_version=profile.version;
      $('[data-agent-name]').textContent=profile.display_name;renameForm.hidden=true;$('[data-rename-agent]').focus();onRename(profile);paint();
    }catch(err){if(!disposed){renameStatus.setAttribute('role','alert');renameStatus.textContent=err.status===409?`Name could not be saved: ${err.message}. Choose another name or refresh to load the latest name.`:`Name could not be saved: ${err.message}`;}}
    finally{submit.disabled=false;}
  });
  $('[data-regenerate-summary]').addEventListener('click',async event=>{
    const button=event.currentTarget;if(button.disabled)return;button.disabled=true;
    try{const value=await client.regenerateAgentSummary(session.session_id);if(!disposed){agentSummary=agentSummary?.generated_at && !value.generated_at ? {...agentSummary,state:value.state,error:value.error} : value;summaryError='';paintSummary();}}
    catch(err){if(!disposed){summaryError=`Summary refresh could not start: ${err.message}`;paintSummary();}}
    finally{if(!disposed)button.disabled=false;}
  });
  const onVisibility=()=>{if(!document.hidden)loadSummary();};
  document.addEventListener('visibilitychange',onVisibility);
  const summaryTimer=setInterval(loadSummary,10000);
  loadSummary();
  function schedulePaint(){if(!renderTimer)renderTimer=setTimeout(()=>{renderTimer=null;paint();},100);}
  function scheduleRefresh(){if(!refreshTimer)refreshTimer=setTimeout(()=>{refreshTimer=null;load();},2000);}
  function resetHistory(){
    // Stream gaps can reset history without changing the server epoch.
    historyGeneration++;
    events=[];conversationEvents=[];expanded.clear();selection=null;historyReset=true;
    for(const page of Object.values(pages)){page.before=null;page.more=false;}
  }
  function merge(entry,{activity=true,conversation=true}={}) {
    const full=expanded.get(entry.seq);
    if(full && full.rev!==entry.rev)expanded.delete(entry.seq);
    const value=full && full.rev===entry.rev?full:entry;
    if(activity)mergeEntry(events,value,MAX_EVENTS);
    if(conversation && entry.kind==='message')mergeEntry(conversationEvents,value,MAX_EVENTS);
    if(expanded.size>MAX_EVENTS*2)for(const seq of expanded.keys())if(!events.some(e=>e.seq===seq) && !conversationEvents.some(e=>e.seq===seq))expanded.delete(seq);
  }
  async function load(older=false) {
    const token=++request;loading=true;
    const views=['all','conversation'].filter(view=>!older || canLoadOlder(view));
    try {
      // Conversation paging must not scan the activity window: a single turn can
      // emit thousands of tool records while its messages remain retained.
      const results=await Promise.all(views.map(async view=>({view,result:await client.getInspector(session.session_id,{view,limit:PAGE,...(older && pages[view].before!=null?{before_seq:pages[view].before}:{})})})));
      if(disposed || token!==request || !results.length)return;
      const nextEpoch=results[0].result.epoch;
      if(results.some(({result})=>result.epoch!==nextEpoch)){
        resetHistory();error='Recording changed while loading history; retrying.';scheduleRefresh();return;
      }
      if(epoch && nextEpoch!==epoch){
        resetHistory();epoch=nextEpoch;
        if(older){scheduleRefresh();return;}
      }
      epoch=nextEpoch;
      for(const {view,result} of results){
        const incoming=(result.events || []).filter(entry=>view!=='conversation' || entry.kind==='message');
        const buffer=view==='conversation'?conversationEvents:events;
        const first=incoming.length?Math.min(...incoming.map(e=>e.seq)):null;
        const newest=buffer.length?Math.max(...buffer.map(e=>e.seq)):null;
        // Message seqs legitimately skip tool records. Only a full conversation
        // page beyond our newest message establishes an unpageable gap.
        const gap=!older && first!=null && newest!=null && (view==='all'?first>newest+1:result.has_more && first>newest);
        if(gap){
          if(view==='conversation'){conversationEvents=[];selection=null;}else events=[];
          pages[view].before=null;pages[view].more=false;historyReset=true;
        }
        if(result.oldest_available_seq!=null){
          if(view==='conversation')conversationEvents=conversationEvents.filter(e=>e.seq>=result.oldest_available_seq);
          else events=events.filter(e=>e.seq>=result.oldest_available_seq);
        }
        for(const entry of incoming)merge(entry,{activity:view==='all',conversation:view==='conversation'});
        if(view==='all' && (!older || !data))data=result;
        if(!data)data=result;
        const page=pages[view];
        if(older || page.before==null || first!=null && first<=page.before)page.more=Boolean(result.has_more);
        if(first!=null)page.before=page.before==null?first:Math.min(page.before,first);
      }
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
    const currentEpoch=epoch,currentGeneration=historyGeneration;
    try{
      const full=await client.getInspectorEvent(session.session_id,seq);
      if(disposed || currentEpoch!==epoch || currentGeneration!==historyGeneration)return;
      const current=conversationEvents.find(entry=>entry.seq===seq) || events.find(entry=>entry.seq===seq);
      if(!current || current.rev>full.rev)return;
      const entry={...full,data:{...full.data,text_more:false,output_more:false,diff_more:false}};
      expanded.set(seq,entry);merge(entry);paint();
    }catch(err){if(!disposed && currentEpoch===epoch && currentGeneration===historyGeneration && button.isConnected){button.disabled=false;button.textContent=`Could not expand: ${err.message}. Retry`;}}
  });
  const splitter=$('[data-splitter]');
  const resize=value=>{split=Math.min(75,Math.max(30,Math.round(value)));panes.style.setProperty('--sw-split',`${split}%`);splitter.setAttribute('aria-valuenow',String(split));};
  splitter.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();resize(e.key==='Home'?30:e.key==='End'?75:split+(e.key==='ArrowRight'?5:-5));}});
  splitter.addEventListener('pointerdown',e=>{splitter.setPointerCapture(e.pointerId);});
  splitter.addEventListener('pointermove',e=>{if(splitter.hasPointerCapture(e.pointerId)){const r=panes.getBoundingClientRect();resize((e.clientX-r.left)/r.width*100);}});
  const stop=connect({url:client.inspectorStreamUrl(session.session_id),onEvent:frame=>{
    if(disposed)return;
    streamError=false;
    if(frame.type==='connected' || frame.type==='gap'){if(events.length || conversationEvents.length)resetHistory();load();return;}
    if(frame.type==='event' && frame.event){merge(frame.event);schedulePaint();scheduleRefresh();}
  },onError:()=>{streamError=true;schedulePaint();}});
  client.listSessions().then(result=>{if(!disposed){sessions=result.sessions || [];schedulePaint();}}).catch(()=>{});
  entityIndex.load().then(changed=>{if(changed && !disposed){summarySignature='';schedulePaint();}}).catch(()=>{});
  client.listCollabAgentLinks?.().then(result=>{if(!disposed){collaborators=result.agents || [];schedulePaint();}}).catch(()=>{});
  load();
  return ()=>{disposed=true;request++;clearInterval(summaryTimer);document.removeEventListener('visibilitychange',onVisibility);clearTimeout(renderTimer);clearTimeout(refreshTimer);stop();composer.dispose();};
}
