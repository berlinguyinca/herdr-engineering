// Session records describe the last observation, not a live process check.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
import {recordOutcome,statusOutcome,recordVisibility} from './record-state.js';

const text = value => typeof value === 'string' ? value.trim() : '';
const records = value => Array.isArray(value) ? value.filter(v=>v && typeof v==='object' && !Array.isArray(v)) : [];
const short = (value, max=160) => {
  const normalized=text(value).replace(/\s+/g,' ');
  return normalized.length>max ? `${normalized.slice(0,max)}…` : normalized;
};
const tabs = [['current','Current'],['unconfirmed','Unconfirmed'],['failed','Failed'],['finished','Finished']];
const missionKey = mission => text(mission.mission_key) || (text(mission.mission_id)
  ? `${text(mission.source) && mission.source!=='herdr' ? `${mission.source}:` : ''}${mission.mission_id}` : '');

function associatedMission(session, missions) {
  const id=text(session.mission_id), key=text(session.mission_key);
  if (!id && !key) return null;
  const matches=missions.filter(m=> {
    if (key) return missionKey(m)===key;
    if (text(m.mission_id)!==id && missionKey(m)!==id) return false;
    if (text(session.host_id) && text(m.host_id) && session.host_id!==m.host_id) return false;
    const members=Array.isArray(m.sessions) ? m.sessions : [];
    return members.some(s=>(typeof s==='string' ? s : s?.session_id)===session.session_id);
  });
  if (matches.length!==1) return null;
  const mission=matches[0];
  return {key:missionKey(mission),title:text(mission.title)||text(mission.mission_id)};
}

export function sessionEntries(sessions, missions=[], now=Date.now()) {
  const available=records(missions);
  return records(sessions).filter(s=>text(s.session_id)).map(record=> {
    const project=text(record.cwd).split(/[\\/]/).filter(Boolean).at(-1) || 'Project not reported';
    const mission=associatedMission(record,available);
    const id=record.session_id, host=text(record.host_id), role=text(record.agent_role)||'Agent session';
    return {record,id,host,role,project,mission,tab:recordVisibility(record,now),
      search:[id,host,role,project,text(record.cwd),text(record.branch),text(record.model),text(record.summary),
        text(record.status),text(record.mission_id),mission?.title].filter(Boolean).join(' ').toLocaleLowerCase()};
  }).sort((a,b)=> {
    const stamp=s=>Number.isFinite(Date.parse(s.record.updated_at)) ? Date.parse(s.record.updated_at) : 0;
    return stamp(b)-stamp(a) || a.project.localeCompare(b.project) || a.id.localeCompare(b.id);
  });
}

export function selectSessions(entries,{tab='current',search='',host=''}={}) {
  const terms=text(search).toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return entries.filter(e=>e.tab===tab && (!host||e.host===host) && terms.every(t=>e.search.includes(t)));
}

export function sessionReport(record,now=Date.now()) {
  const outcome=recordOutcome(record);
  const authoritative=(outcome==='failed'||outcome==='finished') && statusOutcome(record.status)!==outcome ? record.stage : record.status;
  const status=text(authoritative).replaceAll('_',' ').toLowerCase();
  const age=now-Date.parse(text(record.updated_at));
  const state=outcome==='unknown' ? 'Status unknown' : `Last reported ${status||'unknown'}`;
  return `${state} · ${Number.isFinite(age) ? `${formatDuration(Math.max(0,age))} ago` : 'report time unknown'}`;
}

export function compactSessionRow(entry,now=Date.now()) {
  const s=entry.record;
  return `<article class="ss-row" data-session-id="${esc(entry.id)}">
    <div class="ss-main"><a class="ss-title" data-session-link href="#/herd/session/${esc(encodeURIComponent(entry.id))}">${esc(short(entry.role,70))} · ${esc(short(entry.project,80))}</a>
      <p class="ss-summary">${esc(short(s.summary)||'Activity not reported.')}</p>
      ${entry.mission ? `<p class="ss-meta">Mission: <a data-mission-link href="#/missions/${esc(encodeURIComponent(entry.mission.key))}">${esc(short(entry.mission.title,130))}</a></p>` : text(s.mission_id) ? '<p class="ss-meta">Mission association not resolved; see session details.</p>' : ''}
      <p class="ss-meta">${esc(short(entry.id,64))}${text(s.branch) ? ` · Branch ${esc(short(s.branch,70))}` : ''}</p>
    </div>
    <div class="ss-state"><strong data-report>${esc(sessionReport(s,now))}</strong><span>${esc(short(entry.host,90)||'Host not reported')}</span>
      <span class="ss-meta">Model: ${esc(short(s.model,120)||'Not reported')}</span></div>
  </article>`;
}

const styles=`<style>
.ss-intro{max-width:85ch;line-height:1.6;color:var(--text-secondary)}
.ss-tabs{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0}.ss-tabs button{font:inherit;padding:10px 16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);cursor:pointer}.ss-tabs button[aria-selected=true]{border-color:var(--accent);box-shadow:inset 0 -2px var(--accent)}.ss-tabs strong{margin-left:6px}
.ss-controls{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}.ss-controls label{display:flex;flex-direction:column;gap:6px;font-size:13px}.ss-controls .ss-search{flex:1;min-width:180px}.ss-controls input,.ss-controls select{padding:9px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit;color:var(--text);background:var(--surface);max-width:100%}
.ss-group{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin:12px 0;overflow:hidden}.ss-group h2{font-size:14px;margin:0;padding:12px 16px;overflow-wrap:anywhere}.ss-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(190px,290px);gap:16px;padding:14px 16px;border-top:1px solid var(--border-subtle);overflow-wrap:anywhere}.ss-main{min-width:0}.ss-title{font-size:14px;font-weight:600;line-height:1.5}.ss-summary{font-size:13px;line-height:1.5;margin:6px 0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.ss-meta{font-size:12px;color:var(--text-secondary);line-height:1.5;margin:6px 0}.ss-state{display:flex;flex-direction:column;gap:5px;font-size:12px;line-height:1.5}.ss-state strong{font-weight:500}.ss-pages{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:18px 0;font-size:13px}.ss-empty{padding:22px;color:var(--text-secondary)}
@media(max-width:650px){.ss-row{grid-template-columns:minmax(0,1fr);gap:8px}.ss-controls label{min-width:130px;flex:1}.ss-tabs button{padding:9px 12px}}
</style>`;

export function mountSessionsView(container,client,{tab='current'}={}) {
  const state={tab:tabs.some(([key])=>key===tab)?tab:'current',search:'',host:'',group:'project',page:0};
  const pageSize=20;
  let disposed=false,sessions=[],missions=[],loaded=false,sessionError='',missionError='',lastSignature='',hostSignature='';
  let sessionRequest=null,missionRequest=null;
  container.innerHTML=`${styles}<h1>Sessions</h1>
    <p class="ss-intro">Find an agent by project, host or model. Current shows open sessions reported in the last 15 minutes. Older or missing reports move to Unconfirmed automatically; Failed and Finished retain previous work.</p>
    <div class="ss-tabs" role="tablist" aria-label="Session outcome">${tabs.map(([key,label])=>`<button type="button" role="tab" id="ss-tab-${key}" data-tab="${key}" aria-controls="ss-panel-${key}" aria-selected="${state.tab===key}" tabindex="${state.tab===key?0:-1}">${label} <strong data-count="${key}">…</strong></button>`).join('')}</div>
    <div class="ss-controls"><label class="ss-search">Search sessions<input id="ss-search" type="search" placeholder="Project, activity, agent, model or ID"></label>
      <label>Host<select id="ss-host"><option value="">All hosts</option></select></label>
      <label>Group by<select id="ss-group"><option value="project">Project folder</option><option value="none">Recent reports</option></select></label></div>
    <p class="ss-meta" id="ss-result" role="status">Loading sessions…</p><p class="ss-meta" id="ss-error" role="status"></p>
    ${tabs.map(([key])=>`<section id="ss-panel-${key}" role="tabpanel" aria-labelledby="ss-tab-${key}" ${state.tab===key?'':'hidden'}></section>`).join('')}
    <div class="ss-pages"><button type="button" class="hd-btn" id="ss-previous">Previous</button><span id="ss-page"></span><button type="button" class="hd-btn" id="ss-next">Next</button></div>`;
  const el=id=>container.querySelector(`#ss-${id}`);
  const entries=()=>sessionEntries(sessions,missions);
  function draw() {
    if(disposed)return;
    const all=entries(), selected=selectSessions(all,state);
    state.page=Math.min(state.page,Math.max(0,Math.ceil(selected.length/pageSize)-1));
    el('error').textContent=[sessionError,missionError].filter(Boolean).join(' ');
    const signature=JSON.stringify([state,sessions,missions,loaded,all.map(e=>e.tab)]);
    if(signature===lastSignature)return;
    lastSignature=signature;
    const focused=document.activeElement;
    const focusId=focused?.closest('[data-session-id]')?.dataset.sessionId;
    const focusIndex=[...container.querySelectorAll('[data-session-id]')].findIndex(row=>row.dataset.sessionId===focusId);
    const focusMission=focused?.hasAttribute('data-mission-link');
    const scroll={x:window.scrollX,y:window.scrollY};
    for(const [key] of tabs) {
      const button=el(`tab-${key}`), active=state.tab===key;
      button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;
      button.querySelector('strong').textContent=loaded ? all.filter(e=>e.tab===key).length : '…';
      el(`panel-${key}`).hidden=!active;
      if(!active)el(`panel-${key}`).innerHTML='';
    }
    const hosts=[...new Set(all.map(e=>e.host).filter(Boolean).concat(state.host?[state.host]:[]))].sort();
    if(JSON.stringify(hosts)!==hostSignature){hostSignature=JSON.stringify(hosts);el('host').innerHTML='<option value="">All hosts</option>'+hosts.map(h=>`<option value="${esc(h)}">${esc(h)}</option>`).join('');el('host').value=state.host;}
    const visible=selected.slice(state.page*pageSize,(state.page+1)*pageSize),groups=new Map();
    for(const entry of visible){const label=state.group==='project'?entry.project:'Recent reports';if(!groups.has(label))groups.set(label,[]);groups.get(label).push(entry);}
    el(`panel-${state.tab}`).innerHTML=visible.length ? [...groups].map(([name,rows])=>`<div class="ss-group"><h2>${esc(name)}</h2>${rows.map(e=>compactSessionRow(e)).join('')}</div>`).join('')
      : `<p class="ss-empty">${loaded?'No sessions match this tab and these filters.':'Loading sessions…'}</p>`;
    el('result').textContent=loaded ? `${selected.length} matching ${state.tab} sessions${visible.length?` · Showing ${state.page*pageSize+1}–${state.page*pageSize+visible.length}`:''}` : 'Loading sessions…';
    el('page').textContent=`Page ${state.page+1} of ${Math.max(1,Math.ceil(selected.length/pageSize))}`;
    el('previous').disabled=state.page===0;el('next').disabled=(state.page+1)*pageSize>=selected.length;
    if(focusId){
      const rows=[...container.querySelectorAll('[data-session-id]')];
      const row=rows.find(n=>n.dataset.sessionId===focusId)||rows[Math.min(Math.max(focusIndex,0),rows.length-1)];
      const target=(focusMission?row?.querySelector('[data-mission-link]'):null)||row?.querySelector('[data-session-link]')||el('search');
      target.focus({preventScroll:true});window.scrollTo(scroll.x,scroll.y);
    }
  }
  function chooseTab(key){if(!tabs.some(([candidate])=>candidate===key))return;state.tab=key;state.page=0;draw();}
  function click(event){const button=event.target.closest('[data-tab]');if(button){chooseTab(button.dataset.tab);return;}if(event.target===el('previous')){state.page=Math.max(0,state.page-1);draw();}if(event.target===el('next')){state.page++;draw();}}
  function keydown(event){const button=event.target.closest('[data-tab]');if(!button)return;const keys=tabs.map(([key])=>key),index=keys.indexOf(button.dataset.tab);let next;
    if(event.key==='ArrowRight')next=(index+1)%keys.length;else if(event.key==='ArrowLeft')next=(index+keys.length-1)%keys.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=keys.length-1;else return;
    event.preventDefault();chooseTab(keys[next]);el(`tab-${keys[next]}`).focus();}
  function controls(event){for(const key of ['search','host','group'])if(event.target===el(key)){state[key]=event.target.value;state.page=0;draw();}}
  container.addEventListener('click',click);container.addEventListener('keydown',keydown);container.addEventListener('input',controls);container.addEventListener('change',controls);

  async function request(kind) {
    if(disposed||document.hidden||(kind==='sessions'?sessionRequest:missionRequest))return;
    const controller=new AbortController();
    if(kind==='sessions')sessionRequest=controller;else missionRequest=controller;
    let timeout;
    try {
      const method=kind==='sessions'?'listSessions':'listMissions';
      const data=await Promise.race([client[method](undefined,{signal:controller.signal}),new Promise((_,reject)=>{timeout=setTimeout(()=>{controller.abort();reject(new Error('Request timed out'));},12000);})]);
      if(disposed||controller.signal.aborted)return;
      if(!Array.isArray(data?.[kind]))throw new Error('Invalid response');
      if(kind==='sessions'){sessions=records(data.sessions);loaded=true;sessionError='';}else{missions=records(data.missions);missionError='';}
    } catch {
      if(!disposed&&!document.hidden){if(kind==='sessions')sessionError=loaded?'Session updates unavailable. Showing last received records.':'Sessions could not be loaded; retrying automatically.';else missionError='Mission context unavailable.';}
    } finally {
      clearTimeout(timeout);
      if(kind==='sessions')sessionRequest=null;else missionRequest=null;
      draw();
    }
  }
  function refresh(){return Promise.all([request('sessions'),request('missions')]);}
  function visibility(){if(document.hidden){sessionRequest?.abort();missionRequest?.abort();}else refresh();}
  document.addEventListener('visibilitychange',visibility);
  const poll=setInterval(()=>request('sessions'),5000), contextPoll=setInterval(()=>request('missions'),30000);
  const clock=setInterval(()=>{if(disposed||document.hidden)return;draw();const byId=new Map(entries().map(e=>[e.id,e]));container.querySelectorAll('[data-session-id]').forEach(row=>{const entry=byId.get(row.dataset.sessionId);if(entry)row.querySelector('[data-report]').textContent=sessionReport(entry.record);});},1000);
  draw();refresh();
  return {refresh,dispose(){disposed=true;sessionRequest?.abort();missionRequest?.abort();clearInterval(poll);clearInterval(contextPoll);clearInterval(clock);document.removeEventListener('visibilitychange',visibility);container.removeEventListener('click',click);container.removeEventListener('keydown',keydown);container.removeEventListener('input',controls);container.removeEventListener('change',controls);}};
}
