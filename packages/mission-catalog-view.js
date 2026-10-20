// A compact, read-only mission catalog. Stale telemetry is never current work.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { freshnessAt, missionHealth } from './mission-live-view.js';
import { recordOutcome, statusOutcome } from './record-state.js';

const text = value => typeof value === 'string' ? value : '';
const records = value => Array.isArray(value) ? value.filter(v => v && typeof v === 'object' && !Array.isArray(v)) : [];
const short = (value, max) => text(value).replace(/\s+/g, ' ').trim().slice(0, max);
const human = value => text(value).replaceAll('_', ' ').toLowerCase();
const isTerminal = entry => entry.outcome === 'failed' || entry.outcome === 'finished';
const TABS = [['current', 'Current'], ['failed', 'Failed'], ['finished', 'Finished']];
const duration = seconds => {
  if (!Number.isFinite(seconds)) return 'unknown';
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m`;
};
const age = (stamp, now) => duration((now - Date.parse(stamp)) / 1000);
const GROUPS = [
  ['attention', 'Needs attention', 'Current reports of stalls, blockers or pauses.'],
  ['running', 'Running', 'Missions with current telemetry.'],
  ['lastknown', 'Updating / last known', 'Current details unavailable. These are not counted as running or needing attention.'],
  ['recorded', 'Recorded / no live telemetry', 'Open or unknown records without live telemetry. Their current activity is unconfirmed.'],
  ['failed', 'Failed', 'Missions that reported a failed outcome.'],
  ['finished', 'Finished', 'Completed, closed, stopped and canceled missions.'],
];
const currentGroup = key => key !== 'failed' && key !== 'finished';

export function measuredProgress(activity = {}) {
  const c = activity.acceptanceCoverage;
  const valid = v => v && Number.isFinite(v.completed) && Number.isFinite(v.total) && v.total > 0 && v.completed >= 0 && v.completed <= v.total;
  if (valid(c)) return {percent: Math.floor(c.completed / c.total * 100), label: `${c.completed} / ${c.total} acceptance criteria verified`};
  const w = activity.workflowProgress;
  if (w?.basis === 'weighted_dag' && valid(w)) return {percent: Math.floor(w.completed / w.total * 100), label: 'Workflow progress · weighted tasks'};
  return null;
}

export function missionEntries(projections, recorded, received, now = Date.now()) {
  const byKey = new Map();
  for (const record of records(recorded)) {
    const key = text(record.mission_key) || (record.source && record.source !== 'herdr' ? `${record.source}:${record.mission_id}` : text(record.mission_id));
    if (key) byKey.set(key, {key, recorded: record});
  }
  for (const projection of records(projections)) {
    const key = text(projection.mission_key);
    if (key) byKey.set(key, {...byKey.get(key), key, projection});
  }
  return [...byKey.values()].map(entry => {
    const p = entry.projection, record = entry.recorded || {}, mission = p?.mission || {}, a = p?.activity || {};
    const title = text(mission.title) || text(record.title) || text(mission.id) || entry.key;
    const lifecycle = p ? mission : record;
    const outcome = recordOutcome(lifecycle);
    const stageSuppliesOutcome = (outcome === 'failed' || outcome === 'finished') && statusOutcome(lifecycle.status) !== outcome;
    const status = text(stageSuppliesOutcome ? lifecycle.stage : lifecycle.status || lifecycle.stage).toUpperCase();
    const freshness = p ? freshnessAt(p, received, now) : null;
    const health = p ? missionHealth(a, mission) : human(status) || 'unknown';
    const tab = outcome === 'unknown' ? 'current' : outcome;
    let group = tab === 'current' ? 'recorded' : tab;
    if (p && tab === 'current') {
      if (freshness.state !== 'live' || health === 'unknown') group = 'lastknown';
      else group = ['failed', 'stalled', 'blocked', 'slow'].includes(health) || health.startsWith('paused') ? 'attention' : 'running';
    }
    return {...entry, title, status, health, freshness, outcome, tab, group, host: text(p?.host_id || record.host_id),
      search: [title, entry.key, mission.id, a.currentObjective, a.currentActivity?.summary, a.waitingReason,
        record.purpose, record.cwd, p?.host_id, status, health].filter(Boolean).join(' ').toLocaleLowerCase()};
  }).sort((a,b) => a.title.localeCompare(b.title) || a.key.localeCompare(b.key));
}

export function filterMissions(entries, {search = '', host = '', status = 'all', tab = 'all'} = {}) {
  const terms = search.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  return entries.filter(e => (tab === 'all' || e.tab === tab) && (!host || e.host === host) && (status === 'all' || e.group === status) && terms.every(t => e.search.includes(t)));
}

function freshnessLabel(entry) {
  const f = entry.freshness;
  if (isTerminal(entry)) {
    const label = human(entry.status);
    return `${label ? label[0].toUpperCase() + label.slice(1) : entry.outcome} · ${f ? 'last report retained' : 'recorded'}`;
  }
  if (!f) return `Recorded · ${human(entry.status) || 'state not reported'}`;
  if (f.state === 'live') return `Live telemetry · ${entry.health}`;
  if (f.state === 'updating') return 'Updating telemetry · current details unavailable';
  return `${f.state === 'stale' ? 'Stale' : 'Disconnected'} telemetry · current state unknown`;
}

export function compactMissionRow(entry, now = Date.now()) {
  const p = entry.projection, record = entry.recorded || {}, a = p?.activity || {};
  const current = entry.freshness?.state === 'live' && !isTerminal(entry);
  const workers = records(a.workerDetails);
  const active = workers.filter(w => !w.endedAt && ['running', 'waiting', 'stalled', 'blocked'].includes(w.state)).length;
  const finished = workers.filter(w => w.endedAt || ['complete','completed','done','failed','canceled','cancelled'].includes(text(w.state).toLowerCase())).length;
  const other = Math.max(0, workers.length - active - finished);
  const workerCounts = `${active} ${current && !isTerminal(entry) ? 'current' : 'active at last report'} · ${finished} finished${other ? ` · ${other} other` : ''} workers`;
  const progress = measuredProgress(a);
  const waitAt = Date.parse(a.waitingSince);
  const waitReference = current ? now : Date.parse(p?.activity_snapshot_at || p?.snapshot_at);
  const activity = text(a.currentActivity?.summary) || text(record.purpose) || 'Activity not reported';
  const title = short(entry.title, 140);
  return `<article class="mc-row" data-mission-key="${esc(entry.key)}">
    <div class="mc-main"><a class="mc-title" href="#/missions/${esc(encodeURIComponent(entry.key))}">${esc(title)}${entry.title.length > 140 ? '…' : ''}</a>
      <p class="mc-activity"><span>${p ? current ? 'Activity' : 'Last known activity' : 'Recorded scope'}:</span> ${esc(short(activity, 180))}${activity.length > 180 ? '…' : ''}</p>
      ${a.waitingReason ? `<p class="mc-wait">Waiting: ${esc(short(human(a.waitingReason), 100))}${Number.isFinite(waitAt) ? ` · <span data-wait-age>${duration((waitReference - waitAt) / 1000)}</span>${current ? '' : ' at last snapshot'}` : ''}</p>` : ''}
      <div class="mc-meta">${esc(entry.host || (p ? 'Host not reported' : record.source || 'HerdR'))}${workers.length ? ` · ${workerCounts}` : ''}${a.lastOutputAt ? ` · Last output <span data-output-age>${age(a.lastOutputAt, now)}</span> ago` : ''}</div>
    </div>
    <div class="mc-status"><span data-freshness data-state="${esc(entry.freshness?.state || 'recorded')}">${esc(freshnessLabel(entry))}</span>
      ${p ? `<span class="mc-meta">Report age <span data-report-age>${duration(entry.freshness.age)}</span></span>` : `<span class="mc-meta">${record.updated_at ? `Last recorded change <span data-recorded-age>${esc(age(record.updated_at, now))}</span> ago` : 'No live telemetry'}</span>`}
      ${progress ? `<label class="mc-progress">${esc(progress.label)} · ${progress.percent}%<progress aria-label="${esc(progress.label)}" max="100" value="${progress.percent}"></progress></label>` : '<span class="mc-meta">Progress not measured</span>'}
    </div>
  </article>`;
}

const styles = `<style>
.mc-counts[hidden],.mc-controls label[hidden]{display:none}
.mc-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0;border-bottom:1px solid var(--border)}.mc-tabs button{font:inherit;background:none;border:0;border-bottom:3px solid transparent;color:var(--text-secondary);padding:12px;cursor:pointer}.mc-tabs button[aria-selected=true]{color:var(--text);border-bottom-color:var(--accent)}.mc-tabs span{font-size:12px;margin-left:5px}.mc-tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.mc-intro{color:var(--text-secondary);line-height:1.5;max-width:80ch}.mc-counts{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}
.mc-counts button{font:inherit;font-size:13px;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 14px;cursor:pointer}.mc-counts strong{font-size:20px;margin-right:6px}.mc-counts button[aria-pressed=true]{border-color:var(--accent)}
.mc-controls{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}.mc-controls label{display:flex;flex-direction:column;gap:5px;font-size:13px}.mc-search{flex:1;min-width:180px}.mc-controls input,.mc-controls select{padding:9px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit;color:var(--text);background:var(--surface);max-width:100%}
.mc-group{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin:14px 0;overflow:hidden}.mc-group>summary{padding:15px;cursor:pointer;font-weight:600}.mc-group>summary span{font-size:12px;font-weight:400;color:var(--text-secondary);margin-left:8px}.mc-group[data-group=attention]>summary{color:var(--status-attention)}
.mc-note{color:var(--text-secondary);font-size:12px;padding:0 15px 12px;margin:0}.mc-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(180px,280px);gap:16px;padding:14px 16px;border-top:1px solid var(--border-subtle);overflow-wrap:anywhere}.mc-main{min-width:0}
.mc-title{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:14px;font-weight:600;line-height:1.5}.mc-activity{font-size:13px;line-height:1.5;margin:6px 0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.mc-activity>span{color:var(--text-secondary)}.mc-wait{font-size:12px;margin:6px 0;color:var(--status-attention)}
.mc-meta{font-size:12px;color:var(--text-secondary);line-height:1.5}.mc-status{display:flex;flex-direction:column;gap:5px;font-size:12px;line-height:1.5}.mc-status [data-state=live]{color:var(--status-active)}.mc-progress{font-size:12px}.mc-progress progress{display:block;width:100%;height:7px;accent-color:var(--accent);margin-top:5px}.mc-more{padding:10px 16px;border-top:1px solid var(--border-subtle)}
@media(max-width:650px){.mc-row{grid-template-columns:minmax(0,1fr);gap:8px}.mc-controls label{min-width:130px;flex:1}.mc-status{gap:3px}.mc-counts button{padding:8px}.mc-group>summary span{margin-left:4px}}
</style>`;

export function mountMissionCatalog(container, client, {tab = 'current'} = {}) {
  let disposed = false, projections = [], recorded = [], received = Date.now();
  let liveController = null, pollTimer = null, clockTimer = null, recordedTimer = null, recordedController = null;
  let failures = 0, liveError = '', recordedError = '', lastSignature = '', lastClassification = '', liveLoaded = false, recordedLoaded = false;
  const state = {search:'',host:'',status:'all',tab:TABS.some(([key])=>key===tab)?tab:'current'};
  const opened = new Map(), limits = new Map();
  container.innerHTML = `${styles}<p class="mc-intro">See what needs attention and what is running. Use Failed and Finished to inspect terminal missions; current records keep their telemetry quality visible.</p>
    <div class="mc-tabs" id="mc-tabs" role="tablist" aria-label="Mission outcome"></div><div class="mc-counts" id="mc-counts"></div><div class="mc-controls">
      <label class="mc-search">Search missions<input id="mc-search" type="search" placeholder="Title, objective, activity or mission ID"></label>
      <label>Host<select id="mc-host"><option value="">All hosts</option></select></label>
      <label>Show<select id="mc-status"><option value="all">All groups</option>${GROUPS.filter(([key])=>currentGroup(key)).map(([key,label])=>`<option value="${key}">${label}</option>`).join('')}</select></label>
    </div><p id="mc-result" class="mc-meta" role="status">Loading missions…</p><p class="ml-connection mc-meta" role="status"></p><div id="mc-groups" role="tabpanel" aria-labelledby="mc-tab-current"></div>`;
  const el = name => container.querySelector(`#mc-${name}`);
  const connection = container.querySelector('.ml-connection');
  const entries = () => missionEntries(projections, recorded, received);
  function capture() {
    el('groups').querySelectorAll('details[data-group]').forEach(d=>opened.set(d.dataset.group,d.open));
  }
  function draw(force = false) {
    if(disposed)return;
    const all=entries(), selected=filterMissions(all,state);
    const counts=Object.fromEntries(GROUPS.map(([key])=>[key,all.filter(e=>e.group===key).length]));
    const signature=JSON.stringify([state,liveLoaded,recordedLoaded,all.map(e=>[e.key,e.group,e.freshness?.state,e.projection,e.recorded])]);
    connection.textContent=[liveError,recordedError].filter(Boolean).join(' ');
    if(!force && signature===lastSignature)return;
    lastSignature=signature;
    capture();
    const focused=document.activeElement;
    const focusKey=container.contains(focused) ? {mission:focused.closest('[data-mission-key]')?.dataset.missionKey,group:focused.closest('details[data-group]')?.dataset.group,more:focused.dataset.more,tab:focused.closest('[data-tab]')?.dataset.tab,filter:focused.closest('[data-filter]')?.dataset.filter,isSummary:focused.tagName==='SUMMARY'} : null;
    const scroll={x:window.scrollX,y:window.scrollY};
    const countLabel=n=>liveLoaded&&recordedLoaded?String(n):liveLoaded||recordedLoaded?`${n}+`:'…';
    el('tabs').innerHTML=TABS.map(([key,label])=>`<button type="button" role="tab" id="mc-tab-${key}" aria-controls="mc-groups" aria-selected="${state.tab===key}" tabindex="${state.tab===key?0:-1}" data-tab="${key}">${label} <span>${countLabel(all.filter(e=>e.tab===key).length)}</span></button>`).join('');
    el('groups').setAttribute('aria-labelledby',`mc-tab-${state.tab}`);
    el('counts').hidden=state.tab!=='current';
    el('status').closest('label').hidden=state.tab!=='current';
    el('counts').innerHTML=GROUPS.filter(([key])=>currentGroup(key)).map(([key,label])=>`<button type="button" data-filter="${key}" aria-pressed="${state.status===key}"><strong>${countLabel(counts[key])}</strong>${label}</button>`).join('');
    const hosts=[...new Set(all.map(e=>e.host).filter(Boolean))].sort();
    el('host').innerHTML='<option value="">All hosts</option>'+hosts.map(h=>`<option value="${esc(h)}">${esc(h)}</option>`).join('');el('host').value=state.host;
    el('result').textContent=liveLoaded||recordedLoaded?`${selected.length} of ${all.length} received missions${liveLoaded&&recordedLoaded?'':' · partial results (+): waiting for remaining source'} · live activity refreshes every 5 seconds`:'Loading missions…';
    el('groups').innerHTML=GROUPS.filter(([key])=>(state.tab==='current'?currentGroup(key):state.tab===key)&&(state.status==='all'||state.status===key)).map(([key,label,note])=>{
      const rows=selected.filter(e=>e.group===key), limit=limits.get(key)||10;
      const open=opened.has(key)?opened.get(key):Boolean(state.tab!=='current'||state.search || state.status!=='all'||key==='attention'||key==='running');
      return `<details class="mc-group" data-group="${key}" ${open?'open':''}><summary>${label}<span>${rows.length}</span></summary><p class="mc-note">${note}</p>
        ${rows.length?rows.slice(0,limit).map(e=>compactMissionRow(e)).join(''):`<p class="mc-note">${liveLoaded&&recordedLoaded?'No missions in this group match your filters.':'Loading reports…'}</p>`}
        ${rows.length>limit?`<div class="mc-more"><button class="hd-btn" data-more="${key}">Show ${Math.min(10,rows.length-limit)} more (${rows.length-limit} remaining)</button></div>`:''}</details>`;
    }).join('');
    el('groups').querySelectorAll('details').forEach(d=>d.addEventListener('toggle',()=>{if(d.isConnected)opened.set(d.dataset.group,d.open);}));
    if(focusKey){
      let target;
      if(focusKey.mission)target=[...el('groups').querySelectorAll('[data-mission-key]')].find(row=>row.dataset.missionKey===focusKey.mission)?.querySelector('.mc-title');
      else if(focusKey.more)target=el('groups').querySelector(`[data-more="${focusKey.more}"]`) || el('groups').querySelector(`[data-group="${focusKey.more}"] > summary`);
      else if(focusKey.isSummary)target=el('groups').querySelector(`[data-group="${focusKey.group}"] > summary`);
      else if(focusKey.tab)target=el('tabs').querySelector(`[data-tab="${focusKey.tab}"]`);
      else if(focusKey.filter)target=el('counts').querySelector(`[data-filter="${focusKey.filter}"]`);
      if(target){const group=target.closest('details');if(group&&!focusKey.isSummary){group.open=true;opened.set(group.dataset.group,true);}target.focus({preventScroll:true});window.scrollTo(scroll.x,scroll.y);}
    }
  }
  function tick() {
    if(disposed||document.hidden)return;
    const all=entries(), byKey=new Map(all.map(e=>[e.key,e]));
    // Reclassify once when evidence ages out; never leave an old stall counted current.
    const classification=JSON.stringify(all.map(e=>[e.key,e.group,e.freshness?.state]));
    if(lastClassification!==classification){lastClassification=classification;draw();}
    const now=Date.now();
    el('groups').querySelectorAll('[data-mission-key]').forEach(row=>{
      const e=byKey.get(row.dataset.missionKey);if(!e)return;
      const f=row.querySelector('[data-freshness]');f.textContent=freshnessLabel(e);f.dataset.state=e.freshness?.state||'recorded';
      const report=row.querySelector('[data-report-age]');if(report)report.textContent=duration(e.freshness.age);
      const recordedAge=row.querySelector('[data-recorded-age]');if(recordedAge)recordedAge.textContent=age(e.recorded.updated_at,now);
      const a=e.projection?.activity||{}, output=row.querySelector('[data-output-age]');if(output)output.textContent=age(a.lastOutputAt,now);
      const wait=row.querySelector('[data-wait-age]');if(wait){const reference=e.freshness?.state==='live'&&!isTerminal(e)?now:Date.parse(e.projection.activity_snapshot_at||e.projection.snapshot_at);wait.textContent=duration((reference-Date.parse(a.waitingSince))/1000);}
    });
    connection.textContent=[liveError,recordedError].filter(Boolean).join(' ');
  }
  function changeFilters() {
    state.search=el('search').value;state.host=el('host').value;state.status=el('status').value;
    capture();
    for(const [key]of GROUPS)opened.set(key,Boolean(state.tab!=='current'||state.search||state.status!=='all'||key==='attention'||key==='running'));
    limits.clear();
    // draw captures rendered disclosures; remove them after recording the new defaults.
    el('groups').innerHTML='';draw(true);tick();
  }
  function selectTab(key) {
    if(!TABS.some(([tab])=>tab===key))return;
    state.tab=key;el('status').value='all';changeFilters();
    el('tabs').querySelector(`[data-tab="${key}"]`)?.focus({preventScroll:true});
  }
  el('tabs').addEventListener('click',event=>selectTab(event.target.closest('[data-tab]')?.dataset.tab));
  el('tabs').addEventListener('keydown',event=>{
    const index=TABS.findIndex(([key])=>key===event.target.dataset.tab);
    if(index<0)return;
    const next=event.key==='ArrowRight'?(index+1)%TABS.length:event.key==='ArrowLeft'?(index+TABS.length-1)%TABS.length:event.key==='Home'?0:event.key==='End'?TABS.length-1:-1;
    if(next>=0){event.preventDefault();selectTab(TABS[next][0]);}
  });
  el('search').addEventListener('input',changeFilters);el('host').addEventListener('change',changeFilters);el('status').addEventListener('change',changeFilters);
  el('counts').addEventListener('click',event=>{const key=event.target.closest('[data-filter]')?.dataset.filter;if(key){el('status').value=key;changeFilters();}});
  el('groups').addEventListener('click',event=>{const key=event.target.closest('[data-more]')?.dataset.more;if(key){limits.set(key,(limits.get(key)||10)+10);draw(true);tick();}});
  async function refreshRecorded() {
    if(disposed||document.hidden||recordedController)return;
    const controller=new AbortController();recordedController=controller;let timedOut=false;
    const deadline=setTimeout(()=>{timedOut=true;controller.abort();},15000);
    try {
      const response=await client.listMissions(undefined,{signal:controller.signal});
      if(disposed||controller.signal.aborted)return;
      recorded=records(response.missions);recordedLoaded=true;recordedError='';draw();tick();
    } catch {
      if(!disposed&&(!controller.signal.aborted||timedOut)){recordedError='Recorded history could not refresh; keeping the last received records.';tick();}
    } finally {clearTimeout(deadline);if(recordedController===controller)recordedController=null;}
  }
  async function poll() {
    if(disposed||document.hidden||liveController)return;
    const controller=new AbortController();liveController=controller;let timedOut=false;
    const deadline=setTimeout(()=>{timedOut=true;controller.abort();},15000);
    try {
      const response=await client.listMissionActivity({signal:controller.signal});
      if(disposed||controller.signal.aborted)return;
      projections=records(response.activities);received=Date.now();liveLoaded=true;failures=0;liveError='';draw();tick();
    } catch {
      if(disposed||(controller.signal.aborted&&!timedOut))return;
      failures++;liveError='Activity connection interrupted · showing last known data; retrying automatically.';tick();
    } finally {
      clearTimeout(deadline);liveController=null;
      if(!disposed&&!document.hidden)pollTimer=setTimeout(poll,Math.min(30000,5000*2**Math.min(failures,3)));
    }
  }
  const visibility=()=>{
    clearTimeout(pollTimer);clearInterval(clockTimer);clearInterval(recordedTimer);
    if(document.hidden){liveController?.abort();recordedController?.abort();return;}
    tick();poll();refreshRecorded();clockTimer=setInterval(tick,1000);recordedTimer=setInterval(refreshRecorded,30000);
  };
  document.addEventListener('visibilitychange',visibility);draw();visibility();
  return {refreshRecorded,dispose(){disposed=true;liveController?.abort();recordedController?.abort();clearTimeout(pollTimer);clearInterval(clockTimer);clearInterval(recordedTimer);document.removeEventListener('visibilitychange',visibility);}};
}
