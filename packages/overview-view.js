// Read-only overview: lifecycle records and current telemetry are separate evidence.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatDuration } from './herdr-web-components/format.js';
import { missionEntries } from './mission-catalog-view.js';
import { recordOutcome } from './record-state.js';
import { serviceState } from './services-view.js';

const records = value => Array.isArray(value) ? value.filter(v => v && typeof v === 'object' && !Array.isArray(v)) : [];
const text = value => typeof value === 'string' ? value : '';
const short = (value, limit = 120) => {
  const compact = text(value).replace(/\s+/g, ' ').trim();
  return compact.length > limit ? `${compact.slice(0, limit)}…` : compact;
};
const human = value => text(value).toLowerCase().replaceAll('_', ' ');
const link = (path, label) => `<a href="${esc(path)}">${esc(label)}</a>`;
const stamp = value => Date.parse(value) || 0;
const recent = (a,b) => stamp(b.updated_at || b.last_seen) - stamp(a.updated_at || a.last_seen);
const countByOutcome = list => ({
  current:list.filter(item => !['failed','finished'].includes(recordOutcome(item))),
  failed:list.filter(item => recordOutcome(item) === 'failed'),
  finished:list.filter(item => recordOutcome(item) === 'finished'),
});

export function overviewModel(data = {}, received = Date.now(), now = Date.now()) {
  const entries = missionEntries(data.projections, data.missions, received, now);
  const missions = {
    current:entries.filter(e => e.tab === 'current'),
    failed:entries.filter(e => e.tab === 'failed'),
    finished:entries.filter(e => e.tab === 'finished'),
  };
  for (const group of ['attention','running','lastknown','recorded']) missions[group] = missions.current.filter(e => e.group === group);
  const sessions = countByOutcome(records(data.sessions).sort(recent));
  const services = {current:0,unavailable:0,archived:0,unknown:0};
  for (const service of records(data.services)) services[serviceState(service)]++;
  return {missions,sessions,services,hosts:records(data.hosts).sort((a,b)=>text(a.host_name || a.host_id).localeCompare(text(b.host_name || b.host_id)))};
}

const eventLabels = {
  MissionStarted:'Mission started', MissionEnded:'Mission finished', MissionFailed:'Mission failed',
  MissionCompleted:'Mission finished', MissionCancelled:'Mission canceled',
  SessionStarted:'Session started', SessionTerminated:'Session stopped', SessionFailed:'Session failed',
  HostUnavailable:'Host unavailable', ArtifactRegistered:'Artifact saved',
};
function eventLabel(event) {
  if (eventLabels[event.event_type]) return eventLabels[event.event_type];
  if (['MissionUpdated','MissionStageChanged','SessionUpdated'].includes(event.event_type)) {
    const payload=event.payload || {};
    const outcome=recordOutcome({...payload,status:payload.to || payload.status});
    if (outcome === 'failed' || outcome === 'finished') return `${event.event_type.startsWith('Session') ? 'Session' : 'Mission'} reported ${outcome}`;
  }
  return null;
}
export function meaningfulEvents(events) {
  return records(events).filter(e => eventLabel(e)).slice(0,8);
}
function ageMarkup(value, now, label = 'Report') {
  const valid = Number.isFinite(Date.parse(value));
  return valid ? `${label} <span data-overview-stamp="${esc(value)}">${esc(formatDuration(Math.max(0,now-Date.parse(value))))}</span> ago` : `${label} time unknown`;
}
const metric = (value, label, href) => `<a class="ov-metric" href="${esc(href)}"><strong>${value}</strong><span>${esc(label)}</span></a>`;
const empty = label => `<p class="ov-note">${esc(label)}</p>`;
function availability(keys, states) {
  return keys.map(key => {
    const state = states[key];
    const label = {projections:'Live mission reports',missions:'Recorded missions',sessions:'Sessions',services:'Services',hosts:'Hosts',activity:'Recent activity',health:'Control plane'}[key];
    if (state?.error) return `<p class="ov-warning" role="status">${esc(label)}: Could not refresh.${state.loaded ? ' Showing last received records.' : ' Data is unavailable; retrying automatically.'}</p>`;
    if (!state?.loaded) return `<p class="ov-note" role="status">Loading ${esc(label.toLowerCase())}…</p>`;
    return '';
  }).join('');
}
function historyLinks(base, counts) {
  return `<nav class="ov-history" aria-label="${base === 'missions' ? 'Mission' : 'Session'} history">${link(`#/${base}?tab=failed`,`${counts.failed.length} failed`)}${link(`#/${base}?tab=finished`,`${counts.finished.length} finished / closed`)}</nav>`;
}
function missionRow(entry) {
  const activity = entry.projection?.activity || {};
  const detail = activity.waitingReason ? `Waiting: ${human(activity.waitingReason)}` : activity.currentActivity?.summary;
  return `<li class="ov-row"><div>${link(`#/missions/${encodeURIComponent(entry.key)}`,short(entry.title))}<p>${esc(short(detail || (entry.group === 'attention' ? entry.health : 'Activity not reported')))}</p></div><span class="ov-tag ${entry.group === 'attention' ? 'ov-alert' : ''}">${esc(human(entry.health))}${entry.host ? ` · ${esc(entry.host)}` : ''}</span></li>`;
}

export function renderOverviewSection(section, data = {}, states = {}, received = Date.now(), now = Date.now()) {
  const model = overviewModel(data,received,now);
  if (section === 'missions') {
    const m = model.missions;
    const ready = states.projections?.loaded || states.missions?.loaded;
    const rows = [...m.attention,...m.running].slice(0,5);
    return `<div class="ov-heading"><h2>Current missions</h2>${link('#/missions','All missions →')}</div>${availability(['projections','missions'],states)}
      ${ready ? `<div class="ov-metrics">${metric(states.projections?.loaded ? m.attention.length : '—','Need attention','#/missions?tab=current&status=attention')}${metric(states.projections?.loaded ? m.running.length : '—','Running with live reports','#/missions?tab=current&status=running')}${metric(states.projections?.loaded ? m.lastknown.length : '—','Updating / last known','#/missions?tab=current&status=lastknown')}</div>
      ${rows.length ? `<ul class="ov-list">${rows.map(e=>missionRow(e)).join('')}</ul>` : empty(states.projections?.loaded ? 'No missions currently report running or needing attention.' : 'Current mission activity is unavailable.')}
      <p class="ov-note">${m.current.length} known open mission records${m.recorded.length ? ` · ${m.recorded.length} without live telemetry` : ''}. ${rows.length < m.attention.length+m.running.length ? 'Showing 5; open All missions for the rest.' : 'Live evidence determines running and attention counts.'}</p>
      ${historyLinks('missions',m)}${!states.projections?.loaded || !states.missions?.loaded ? empty('Partial mission totals: waiting for both live reports and recorded missions.') : ''}` : ''}`;
  }
  if (section === 'sessions') {
    const s = model.sessions;
    return `<div class="ov-heading"><h2>Sessions</h2>${link('#/herd','All sessions →')}</div>${availability(['sessions'],states)}${states.sessions?.loaded ? `
      <div class="ov-metrics">${metric(s.current.length,'Open session records','#/herd?tab=current')}</div>
      <p class="ov-note">Latest reported state; an open record does not confirm a worker is running.</p>
      ${s.current.length ? `<ul class="ov-list">${s.current.slice(0,5).map(session=>`<li class="ov-row"><div>${link(`#/herd/session/${encodeURIComponent(session.session_id)}`,short(session.title || session.summary || session.agent_role || 'Agent session'))}<p>${esc(short(session.cwd || session.branch || session.model || 'Workspace not reported'))}</p></div><span class="ov-tag">Last reported ${esc(human(session.status) || 'unknown')}<br>${esc(session.host_id || 'Host unknown')} · ${ageMarkup(session.last_activity || session.updated_at,now)}</span></li>`).join('')}</ul>` : empty('No open session records.')}
      ${s.current.length > 5 ? empty(`Showing 5 of ${s.current.length} open records.`) : ''}${historyLinks('herd',s)}` : ''}`;
  }
  if (section === 'services') {
    const s=model.services;
    return `<div class="ov-heading"><h2>Services</h2>${link('#/services','Service catalog →')}</div>${availability(['services'],states)}${states.services?.loaded ? `
      <div class="ov-metrics">${metric(s.current,'Reported healthy','#/services')}${metric(s.unavailable,'Unavailable','#/services?status=unavailable')}</div>
      <p class="ov-note">${s.unknown} with unknown status · ${s.archived} archived. Service groups and discovered endpoints are in the catalog.</p>` : ''}`;
  }
  if (section === 'hosts') {
    return `<div class="ov-heading"><h2>Hosts</h2>${link('#/hosts','All hosts →')}</div>${availability(['hosts'],states)}${states.hosts?.loaded ? `
      ${model.hosts.length ? `<ul class="ov-list">${model.hosts.slice(0,5).map(host=>`<li class="ov-row"><div>${link(`#/hosts/${encodeURIComponent(host.host_id)}`,short(host.host_name || host.host_id || 'Unnamed host'))}<p>${ageMarkup(host.last_seen || host.last_heartbeat || host.updated_at,now)}</p></div><span class="ov-tag">Last reported ${esc(human(host.status) || 'unknown')}</span></li>`).join('')}</ul>` : empty('No host reports received.')}
      <p class="ov-note">${model.hosts.length} hosts recorded. Report age shows how recently each host was observed.</p>` : ''}`;
  }
  if (section === 'activity') {
    const events=meaningfulEvents(data.events);
    return `<div class="ov-heading"><h2>Recent changes</h2>${link('#/activity','All activity →')}</div>${availability(['activity'],states)}${states.activity?.loaded ? events.length ? `<ul class="ov-list">${events.map(event=>{
      const payload=event.payload || {};
      const title=eventLabel(event);
      const name=payload.title || payload.filename || payload.host_name || payload.agent_role || '';
      return `<li class="ov-row"><div><strong>${esc(title)}</strong>${name ? `<p>${esc(short(name))}</p>` : ''}</div><span class="ov-tag">${ageMarkup(event.source_timestamp || event.timestamp || event.occurred_at,now,'Changed')}</span></li>`;
    }).join('')}</ul>` : empty('No lifecycle changes in the latest activity window. Routine telemetry is hidden here.') : ''}`;
  }
  if (section === 'health') {
    return `${availability(['health'],states)}${states.health?.loaded ? `<span>Control plane: ${esc(data.health?.status === 'ok' ? 'responding' : human(data.health?.status) || 'status unknown')}</span><span>Read-only refresh every 5 seconds</span>` : ''}`;
  }
  return '';
}

const styles = `<style>
.ov-intro,.ov-note{color:var(--text-secondary);font-size:13px;line-height:1.6}.ov-intro{max-width:75ch;margin:0 0 18px}.ov-layout{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.ov-panel{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;overflow-wrap:anywhere}.ov-panel[data-overview=missions]{grid-column:1/-1}.ov-heading{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:14px}.ov-heading h2{font-size:17px;margin:0}.ov-heading>a{font-size:12px;white-space:nowrap}.ov-metrics{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}.ov-metric{display:flex;flex-direction:column;gap:4px;padding:10px 13px;background:var(--bg);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);font-size:12px;color:var(--text);text-decoration:none}.ov-metric strong{font-size:25px;line-height:1.2}.ov-list{list-style:none;padding:0;margin:0}.ov-row{display:flex;justify-content:space-between;align-items:start;gap:16px;padding:11px 0;border-top:1px solid var(--border-subtle);font-size:13px;line-height:1.5}.ov-row>div{min-width:0;flex:1}.ov-row>div>a{font-weight:600;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.ov-row p{margin:4px 0 0;color:var(--text-secondary);font-size:12px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.ov-tag{font-size:12px;color:var(--text-secondary);max-width:45%;text-align:right}.ov-alert,.ov-warning{color:var(--status-attention)}.ov-warning{font-size:12px;line-height:1.6}.ov-history{display:flex;flex-wrap:wrap;gap:16px;border-top:1px solid var(--border-subtle);padding-top:12px;margin-top:12px;font-size:12px}.ov-health{display:flex;flex-wrap:wrap;gap:16px;color:var(--text-secondary);font-size:12px;margin:18px 0}.ov-health p{margin:0}
@media(max-width:750px){.ov-layout{grid-template-columns:minmax(0,1fr)}.ov-panel{padding:14px}.ov-row{gap:10px}.ov-tag{max-width:42%}.ov-metric{padding:9px}.ov-metrics{gap:8px}.ov-heading{gap:8px}}
</style>`;

export function mountOverview(container, client) {
  let disposed=false, clock=null, received=Date.now();
  const data={}, states={}, controllers=new Map(), timers=new Map(), signatures=new Map();
  const panels=['missions','sessions','services','hosts','activity','health'];
  const sources={
    projections:signal=>client.listMissionActivity({signal}),
    missions:signal=>client.listMissions(undefined,{signal}),
    sessions:signal=>client.listSessions(undefined,{signal}),
    services:signal=>client.listServices({signal}),
    hosts:signal=>client.listHosts({signal}),
    activity:signal=>client.activity(50,{signal}),
    health:signal=>client.health({signal}),
  };
  container.innerHTML=`${styles}<p class="ov-intro">Current work, recent reports, and changes that matter. Finished, closed, and failed records stay in their respective tabs.</p><div class="ov-layout">${panels.filter(key=>key!=='health').map(key=>`<section class="ov-panel" data-overview="${key}" aria-label="${key}"></section>`).join('')}</div><footer class="ov-health" data-overview="health"></footer>`;
  function draw() {
    if(disposed || document.hidden)return;
    for(const panel of panels) {
      const element=container.querySelector(`[data-overview="${panel}"]`);
      const html=renderOverviewSection(panel,data,states,received);
      // Ignore clock text in the signature so an unchanged report keeps DOM/focus.
      const signature=html.replace(/(<span data-overview-stamp="[^"]+">)[^<]*(<\/span>)/g,'$1$2');
      if(signature===signatures.get(panel))continue;
      signatures.set(panel,signature);
      const focused=document.activeElement;
      const href=element.contains(focused) ? focused.closest('a')?.getAttribute('href') : null;
      element.innerHTML=html;
      if(href)[...element.querySelectorAll('a')].find(a=>a.getAttribute('href')===href)?.focus({preventScroll:true});
    }
    tickClocks();
  }
  function tickClocks() {
    const now=Date.now();
    container.querySelectorAll('[data-overview-stamp]').forEach(el=>{el.textContent=formatDuration(Math.max(0,now-Date.parse(el.dataset.overviewStamp)));});
  }
  async function poll(key) {
    if(disposed || document.hidden || controllers.has(key))return;
    clearTimeout(timers.get(key));
    const controller=new AbortController();controllers.set(key,controller);
    let timedOut=false;
    const timeout=setTimeout(()=>{timedOut=true;controller.abort();},15000);
    try {
      const response=await sources[key](controller.signal);
      if(disposed || controller.signal.aborted)return;
      const field={projections:'activities',activity:'events'}[key] || key;
      if (key === 'health' ? !response || typeof response.status !== 'string' : !Array.isArray(response?.[field])) throw new Error('Invalid response');
      data[key==='activity' ? 'events' : key]=key==='health' ? response : records(response?.[field]);
      if(key==='projections')received=Date.now();
      states[key]={loaded:true,error:false};
      draw();
    } catch {
      if(!disposed && (!controller.signal.aborted || timedOut)){states[key]={...states[key],error:true};draw();}
    } finally {
      clearTimeout(timeout);controllers.delete(key);
      if(!disposed && !document.hidden)timers.set(key,setTimeout(()=>poll(key),5000));
    }
  }
  function refresh() {for(const key of Object.keys(sources))poll(key);}
  function visibility() {
    clearInterval(clock);for(const timer of timers.values())clearTimeout(timer);timers.clear();
    if(document.hidden){for(const controller of controllers.values())controller.abort();return;}
    draw();refresh();clock=setInterval(draw,1000);
  }
  document.addEventListener('visibilitychange',visibility);visibility();
  return {refresh,dispose(){disposed=true;clearInterval(clock);for(const timer of timers.values())clearTimeout(timer);for(const controller of controllers.values())controller.abort();document.removeEventListener('visibilitychange',visibility);}};
}
