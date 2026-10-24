// Topology shows observed ownership. Folder names and ports never establish a project edge.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
import {recordVisibility,reportFreshness,reportTimestamp} from './record-state.js';
import {serviceState,serviceScope,safeServiceUrl} from './services-view.js';
import {missionEntries} from './mission-catalog-view.js';
import {integrationOutcome} from './integrations-view.js';
import {snapshotText,animateChanges} from './motion.js';

const text=value=>typeof value==='string'?value.trim():'';
const records=value=>Array.isArray(value)?value.filter(v=>v && typeof v==='object' && !Array.isArray(v)):[];
const short=(value,length=150)=>text(value).replace(/\s+/g,' ').slice(0,length);
const human=value=>text(value).toLowerCase().replaceAll('_',' ');
const TYPES={service:'Services',session:'Agents',mission:'Missions',ci:'CI runs'};
const TABS=[['current','Current'],['unconfirmed','Unconfirmed'],['history','History'],['all','All records']];
const history=new Set(['failed','finished','unavailable','archived']);
const visible=outcome=>history.has(outcome)?'history':outcome==='current'?'current':'unconfirmed';
function entry(record,type,id,fields) {
  const project=text(record.project_id),repository=text(record.repository_id);
  const row={record,type,id:`${type}:${id}`,host:text(record.host_id),project,repository,...fields};
  row.search=[row.title,row.host,row.project,row.repository,row.status,record.branch,record.revision,record.service_id,record.session_id,record.owner,record.cwd,record.catalog?.project,record.catalog?.label].filter(Boolean).join(' ').toLowerCase();
  return row;
}
export function topologyEntries(data={},received={},now=Date.now()) {
  const result=[];
  for(const s of records(data.services)) {
    if(!text(s.service_id)||serviceScope(s)!=='herdr')continue;
    const outcome=serviceState(s,now);
    result.push(entry(s,'service',s.service_id,{outcome,visibility:visible(outcome),title:text(s.service_name)||s.service_id,status:outcome==='current'?'Recently reported healthy':outcome==='unknown'?'Current health unconfirmed':human(s.status)||outcome,
      category:text(s.catalog?.category)||'other',categoryLabel:text(s.catalog?.label)||'Other services',stamp:s.lifecycle?.last_seen_at||s.updated_at,
      href:`#/services/${encodeURIComponent(s.service_id)}`,endpoint:outcome==='current'&&s.catalog?.endpoint_scope!=='local_only'&&s.endpoint_scope!=='local_only'?safeServiceUrl(s.url):null}));
  }
  for(const s of records(data.sessions)) {
    if(!text(s.session_id))continue;
    const outcome=recordVisibility(s,now);
    result.push(entry(s,'session',s.session_id,{outcome,visibility:visible(outcome),title:short(s.display_name||s.title||s.summary)||text(s.agent_role)||'Agent',status:outcome==='unconfirmed'?`Unconfirmed · last reported ${human(s.status)||'unknown'}`:`Last reported ${human(s.status)||outcome}`,stamp:s.last_seen_at||s.updated_at,href:`#/herd/session/${encodeURIComponent(s.session_id)}`}));
  }
  for(const m of missionEntries(data.projections,data.missions,received.projections||now,now)) {
    const record={...(m.recorded||{}),...(m.projection?.mission||{}),host_id:m.host};
    const outcome=['current','unconfirmed','failed','finished'].includes(m.tab)?m.tab:'unconfirmed';
    result.push(entry(record,'mission',m.key,{outcome,visibility:visible(outcome),title:m.title,status:outcome==='unconfirmed'?'Activity unconfirmed':human(m.health)||human(m.status),attention:m.group==='attention',stamp:m.projection?.activity_snapshot_at||m.projection?.snapshot_at||record.updated_at,
      description:short(m.projection?.activity?.currentActivity?.summary||record.purpose),href:`#/missions/${encodeURIComponent(m.key)}`}));
  }
  const ci=data.ci||{};
  for(const [repository,runs] of Object.entries(ci.pipelines||{}))for(const r of records(runs)) {
    const record={...r,repository:r.repository||repository};
    const outcome=integrationOutcome(record);
    const state=human(r.state||r.status);
    const confirmed=ci.available!==false && !ci.stale && reportFreshness(ci.last_success_at||ci.checked_at,now)==='current' && ['running','pending','queued','blocked','waiting','scheduled'].includes(state);
    const visibility=outcome==='current'?(confirmed?'current':'unconfirmed'):'history';
    const query=new URLSearchParams({repository:record.repository_id||record.repository,tab:outcome==='failed'?'failed':outcome==='finished'?'finished':'current',search:String(r.run_number??'')});
    result.push(entry(record,'ci',`${r.provider_repo_id||record.repository}#${r.run_number}`,{outcome,visibility,host:'',title:`${record.repository} · #${r.run_number??'?'}`,status:visibility==='unconfirmed'?`Unconfirmed · ${state||'state unknown'}`:state||'State unknown',stamp:ci.last_success_at,description:short(r.branch),href:`#/ci?${query}`,endpoint:safeServiceUrl(r.url)}));
  }
  const projectsByRepository=new Map();
  for(const repo of records(ci.repos)){if(!text(repo.repository_id)||!text(repo.project_id))continue;if(!projectsByRepository.has(repo.repository_id))projectsByRepository.set(repo.repository_id,new Set());projectsByRepository.get(repo.repository_id).add(repo.project_id);}
  for(const row of result){const projects=projectsByRepository.get(row.repository);if(!row.project && projects?.size===1){row.project=[...projects][0];row.search+=` ${row.project.toLowerCase()}`;}}
  return result.sort((a,b)=>Number(!!b.attention)-Number(!!a.attention)||a.title.localeCompare(b.title)||a.id.localeCompare(b.id));
}
export function selectTopology(entries,{status='current',search='',type='',host=''}={}) {
  const terms=text(search).toLowerCase().split(/\s+/).filter(Boolean);
  return entries.filter(e=>(status==='all'||e.visibility===status)&&(!type||e.type===type)&&(!host||e.host===host)&&terms.every(t=>e.search.includes(t)));
}
export function groupTopology(entries,hosts=[],{group='host',includeEmptyHosts=false}={}) {
  const hostMap=new Map(records(hosts).filter(h=>text(h.host_id)).map(h=>[h.host_id,h]));
  const groups=new Map();
  const add=(key,label,extra={})=>{if(!groups.has(key))groups.set(key,{key,label,entries:[],...extra});return groups.get(key);};
  if(group==='host'&&includeEmptyHosts)for(const host of hostMap.values())add(`host:${host.host_id}`,text(host.host_name)||host.host_id,{host});
  for(const row of entries) {
    let node;
    if(group==='project') {
      node=row.project && row.project!==row.repository?add(`project:${row.project}`,row.project,{evidence:'Explicit project identity'}):row.repository?add(`repository:${row.repository}`,row.repository,{evidence:'Reported repository identity'}):row.project?add(`project:${row.project}`,row.project,{evidence:'Explicit project identity'}):add('unassigned','Project not established',{evidence:'Workspace names are shown as context; they do not establish a shared project.'});
    } else if(row.type==='ci') node=add('ci','Woodpecker',{external:true,evidence:'CI runs have no confirmed fleet host association.'});
    else if(hostMap.has(row.host)){const host=hostMap.get(row.host);node=add(`host:${row.host}`,text(host.host_name)||row.host,{host});}
    else node=add(`unmatched:${row.host}`,row.host||'Host not reported',{unconfirmedHost:true,evidence:row.host?'No matching host record. These observations remain inspectable.':'The reporter did not provide a host identity.'});
    node.entries.push(row);
  }
  return [...groups.values()].sort((a,b)=>Number(!!a.unconfirmedHost||!!a.external)-Number(!!b.unconfirmedHost||!!b.external)||a.label.localeCompare(b.label));
}
const age=(stamp,now)=>{const value=reportTimestamp(stamp);return Number.isFinite(value)?`<span data-topology-time="${value}">${esc(formatDuration(Math.max(0,now-value)))}</span> ago`:'Time not reported';};
export function topologyRow(row,now=Date.now()) {
  const r=row.record;
  const context=row.type==='service'?[row.categoryLabel,text(r.owner)&&`Process: ${r.owner}`,r.catalog?.source_port!=null?`Source :${r.catalog.source_port}`:null,r.port!=null?`${(r.catalog?.endpoint_scope==='local_only'||r.endpoint_scope==='local_only')?'Observed':'Exposed'} :${r.port}`:null,(r.catalog?.endpoint_scope==='local_only'||r.endpoint_scope==='local_only')?'Local only':null].filter(Boolean).join(' · '):row.type==='session'?[r.agent_role,r.branch,r.model].filter(Boolean).join(' · '):row.description;
  const workspace=row.type==='service'?text(r.catalog?.project):row.type==='session'?text(r.cwd):'';
  const source=row.type==='service'?text(r.catalog?.project_source):'agent cwd';
  return `<article class="tx-row" data-topology-entry="${esc(row.id)}"><div class="tx-main"><a data-entry-link href="${esc(row.href)}">${esc(short(row.title))}</a>${context?`<p>${esc(short(context,220))}</p>`:''}${workspace?`<p class="tx-context">Workspace: ${esc(short(workspace))}${source?` · ${esc(source)}`:''}</p>`:''}<div class="tx-links">${row.host?`<span>${esc(row.host)}</span>`:''}${row.endpoint?`<a href="${esc(row.endpoint)}" target="_blank" rel="noopener noreferrer">${row.type==='ci'?'Upstream run':'Open endpoint'} ↗</a>`:''}</div></div><div class="tx-state"><strong class="${row.attention?'tx-warning':''}">${row.visibility==='current'?'<span class="tx-dot" aria-hidden="true"></span>':''}${esc(row.status)}</strong><span>Report ${age(row.stamp,now)}</span></div></article>`;
}
const counts=entries=>Object.keys(TYPES).map(type=>[type,entries.filter(e=>e.type===type).length]).filter(([,n])=>n);
function subgroupEntries(entries) {
  const groups=new Map();
  for(const row of entries){const key=row.type==='service'?`service:${row.category}`:row.type;if(!groups.has(key))groups.set(key,{key,label:row.type==='service'?row.categoryLabel:TYPES[row.type],entries:[]});groups.get(key).entries.push(row);}
  return [...groups.values()].sort((a,b)=>(a.key.startsWith('service:')?1:0)-(b.key.startsWith('service:')?1:0)||a.label.localeCompare(b.label));
}
const styles=`<style>
.tx-intro,.tx-note{font-size:13px;line-height:1.6;color:var(--text-secondary)}.tx-intro{max-width:86ch}.tx-note{font-size:12px;margin:8px 0}.tx-warning{color:var(--status-attention)}.tx-metrics{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.tx-metrics>div{padding:11px 15px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);display:flex;align-items:baseline;gap:8px;font-size:12px;color:var(--text-secondary)}.tx-metrics strong{font-size:24px;color:var(--text)}.tx-tabs{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}.tx-tabs button{font:inherit;padding:9px 14px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);cursor:pointer}.tx-tabs button[aria-selected=true]{border-color:var(--accent);box-shadow:inset 0 -2px var(--accent)}.tx-tabs small{margin-left:7px;color:var(--text-secondary)}.tx-controls{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}.tx-controls label{display:flex;flex-direction:column;gap:6px;font-size:13px;min-width:120px}.tx-controls label:first-child{flex:1;min-width:180px}.tx-controls input,.tx-controls select{padding:9px;font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);width:100%;max-width:100%;box-sizing:border-box}.tx-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:12px 0}.tx-group{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin:12px 0;overflow:hidden}.tx-group>summary,.tx-subgroup>summary{cursor:pointer;padding:10px 13px;overflow-wrap:anywhere;line-height:1.5;font-size:14px;font-weight:600}.tx-group>summary>span{font-weight:400;font-size:12px;color:var(--text-secondary);margin-left:10px}.tx-group-body{padding:0 16px 14px}.tx-host-context{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 10px;font-size:12px;line-height:1.6;color:var(--text-secondary)}.tx-subgroup{border:1px solid var(--border-subtle);border-radius:var(--radius-sm);margin-top:10px;overflow:hidden}.tx-subgroup>summary{background:var(--bg);font-size:13px;padding:10px 12px}.tx-subgroup>summary span{font-weight:400;color:var(--text-secondary);margin-left:8px}.tx-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(150px,220px);gap:12px;padding:9px 11px;border-top:1px solid var(--border-subtle);overflow-wrap:anywhere}.tx-main{min-width:0}.tx-main>a{font-size:13px;font-weight:600;line-height:1.6}.tx-main p{font-size:12px;line-height:1.6;color:var(--text-secondary);margin:5px 0}.tx-context{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.tx-state{font-size:12px;line-height:1.6;display:flex;flex-direction:column;gap:4px}.tx-state strong{font-weight:500}.tx-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--status-ok);margin-right:6px;vertical-align:middle}.tx-state span{color:var(--text-secondary)}.tx-links{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--text-secondary)}.tx-more{padding:10px 12px;border-top:1px solid var(--border-subtle)}.tx-empty{padding:20px;color:var(--text-secondary);font-size:13px}.tx-footer{display:flex;flex-wrap:wrap;gap:14px;margin:18px 0;font-size:12px}.tx-feedback p{margin:5px 0}.tx-controls select{max-width:250px}@media(min-width:2200px){.tx-controls label:first-child{flex:0 1 720px}.tx-controls{padding-right:max(0px,calc(100% - 1500px))}#tx-results{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}.tx-group{margin:0}}@media(max-width:650px){.tx-row{grid-template-columns:minmax(0,1fr);gap:8px}.tx-group-body{padding:0 10px 10px}.tx-group>summary>span{display:block;margin:5px 0 0}.tx-toolbar{align-items:start}.tx-tabs button{padding:9px 11px}.tx-controls label{flex:1}.tx-controls select{max-width:none}}
</style>`;

export function mountTopology(container,client,{status='current'}={}) {
  const state={status:TABS.some(([key])=>key===status)?status:'current',search:'',type:'',host:'',group:'host',groupLimit:12};
  let disposed=false,clock=null,lastSignature='',hostSignature='';
  const data={},received={},sourceStates={},requests=new Map(),timers=new Map(),opened=new Map(),limits=new Map();
  const listeners=new AbortController();
  const sources={hosts:{label:'Hosts',field:'hosts',read:signal=>client.listHosts({signal})},sessions:{label:'Agents',field:'sessions',read:signal=>client.listSessions(undefined,{signal})},services:{label:'Services',field:'services',read:signal=>client.listServices({signal})},missions:{label:'Recorded missions',field:'missions',read:signal=>client.listMissions(undefined,{signal})},projections:{label:'Live missions',field:'activities',read:signal=>client.listMissionActivity({signal})},ci:{label:'CI',read:signal=>client.getCi({signal})}};
  container.innerHTML=`${styles}<h1>Topology</h1><p class="tx-intro">Explore current work and services started through HerdR by host or confirmed project identity. Expand a group to inspect its records. Stopped, finished and unconfirmed records remain in their own views.</p><div data-tx-metrics class="tx-metrics"></div>
    <div class="tx-tabs" role="tablist" aria-label="Topology visibility">${TABS.map(([key,label])=>`<button type="button" id="tx-tab-${key}" role="tab" data-tx-tab="${key}" aria-controls="tx-results" aria-selected="${key===state.status}" tabindex="${key===state.status?0:-1}">${label}<small data-tab-count="${key}">…</small></button>`).join('')}</div>
    <div class="tx-controls"><label>Search topology<input type="search" data-tx-search placeholder="Service, project, agent, repository or host"></label><label>Group by<select data-tx-group><option value="host">Host</option><option value="project">Project / repository</option></select></label><label>Type<select data-tx-type><option value="">All types</option>${Object.entries(TYPES).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')}</select></label><label>Host<select data-tx-host><option value="">All hosts</option></select></label></div>
    <div class="tx-toolbar"><p class="tx-note" data-tx-result role="status">Loading observations…</p><button type="button" class="hd-btn" data-tx-collapse>Collapse groups</button></div><div class="tx-feedback" data-tx-feedback role="status"></div><p class="tx-note" data-tx-policy></p><section id="tx-results" role="tabpanel" aria-labelledby="tx-tab-current"></section><div data-tx-more-groups></div>
    <nav class="tx-footer" aria-label="Detailed catalogs"><a href="#/hosts">Hosts</a><a href="#/services">HerdR services</a><a href="#/system-services">System services</a><a href="#/herd">Agents</a><a href="#/missions">Missions</a><a href="#/ci">CI</a><a href="#/reviews">Reviews</a></nav><p class="tx-note">Reports refresh independently every 5 seconds. A recent report does not prove a process is currently running.</p>`;
  const el=selector=>container.querySelector(selector);
  const allEntries=()=>topologyEntries(data,received);
  const isOpen=(key,otherwise=false)=>opened.has(key)?opened.get(key):otherwise;
  function groupMarkup(group,now) {
    const open=isOpen(group.key,!!state.search);
    const summary=counts(group.entries).map(([type,n])=>`${n} ${TYPES[type].toLowerCase()}`).join(' · ')||'No matching records';
    const host=group.host;
    const hostStamp=host?.last_seen||host?.last_heartbeat||host?.updated_at;
    return `<details class="tx-group" data-tx-disclosure="${esc(group.key)}" ${open?'open':''}><summary>${esc(short(group.label,180))}<span>${esc(summary)}</span></summary>${open?`<div class="tx-group-body">${host?`<div class="tx-host-context"><a href="#/hosts/${esc(encodeURIComponent(host.host_id))}">Inspect host</a><span>Last reported ${esc(human(host.status)||'unknown')} · ${age(hostStamp,now)}${reportFreshness(hostStamp,now)!=='current'?' · Host report unconfirmed':''}</span></div>`:`<p class="tx-note">${esc(group.evidence||'')}</p>`}${subgroupEntries(group.entries).map(sub=>{
      const key=`${group.key}/${sub.key}`,subOpen=isOpen(key,!!state.search||!sub.key.startsWith('service:')),limit=limits.get(key)||6;
      return `<details class="tx-subgroup" data-tx-disclosure="${esc(key)}" ${subOpen?'open':''}><summary>${esc(sub.label)}<span>${sub.entries.length}</span></summary>${subOpen?sub.entries.slice(0,limit).map(row=>topologyRow(row,now)).join('')+(sub.entries.length>limit?`<div class="tx-more"><button type="button" class="hd-btn" data-tx-more="${esc(key)}">Show ${Math.min(6,sub.entries.length-limit)} more · ${sub.entries.length-limit} remaining</button></div>`:''):''}</details>`;
    }).join('')||'<p class="tx-note">No records match the selected view for this host.</p>'}</div>`:''}</details>`;
  }
  const metricKey=node=>node.nextElementSibling?.textContent||null;
  const entryKey=node=>node.closest('[data-topology-entry]')?.dataset.topologyEntry;
  function tick(){for(const stamp of container.querySelectorAll('[data-topology-time]'))stamp.textContent=formatDuration(Math.max(0,Date.now()-Number(stamp.dataset.topologyTime)));}
  function draw() {
    if(disposed||document.hidden)return;
    const now=Date.now(),all=allEntries(),selected=selectTopology(all,state);
    const signature=JSON.stringify([data,sourceStates,state,[...opened],[...limits],all.map(e=>[e.id,e.visibility,e.status])]);
    if(signature===lastSignature){tick();return;}lastSignature=signature;
    const hosts=records(data.hosts),groups=groupTopology(selected,hosts,{group:state.group,includeEmptyHosts:!state.search&&!state.type&&!state.host});
    const sourceReady=kind=>kind==='ci'?sourceStates.ci?.loaded && data.ci?.available!==false:kind==='mission'?sourceStates.projections?.loaded&&sourceStates.missions?.loaded:sourceStates[{service:'services',session:'sessions',ci:'ci'}[kind]]?.loaded;
    // Panels are rebuilt with innerHTML (Task 19 moves them onto the patcher); motion pairs old and
    // new nodes by key: metric tiles by label, rows by data-topology-entry (status change = flash).
    const metricsBefore=snapshotText(el('[data-tx-metrics]'),'strong',metricKey),tabsBefore=snapshotText(container,'[data-tab-count]');
    const rowsBefore=snapshotText(el('#tx-results'),'[data-topology-entry] .tx-state strong',entryKey);
    el('[data-tx-metrics]').innerHTML=Object.entries(TYPES).map(([kind,label])=>`<div><strong>${sourceReady(kind)?all.filter(e=>e.type===kind&&e.visibility==='current').length:'—'}</strong><span>${label==='Services'?'recently healthy HerdR services':label==='CI runs'?'current CI runs':`current ${label.toLowerCase()}`}</span></div>`).join('');
    animateChanges(el('[data-tx-metrics]'),'strong',metricsBefore,metricKey);
    for(const [key] of TABS){const button=el(`[data-tx-tab="${key}"]`);button.setAttribute('aria-selected',String(state.status===key));button.tabIndex=state.status===key?0:-1;el(`[data-tab-count="${key}"]`).textContent=Object.values(sourceStates).some(s=>s.loaded)?selectTopology(all,{...state,status:key}).length:'…';}
    const hostIds=[...new Set([...hosts.map(h=>text(h.host_id)),...all.map(e=>e.host),state.host].filter(Boolean))].sort();
    if(JSON.stringify(hostIds)!==hostSignature){hostSignature=JSON.stringify(hostIds);el('[data-tx-host]').innerHTML='<option value="">All hosts</option>'+hostIds.map(id=>`<option value="${esc(id)}">${esc(id)}</option>`).join('');el('[data-tx-host]').value=state.host;}
    const unavailable=Object.entries(sources).filter(([key])=>!sourceStates[key]?.loaded).map(([,source])=>source.label);
    el('[data-tx-result]').textContent=`${selected.length} matching ${state.status==='current'?'current ':''}records · ${groups.length} ${state.group==='host'?'host / provider':'project / repository'} groups${unavailable.length?' · Partial view':''}`;
    const notices=Object.entries(sources).map(([key,source])=>sourceStates[key]?.error?`${source.label} could not refresh.${sourceStates[key].loaded?' Showing last received records.':' Retrying automatically.'}`:!sourceStates[key]?.loaded?`Loading ${source.label.toLowerCase()}…`:null).filter(Boolean);
    if(data.ci?.status==='auth_error')notices.push('Woodpecker authentication needs attention.');
    else if(data.ci?.available===false)notices.push(data.ci.status==='syncing'?'Woodpecker is discovering repositories and runs.':'CI reports are unavailable.');
    if(data.ci?.stale)notices.push('CI reports are stale; open runs are shown under Unconfirmed.');
    if(data.ci?.project_mapping_error)notices.push(data.ci.project_mapping_error);
    el('[data-tx-feedback]').innerHTML=notices.map(note=>`<p class="tx-note tx-warning">${esc(note)}</p>`).join('');
    el('[data-tx-policy]').textContent=state.group==='project'?'Only explicit project or repository identities connect records. Inferred workspace folders remain context in Project not established.':state.status==='current'?'Current includes recent positive observations and fresh mission attention. Unavailable, stopped and finished records stay in History; old or unknown reports stay in Unconfirmed.':state.status==='history'?'History includes stopped, failed, finished, unavailable and archived records. Nothing is deleted.':'Unconfirmed reports retain their last known state; missing updates do not mean a process stopped.';
    const results=el('#tx-results'),focused=document.activeElement;
    const focusDisclosure=results.contains(focused)&&focused.tagName==='SUMMARY'?focused.closest('[data-tx-disclosure]')?.dataset.txDisclosure:null;
    const focusMore=results.contains(focused)?focused.closest('[data-tx-more]')?.dataset.txMore:null;
    const focusHref=results.contains(focused)&&focused.tagName==='A'?focused.getAttribute('href'):null;
    const focusGroupPager=container.contains(focused)&&focused.hasAttribute('data-tx-more-groups-button');
    const previousGroups=focusGroupPager?new Set([...results.children].map(group=>group.dataset.txDisclosure)):null;
    const scroll={x:window.scrollX,y:window.scrollY};
    results.setAttribute('aria-labelledby',`tx-tab-${state.status}`);
    results.innerHTML=groups.slice(0,state.groupLimit).map(g=>groupMarkup(g,now)).join('')||`<div class="tx-group tx-empty">${unavailable.length?'Waiting for matching reports. Source availability is shown above.':'No records match this view. Change the filters or open Unconfirmed / History to inspect previous observations.'}</div>`;
    animateChanges(container,'[data-tab-count]',tabsBefore);
    animateChanges(results,'[data-topology-entry] .tx-state strong',rowsBefore,entryKey,{count:false,target:node=>node.closest('[data-topology-entry]')});
    el('[data-tx-more-groups]').innerHTML=groups.length>state.groupLimit?`<button type="button" class="hd-btn" data-tx-more-groups-button>Show ${Math.min(12,groups.length-state.groupLimit)} more groups · ${groups.length-state.groupLimit} remaining</button>`:'';
    if(focusGroupPager){
      const pager=el('[data-tx-more-groups-button]');
      const added=[...results.children].find(group=>!previousGroups.has(group.dataset.txDisclosure));
      (pager||added?.querySelector('summary')||results.lastElementChild?.querySelector('summary'))?.focus({preventScroll:true});
    }
    if(focusDisclosure)[...results.querySelectorAll('[data-tx-disclosure]')].find(e=>e.dataset.txDisclosure===focusDisclosure)?.querySelector('summary')?.focus({preventScroll:true});
    if(focusMore)[...results.querySelectorAll('[data-tx-more]')].find(e=>e.dataset.txMore===focusMore)?.focus({preventScroll:true});
    if(focusHref)[...results.querySelectorAll('a')].find(e=>e.getAttribute('href')===focusHref)?.focus({preventScroll:true});
    if(focusDisclosure||focusMore||focusHref||focusGroupPager){if(document.activeElement===document.body)el(`[data-tx-tab="${state.status}"]`).focus({preventScroll:true});window.scrollTo(scroll.x,scroll.y);}tick();
  }
  async function poll(key) {
    if(disposed||document.hidden||requests.has(key))return;
    clearTimeout(timers.get(key));const controller=new AbortController();requests.set(key,controller);let timeout=false;
    const deadline=setTimeout(()=>{timeout=true;controller.abort();},15000);
    try{const response=await sources[key].read(controller.signal);if(disposed||controller.signal.aborted)return;
      if(!response||(key==='ci'?typeof response.available!=='boolean':!Array.isArray(response[sources[key].field])))throw new Error('Invalid response');
      data[key]=key==='ci'?response:records(response[sources[key].field]);received[key]=Date.now();sourceStates[key]={loaded:true,error:false};draw();
    }catch{if(!disposed&&(!controller.signal.aborted||timeout)){sourceStates[key]={...sourceStates[key],error:true};draw();}}
    finally{clearTimeout(deadline);if(requests.get(key)===controller)requests.delete(key);if(!disposed&&!document.hidden)timers.set(key,setTimeout(()=>poll(key),5000));}
  }
  const refresh=()=>Object.keys(sources).forEach(key=>poll(key));
  container.addEventListener('toggle',event=>{const element=event.target;if(disposed||!element.isConnected||!element.hasAttribute?.('data-tx-disclosure'))return;const key=element.dataset.txDisclosure;if(opened.get(key)===element.open)return;opened.set(key,element.open);draw();},{capture:true,signal:listeners.signal});
  container.addEventListener('click',event=>{const button=event.target.closest('button');if(!button||disposed)return;
    if(button.dataset.txTab){state.status=button.dataset.txTab;state.groupLimit=12;draw();}
    if(button.hasAttribute('data-tx-collapse')){for(const d of container.querySelectorAll('[data-tx-disclosure]'))opened.set(d.dataset.txDisclosure,false);draw();}
    if(button.dataset.txMore){limits.set(button.dataset.txMore,(limits.get(button.dataset.txMore)||6)+6);draw();}
    if(button.hasAttribute('data-tx-more-groups-button')){state.groupLimit+=12;draw();}
  },{signal:listeners.signal});
  for(const key of ['search','group','type','host'])el(`[data-tx-${key}]`).addEventListener(key==='search'?'input':'change',event=>{state[key]=event.target.value;state.groupLimit=12;draw();},{signal:listeners.signal});
  container.querySelector('.tx-tabs').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const i=TABS.findIndex(([key])=>key===state.status);state.status=TABS[event.key==='Home'?0:event.key==='End'?TABS.length-1:(i+(event.key==='ArrowRight'?1:TABS.length-1))%TABS.length][0];event.preventDefault();draw();el(`[data-tx-tab="${state.status}"]`).focus();},{signal:listeners.signal});
  function visibility(){clearInterval(clock);for(const timer of timers.values())clearTimeout(timer);timers.clear();if(document.hidden){for(const controller of requests.values())controller.abort();requests.clear();return;}draw();refresh();clock=setInterval(draw,1000);}
  document.addEventListener('visibilitychange',visibility,{signal:listeners.signal});visibility();
  return {refresh,dispose(){disposed=true;listeners.abort();clearInterval(clock);for(const timer of timers.values())clearTimeout(timer);for(const controller of requests.values())controller.abort();requests.clear();}};
}
