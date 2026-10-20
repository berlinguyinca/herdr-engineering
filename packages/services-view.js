// Services are observations, not proof that an old process is still running.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatTimestamp, formatDuration } from './herdr-web-components/format.js';

export function serviceState(s) {
  if (s.lifecycle?.archived) return 'archived';
  if (['current', 'unavailable', 'archived', 'unknown'].includes(s.lifecycle?.state)) return s.lifecycle.state;
  if (['healthy', 'running', 'active', 'available'].includes(s.status)) return 'current';
  if (['unavailable', 'stopped', 'dead', 'failed'].includes(s.status)) return 'unavailable';
  return 'unknown';
}
const stateLabel = s => ({current:'Reported healthy', unavailable:'Unavailable', archived:'Archived', unknown:'Unknown'})[serviceState(s)];
const reportedText = value => typeof value === 'string' && value.trim() ? value.trim() : null;
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

export function selectServices(services, {search = '', host = '', status = 'current', archived = false} = {}) {
  const terms = search.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  return services.filter(s => {
    const state = serviceState(s);
    if (state === 'archived' && !archived) return false;
    if (status !== 'all' && state !== status) return false;
    if (host && hostLabel(s) !== host) return false;
    const text = [s.service_name,s.description,s.owner,s.host_id,s.port,s.service_id,s.catalog?.source_port,
      s.catalog?.project,s.catalog?.label,s.catalog?.purpose].filter(x => x != null).join(' ').toLocaleLowerCase();
    return terms.every(term => text.includes(term));
  }).sort((a,b) => String(a.service_name || '').localeCompare(String(b.service_name || '')) || String(a.service_id).localeCompare(String(b.service_id)));
}

export function groupServices(services, by = 'project') {
  const groups = new Map();
  for (const service of services) {
    const label = by === 'host' ? hostLabel(service) : by === 'type' ? typeLabel(service)
      : reportedText(service.catalog?.project) || `No project reported · ${typeLabel(service)}`;
    if (!groups.has(label)) groups.set(label, {label, services:[]});
    groups.get(label).services.push(service);
  }
  return [...groups.values()].sort((a,b) => a.label.localeCompare(b.label));
}

function lastSeen(s) {
  const date = s.lifecycle?.last_seen_at || s.updated_at;
  const age = date ? Date.now() - Date.parse(date) : NaN;
  return Number.isFinite(age) ? `${formatDuration(Math.max(0,age))} ago` : 'Not reported';
}
function endpoint(s) {
  const url = safeServiceUrl(s.url);
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open endpoint ↗</a>` : '<span class="muted">No web endpoint</span>';
}
function serviceRow(s) {
  return `<article class="sc-row" data-service-id="${esc(s.service_id)}">
    <div class="sc-identity"><a class="sc-name" href="${esc(serviceLink(s))}">${esc(s.service_name || s.service_id)}</a>
      <p>${esc(s.catalog?.purpose || s.description || 'Purpose not reported.')}</p>
      ${s.description && s.catalog?.purpose && s.description !== s.catalog.purpose ? `<p class="sc-meta">${esc(s.description)}</p>` : ''}
      <span class="sc-meta">${esc(typeLabel(s))}${s.owner ? ` · Process: ${esc(s.owner)}` : ''}</span></div>
    <div class="sc-location"><a href="#/hosts/${esc(encodeURIComponent(hostLabel(s)))}">${esc(hostLabel(s))}</a>
      <span>${s.catalog?.source_port != null ? `Source :${esc(s.catalog.source_port)} · ` : ''}Exposed ${s.port != null ? `:${esc(s.port)}` : 'port unknown'}</span>${endpoint(s)}</div>
    <div class="sc-health"><strong data-state="${serviceState(s)}">${stateLabel(s)}</strong><span>Last report ${esc(lastSeen(s))}</span>
      <span class="sc-meta">${esc(s.lifecycle?.reason || 'Status from the latest service report.')}</span></div>
  </article>`;
}

const styles = `<style>
.sc-intro {color:var(--text-secondary); max-width:80ch; line-height:1.6}
.sc-counts {display:flex; flex-wrap:wrap; gap:12px; margin:20px 0}
.sc-counts button {font:inherit;color:inherit;text-align:left;cursor:pointer;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 14px;font-size:13px}
.sc-counts strong {font-size:20px;margin-right:5px}
.sc-controls {display:flex;flex-wrap:wrap;gap:12px;align-items:end;margin:20px 0}
.sc-controls label {display:flex;flex-direction:column;gap:5px;font-size:13px}
.sc-controls input[type=search],.sc-controls select {border:1px solid var(--border);border-radius:var(--radius-sm);padding:9px;background:var(--surface);color:var(--text);font:inherit;max-width:100%}
.sc-controls .sc-search {flex:1;min-width:190px}.sc-controls .sc-check {flex-direction:row;align-items:center;min-height:38px}
.sc-toolbar {display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin:15px 0;font-size:13px}
.sc-group {border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);margin:12px 0;overflow:hidden}
.sc-group>summary {cursor:pointer;padding:16px;font-weight:600;overflow-wrap:anywhere}
.sc-group>summary span {font-weight:400;font-size:12px;color:var(--text-secondary);margin-left:12px}
.sc-row {display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr) minmax(0,1.2fr);gap:20px;padding:16px;border-top:1px solid var(--border-subtle);font-size:13px;overflow-wrap:anywhere}
.sc-name {font-size:14px;font-weight:600}.sc-row p {margin:6px 0;line-height:1.5}.sc-meta {color:var(--text-secondary);font-size:12px}
.sc-location,.sc-health {display:flex;flex-direction:column;gap:7px}.sc-health strong {font-size:12px}
.sc-health [data-state=current] {color:var(--status-ok)}.sc-health [data-state=unavailable] {color:var(--status-attention)}
.sc-detail {display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:18px;margin:24px 0}
.sc-detail div {min-width:0;overflow-wrap:anywhere}.sc-detail dt {font-size:12px;color:var(--text-secondary)}.sc-detail dd {margin:5px 0;line-height:1.5}
@media(max-width:650px){.sc-row {grid-template-columns:minmax(0,1fr);gap:12px}.sc-controls label {flex:1;min-width:140px}.sc-counts {gap:6px}.sc-counts button {font:inherit;color:inherit;text-align:left;cursor:pointer;padding:8px}.sc-group>summary span {display:block;margin:8px 0 0}}
</style>`;

export function renderServiceDetails(s) {
  const fields = [
    ['Type',typeLabel(s)],['Purpose',s.catalog?.purpose || 'Not reported'],['Host',hostLabel(s)],
    ['Project',s.catalog?.project || 'Not reported'],['Project evidence',s.catalog?.project_source || 'No project association reported'],
    ['Process',s.owner || 'Not reported'],['Source port',s.catalog?.source_port ?? 'Not reported'],['Exposed port',s.port ?? 'Not reported'],
    ['Last report',formatTimestamp(s.lifecycle?.last_seen_at || s.updated_at)],['Unavailable since',formatTimestamp(s.lifecycle?.unavailable_since)],
    ['Registered',formatTimestamp(s.created_at)],['Service ID',s.service_id],
  ];
  return `${styles}<a href="#/services">← Services</a><h1>${esc(s.service_name || s.service_id)}</h1>
    <p class="sc-intro">${esc(s.description || s.catalog?.purpose || 'Description not reported.')}</p>
    <p><strong>${stateLabel(s)}</strong> · ${esc(s.lifecycle?.reason || 'Status from the latest service report.')} Last report ${esc(lastSeen(s))}.</p>
    ${s.lifecycle?.archived ? '<p class="sc-intro">Archived services are hidden from the current catalog. A healthy report restores this service automatically; its history is retained.</p>' : ''}
    <dl class="sc-detail">${fields.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>
    <p>${endpoint(s)}</p>`;
}

export function mountServices(root, client, initial, {showAll = false} = {}) {
  let services = initial;
  let disposed = false;
  let pending = false;
  const state = {search:'',host:'',status:showAll?'all':'current',archived:false,group:'project'};
  const openGroups = new Map();
  root.innerHTML = `${styles}<h1>Services</h1>
    <p class="sc-intro">Explore what is running across the fleet. Services are grouped by reported project or service type; each entry shows its purpose, host and ports.</p>
    <div class="sc-counts" id="sc-counts"></div>
    <p class="sc-meta" id="sc-policy"></p>
    <div class="sc-controls">
      <label class="sc-search">Search services<input id="sc-search" type="search" placeholder="Name, purpose, project, process or port"></label>
      <label>Host<select id="sc-host"><option value="">All hosts</option></select></label>
      <label>Status<select id="sc-status"><option value="current">Reported healthy</option><option value="unavailable">Unavailable</option><option value="unknown">Unknown</option><option value="archived">Archived</option><option value="all">All statuses</option></select></label>
      <label>Group by<select id="sc-group"><option value="project">Project / service type</option><option value="host">Host</option><option value="type">Service type</option></select></label>
      <label class="sc-check"><input type="checkbox" id="sc-archived">Include archived</label>
    </div>
    <div class="sc-toolbar"><span id="sc-result" role="status"></span><div><button class="hd-btn" id="sc-expand">Expand groups</button> <button class="hd-btn" id="sc-collapse">Collapse groups</button></div></div>
    <p id="sc-error" role="status" class="sc-meta"></p><div id="sc-results"></div>`;
  const el = id => root.querySelector(`#sc-${id}`);
  el('status').value = state.status;
  function draw() {
    const counts = {current:0,unavailable:0,archived:0,unknown:0};
    services.forEach(s=>counts[serviceState(s)]++);
    el('counts').innerHTML = [['current','Reported healthy'],['unavailable','Unavailable'],['archived','Archived'],['unknown','Unknown']]
      .filter(([key])=>key!=='unknown'||counts[key]).map(([key,label])=>`<button type="button" data-filter="${key}" aria-pressed="${state.status===key}"><strong>${counts[key]}</strong>${label}</button>`).join('');
    const days = services.find(s=>s.lifecycle?.archive_after_days != null)?.lifecycle.archive_after_days;
    el('policy').textContent = days != null ? `Unavailable services automatically leave the current catalog after ${days} days. Archived records remain inspectable and return after a healthy report.`
      : 'Availability reflects the last report. A healthy report is not a live connection check.';
    const hosts = [...new Set(services.map(hostLabel))].sort();
    el('host').innerHTML = '<option value="">All hosts</option>'+hosts.map(h=>`<option value="${esc(h)}">${esc(h)}</option>`).join('');
    el('host').value=state.host;
    const selected=selectServices(services,state);
    const groups=groupServices(selected,state.group);
    el('result').textContent=`${selected.length} of ${services.length} services · ${groups.length} groups`;
    el('results').innerHTML=groups.length ? groups.map(g=>{
      const key=state.group+':'+g.label;
      const open=openGroups.has(key)?openGroups.get(key):groups.length<=3&&selected.length<=12;
      const summary=[...new Set(g.services.map(stateLabel))].map(label=>`${g.services.filter(s=>stateLabel(s)===label).length} ${label.toLowerCase()}`).join(' · ');
      return `<details class="sc-group" data-group="${esc(key)}" ${open?'open':''}><summary>${esc(g.label)}<span>${g.services.length} services · ${esc(summary)}</span></summary>${g.services.map(serviceRow).join('')}</details>`;
    }).join(''):'<herdr-empty message="No services match these filters. Choose a status count above, select All statuses, or clear your search to explore the catalog."></herdr-empty>';
    root.querySelectorAll('.sc-group').forEach(d=>d.addEventListener('toggle',()=>{if(d.isConnected)openGroups.set(d.dataset.group,d.open);}));
  }
  el('counts').addEventListener('click',event=>{
    const key=event.target.closest('button[data-filter]')?.dataset.filter;
    if(!key)return;
    state.status=key;
    state.archived=key==='archived';
    el('status').value=state.status;el('archived').checked=state.archived;
    draw();
  });
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
  return {refresh,dispose(){disposed=true;clearInterval(timer);}};
}
