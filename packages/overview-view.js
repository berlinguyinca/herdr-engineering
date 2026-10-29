// Read-only overview: lifecycle records and current telemetry are separate evidence.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatDuration } from './herdr-web-components/format.js';
import { missionEntries } from './mission-catalog-view.js';
import { recordOutcome, recordVisibility, reportFreshness } from './record-state.js';
import { serviceState, serviceScope } from './services-view.js';
import { integrationEntries, integrationTimestamp } from './integrations-view.js';
import { isCalm, snapshotText, animateChanges, loadingProgress } from './motion.js';
import { freshnessSources } from './freshness.js';
import { createResourcePoller } from './refresh-bus.js';
import { requestErrorDetail } from './dashboard-load-state.js';
import { morph, handOff } from './patch.js';

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
const countByOutcome = (list,now) => ({
  current:list.filter(item => recordVisibility(item,now) === 'current'),
  unconfirmed:list.filter(item => recordVisibility(item,now) === 'unconfirmed'),
  failed:list.filter(item => recordOutcome(item) === 'failed'),
  finished:list.filter(item => recordOutcome(item) === 'finished'),
});

export function overviewModel(data = {}, received = Date.now(), now = Date.now()) {
  const entries = missionEntries(data.projections, data.missions, received, now);
  const missions = {
    current:entries.filter(e => e.tab === 'current'),
    unconfirmed:entries.filter(e => e.tab === 'unconfirmed'),
    failed:entries.filter(e => e.tab === 'failed'),
    finished:entries.filter(e => e.tab === 'finished'),
  };
  for (const group of ['attention','running','lastknown','recorded']) missions[group] = missions.current.filter(e => e.group === group);
  const sessions = countByOutcome(records(data.sessions).sort(recent),now);
  const services = {current:0,unavailable:0,archived:0,unknown:0};
  for (const service of records(data.services)) if (serviceScope(service) === 'herdr') services[serviceState(service,now)]++;
  return {missions,sessions,services,hosts:records(data.hosts).sort((a,b)=>text(a.host_name || a.host_id).localeCompare(text(b.host_name || b.host_id)))};
}

const OVERVIEW_CARDS = ['sessions','missions','fleet','ci','reviews','services','hosts','activity'];
const CARD_LABELS = {missions:'Current missions',fleet:'Fleet health',sessions:'Agents',ci:'CI builds',reviews:'Reviews',services:'HerdR services',hosts:'Hosts',activity:'Recent changes'};
const PREFERENCES_KEY = 'herdr.overview.layout.v1';
export function sanitizeOverviewPreferences(value) {
  const valid = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const order = [...new Set((Array.isArray(valid.order) ? valid.order : []).filter(key=>OVERVIEW_CARDS.includes(key)))];
  return {order:[...order,...OVERVIEW_CARDS.filter(key=>!order.includes(key))],hidden:[...new Set((Array.isArray(valid.hidden) ? valid.hidden : []).filter(key=>OVERVIEW_CARDS.includes(key)))]};
}

// Attention is a projection of the same retained observations as the cards.
export function overviewAttention(data = {}, states = {}, received = Date.now(), now = Date.now()) {
  const model=overviewModel(data,received,now), items=new Map();
  const add=(key,severity,actionability,title,detail,href,updated,source)=>{
    const item={key,severity,actionability,title:short(title),detail:short(detail),href,updated:integrationTimestamp(updated),source};
    if(!items.has(key) || item.updated>items.get(key).updated)items.set(key,item);
  };
  for(const entry of [...model.missions.attention,...model.missions.failed]) {
    if(snoozedUntil(data,entry.key)>now)continue;
    add(`mission:${entry.key}`,entry.tab==='failed'?'critical':'warning',2,entry.title,human(entry.health || 'failed'),`#/missions/${encodeURIComponent(entry.key)}`,entry.projection?.activity_snapshot_at || entry.recorded?.updated_at,'projections');
  }
  for(const session of model.sessions.failed)if(session.session_id)add(`session:${session.session_id}`,'critical',1,session.display_name || session.title || session.agent_role || 'Failed agent','Agent reported failed',`#/herd/session/${encodeURIComponent(session.session_id)}`,session.updated_at || session.last_activity,'sessions');
  for(const kind of ['ci','reviews']) {
   const seen=new Set();
   for(const entry of integrationEntries(data[kind],kind)) {
    if(seen.has(entry.id))continue;
    seen.add(entry.id);
    if(entry.tab!=='failed' && entry.tab!=='changes')continue;
    const r=entry.record;
    add(`${kind}:${entry.id}`,entry.tab==='failed'?'critical':'warning',entry.tab==='changes'?1:2,r.title || r.repository || (kind==='ci'?`CI run #${r.run_number}`:'Review'),`${human(r.state || r.status || r.decision)}${r.repository ? ` · ${r.repository}` : ''}${kind==='ci'?` · Run #${r.run_number}`:''}`,kind==='ci'?`#/ci?tab=failed&repository=${encodeURIComponent(r.provider_repo_id ?? r.repository_id ?? r.repository ?? '')}&provider=${encodeURIComponent(r.provider_url || '')}&search=${encodeURIComponent(r.run_number ?? '')}`:`#/reviews?tab=${entry.tab}&search=${encodeURIComponent(r.review_id || r.id || '')}`,r.updated_at || r.created_at,kind);
   }
  }
  for(const service of records(data.services))if(serviceScope(service)==='herdr' && serviceState(service,now)==='unavailable' && service.service_id)add(`service:${service.service_id}`,'critical',2,service.service_name || service.service_id,'Service reported unavailable',`#/services/${encodeURIComponent(service.service_id)}`,service.updated_at,'services');
  for(const host of model.hosts)if(host.host_id && (['unavailable','offline','failed'].includes(host.status) || reportFreshness(host.last_seen || host.updated_at,now)!=='current'))add(`host:${host.host_id}`,'warning',0,host.host_name || host.host_id,'No recent healthy host report',`#/hosts/${encodeURIComponent(host.host_id)}`,host.last_seen || host.updated_at,'hosts');
  if(data.health && data.health.status!=='ok')add('health','critical',2,'Control plane needs attention',human(data.health.status) || 'Status unknown','#/topology',null,'health');
  if(Number.isFinite(data.fleet?.summary?.drifted) && data.fleet.summary.drifted>0)add('fleet:drift','warning',1,'Fleet versions differ',`${data.fleet.summary.drifted} tool or plugin versions differ across hosts`,'#/hosts?drift=1',null,'fleet');
  const sources=['projections','missions','sessions','services','hosts','ci','reviews','health','fleet'];
  const unavailable=sources.filter(key=>!states[key]?.loaded || states[key]?.error || data[key]?.stale || data[key]?.available===false || data[key]?.status==='partial' || data[key]?.partial===true);
  const severity={critical:0,warning:1};
  return {items:[...items.values()].sort((a,b)=>severity[a.severity]-severity[b.severity] || b.actionability-a.actionability || b.updated-a.updated || a.key.localeCompare(b.key)),complete:!unavailable.length,unavailable};
}

// The server derives the same items for every browser (design 2026-10-24). A server answer that loaded
// is the source of truth; before it loads, or when it fails, the browser projection above is the fallback.
const SERVER_RANK = {critical:0,warning:1,info:2};
export function serverAttention(body, now = Date.now()) {
  const items = records(body?.items).filter(item=>item.state==='open' && typeof item.key==='string').map(item=>({
    key:item.key, severity:SERVER_RANK[item.severity]===undefined ? 'info' : item.severity, title:short(item.title), detail:short(item.detail),
    href:/^#\/\S*$/.test(item.href || '') ? item.href : '#/attention', updated:Date.parse(item.first_seen) || 0, source:text(item.source), count:Number(item.count) || 1,
  })).sort((a,b)=>SERVER_RANK[a.severity]-SERVER_RANK[b.severity] || b.updated-a.updated || a.key.localeCompare(b.key));
  const unavailable = (Array.isArray(body?.unavailable) ? body.unavailable : []).map(text).filter(Boolean);
  return {items,complete:body?.partial!==true && !unavailable.length,unavailable,server:true,acknowledged:Number(body?.counts?.acknowledged) || 0,snoozed:Number(body?.counts?.snoozed) || 0};
}

const eventLabels = {
  MissionStarted:'Mission started', MissionEnded:'Mission finished', MissionFailed:'Mission failed',
  MissionCompleted:'Mission finished', MissionCancelled:'Mission canceled',
  SessionStarted:'Agent started', SessionTerminated:'Agent stopped', SessionFailed:'Agent failed',
  HostUnavailable:'Host unavailable', ArtifactRegistered:'Artifact saved',
  PlannotatorReviewCompleted:'Review completed', PlannotatorReviewFailed:'Review failed',
  CIPipelineFinished:'CI run finished', CIPipelineFailed:'CI run failed',
};
function eventLabel(event) {
  if (eventLabels[event.event_type]) return eventLabels[event.event_type];
  if (['MissionUpdated','MissionStageChanged','SessionUpdated'].includes(event.event_type)) {
    const payload=event.payload || {};
    const outcome=recordOutcome({...payload,status:payload.to || payload.status});
    if (outcome === 'failed' || outcome === 'finished') return `${event.event_type.startsWith('Session') ? 'Agent' : 'Mission'} reported ${outcome}`;
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
const SOURCE_LABELS = {projections:'Live mission reports',missions:'Recorded missions',sessions:'Agents',services:'Services',hosts:'Hosts',fleet:'Fleet versions',activity:'Recent activity',health:'Control plane',ci:'CI',reviews:'Reviews',attention:'Attention'};
function availability(keys, states) {
  return keys.map(key => {
    const state = states[key];
    const label = SOURCE_LABELS[key];
    if (state?.error) {
      // One short line per card; the full reason (rate limit, request id) is in the tooltip rather than three lines of text.
      const detail = typeof state.error === 'string' ? state.error : 'Could not refresh.';
      const limited = /too many requests/i.test(detail);
      const short = limited ? 'Could not refresh (rate limited)' : 'Could not refresh';
      return `<p class="ov-warning ov-stale" role="status" title="${esc(`${label}: ${detail}`)}">↻ ${short} · ${state.loaded ? 'showing last data' : 'retrying'}</p>`;
    }
    if (!state?.loaded) return `<div data-skeleton aria-busy="true" role="status" aria-label="Loading ${esc(label.toLowerCase())}"><div class="hd-skeleton ov-skeleton" aria-hidden="true"></div>${loadingProgress(label.toLowerCase())}</div>`;
    return '';
  }).join('');
}
function historyLinks(base, counts) {
  return `<nav class="ov-history" aria-label="${base === 'missions' ? 'Mission' : 'Agent'} history">${link(`#/${base}?tab=unconfirmed`,`${counts.unconfirmed.length} unconfirmed`)}${link(`#/${base}?tab=failed`,`${counts.failed.length} failed`)}${link(`#/${base}?tab=finished`,`${counts.finished.length} finished / closed`)}</nav>`;
}
const SNOOZE_MS = 24 * 3600 * 1000;
const snoozedUntil = (data, key) => (data.snoozed instanceof Map ? data.snoozed.get(key) : 0) || 0;
function missionRow(entry, canSnooze = false) {
  const activity = entry.projection?.activity || {};
  const detail = activity.waitingReason ? `Waiting: ${human(activity.waitingReason)}` : activity.currentActivity?.summary;
  return `<li class="ov-row"><div>${link(`#/missions/${encodeURIComponent(entry.key)}`,short(entry.title))}<p>${esc(short(detail || (entry.group === 'attention' ? entry.health : 'Activity not reported')))}</p></div><span class="ov-tag ${entry.group === 'attention' ? 'ov-alert' : ''}">${esc(human(entry.health))}${entry.host ? ` · ${esc(entry.host)}` : ''}${canSnooze ? `<br><button type="button" class="ov-snooze" data-snooze="${esc(entry.key)}" title="Hide from this list for 24 hours">Snooze 24h</button>` : ''}</span></li>`;
}

export function renderOverviewSection(section, data = {}, states = {}, received = Date.now(), now = Date.now()) {
  const model = overviewModel(data,received,now);
  if (section === 'attention') {
    const mode=states.attention===undefined ? 'browser' : states.attention.error ? 'fallback' : states.attention.loaded && data.attention ? 'server' : states.attention.disabled ? 'browser' : states.attention.loaded ? 'browser' : 'loading';
    if(mode==='loading')return `<div class="ov-heading"><h2 tabindex="-1">Attention</h2></div><div data-skeleton aria-busy="true" role="status" aria-label="Loading attention"><div class="hd-skeleton ov-skeleton" aria-hidden="true"></div>${loadingProgress('attention')}</div>`;
    const attention=mode==='server' ? serverAttention(data.attention,now) : overviewAttention(data,states,received,now),expanded=data.attentionExpanded===true,limit=3;
    const visible=expanded?attention.items:attention.items.slice(0,limit);
    return `<div class="ov-heading"><h2 tabindex="-1">Attention</h2><span class="ov-tag">${attention.items.length ? `${attention.items.length}${attention.complete?'':'+'}` : attention.complete?'0':'—'} ${attention.server?'open':'observed'} items</span></div>
      ${mode==='fallback' ? '<p class="ov-warning" role="status">The shared attention list could not be loaded, so this list is computed in this browser and may differ from the Attention page.</p>' : ''}
      ${attention.server ? `<p class="ov-note">${link('#/attention','Open the Attention page →')}${attention.acknowledged || attention.snoozed ? ` · ${attention.acknowledged} acknowledged, ${attention.snoozed} snoozed` : ''}</p>` : ''}
      ${!attention.complete ? `<p class="ov-warning" role="status">Partial coverage: ${esc(attention.unavailable.map(key=>CARD_LABELS[key] || {projections:'Live missions',health:'Control plane'}[key] || key).join(', '))}. Loading, unavailable, or stale sources may contain more items; retained reports remain visible.</p>` : ''}
      ${attention.items.length>limit ? `<div class="ov-attention-controls"><span class="ov-note">Showing ${visible.length} of ${attention.items.length} observed items</span><button type="button" data-attention-toggle aria-expanded="${expanded}" aria-controls="ov-attention-items">${expanded?'Show fewer':`Show all ${attention.items.length}`}</button></div>` : ''}
      ${attention.items.length ? `<ul id="ov-attention-items" class="ov-list ov-attention-list${expanded?' is-expanded':''}" aria-label="Attention items"${expanded?' tabindex="0"':''}>${visible.map(item=>`<li class="ov-row" data-attention-key="${esc(item.key)}"${attention.server?' data-attention-origin="server"':''}><div>${link(item.href,item.title)}<p>${esc(item.detail)}</p></div><span class="ov-tag ov-alert">${esc(item.severity)}${item.updated?`<br>${ageMarkup(new Date(item.updated).toISOString(),now)}`:''}</span></li>`).join('')}</ul>` : empty(attention.complete?'No attention items in the received reports.':'Attention is not yet fully known.')}`;
  }
  if (section === 'missions') {
    const m = model.missions;
    const ready = states.projections?.loaded || states.missions?.loaded;
    const awake = m.attention.filter(e => snoozedUntil(data, e.key) <= now);
    const snoozedCount = m.attention.length - awake.length;
    const rows = [...awake,...m.running].slice(0,5);
    return `<div class="ov-heading"><h2>Current missions</h2>${link('#/missions','All missions →')}</div>${availability(['projections','missions'],states)}
      ${ready ? `<div class="ov-metrics">${metric(states.projections?.loaded ? awake.length : '—','Need attention','#/missions?tab=current&status=attention')}${metric(states.projections?.loaded ? m.running.length : '—','Running with live reports','#/missions?tab=current&status=running')}</div>
      ${snoozedCount ? `<p class="ov-note">${snoozedCount} snoozed for 24 hours. <button type="button" class="ov-snooze" data-snooze-clear>Show them again</button></p>` : ''}
      ${rows.length ? `<ul class="ov-list">${rows.map(e=>missionRow(e, e.group === 'attention')).join('')}</ul>` : empty(states.projections?.loaded ? 'No missions currently report running or needing attention.' : 'Current mission activity is unavailable.')}
      <p class="ov-note">${m.current.length} missions with current reports${m.recorded.length ? ` · ${m.recorded.length} recorded without live activity` : ''}. ${rows.length < awake.length+m.running.length ? 'Showing 5; open All missions for the rest.' : 'Live evidence determines running and attention counts.'}</p>
      ${historyLinks('missions',m)}${!states.projections?.loaded || !states.missions?.loaded ? empty('Partial mission totals: waiting for both live reports and recorded missions.') : ''}` : ''}`;
  }
  if (section === 'sessions') {
    const s = model.sessions;
    return `<div class="ov-heading"><h2>Agents</h2>${link('#/herd','All agents →')}</div>${availability(['sessions'],states)}${states.sessions?.loaded ? `
      <div class="ov-metrics">${metric(s.current.length,'Recently reported agents','#/herd?tab=current')}</div>
      <p class="ov-note">Open agents reported in the last 15 minutes. Older reports are available in Unconfirmed.</p>
      ${s.current.length ? `<ul class="ov-list">${s.current.slice(0,5).map(session=>`<li class="ov-row"><div>${link(`#/herd/session/${encodeURIComponent(session.session_id)}`,short(session.display_name || session.title || session.summary || session.agent_role || 'Agent'))}<p>${esc(short(session.cwd || session.branch || session.model || 'Workspace not reported'))}</p></div><span class="ov-tag">Last reported ${esc(human(session.status) || 'unknown')}<br>${esc(session.host_id || 'Host unknown')} · ${ageMarkup(session.last_activity || session.updated_at,now)}</span></li>`).join('')}</ul>` : empty('No recently reported open agents.')}
      ${s.current.length > 5 ? empty(`Showing 5 of ${s.current.length} current agents.`) : ''}${historyLinks('herd',s)}` : ''}`;
  }
  if (section === 'ci' || section === 'reviews') {
    const isCi=section==='ci', snapshot=data[section], entries=integrationEntries(snapshot,section);
    const current=entries.filter(e=>e.tab==='current'), attention=entries.filter(e=>e.tab===(isCi?'failed':'changes'));
    const ready=states[section]?.loaded;
    const title=isCi?'CI builds':'Plan and code reviews';
    return `<div class="ov-heading"><h2>${title}</h2>${link(`#/${section}`,isCi?'All CI →':'All reviews →')}</div>${availability([section],states)}${ready ? `
      ${isCi && snapshot.available===false && !entries.length ? `<p class="ov-warning">${esc(snapshot.status==='auth_error'?'Woodpecker authentication needs attention.':snapshot.status==='not_configured'?'Woodpecker is not configured.':'CI reports are unavailable.')}</p>` : `<div class="ov-metrics">${metric(current.length,isCi?'Queued / running / unknown':'Awaiting review',`#/${section}`)}${metric(attention.length,isCi?'Failed':'Changes requested',`#/${section}?tab=${isCi?'failed':'changes'}`)}</div>`}
      ${snapshot.stale ? '<p class="ov-warning">Showing stale reports. Open the catalog for connection details.</p>' : ''}
      ${current.length ? `<ul class="ov-list">${current.slice(0,2).map(e=>`<li class="ov-row"><div>${link(`#/${section}?search=${encodeURIComponent(e.record.repository || e.record.review_id || '')}`,short(e.record.title || e.record.repository || (isCi?'CI run':'Review')))}<p>${esc(isCi?`Run #${e.record.run_number} · ${e.record.branch || 'Branch not reported'}`:e.record.repository || 'Repository not reported')}</p></div><span class="ov-tag">${esc(human(e.record.state || e.record.status) || 'Status unknown')}</span></li>`).join('')}</ul>` : ''}
      ${isCi && attention.length ? `<h3 class="ov-sub">Latest failures</h3><ul class="ov-list">${attention.slice(0,2).map(e=>`<li class="ov-row"><div>${link(`#/ci?search=${encodeURIComponent(e.record.repository || '')}`,short(e.record.repository || e.record.title || 'CI run'))}<p>${esc(`Run #${e.record.run_number} · ${e.record.branch || 'Branch not reported'}`)}</p></div><span class="ov-tag ov-alert">${esc(human(e.record.state || e.record.status) || 'failed')}</span></li>`).join('')}</ul>` : ''}
      <p class="ov-note">${isCi?'Recent pipeline window; inspect steps, output and provider refresh progress.':'Review decisions and revision links stay with Plannotator.'}</p>` : ''}`;
  }
  if (section === 'services') {
    const s=model.services;
    return `<div class="ov-heading"><h2>HerdR services</h2>${link('#/services','Service catalog →')}</div>${availability(['services'],states)}${states.services?.loaded ? `
      <div class="ov-metrics">${metric(s.current,'Reported healthy','#/services')}</div>
      <p class="ov-note">${link('#/services?status=unavailable',`${s.unavailable} unavailable`)} · ${link('#/services?status=unknown',`${s.unknown} unconfirmed`)} · ${link('#/services?status=archived',`${s.archived} archived`)}. History remains in the catalog. ${link('#/local-services','Local services →')} · ${link('#/system-services','System services →')}</p>` : ''}`;
  }
  if (section === 'fleet') {
    const hosts=model.hosts, fleet=data.fleet || {}, sum=fleet.summary || {};
    const stale=hosts.filter(h => reportFreshness(h.last_seen || h.updated_at,now) !== 'current');
    const fresh=hosts.length-stale.length;
    return `<div class="ov-heading"><h2>Fleet health</h2>${link('#/hosts','Hosts and versions →')}</div>${availability(['hosts'],states)}${states.hosts?.loaded ? `
      <div class="ov-metrics">${metric(`${fresh} / ${hosts.length}`,'Hosts reporting now','#/hosts')}${metric(states.fleet?.loaded ? (sum.drifted ?? 0) : '—','Versions that differ','#/hosts?drift=1')}${metric(states.fleet?.loaded ? (sum.missing ?? 0) : '—','Not installed on a host','#/hosts?drift=1')}</div>
      ${stale.length ? `<p class="ov-warning">No recent report from ${stale.map(h=>esc(h.host_name || h.host_id)).join(', ')}.</p>` : ''}
      ${states.fleet?.loaded && !(fleet.hosts || []).length ? empty('No host has reported its tool and plugin versions yet.') : ''}` : ''}`;
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
.ov-sub{font-size:13px;margin:14px 0 0}.ov-stale{font-size:12px;margin:8px 0 0;color:var(--status-attention)}.ov-panel .ov-note,.ov-panel p.ov-note{font-size:12px;color:var(--text-muted);margin:8px 0 0}.ov-skeleton{height:58px;margin:10px 0}.ov-health .ov-skeleton{height:1em;width:16em;margin:0}.ov-snooze{font:inherit;font-size:11px;margin-top:6px;padding:2px 8px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text-secondary);cursor:pointer}.ov-intro,.ov-note{color:var(--text-secondary);font-size:13px;line-height:1.6}.ov-intro{max-width:75ch;margin:0 0 18px}.ov-layout{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;align-items:start}.ov-panel{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;overflow-wrap:anywhere}.ov-panel[data-overview=sessions]{grid-column:span 2}.ov-panel[data-empty]{display:none}.ov-panel[data-overview=fleet]{grid-column:auto}.ov-heading{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:12px}.ov-heading h2{font-size:17px;margin:0}.ov-heading>a{font-size:12px;white-space:nowrap}.ov-metrics{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}.ov-metric{display:flex;flex-direction:column;gap:4px;padding:9px 12px;background:var(--bg);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);font-size:12px;color:var(--text);text-decoration:none}.ov-metric strong{font-size:23px;line-height:1.2}.ov-list{list-style:none;padding:0;margin:0}.ov-row{display:flex;justify-content:space-between;align-items:start;gap:14px;padding:9px 0;border-top:1px solid var(--border-subtle);font-size:13px;line-height:1.45}.ov-row>div{min-width:0;flex:1}.ov-row>div>a{font-weight:600;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.ov-row p{margin:3px 0 0;color:var(--text-secondary);font-size:12px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.ov-tag{font-size:12px;color:var(--text-secondary);max-width:45%;text-align:right}.ov-alert,.ov-warning{color:var(--status-attention)}.ov-warning{font-size:12px;line-height:1.6}.ov-history{display:flex;flex-wrap:wrap;gap:14px;border-top:1px solid var(--border-subtle);padding-top:10px;margin-top:10px;font-size:12px}.ov-health{display:flex;flex-wrap:wrap;gap:16px;color:var(--text-secondary);font-size:12px;margin:18px 0}.ov-health p{margin:0}
@media(min-width:2200px){.ov-layout{grid-template-columns:repeat(4,minmax(0,1fr))}.ov-panel[data-overview=sessions],.ov-panel[data-overview=missions],.ov-panel[data-overview=hosts],.ov-panel[data-overview=activity]{grid-column:span 2}}
@media(max-width:1180px){.ov-layout{grid-template-columns:repeat(2,minmax(0,1fr))}.ov-panel[data-overview=sessions]{grid-column:1/-1;grid-row:auto}}
@media(max-width:960px){.ov-layout{grid-template-columns:minmax(0,1fr)}.ov-panel[data-overview=sessions]{grid-column:auto}.ov-panel{padding:14px}.ov-row{gap:10px}.ov-tag{max-width:42%}.ov-metric{padding:9px}.ov-metrics{gap:8px}.ov-heading{gap:8px}}
.ov-attention{margin-bottom:16px}.ov-attention-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:0 24px}.ov-attention-list.is-expanded{max-height:min(60vh,560px);overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable}.ov-attention-controls{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px}.ov-attention-controls button{font:inherit;font-size:13px;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:9px 12px;min-height:44px;cursor:pointer}.ov-attention-controls button:focus-visible,.ov-attention-list:focus-visible{outline:2px solid var(--accent);outline-offset:3px}.ov-panel[hidden]{display:none}.ov-customize{margin:0 0 16px;font-size:13px}.ov-customize>summary{cursor:pointer;width:fit-content;padding:8px 0}.ov-control-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:8px 0}.ov-control-row strong{min-width:145px}.ov-customize button{font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);padding:7px 10px;cursor:pointer}.ov-customize button:disabled{opacity:.45;cursor:default}.ov-customize button:focus-visible,.ov-customize summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}.ov-layout:has(>[hidden]){grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr))}.ov-layout:has(>[hidden])>.ov-panel{grid-column:auto}
@media(max-width:960px){.ov-layout:has(>[hidden]){grid-template-columns:minmax(0,1fr)}.ov-control-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));padding-bottom:8px;border-bottom:1px solid var(--border)}.ov-control-row strong{grid-column:1/-1}.ov-customize button{min-height:44px;padding:8px 4px}}
</style>`;

const SNOOZE_KEY = 'herdr.snoozedMissions';
function loadSnoozed(now = Date.now()) {
  const map = new Map();
  try {
    const raw = JSON.parse(localStorage.getItem(SNOOZE_KEY) || '{}');
    for (const [key, until] of Object.entries(raw)) if (Number.isFinite(until) && until > now) map.set(key, until);
  } catch { /* storage unavailable: snooze simply lasts until reload */ }
  return map;
}
function saveSnoozed(map) {
  try { localStorage.setItem(SNOOZE_KEY, JSON.stringify(Object.fromEntries(map))); } catch { /* ignore */ }
}

export function mountOverview(container, client, { freshness = () => null } = {}) {
  let disposed=false, clock=null, received=Date.now();
  const data={snoozed:loadSnoozed()}, states={}, signatures=new Map();
  const chip=freshnessSources(freshness);
  const panels=['attention',...OVERVIEW_CARDS,'health'];
  let preferences;
  try { preferences=sanitizeOverviewPreferences(JSON.parse(localStorage.getItem(PREFERENCES_KEY) || 'null')); } catch { preferences=sanitizeOverviewPreferences(); }
  const sources={
    projections:signal=>client.listMissionActivity({signal}),
    missions:signal=>client.listMissions(undefined,{signal}),
    sessions:signal=>client.listSessions(undefined,{signal}),
    services:signal=>client.listServices({signal}),
    hosts:signal=>client.listHosts({signal}),
    fleet:signal=>client.request('/api/v1/fleet/inventory',{signal}),
    activity:signal=>client.activity(50,{signal}),
    health:signal=>client.health({signal}),
    ci:signal=>client.getCi({signal}),
    reviews:signal=>client.listReviews({signal}),
    // Disabled on the server (404) is not an error: the browser projection stays, with no warning.
    attention:signal=>client.attention({state:'open'},{signal}).catch(error=>{if(error?.status===404)return {disabled:true};throw error;}),
  };
  states.attention={loaded:false,error:false};
  container.innerHTML=`${styles}<details class="ov-customize"><summary>Customize Overview</summary><p class="ov-note">Attention is always visible. Health stays in the footer.</p><div data-overview-controls></div><button type="button" data-overview-reset>Reset layout</button></details><section class="ov-panel ov-attention" data-overview="attention" aria-label="Attention"></section><div class="ov-layout">${OVERVIEW_CARDS.map(key=>`<section class="ov-panel" data-overview="${key}" aria-label="${key}"></section>`).join('')}</div><footer class="ov-health" data-overview="health"></footer>`;
  function applyPreferences() {
    const focused=document.activeElement;
    const action=focused?.dataset.overviewAction, card=focused?.dataset.card;
    const layout=container.querySelector('.ov-layout');
    preferences.order.forEach(key=>{
      const panel=container.querySelector(`[data-overview="${key}"]`);
      panel.hidden=preferences.hidden.includes(key);layout.append(panel);
    });
    container.querySelector('[data-overview-controls]').innerHTML=preferences.order.map((key,index)=>`<div class="ov-control-row"><strong>${CARD_LABELS[key]}</strong><button type="button" data-overview-action="toggle" data-card="${key}" aria-pressed="${!preferences.hidden.includes(key)}" aria-label="${preferences.hidden.includes(key)?'Show':'Hide'} ${CARD_LABELS[key]}">${preferences.hidden.includes(key)?'Show':'Hide'}</button><button type="button" data-overview-action="up" data-card="${key}" aria-label="Move ${CARD_LABELS[key]} up" ${index===0?'disabled':''}>Move up</button><button type="button" data-overview-action="down" data-card="${key}" aria-label="Move ${CARD_LABELS[key]} down" ${index===preferences.order.length-1?'disabled':''}>Move down</button></div>`).join('');
    if(action && card) {
      const target=container.querySelector(`[data-overview-action="${action}"][data-card="${card}"]`);
      (target?.disabled?container.querySelector(`[data-overview-action="toggle"][data-card="${card}"]`):target)?.focus({preventScroll:true});
    }
  }
  applyPreferences();
  // Motion pairs morphed metric tiles by link + label, attention rows by data-attention-key.
  const metricKey=el=>{const a=el.closest('.ov-metric');return a?`${a.getAttribute('href')}|${a.querySelector('span')?.textContent}`:null;};
  const PULSE_MS=6000;
  const pulseUntil=new Map();
  const ATTENTION_SOURCES={mission:'projections',session:'sessions',ci:'ci',reviews:'reviews',service:'services',host:'hosts',health:'health',fleet:'fleet'};
  let shownCritical=null, loadedAtLastAttention=new Set();
  function markNewCritical(element) {
    const now=Date.now();
    const rows=[...element.querySelectorAll('[data-attention-key]')].filter(row=>row.querySelector('.ov-alert')?.textContent.startsWith('critical'));
    const critical=rows.map(row=>row.getAttribute('data-attention-key'));
    // A critical item is "new" only when its source had already loaded at the previous render:
    // everything on first load, and items arriving with a source's first response, stay still.
    // Server inbox items (any key shape) come from the attention source itself.
    if(shownCritical && !isCalm())for(const row of rows){
      const key=row.getAttribute('data-attention-key');
      const source=row.hasAttribute('data-attention-origin') ? 'attention' : ATTENTION_SOURCES[key.split(':')[0]];
      if(!shownCritical.has(key) && loadedAtLastAttention.has(source))pulseUntil.set(key,now+PULSE_MS);
    }
    shownCritical=new Set(critical);
    loadedAtLastAttention=new Set(Object.keys(states).filter(key=>states[key]?.loaded));
  }
  function applyPulses() {
    const now=Date.now();
    for(const [key,until] of pulseUntil)if(until<=now || isCalm())pulseUntil.delete(key);
    container.querySelectorAll('[data-overview="attention"] [data-attention-key]').forEach(row=>{
      const alert=row.querySelector('.ov-alert');
      if(!alert)return;
      if(pulseUntil.has(row.getAttribute('data-attention-key'))){if(alert.getAttribute('data-pulse')!=='once')alert.setAttribute('data-pulse','once');}
      else if(alert.hasAttribute('data-pulse'))alert.removeAttribute('data-pulse');
    });
  }
  // Tiles count up only for data-driven changes: not while sources are still landing on first load,
  // and not right after the user snoozed or restored missions.
  let motionSettled=false, userChange=false;
  // A card with nothing active (loaded, no rows, every tile zero, no warning) is not drawn; it returns when something happens.
  const EMPTY_SOURCE={sessions:'sessions',missions:'projections',fleet:'fleet',ci:'ci',reviews:'reviews',services:'services',hosts:'hosts',activity:'activity'};
  function setEmpty(panel,element) {
    const source=EMPTY_SOURCE[panel];
    if(!source)return;
    // A refresh that failed after data had loaded (a rate limit, a blip) does not bring an empty card back.
    const ready=states[source]?.loaded;
    const empty=ready && !element.querySelector('.ov-row,.ov-warning:not(.ov-stale),[data-skeleton],herdr-error') && [...element.querySelectorAll('.ov-metric strong')].every(n=>/^[0—]$/.test(n.textContent.trim()));
    if(empty)element.setAttribute('data-empty','');else element.removeAttribute('data-empty');
  }
  function draw() {
    if(disposed || document.hidden)return;
    // Settled = every source has answered once, with data or an error: a source that keeps failing
    // must not hold the counters still for ever.
    const settled=Object.keys(sources).every(key=>states[key]?.loaded || states[key]?.error);
    const live=settled&&motionSettled&&!userChange;
    motionSettled=settled;userChange=false;
    for(const panel of panels) {
      const element=container.querySelector(`[data-overview="${panel}"]`);
      const html=renderOverviewSection(panel,data,states,received);
      // Ignore clock text in the signature so an unchanged report keeps DOM/focus.
      const signature=html.replace(/(<span data-overview-stamp="[^"]+">)[^<]*(<\/span>)/g,'$1$2');
      if(signature===signatures.get(panel))continue;
      signatures.set(panel,signature);
      const focused=document.activeElement;
      const hadFocus=element.contains(focused);
      const href=hadFocus ? focused.closest('a')?.getAttribute('href') : null;
      const snooze=hadFocus ? focused.dataset?.snooze : null;
      const attentionToggle=hadFocus && focused.hasAttribute('data-attention-toggle');
      const attentionList=hadFocus && focused.id==='ov-attention-items';
      const attentionScroll=element.querySelector('.ov-attention-list.is-expanded')?.scrollTop || 0;
      const metrics=live?snapshotText(element,'.ov-metric strong',metricKey):null;
      // Morphed in place: unchanged rows and metric tiles keep their nodes (and any focus/scroll),
      // so a quiet refresh writes nothing. Focus is then restored by the same href/snooze/toggle
      // lookup as before, so a focused control that left is handed to its equivalent (or the panel
      // heading) instead of dropping to <body>; handOff cleans up any node morph kept only because
      // the user was in it.
      const stranded=morph(element,html);
      setEmpty(panel,element);
      animateChanges(element,'.ov-metric strong',metrics,metricKey);
      if(panel==='attention')markNewCritical(element);
      const expandedList=element.querySelector('.ov-attention-list.is-expanded');
      if(expandedList)expandedList.scrollTop=attentionScroll;
      if(hadFocus) {
        const target=attentionToggle ? element.querySelector('[data-attention-toggle]') : attentionList ? expandedList : href ? [...element.querySelectorAll('a')].find(a=>a.getAttribute('href')===href) : snooze ? [...element.querySelectorAll('[data-snooze]')].find(button=>button.dataset.snooze===snooze) : null;
        const fallback=element.querySelector('h2');
        if(fallback)fallback.tabIndex=-1;
        (target || fallback)?.focus({preventScroll:true});
      }
      handOff(stranded, () => null);
    }
    applyPulses();
    tickClocks();
  }
  function tickClocks() {
    const now=Date.now();
    container.querySelectorAll('[data-overview-stamp]').forEach(el=>{el.textContent=formatDuration(Math.max(0,now-Date.parse(el.dataset.overviewStamp)));});
  }
  const polling = createResourcePoller({ attention: sources.attention, ...sources }, {
    apply(key, response) {
      const field={projections:'activities',activity:'events'}[key] || key;
      const objectSource=['health','ci','reviews','fleet','attention'].includes(key);
      if (objectSource ? !response || (key==='attention' ? !response.disabled && !Array.isArray(response.items) : key==='health' ? typeof response.status!=='string' : key==='ci' ? typeof response.available!=='boolean' : key==='fleet' ? !Array.isArray(response.components) : !Array.isArray(response.reviews)) : !Array.isArray(response?.[field])) throw new Error('Invalid response');
      data[key==='activity' ? 'events' : key]=objectSource ? response : records(response?.[field]);
      if(key==='projections')received=Date.now();
      if(key==='attention' && response.disabled)data.attention=null;
      states[key]={loaded:true,error:false,...(key==='attention' && response.disabled ? {disabled:true} : {})};
      chip.ok(key); draw();
    },
    onError(key, error) {
      const detail = requestErrorDetail(error);
      states[key]={...states[key],error:`Could not refresh: ${detail}`};
      chip.fail(key,/too many requests|rate.?limit|429/i.test(detail)?`${SOURCE_LABELS[key]} rate limited`:`${SOURCE_LABELS[key]}: ${String(detail).split(/\s*·\s*Request\b/)[0].slice(0,80)}`); draw();
    },
  });
  function refresh() { polling.refresh(); }
  function visibility() {
    clearInterval(clock);
    if(document.hidden){polling.pause();return;}
    draw();polling.resume();clock=setInterval(draw,1000);
  }
  container.addEventListener('click',event=>{
    if(event.target.closest?.('[data-attention-toggle]')){data.attentionExpanded=!data.attentionExpanded;draw();return;}
    const control=event.target.closest?.('[data-overview-action], [data-overview-reset]');
    if(control) {
      if(control.hasAttribute('data-overview-reset'))preferences=sanitizeOverviewPreferences();
      else {
        const key=control.dataset.card, action=control.dataset.overviewAction,index=preferences.order.indexOf(key);
        if(action==='toggle')preferences.hidden=preferences.hidden.includes(key)?preferences.hidden.filter(value=>value!==key):[...preferences.hidden,key];
        else {const next=index+(action==='up'?-1:1);if(next>=0 && next<preferences.order.length)[preferences.order[index],preferences.order[next]]=[preferences.order[next],preferences.order[index]];}
      }
      try {localStorage.setItem(PREFERENCES_KEY,JSON.stringify(preferences));}catch { /* Session preferences remain usable. */ }
      applyPreferences();return;
    }
    const button=event.target.closest?.('.ov-snooze');
    if(!button)return;
    if(button.hasAttribute('data-snooze'))data.snoozed.set(button.dataset.snooze,Date.now()+SNOOZE_MS);
    else data.snoozed.clear();
    saveSnoozed(data.snoozed);signatures.delete('missions');userChange=true;draw();
  });
  document.addEventListener('visibilitychange',visibility);visibility();
  return {refresh,dispose(){disposed=true;clearInterval(clock);polling.dispose();document.removeEventListener('visibilitychange',visibility);}};
}
