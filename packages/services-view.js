// Services are observations, not proof that an old process is still running.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { reportFreshness } from './record-state.js';
import { formatTimestamp, formatDuration } from './herdr-web-components/format.js';

const metadataObservation = service => service.catalog?.metadata_observation ?? service.metadata_observation;
const localOnly = service => service.endpoint_scope === 'local_only' || service.catalog?.endpoint_scope === 'local_only';
function observationCurrent(observation, now) {
  const seconds=Number.isFinite(observation?.stale_after_seconds) && observation.stale_after_seconds>0 ? observation.stale_after_seconds : 90;
  return observation?.state === 'observed' && reportFreshness(observation.observed_at,now,seconds*1000)==='current';
}
export function serviceState(s, now = Date.now()) {
  if (s.lifecycle?.archived || s.lifecycle?.state === 'archived') return 'archived';
  const status = typeof s.status === 'string' ? s.status.toLowerCase() : '';
  if (['unavailable', 'stopped', 'dead', 'failed'].includes(status) || s.lifecycle?.state === 'unavailable') return 'unavailable';
  if (s.lifecycle?.state === 'unknown') return 'unknown';
  const observation=metadataObservation(s);
  if (['missing','unconfirmed'].includes(observation?.state) || (observation && serviceScope(s)==='herdr' && !observationCurrent(observation,now))) return 'unknown';
  if (s.lifecycle?.state === 'current' || ['healthy', 'running', 'active', 'available'].includes(status)) {
    return reportFreshness(s.lifecycle?.last_seen_at ?? s.updated_at, now) === 'current' ? 'current' : 'unknown';
  }
  return 'unknown';
}
// Origin is verified by the backend. A project name, repository or reused port
// does not establish that a service was started by a HerdR session.
export function serviceScope(s) {
  return ['herdr','system','unassigned'].includes(s?.catalog?.scope) ? s.catalog.scope : 'unassigned';
}
// Three catalogs: started in HerdR, verified system units, and everything else the
// host listens on (local dev servers, tunnels) which has no proven owner.
const SCOPE_OF = {herdr:'herdr', system:'system', local:'unassigned'};
const inScope = (service, scope) => serviceScope(service) === (SCOPE_OF[scope] || 'herdr');
const SCOPE_PAGE = {herdr:{path:'#/services',title:'Services',label:'HerdR services'}, system:{path:'#/system-services',title:'System services',label:'System services'}, local:{path:'#/local-services',title:'Local services',label:'Local services'}};
const SCOPE_INTRO = {herdr:'Applications started in HerdR. Each service shows its reported project, purpose and discovered endpoint.', system:'Services confirmed to belong to a system unit. Their origin and last report remain inspectable.', local:'Dev servers, tunnels and other listeners on your machines that were not launched from a HerdR pane. HerdR cannot prove who started them, so they are listed here rather than as HerdR services.'};
const stateLabel = s => ({current:'Reported healthy', unavailable:'Unavailable', archived:'Archived', unknown:'Unconfirmed'})[serviceState(s)];
function serviceReason(s) {
  const observation=metadataObservation(s);
  if(serviceState(s)==='unknown') {
    if(observation?.state==='unconfirmed')return 'Listener ownership could not be confirmed; current app activity is unconfirmed.';
    if(observation?.state==='missing')return 'Listener was absent from the latest observation; its current state is unconfirmed.';
    if(observation && serviceScope(s)==='herdr' && !observationCurrent(observation,Date.now()))return 'No recent listener observation; current app activity is unconfirmed.';
  }
  return s.lifecycle?.reason || 'Status from the latest service report.';
}
const reportedText = value => typeof value === 'string' && value.trim() ? value.trim() : null;
const identity = (service, key) => reportedText(service.catalog?.[key]) || reportedText(service[key]);
const typeLabel = s => reportedText(s.catalog?.label) || 'Unclassified service';
const hostLabel = s => reportedText(s.host_id) || 'Unknown host';
const serviceLink = s => `#/services/${encodeURIComponent(s.service_id)}`;

export function safeServiceUrl(value) {
  if (typeof value !== 'string' || /[\x00-\x20\x7f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function selectServices(services, {search = '', host = '', status = 'current', archived = false, now = Date.now(), scope = 'herdr'} = {}) {
  const terms = search.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  return services.filter(s => {
    if (!inScope(s, scope)) return false;
    const state = serviceState(s, now);
    if (state === 'archived' && !archived) return false;
    if (status !== 'all' && state !== status) return false;
    if (host && hostLabel(s) !== host) return false;
    const text = [s.service_name,s.description,s.owner,s.host_id,s.port,s.service_id,s.catalog?.source_port,
      s.catalog?.project,s.catalog?.label,s.catalog?.purpose,identity(s,'project_id'),identity(s,'repository_id'),identity(s,'commit_sha')].filter(x => x != null).join(' ').toLocaleLowerCase();
    return terms.every(term => text.includes(term));
  }).sort((a,b) => String(a.service_name || '').localeCompare(String(b.service_name || '')) || String(a.service_id).localeCompare(String(b.service_id)));
}

export function groupServices(services, by = 'project') {
  const groups = new Map();
  for (const service of services) {
    const project=identity(service,'project_id'), repository=identity(service,'repository_id');
    const label = by === 'host' ? hostLabel(service) : by === 'type' ? typeLabel(service)
      : project || repository || `No verified project · ${typeLabel(service)}`;
    const category=by==='host'||by==='type' ? by : project ? 'project' : repository ? 'repository' : 'unassigned';
    const key=`${category}:${label}`;
    if (!groups.has(key)) groups.set(key, {key, label, services:[]});
    groups.get(key).services.push(service);
  }
  return [...groups.values()].sort((a,b) => a.label.localeCompare(b.label));
}

function lastSeen(s) {
  const date = s.lifecycle?.last_seen_at || s.updated_at;
  const age = date ? Date.now() - Date.parse(date) : NaN;
  return Number.isFinite(age) ? `${formatDuration(Math.max(0,age))} ago` : 'Not reported';
}
function endpoint(s) {
  if(localOnly(s))return '<span class="sc-meta">Local only · no remote endpoint</span>';
  const url = safeServiceUrl(s.url);
  if (!url) return '<span class="muted">No web endpoint</span>';
  return serviceState(s) === 'current' ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open endpoint ↗</a>`
    : `<span class="sc-meta">Recorded endpoint (not current): ${esc(url)}</span>`;
}
function serviceRow(s) {
  return `<article class="sc-row" data-service-id="${esc(s.service_id)}">
    <div class="sc-identity"><a class="sc-name" href="${esc(serviceLink(s))}">${esc(s.service_name || s.service_id)}</a>
      <p>${esc(s.catalog?.purpose || s.description || 'Purpose not reported.')}</p>
      ${s.description && s.catalog?.purpose && s.description !== s.catalog.purpose ? `<p class="sc-meta">${esc(s.description)}</p>` : ''}
      ${identity(s,'repository_id') ? `<p class="sc-meta">Repository: ${esc(identity(s,'repository_id'))}</p>` : ''}
      ${reportedText(s.catalog?.project) ? `<p class="sc-meta">Workspace folder: ${esc(s.catalog.project)}</p>` : ''}
      <span class="sc-meta">${esc(typeLabel(s))}${s.owner ? ` · Process: ${esc(s.owner)}` : ''}</span></div>
    <div class="sc-location"><a href="#/hosts/${esc(encodeURIComponent(hostLabel(s)))}">${esc(hostLabel(s))}</a>
      <span>${localOnly(s) ? 'Observed port ' : `${s.catalog?.source_port != null ? `Source :${esc(s.catalog.source_port)} · ` : ''}Exposed `}${s.port != null ? `:${esc(s.port)}` : 'port unknown'}</span>${endpoint(s)}</div>
    <div class="sc-health"><strong data-state="${serviceState(s)}">${stateLabel(s)}</strong><span>Last report ${esc(lastSeen(s))}</span>
      <span class="sc-meta">${esc(serviceReason(s))}</span></div>
  </article>`;
}

const styles = `<style>
.sc-intro {color:var(--text-secondary); max-width:80ch; line-height:1.6}
.sc-history>summary {cursor:pointer;color:var(--text-secondary);font-size:13px}.sc-history .sc-counts {margin:12px 0}
.sc-counts {display:flex; flex-wrap:wrap; gap:8px; margin:12px 0}
.sc-counts button {font:inherit;color:inherit;text-align:left;cursor:pointer;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 14px;font-size:13px}
.sc-counts strong {font-size:20px;margin-right:5px}
.sc-controls {display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:12px 0}
.sc-controls label {display:flex;flex-direction:column;gap:5px;font-size:13px}
.sc-controls input[type=search],.sc-controls select {border:1px solid var(--border);border-radius:var(--radius-sm);padding:9px;background:var(--surface);color:var(--text);font:inherit;max-width:100%}
.sc-controls .sc-search {flex:1;min-width:190px}.sc-controls .sc-check {flex-direction:row;align-items:center;min-height:38px}
.sc-toolbar {display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin:15px 0;font-size:13px}
.sc-group {border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);margin:12px 0;overflow:hidden}
.sc-group>summary {cursor:pointer;padding:11px 13px;font-weight:600;overflow-wrap:anywhere}
.sc-group>summary span {font-weight:400;font-size:12px;color:var(--text-secondary);margin-left:12px}
.sc-row {display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr) minmax(0,1.2fr);gap:16px;padding:11px 13px;border-top:1px solid var(--border-subtle);font-size:13px;overflow-wrap:anywhere}
.sc-name {font-size:14px;font-weight:600}.sc-row p {margin:6px 0;line-height:1.5}.sc-meta {color:var(--text-secondary);font-size:12px}
.sc-location,.sc-health {display:flex;flex-direction:column;gap:7px}.sc-health strong {font-size:12px}
.sc-health [data-state=current] {color:var(--status-ok)}.sc-health [data-state=unavailable] {color:var(--status-attention)}
.sc-detail {display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:18px;margin:24px 0}
.sc-detail div {min-width:0;overflow-wrap:anywhere}.sc-detail dt {font-size:12px;color:var(--text-secondary)}.sc-detail dd {margin:5px 0;line-height:1.5}
@media(min-width:2200px){.sc-controls .sc-search{flex:0 1 720px}.sc-controls{padding-right:max(0px,calc(100% - 1500px))}#sc-results{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}.sc-group{margin:0}}
@media(max-width:650px){.sc-row {grid-template-columns:minmax(0,1fr);gap:12px}.sc-controls label {flex:1;min-width:140px}.sc-counts {gap:6px}.sc-counts button {font:inherit;color:inherit;text-align:left;cursor:pointer;padding:8px}.sc-group>summary span {display:block;margin:8px 0 0}}
</style>`;

export function renderServiceDetails(s) {
  const fields = [
    ['Origin',serviceScope(s) === 'herdr' ? 'Started in HerdR' : serviceScope(s) === 'system' ? 'System service' : 'Unassigned'],['Origin evidence',s.catalog?.scope_reason || 'No confirmed HerdR launch association'],
    ['Type',typeLabel(s)],['Purpose',s.catalog?.purpose || 'Not reported'],['Host',hostLabel(s)],
    ['Project',identity(s,'project_id') || 'No verified project'],['Repository',identity(s,'repository_id') || 'Not reported'],['Commit',identity(s,'commit_sha') || 'Not reported'],
    ['Workspace folder',reportedText(s.catalog?.project) || 'Not reported'],['Workspace evidence',s.catalog?.project_source || 'No workspace association reported'],
    ['Process',s.owner || 'Not reported'],...(!localOnly(s) ? [['Source port',s.catalog?.source_port ?? 'Not reported']] : []),[localOnly(s) ? 'Observed port' : 'Exposed port',s.port ?? 'Not reported'],
    ...(metadataObservation(s) ? [['Listener observation',({observed:'Observed',missing:'Missing from latest observation',unconfirmed:'Unconfirmed'})[metadataObservation(s).state] || 'Unconfirmed'],['Listener observation time',formatTimestamp(metadataObservation(s).observed_at)]] : []),
    ['Last report',formatTimestamp(s.lifecycle?.last_seen_at || s.updated_at)],['Unavailable since',formatTimestamp(s.lifecycle?.unavailable_since)],
    ['Registered',formatTimestamp(s.created_at)],['Service ID',s.service_id],
  ];
  return `${styles}<a data-service-back href="${SCOPE_PAGE[{herdr:'herdr',system:'system'}[serviceScope(s)] || 'local'].path}">← ${SCOPE_PAGE[{herdr:'herdr',system:'system'}[serviceScope(s)] || 'local'].title}</a><h1>${esc(s.service_name || s.service_id)}</h1>
    <p class="sc-intro">${esc(s.description || s.catalog?.purpose || 'Description not reported.')}</p>
    <p><strong>${stateLabel(s)}</strong> · ${esc(serviceReason(s))} Last report <span data-service-age>${esc(lastSeen(s))}</span>.</p>
    ${s.lifecycle?.archived ? '<p class="sc-intro">Archived services are hidden from the current catalog. A healthy report restores this service automatically; its history is retained.</p>' : ''}
    <dl class="sc-detail">${fields.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
    <p>${endpoint(s)}</p>${reportedText(s.session_id) ? `<p><a data-owning-session href="#/herd/session/${esc(encodeURIComponent(s.session_id))}">Owning agent →</a></p>` : ''}`;
}

// Keep an open detail page honest even when the provider stops updating.
export function mountServiceDetails(root, client, initial) {
  let service=initial, disposed=false, controller=null, signature='', error='';
  function draw() {
    if(disposed)return;
    const next=JSON.stringify([service,serviceState(service)]);
    if(next!==signature) {
      signature=next;
      const focused=document.activeElement;
      const href=root.contains(focused) ? focused.getAttribute('href') : null;
      const restore=root.contains(focused);
      const scroll={x:window.scrollX,y:window.scrollY};
      root.innerHTML=renderServiceDetails(service)+'<p class="sc-meta" data-service-refresh role="status"></p>';
      if(restore) {
        const target=[...root.querySelectorAll('a')].find(anchor=>href && anchor.getAttribute('href')===href) || root.querySelector('[data-service-back]');
        target?.focus({preventScroll:true});window.scrollTo(scroll.x,scroll.y);
      }
    }
    root.querySelector('[data-service-age]').textContent=lastSeen(service);
    root.querySelector('[data-service-refresh]').textContent=error;
  }
  async function refresh() {
    if(disposed||controller||document.hidden)return;
    const request=new AbortController();controller=request;
    const timeout=setTimeout(()=>request.abort(),12000);
    try {
      const data=await client.listServices({signal:request.signal});
      if(disposed||request.signal.aborted)return;
      const found=Array.isArray(data?.services) && data.services.find(item=>item?.service_id===service.service_id);
      if(!found)throw new Error('Service report unavailable');
      service=found;error='';
    } catch {
      if(!disposed)error='Could not refresh this service. Showing its last received report.';
    } finally {clearTimeout(timeout);controller=null;if(!disposed)draw();}
  }
  function visibility(){if(document.hidden)controller?.abort();else{draw();refresh();}}
  const clock=setInterval(()=>{if(!document.hidden)draw();},1000);
  const poll=setInterval(refresh,30000);
  document.addEventListener('visibilitychange',visibility);
  draw();
  return {refresh,dispose(){disposed=true;controller?.abort();clearInterval(clock);clearInterval(poll);document.removeEventListener('visibilitychange',visibility);}};
}

export function mountServices(root, client, initial, {showAll = false, status, scope = 'herdr'} = {}) {
  let services = initial;
  let disposed = false;
  let pending = false;
  const state = {scope:SCOPE_PAGE[scope] ? scope : 'herdr',search:'',host:'',status:['current','unavailable','unknown','all','archived'].includes(status)?status:showAll?'all':'current',archived:status==='archived',group:SCOPE_PAGE[scope] && scope!=='herdr'?'type':'project'};
  const openGroups = new Map();
  root.innerHTML = `${styles}<h1>${SCOPE_PAGE[state.scope].title}</h1>
    <p class="sc-intro">${SCOPE_INTRO[state.scope]}</p>
    <p class="sc-meta">${Object.entries(SCOPE_PAGE).filter(([key])=>key!==state.scope).map(([,page])=>`<a href="${page.path}">${page.label} →</a>`).join(' · ')}</p>
    <div class="sc-counts" id="sc-counts"></div><details class="sc-history" id="sc-history"><summary id="sc-history-summary">History and unconfirmed reports</summary><div class="sc-counts" id="sc-history-counts"></div></details>
    <p class="sc-meta" id="sc-policy"></p>
    <div class="sc-controls">
      <label class="sc-search">Search services<input id="sc-search" type="search" placeholder="Name, repository, project, commit or port"></label>
      <label>Host<select id="sc-host"><option value="">All hosts</option></select></label>
      <label>Status<select id="sc-status"><option value="current">Reported healthy</option><option value="unavailable">Unavailable</option><option value="unknown">Unconfirmed</option><option value="archived">Archived</option><option value="all">All statuses</option></select></label>
      <label>Group by<select id="sc-group"><option value="project">Project / repository</option><option value="host">Host</option><option value="type">Service type</option></select></label>
      <label class="sc-check"><input type="checkbox" id="sc-archived">Include archived</label>
    </div>
    <div class="sc-toolbar"><span id="sc-result" role="status"></span><div><button class="hd-btn" id="sc-expand">Expand groups</button> <button class="hd-btn" id="sc-collapse">Collapse groups</button></div></div>
    <p id="sc-error" role="status" class="sc-meta"></p><div id="sc-results"></div>`;
  const el = id => root.querySelector(`#sc-${id}`);
  el('status').value = state.status; el('archived').checked = state.archived; el('group').value = state.group;
  function draw() {
    const focused=document.activeElement;
    const focusedRow=root.contains(focused) ? focused.closest('[data-service-id]') : null;
    const focusKey=root.contains(focused) ? {
      service:focusedRow?.dataset.serviceId,
      rowIndex:[...root.querySelectorAll('[data-service-id]')].indexOf(focusedRow),
      href:focused.getAttribute('href'),
      group:focused.closest('details[data-group]')?.dataset.group,
      summary:focused.tagName==='SUMMARY',
      filter:focused.closest('[data-filter]')?.dataset.filter,
    } : null;
    const scroll={x:window.scrollX,y:window.scrollY};
    root.querySelectorAll('.sc-group').forEach(d=>openGroups.set(d.dataset.group,d.open));
    const scoped = services.filter(service=>inScope(service,state.scope));
    const counts = {current:0,unavailable:0,archived:0,unknown:0};
    scoped.forEach(s=>counts[serviceState(s)]++);
    const countButton = (key,label) => `<button type="button" data-filter="${key}" aria-pressed="${state.status===key}"><strong>${counts[key]}</strong>${label}</button>`;
    el('counts').innerHTML = countButton('current','Reported healthy');
    el('history-counts').innerHTML = [['unavailable','Unavailable'],['unknown','Unconfirmed'],['archived','Archived']].map(([key,label])=>countButton(key,label)).join('');
    el('history-summary').textContent = `${scoped.length-counts.current} history and unconfirmed reports`;
    if(state.status!=='current')el('history').open=true;
    el('policy').textContent = 'Current requires a recent healthy report and, when available, a fresh listener observation. Stopped or unconfirmed services leave this view automatically; history remains available below.';
    const hosts = [...new Set(scoped.map(hostLabel))].sort();
    el('host').innerHTML = '<option value="">All hosts</option>'+hosts.map(h=>`<option value="${esc(h)}">${esc(h)}</option>`).join('');
    el('host').value=state.host;
    const selected=selectServices(services,state);
    const groups=groupServices(selected,state.group);
    el('result').textContent=`${selected.length} matching services · ${groups.length} groups`;
    el('results').innerHTML=groups.length ? groups.map(g=>{
      const key=g.key;
      const open=openGroups.has(key)?openGroups.get(key):groups.length<=3&&selected.length<=12;
      const summary=[...new Set(g.services.map(stateLabel))].map(label=>`${g.services.filter(s=>stateLabel(s)===label).length} ${label.toLowerCase()}`).join(' · ');
      return `<details class="sc-group" data-group="${esc(key)}" ${open?'open':''}><summary>${esc(g.label)}<span>${g.services.length} services · ${esc(summary)}</span></summary>${g.services.map(serviceRow).join('')}</details>`;
    }).join(''):`<herdr-empty message="${!scoped.length ? state.scope==='herdr' ? 'No services have a confirmed HerdR launch association yet. Discovered listeners are under Local services.' : state.scope==='system' ? 'No services have been confirmed as system units.' : 'No local services have been reported.' : 'No services match these filters. Choose a status count above, select All statuses, or clear your search to explore the catalog.'}"></herdr-empty>`;
    root.querySelectorAll('.sc-group').forEach(d=>d.addEventListener('toggle',()=>{if(d.isConnected)openGroups.set(d.dataset.group,d.open);}));
    if(focusKey && !focused.isConnected) {
      let target;
      if(focusKey.service) {
        const rows=[...root.querySelectorAll('[data-service-id]')];
        const row=rows.find(node=>node.dataset.serviceId===focusKey.service)
          || rows[Math.min(Math.max(focusKey.rowIndex,0),rows.length-1)];
        target=[...(row?.querySelectorAll('a') || [])].find(anchor=>anchor.getAttribute('href')===focusKey.href) || row?.querySelector('.sc-name');
      } else if(focusKey.filter) target=[...root.querySelectorAll('[data-filter]')].find(button=>button.dataset.filter===focusKey.filter);
      else if(focusKey.summary) target=[...root.querySelectorAll('.sc-group')].find(group=>group.dataset.group===focusKey.group)?.querySelector('summary');
      target ||= el('search');
      const group=target.closest('details');
      if(group && !group.open && !focusKey.summary)target=group.querySelector('summary');
      target.focus({preventScroll:true});window.scrollTo(scroll.x,scroll.y);
    }
  }
  function chooseStatus(event) {
    const key=event.target.closest('button[data-filter]')?.dataset.filter;
    if(!key)return;
    state.status=key;
    state.archived=key==='archived';
    el('status').value=state.status;el('archived').checked=state.archived;
    draw();
  }
  root.addEventListener('click',chooseStatus);
  for(const name of ['search','host','status','group','archived']) {
    el(name).addEventListener(name==='search'?'input':'change',()=>{
      state[name]=name==='archived'?el(name).checked:el(name).value;
      if(name==='archived'&&state.archived){state.status='all';el('status').value='all';}
      if(name==='status'&&state.status==='archived'){state.archived=true;el('archived').checked=true;}
      if(name==='archived'&&!state.archived&&state.status==='archived'){state.status='current';el('status').value='current';}
      draw();
    });
  }
  for(const [name,open] of [['expand',true],['collapse',false]]) el(name).addEventListener('click',()=>{
    root.querySelectorAll('.sc-group').forEach(d=>{openGroups.set(d.dataset.group,open);d.open=open;});
  });
  async function refresh() {
    if(disposed||pending||document.hidden)return;
    pending=true;
    try {
      const data=await client.listServices();
      if(disposed)return;
      services=data.services;
      el('error').textContent='';
      // Capture before replacement, including toggles whose queued event has not fired.
      root.querySelectorAll('.sc-group').forEach(d=>openGroups.set(d.dataset.group,d.open));
      draw();
    } catch { if(!disposed)el('error').textContent='Could not refresh services. Showing the last received catalog.'; }
    finally {pending=false;}
  }
  draw();
  const timer=setInterval(refresh,30000);
  let classification=services.map(s=>serviceState(s)).join(',');
  const clock=setInterval(()=>{
    if(disposed||document.hidden)return;
    const next=services.map(s=>serviceState(s)).join(',');
    if(next!==classification){classification=next;draw();}
  },1000);
  return {refresh,dispose(){disposed=true;clearInterval(timer);clearInterval(clock);root.removeEventListener('click',chooseStatus);}};
}
