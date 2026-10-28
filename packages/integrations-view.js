// CI and reviews retain their upstream authority; this view presents observations.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
import {safeServiceUrl} from './services-view.js';
import {recordOutcome} from './record-state.js';
import {ciRunKey} from './ci-performance.js';
import {ciChartModel,renderCiCharts,ciChartStyles} from './ci-charts.js';
import {ciInsightsModel,renderCiInsights,ciInsightStyles,renderCiRunReports} from './ci-insights.js';
import {renderCiPipeline,renderCiDiagnosis,ciPipelineStyles,pipelineZoom,sameCiRepository,ciComparisonModel,renderCiComparison} from './ci-pipeline.js';
import {mountCiObservability,ciProjectKey} from './ci-observability-view.js';
import {ciRunInScope,ciRunInWindow,ciSelectRunWindow} from './ci-health.js';
import {mountCiHealth} from './ci-health-view.js';
import {snapshotText,animateChanges,skeleton} from './motion.js';
import {morph,handOff,patchText,patchAttr} from './patch.js';
import {renderCiFix,ciFixStyles,fixRunKey,latestFix,fixIsActive,fixError} from './ci-fix-view.js';
import {attachError,attachForms} from './terminal-attach.js';
import {renderRelated} from './related-panel.js';
import {entityHref} from './entity-links.js';

const text = value => typeof value === 'string' ? value : '';
const records = value => Array.isArray(value) ? value.filter(v=>v && typeof v==='object' && !Array.isArray(v)) : [];
const short = (value, max=180) => text(value).replace(/\s+/g,' ').trim().slice(0,max);
const stateOf = record => text(record.state || record.status || record.decision).toLowerCase().replaceAll('-','_');
const human = value => text(value).replaceAll('_',' ');
export function integrationTimestamp(value) {
  if (typeof value==='number') return value > 0 ? (value < 1e12 ? value*1000 : value) : 0;
  return value ? Date.parse(value) || 0 : 0;
}
export function integrationOutcome(record, kind='ci') {
  const state=stateOf(record);
  if (kind==='reviews' && ['changes_requested','request_changes','rejected','annotated','denied'].includes(state)) return 'changes';
  if (['failure','failed','error','killed','timed_out','crashed'].includes(state)) return 'failed';
  if (['success','passed','finished','complete','completed','canceled','cancelled','skipped','declined','approved','closed','resolved'].includes(state)) return 'finished';
  return 'current';
}
export function integrationEntries(data={},kind='ci') {
  const list=kind==='reviews' ? records(data.reviews) : Object.entries(data.pipelines || {}).flatMap(([repository,runs])=>records(runs).map(r=>({...r,repository:r.repository || repository})));
  return list.map(record=>({record,
    id:kind==='reviews' ? String(record.review_id || record.id || '') : ciRunKey(record),
    tab:integrationOutcome(record,kind),
    project:short(record.project_id || record.project || record.repository) || 'Project not reported',
    search:[record.title,record.provider_url,record.repository,record.repository_id,record.project_id,record.project,record.branch,record.revision,record.run_number,record.review_id,record.status,record.state,record.mission_id,record.session_id].filter(v=>v!=null).join(' ').toLowerCase(),
  })).sort((a,b)=>integrationTimestamp(b.record.updated_at || b.record.created_at)-integrationTimestamp(a.record.updated_at || a.record.created_at));
}
const canonicalProvider = value => {
  const provider=text(value).trim().replace(/\/+$/,'');
  try {const url=new URL(provider);return url.origin+url.pathname.replace(/\/+$/,'');}
  catch {return provider;}
};
export function selectIntegrations(entries,{tab='current',search='',repository='',provider='',mission='',session='',branch='',days=0,scope='all',start=null,end=null,project=null,mode='days',run_number=null}={}) {
  const terms=search.toLowerCase().trim().split(/\s+/).filter(Boolean),providerKey=canonicalProvider(provider);
  const now=Date.now();
  const eligible=entries.filter(e=>(!project||ciProjectKey(e.record)===ciProjectKey(project)) && (!providerKey || canonicalProvider(e.record.provider_url)===providerKey) && (!repository || e.record.repository===repository || e.record.repository_id===repository || String(e.record.provider_repo_id)===repository)
    && (!branch || e.record.branch===branch) && ciRunInScope(e.record,scope,e.record)
    && (!mission || (e.record.mission_key || e.record.mission_id)===mission || records(e.record.related_missions).some(r=>(r.mission_key || r.mission_id)===mission))
    && (!session || e.record.session_id===session || records(e.record.related_sessions).some(r=>r.session_id===session)) && terms.every(t=>e.search.includes(t)));
  const selected=new Set(ciSelectRunWindow(eligible.map(e=>e.record),{days,start,end,mode,run_number},now));
  return eligible.filter(e=>selected.has(e.record)&&(tab==='all'||e.tab===tab));
}
const age = (value,label='Updated') => {
  const stamp=integrationTimestamp(value);
  return stamp ? `${esc(label)} <span data-integration-time="${stamp}">${esc(formatDuration(Math.max(0,Date.now()-stamp)))}</span> ago` : `${esc(label)} time not reported`;
};
function elapsed(record) {
  const start=integrationTimestamp(record.started_at || record.started || record.created_at);
  if (!start) return 'Duration not reported';
  const end=integrationTimestamp(record.finished_at || record.finished);
  if(integrationOutcome(record)!=='current' && !end)return 'Finish time not reported';
  const label=['pending','blocked','queued','waiting'].includes(stateOf(record)) ? 'Waiting' : 'Elapsed';
  return `${label} ${end ? esc(formatDuration(Math.max(0,end-start))) : `<span data-integration-time="${start}">${esc(formatDuration(Math.max(0,Date.now()-start)))}</span>`}`;
}
const external = (url,label) => {
  const safe=safeServiceUrl(url);
  return safe ? `<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>` : '';
};
export function integrationHealth(data,kind='ci') {
  if (!data) return `<p class="ig-note" role="status">Loading ${kind==='ci'?'CI runs':'reviews'}…</p>`;
  const labels={not_configured:'Woodpecker is not configured. Add a CI token to the server configuration.',auth_error:'Woodpecker authentication failed. The server credential needs attention.',unavailable:'Woodpecker could not be reached.',partial:'Some CI repositories could not refresh.',syncing:'Discovering CI repositories and runs…'};
  const error=kind==='ci' ? labels[data.status] || (data.available===false ? 'CI data is unavailable.' : '') : data.error;
  const progress=data.progress;
  const total=Number(progress?.total),completed=Number(progress?.completed);
  const measured=Number.isFinite(total) && total>0 && Number.isFinite(completed);
  return `${error ? `<p class="ig-warning" role="status">${esc(error)}${data.stale ? ' Showing last received records.' : ''}</p>` : ''}
    ${kind==='ci' && Object.keys(data.repo_errors || {}).length ? `<details class="ig-runners"><summary>Repository refresh issues</summary>${Object.entries(data.repo_errors).map(([repo,message])=>`<p>${esc(short(repo))}: ${esc(short(message,300))}</p>`).join('')}</details>` : ''}
    ${data.project_mapping_error ? `<p class="ig-warning">${esc(short(data.project_mapping_error,300))} Using repository groups.</p>` : ''}
    ${data.error && kind==='ci' ? `<p class="ig-note">${esc(short(data.error,400))}</p>` : ''}
    ${data.refreshing || data.status==='syncing' ? `<div class="ig-sync" role="status"><span>${progress?.current ? `Checking ${esc(short(progress.current))}` : 'Refreshing CI reports…'}${measured ? ` · ${Math.min(completed,total)} of ${total}` : ''}</span>${measured ? `<progress value="${Math.min(Math.max(0,completed),total)}" max="${total}" aria-label="Repositories checked"></progress>` : ''}</div>` : ''}
    <p class="ig-note">${age(data.last_success_at || (kind==='reviews' ? data.checked_at || data.updated_at : null),kind==='ci'?'Last successful refresh':'Reviews checked')}${data.stale ? ' · Reports are stale' : ''} · Refreshes automatically while visible</p>`;
}
export function integrationRow(entry,kind='ci') {
  const r=entry.record;
  const status=human(stateOf(r)) || 'status unknown';
  const title=short(r.title) || (kind==='ci' ? `Run #${r.run_number ?? 'unknown'}` : `${r.kind==='code'?'Code':'Plan'} review`);
  const running=kind==='ci'&&entry.tab==='current'&&['running','pending','started'].includes(stateOf(r));
  return `<article class="ig-row" data-entry="${esc(entry.id)}"${running?' data-state="running"':''}><div class="ig-main">
    ${kind==='ci' ? `<button type="button" class="ig-title" data-run="${esc(entry.id)}">${esc(title)}</button>` : `<strong class="ig-title">${esc(title)}</strong>`}
    <p class="ig-note">${esc(short(r.repository)||'Repository not reported')}${r.branch ? ` · ${esc(short(r.branch,80))}` : ''}${r.revision ? ` · <code>${esc(short(r.revision,12))}</code>` : ''}</p>
    ${kind==='reviews' ? `<div class="ig-links">${entry.tab==='current' ? external(r.review_url || r.url,'Open in Plannotator') : '<span class="ig-note">Recorded review</span>'}${r.mission_key || r.mission_id ? `<a href="#/missions/${esc(encodeURIComponent(r.mission_key || r.mission_id))}">Mission</a>` : ''}${r.session_id ? `<a href="#/herd/session/${esc(encodeURIComponent(r.session_id))}">Agent</a>` : ''}</div>${r.summary ? `<p class="ig-summary">${esc(short(r.summary))}</p>` : ''}${r.stale ? '<p class="ig-warning">Review launcher report is stale; open upstream to check its state.</p>' : ''}<details class="ig-review-details" data-review-details="${esc(entry.id)}"><summary>Plan and feedback</summary>${r._detailsError ? `<p class="ig-warning" role="status">${esc(r._detailsError)}</p>` : ''}${r.plan ? `<pre class="ig-log">${esc(text(r.plan).slice(0,50000))}${text(r.plan).length>50000?'\n… Plan display truncated.':''}</pre>` : ''}${r.feedback ? `<p class="ig-review-feedback">${esc(text(r.feedback).slice(0,10000))}</p>` : ''}${!r.plan && !r.feedback ? `<p class="ig-note">${r._detailsLoaded?'No plan text or feedback was recorded.':r._detailsError?'Close and reopen to retry.':'Loading review details…'}</p>` : ''}</details>` : ''}
    </div><div class="ig-state"><strong class="${['failed','changes'].includes(entry.tab)?'ig-warning':''}">${esc(status)}</strong><span>${kind==='ci' ? elapsed(r) : age(r.updated_at || r.created_at)}</span>${kind==='reviews' ? `<span>${esc(r.kind==='code'?'Code review':'Plan review')}</span>` : ''}</div></article>`;
}
export function newOperationId() {
  const bytes=crypto.getRandomValues(new Uint8Array(16));
  bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  const hex=[...bytes].map(byte=>byte.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
export function pipelineDetails(detail, summary={}) {
  return {...summary,...detail,repository:summary.repository || detail.repository,
    run_number:detail.run_number ?? detail.number ?? summary.run_number,
    revision:detail.revision ?? detail.commit ?? summary.revision,
    state:detail.state ?? detail.status ?? summary.state};
}

export function ciSelectedRunReports(reports,observations,active) {
  const identity=JSON.parse(ciRunKey(active)),provider=identity[0];
  if(!provider)return [];
  const matches=record=>{const key=JSON.parse(ciRunKey(record));return key[1]===identity[1]&&key[2]===identity[2]&&record.repository===active.repository;};
  const providers=new Set(records(observations).filter(matches).map(record=>JSON.parse(ciRunKey(record))[0]));
  return records(reports).filter(report=>matches(report)&&(JSON.parse(ciRunKey(report))[0]===provider || !JSON.parse(ciRunKey(report))[0]&&providers.size===1&&providers.has(provider)));
}

export function activePiSessions(sessions) {
  return records(sessions).filter(s=>text(s.session_id) && /(^|[\s/_-])pi($|[\s/_-])/i.test(text(s.agent_role))
    && ['active','running','working','idle','waiting','ready','busy'].includes(text(s.status).toLowerCase())
    && !['failed','finished'].includes(recordOutcome(s))
    && s.agent_session && typeof s.agent_session==='object' && !Array.isArray(s.agent_session)
    && ['pi','integration'].includes(s.agent_session.agent || s.agent_session.source)
    && text(s.host_id) && text(s.terminal_id) && text(s.herdr_session)
    && s.interactive_ready!==false && !s.launch_pending);
}
export function renderPipeline(pipeline,{loading=false,error='',log=null,logError='',step='',actionMessage='',debugSessions=null,debugSessionId='',debugMessage='',collapseSuccess=false,zoom=1,fixHtml='',relatedHtml=''}={}) {
  if (!pipeline) return loading ? '<p class="ig-note" role="status">Loading pipeline steps…</p>' : '<p class="ig-note">Select a run to inspect its steps and logs.</p>';
  const tasks=records(pipeline.tasks);
  const completed=tasks.filter(t=>integrationOutcome(t)!=='current').length;
  return `<div class="ig-heading"><h2>Run #${esc(pipeline.run_number ?? '')}</h2><button type="button" class="hd-btn" data-close-inspector><span class="ig-back-label">Back to runs</span><span class="ig-close-label">Close</span></button></div>
    <p class="ig-note">${esc(pipeline.repository || '')}${pipeline.branch ? ` · ${esc(pipeline.branch)}` : ''}${pipeline.revision ? ` · <code>${esc(pipeline.revision)}</code>` : ''} · ${esc(human(stateOf(pipeline)) || 'Status unknown')} · ${elapsed(pipeline)}</p>
    <div class="ig-links">${external(pipeline.url,'Open in Woodpecker')}${pipeline.can_retry ? '<button class="hd-btn" type="button" data-ci-action="retry">Retry run</button>' : ''}${pipeline.can_cancel ? '<button class="hd-btn" type="button" data-ci-action="cancel">Cancel run</button>' : ''}</div>
    <p class="ig-note">Event: ${esc(pipeline.event||'Not reported')} · Created: ${esc(pipeline.created_at||'Not reported')} · Started: ${esc(pipeline.started_at||'Not reported')} · Finished: ${esc(pipeline.finished_at||'Not reported')}</p>
    ${relatedHtml}
    ${error ? `<p class="ig-warning" role="status">${esc(error)} Showing the last received details.</p>` : ''}${actionMessage ? `<p role="status" class="ig-note">${esc(actionMessage)}</p>` : ''}
    ${integrationOutcome(pipeline)==='failed' ? `<details class="ig-debug" data-debug-disclosure><summary>Debug with Pi</summary><p class="ig-note">Send the failed revision, step details and bounded log output to a Pi agent you choose.</p>${debugSessions===null ? '<p class="ig-note">Open this section to load active Pi agents.</p>' : `<label class="ig-note">Target Pi agent<select data-debug-session aria-label="Target Pi agent"><option value="">Choose an agent…</option>${debugSessions.map(s=>`<option value="${esc(s.session_id)}" ${s.session_id===debugSessionId?'selected':''}>${esc(short(s.display_name || s.title || s.summary || s.cwd || s.session_id,100))}${s.host_id?` · ${esc(short(s.host_id,60))}`:''}${pipeline.branch && s.branch===pipeline.branch?' · on this branch':''}</option>`).join('')}</select></label>${!debugSessions.length ? '<p class="ig-note">No active Pi agents with verified messaging are available.</p>' : ''}<button type="button" class="hd-btn" data-debug-send ${!debugSessionId?'disabled':''}>Send to Pi</button> <button type="button" class="hd-btn" data-debug-fix ${!debugSessionId?'disabled':''}>Fix with Pi</button><p class="ig-note">Fix with Pi asks the agent to reproduce the failure in a new worktree at this exact revision, fix it and open a pull request. It never merges.</p>`}${debugMessage ? `<p class="ig-note" role="status">${esc(debugMessage)}</p>` : ''}</details>` : ''}${integrationOutcome(pipeline)==='failed' ? fixHtml : ''}
    ${tasks.length ? `<p class="ig-note">${completed} of ${tasks.length} steps finished · Select a step for its log</p><progress value="${completed}" max="${tasks.length}" aria-label="Pipeline steps finished"></progress><div class="ci-pipeline-controls" role="group" aria-label="Pipeline display"><button type="button" class="hd-btn" data-collapse-success aria-pressed="${collapseSuccess}">${collapseSuccess?'Expand successful steps':'Collapse successful steps'}</button><button type="button" class="hd-btn" data-pipeline-zoom="out" aria-label="Zoom out" ${zoom<=.75?'disabled':''}>−</button><button type="button" class="hd-btn" data-pipeline-zoom="reset" aria-label="Reset zoom">${Math.round(zoom*100)}%</button><button type="button" class="hd-btn" data-pipeline-zoom="in" aria-label="Zoom in" ${zoom>=1.5?'disabled':''}>+</button></div>${renderCiPipeline(pipeline,{step,collapseSuccess,zoom,diagnosis:pipeline.diagnosis})}` : renderCiDiagnosis(pipeline.diagnosis)+(!Array.isArray(pipeline.tasks) && loading && !error ? '<p class="ig-note" role="status">Loading pipeline steps…</p>' : '<p class="ig-note">No steps reported yet. Waiting for Woodpecker.</p>')}
    ${step ? `<h3>Step log</h3>${logError ? `<p class="ig-warning" role="status">${esc(logError)}</p>` : ''}${log===null ? '<p class="ig-note" role="status">Loading step output…</p>' : `<p class="ig-note">${age(log.checked_at,'Log checked')}${log.truncated?' · Output truncated':''}</p>${log.available===false ? `<p class="ig-note">${esc(log.error || 'Log output is not available yet.')}</p>` : `<pre class="ig-log" tabindex="0" aria-label="Step output">${esc(text(log.log)) || 'No output reported yet.'}</pre>`}`}` : ''}`;
}

// The filter names the entity it came from and links back to it (cross-linking), never from free text.
function filterLink(repository,mission,session) {
  const [kind,id]=repository?['project',repository]:mission?['mission',mission]:['session',session];
  const href=entityHref(kind,id);
  return href?`<a href="${esc(href)}">${esc(id)}</a>`:esc(id);
}

const styles=`<style>
.ig-intro,.ig-note{color:var(--text-secondary);font-size:12px;line-height:1.6}.ig-intro{font-size:13px;max-width:85ch}.ig-note{margin:7px 0}.ig-warning{color:var(--status-attention);font-size:13px;line-height:1.6;overflow-wrap:anywhere}.ig-tabs{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0}.ig-tabs button{font:inherit;padding:10px 16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);cursor:pointer}.ig-tabs button[aria-selected=true]{border-color:var(--accent);box-shadow:inset 0 -2px var(--accent)}.ig-tabs strong{margin-left:6px}.ig-controls{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0}.ig-controls label{display:flex;flex-direction:column;gap:6px;font-size:13px}.ig-controls label:first-child{flex:1;min-width:160px}.ig-controls input,.ig-controls select{padding:9px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit;color:var(--text);background:var(--surface);max-width:100%}.ig-layout{display:grid;grid-template-columns:minmax(0,1fr);gap:18px}.ig-layout:has(.ig-inspector:not([hidden])){grid-template-columns:minmax(0,1fr)}.ig-inspector{grid-row:1}.ig-group,.ig-inspector{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);min-width:0;overflow-wrap:anywhere}.ig-group{margin-bottom:12px}.ig-group h2{font-size:14px;margin:0;padding:12px 16px}.ig-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:14px;padding:14px 16px;border-top:1px solid var(--border-subtle);overflow-wrap:anywhere}.ig-main{min-width:0}.ig-title{font-size:14px;font-weight:600;line-height:1.5;text-align:left}.ig-title:is(button){border:0;padding:0;background:none;color:var(--accent);cursor:pointer;font-family:inherit}.ig-title:is(button):hover{text-decoration:underline}.ig-state{display:flex;flex-direction:column;gap:5px;font-size:12px;line-height:1.5;max-width:170px}.ig-state strong{font-weight:500}.ig-state span{color:var(--text-secondary)}.ig-links{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;align-items:center}.ig-debug{font-size:12px;margin:16px 0}.ig-debug summary{cursor:pointer;font-weight:600}.ig-debug select{display:block;padding:8px;max-width:100%;width:100%;margin:6px 0 10px;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm)}.ig-debug button:disabled{opacity:.45;cursor:default}.ig-review-details{margin-top:12px;font-size:12px}.ig-review-details summary{cursor:pointer}.ig-review-feedback{white-space:pre-wrap;overflow-wrap:anywhere}.ig-summary{font-size:12px;line-height:1.6;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.ig-pages,.ig-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.ig-pages[hidden]{display:none}.ig-pages button:disabled{opacity:.45;cursor:default}.ig-pages{justify-content:flex-start;font-size:12px;margin:18px 0}.ig-inspector{padding:16px;align-self:start}.ig-heading h2{font-size:17px;margin:0}.ig-inspector h3{font-size:14px}.ig-sync{display:flex;flex-direction:column;gap:5px;font-size:12px}.ig-sync progress,.ig-inspector progress{width:100%;height:6px;accent-color:var(--accent)}.ig-steps{display:flex;flex-direction:column;margin:12px 0}.ig-steps button{font:inherit;display:flex;justify-content:space-between;gap:12px;text-align:left;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px;cursor:pointer;font-size:12px;overflow-wrap:anywhere}.ig-steps button[aria-pressed=true]{border-color:var(--accent)}.ig-steps small{display:block;color:var(--text-secondary);margin-top:4px}.ig-log{max-height:400px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.6;background:var(--bg);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);padding:12px}.ig-runners{margin:16px 0;color:var(--text-secondary);font-size:12px}.ig-runners summary{cursor:pointer}.ig-empty{padding:22px;color:var(--text-secondary);font-size:13px}
.ig-controls[hidden]{display:none}.ci-workspace-tabs{position:sticky;top:0;z-index:5;background:var(--bg);padding:6px 0}.ci-workspace-tabs~[role=tabpanel][hidden]{display:none}.ci-pipeline-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:12px 0}.ci-pipeline-success summary{cursor:pointer;font-size:12px;padding:10px 0}.ci-comparison{margin:18px 0;min-width:0}.ci-comparison-scroll{overflow:auto;max-height:320px}.ci-comparison table{width:100%;border-collapse:collapse;font-size:12px}.ci-comparison th,.ci-comparison td{padding:8px;border-bottom:1px solid var(--border);text-align:left;min-width:90px;overflow-wrap:anywhere}.ci-comparison thead th{position:sticky;top:0;background:var(--surface)}.ig-active-pipeline,.ig-comparison-panel{min-width:0}.ci-compare-control{display:flex;flex-direction:column;gap:8px;font-size:12px;margin:16px 0}.ci-compare-control select{max-width:100%;padding:8px;background:var(--surface);color:var(--text);border:1px solid var(--border)}.ig-back-label{display:none}
@media(min-width:1250px){.ig-inspector:has(.ci-comparison){display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,.45fr);gap:18px}.ig-layout:has(.ig-inspector:not([hidden])){grid-template-columns:minmax(280px,.3fr) minmax(0,1fr);align-items:start}.ig-layout>.ig-inspector{grid-row:auto;grid-column:2;position:sticky;top:65px;max-height:calc(100dvh - 100px);overflow:auto}.ig-layout:has(.ig-inspector:not([hidden]))>#ig-results{max-height:calc(100dvh - 100px);overflow:auto;position:sticky;top:65px}.ci-health-pipeline-slot>.ig-inspector{position:sticky;top:65px;max-height:calc(100dvh - 100px);overflow:auto}.ci-health-lower{align-items:start}.ci-health-lower>.ci-health-tree{position:sticky;top:65px;max-height:calc(100dvh - 100px);overflow:auto}}
@media(max-width:700px){[data-ci-focused=true] #ci-workspace-runs #ig-results,[data-ci-focused=true] #ci-workspace-runs>.ig-tabs,[data-ci-focused=true] #ci-workspace-runs>.ig-controls,[data-ci-focused=true] #ci-workspace-runs>.ig-pages,[data-ci-focused=true] [data-health-tree],[data-ci-focused=true] [data-health-map],[data-ci-focused=true] .ci-health-controls,[data-ci-focused=true] .ci-health-layout,[data-ci-focused=true] .ci-health-status,[data-ci-focused=true] .ci-health-selection{display:none}.ig-inspector .ig-heading{position:sticky;top:0;background:var(--surface);z-index:4;padding:8px 0}.ig-back-label{display:inline}.ig-close-label{display:none}.ci-pipeline-controls button{min-height:44px}}
@media(max-width:900px){.ig-layout:has(.ig-inspector:not([hidden])){grid-template-columns:minmax(0,1fr)}.ig-inspector{grid-row:1}}@media(max-width:550px){.ig-row{grid-template-columns:minmax(0,1fr);gap:8px}.ig-state{max-width:none}.ig-tabs button{padding:9px 12px}}
</style>`;
const commandCenterStyles=`<style>
.ci-workspace-tabs{margin:8px 0 20px;padding:5px;background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow)}.ci-workspace-tabs button{border:0!important;border-radius:8px!important;font-weight:650}.ci-workspace-tabs button[aria-selected=true]{background:var(--accent);color:#fff;box-shadow:0 2px 8px color-mix(in srgb,var(--accent) 30%,transparent)}.ig-group,.ig-inspector{box-shadow:var(--shadow)}.ig-inspector{border-top:3px solid var(--accent)}.ci-slot-detail{animation:ci-slot-in .18s ease-out}@keyframes ci-slot-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
</style>`;

export function mountIntegrations(container,client,{kind='ci',tab='current',search='',repository='',provider='',mission='',session=''}={}) {
  const isCi=kind==='ci';
  const tabs=isCi ? [['current','Current'],['failed','Failed'],['finished','Finished']] : [['current','Current'],['changes','Changes requested'],['failed','Failed'],['finished','Finished']];
  const state={tab:tabs.some(([key])=>key===tab)?tab:'current',search,repository,provider,mission,session,page:0,group:'project',branch:'',days:isCi?14:0};
  let related=null,relatedFailed=false,disposed=false,data=null,error='',selected=null,pipeline=null,detailError='',log=null,logError='',step='',actionMessage='';
  let timer=null,clock=null,signature='',detailSignature='',chartSignature='';
  let selectedReports=null,selectedReportsError='';
  let reportData=null,reportsError='',insightsSignature='',failurePage=0,testPage=0,testSearch='';
  let debugSessions=null,debugSessionId='',debugMessage='',debugOperationId='',fixOperationId='';
  let fixAvailable=true,fix=null,fixMessage='',sandboxOperationId='',fixTerminal=null,fixTerminalError='';
  let healthView=null,observabilityView=null,workspace=tab==='current'?'health':'runs',collapseSuccess=false,zoom=1,comparisonKey='',listScroll=0,healthOriginKey='';
  const observedDetails=new Map(),repositoryChoices=new Map();
  let performanceData=null,performanceError='',performanceDays=14;
  const pending=new Map();
  const listenerController=new AbortController();
  const reviewCache=new Map();
  const title=isCi?'CI':'Reviews';
  container.innerHTML=`${styles}${isCi?ciChartStyles+ciInsightStyles+ciPipelineStyles+commandCenterStyles+'<style>'+ciFixStyles+'</style>':''}<h1>${title}</h1><p class="ig-intro">${isCi?'Follow builds from queue to finished steps. Inspect output and open the original run in Woodpecker.':'Review plans and code in Plannotator, with decisions linked to the mission and revision they belong to.'}</p>
    <div data-ig-health role="status"></div><p data-ig-error class="ig-warning" role="status"></p>
    ${isCi?'<div data-ci-observability></div><div data-ci-health></div>':''}
    <div class="ig-tabs" role="tablist" aria-label="${title} outcome">${tabs.map(([key,label])=>`<button type="button" role="tab" id="ig-tab-${key}" data-tab="${key}" aria-controls="ig-results" aria-selected="${state.tab===key}" tabindex="${state.tab===key?0:-1}">${label} <strong data-count="${key}">…</strong></button>`).join('')}</div>
    <div class="ig-controls"><label>Search ${isCi?'runs':'reviews'}<input data-ig-search type="search" value="${esc(search)}" placeholder="Project, repository, branch or revision"></label><label>Group by<select data-ig-group><option value="project">Project / repository</option><option value="none">Most recent</option></select></label></div>
    ${isCi ? '<div class="ig-controls ci-report-filters"><label>Repository<select data-ci-repository><option value="">All repositories</option></select></label><label>Branch<select data-ci-branch><option value="">All branches</option></select></label><label>Period<select data-ci-period><option value="7">Last 7 days</option><option value="14" selected>Last 14 days</option><option value="30">Last 30 days</option></select></label></div><div data-ci-insights></div>' : ''}
    ${repository || mission || session ? `<p class="ig-note">Filtered to ${filterLink(repository,mission,session)} · <a href="#/${kind}">Clear context</a></p>` : ''}
    ${isCi ? '<details class="ci-charts" data-ci-charts><summary>Run durations · project, language & cache health</summary><div class="ig-controls ci-cache-controls"><label>Cache and trend window<select data-ci-performance-window><option value="7">Last 7 days</option><option value="14" selected>Last 14 days</option></select></label><p class="ci-chart-note">Cache and regression windows use an independent 7 or 14 days; the main period controls run and test history.</p></div><div class="ci-charts-body" data-ci-charts-body></div></details>' : ''}
    <p class="ig-note" data-ig-count></p><div class="ig-layout"><section id="ig-results" role="tabpanel" aria-labelledby="ig-tab-${state.tab}"></section><aside class="ig-inspector" aria-label="Pipeline inspector" hidden></aside></div>
    <div class="ig-pages"><button type="button" class="hd-btn" data-page="previous">Previous</button><span data-ig-page></span><button type="button" class="hd-btn" data-page="next">Next</button></div><div data-ig-runners></div>${!isCi ? '<details class="ig-runners"><summary>Start a review</summary><p>Start Plannotator from the project workspace with <code>herdr-plannotator --help</code>. Its review link and upstream decision appear here when the launcher is connected to this control plane.</p></details>' : ''}`;
  const el=selector=>container.querySelector(selector);
  if(isCi){
    healthView=mountCiHealth(el('[data-ci-health]'),client,{repository,mission,session,search:search || undefined,
      onContext:context=>{const next={days:['7','14','30'].includes(context.mode)?Number(context.mode):null,mode:['runs','hours'].includes(context.mode)?context.mode:'days',run_number:context.run_number,scope:context.scope,start:context.start,end:context.end,branch:context.branch,search:context.search,failuresOnly:context.failuresOnly,project:context.project};observabilityView?.setContext(next);if(observabilityView)applyCiContext(next);},
      onSelectRun:record=>openCiRun(record),
      onSelectStep:(record,stepId)=>{
        if(selected?.id!==ciRunKey(record))openCiRun(record);
        pending.get('log')?.abort();pending.delete('log');step=String(stepId);log=null;logError='';draw();refreshDetail();
      }});
    const slot=el('[data-health-pipeline-slot]');
    if(slot)slot.append(el('.ig-inspector'));
    if(typeof client.getCiSummary==='function') {
      observabilityView=mountCiObservability(el('[data-ci-observability]'),client,{repository,provider,mission,session,search:search||undefined,
        onContext:context=>{
          healthView?.setObservabilityContext(context);
          applyCiContext(context);
        },
        onSelectRun:record=>{
          workspace='health';healthView?.setLayout('pipeline');openCiRun(record);
        }});
      const initial=observabilityView.getContext();
      healthView.setObservabilityContext(initial);applyCiContext(initial,{render:false});
    }
  }
  if(isCi){
    const panels={};
    const nav=document.createElement('div');nav.className='ig-tabs ci-workspace-tabs';nav.setAttribute('role','tablist');nav.setAttribute('aria-label','CI workspace');
    nav.innerHTML=['health','runs','tests'].map((key,index)=>`<button type="button" role="tab" data-ci-workspace="${key}" id="ci-workspace-tab-${key}" aria-controls="ci-workspace-${key}" aria-selected="${index===0}" tabindex="${index===0?0:-1}">${key[0].toUpperCase()+key.slice(1)}</button>`).join('');
    el('[data-ci-health]').before(nav);
    for(const key of ['health','runs','tests']){const panel=document.createElement('section');panel.id=`ci-workspace-${key}`;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',`ci-workspace-tab-${key}`);panel.hidden=key!=='health';panels[key]=panel;container.append(panel);}
    panels.health.append(el('[data-ci-health]'));
    for(const selector of ['.ig-tabs:not(.ci-workspace-tabs)','.ig-controls:not(.ci-report-filters)','.ci-report-filters','[data-ci-insights]','[data-ci-charts]','[data-ig-count]','.ig-layout','.ig-pages','[data-ig-runners]']){
      const node=el(selector);if(!node)continue;if(selector==='.ci-report-filters'){nav.after(node);continue;}panels[selector==='[data-ci-insights]'?'tests':'runs'].append(node);
    }
    nav.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const keys=['health','runs','tests'],i=keys.indexOf(workspace);workspace=keys[event.key==='Home'?0:event.key==='End'?2:(i+(event.key==='ArrowRight'?1:2))%3];event.preventDefault();draw();el(`[data-ci-workspace="${workspace}"]`).focus();},{signal:listenerController.signal});
  }
  function applyCiContext(context,{render=true}={}) {
    const changed=state.branch!==context.branch||Number(state.days)!==context.days;
    state.project=context.project||null;
    state.repository=context.project?context.project.repository:(context.repository??repository);
    state.provider=context.project?.provider_url||provider;
    state.branch=context.branch;state.days=context.days;state.mode=context.mode;state.run_number=context.run_number;state.scope=context.scope;state.start=context.start;state.end=context.end;state.search=context.search;state.page=0;
    el('[data-ig-search]').value=state.search;
    el('[data-ci-period]').value=state.days==null?'':String(state.days);
    if(render){if(changed)refreshPerformance(true);draw();}
  }
  const reviewVersion=record=>JSON.stringify([record.updated_at,record.status,record.decision]);
  const ciObservations=({scoped=true}={})=>{
    const recent=Object.entries(data?.pipelines||{}).flatMap(([repository,runs])=>records(runs).map(r=>({...r,repository:r.repository||repository})));
    const matches=r=>!scoped||!state.project||ciProjectKey(r)===ciProjectKey(state.project);
    const repos=new Map([...records(data?.repos),...records(reportData?.repos)].map(p=>[ciProjectKey({...p,provider_repo_id:p.provider_repo_id??p.id}),p]));
    const map=new Map();for(const r of [...records(reportData?.pipelines),...recent].filter(matches)){const repo=repos.get(ciProjectKey(r));map.set(JSON.stringify([ciRunKey(r),r.repository]),{...r,default_branch:repo?.default_branch??r.default_branch});}
    return {...reportData,reports:records(reportData?.reports).filter(matches),pipelines:[...map.values()],stale:!!data?.stale,partial:data?.status==='partial'};
  };
  const entries=()=>integrationEntries(isCi ? {pipelines:{retained:ciObservations().pipelines}} : kind==='reviews' && data ? {...data,reviews:records(data.reviews).map(summary=>{
    const cached=reviewCache.get(String(summary.review_id));
    return cached && cached.version===reviewVersion(summary) ? {...cached.record,...summary,_detailsError:cached.error,_detailsLoaded:!!cached.record} : summary;
  })} : data||{},kind);
  const entryKey=node=>node.closest('[data-entry]')?.dataset.entry;
  let chartsDrawn=false,motionSettled=false,motionView='';
  // Panels are morphed in place: morph keeps a <details> open, never moves a node holding focus or
  // a text selection, and reuses unchanged nodes (so scroll and form state persist). A node the user
  // is in that leaves the new markup is handed to its equivalent control (or the review summary / a
  // link with the same href) instead of dropping focus.
  // The results list and inspector are rebuilt with innerHTML (not morphed): the review rows carry
  // their disclosure open state dynamically, and the visibility handler re-fetches a review detail on
  // every toggle dispatch for an open disclosure. Rebuilding preserves that interaction exactly. The
  // signature gate already prevents any DOM write on a quiet poll; the other panels (health, insights,
  // runners, filters) are morphed.
  function replace(element,html) {
    const focused=document.activeElement;
    const focusKey=element.contains(focused) ? ['data-run','data-step','data-close-inspector','data-ci-action','data-ci-test-search','data-ci-test-page','data-collapse-success','data-pipeline-zoom','data-ci-compare','data-fix-sandbox','data-fix-cancel','data-fix-terminal','data-fix-copy'].find(k=>focused.hasAttribute(k)) : null;
    const focusValue=focusKey ? focused.getAttribute(focusKey) : null;
    const reviewFocus=element.contains(focused) && focused.tagName==='SUMMARY' ? focused.closest('[data-review-details]')?.dataset.reviewDetails : null;
    const linkFocus=element.contains(focused) && focused.tagName==='A' ? focused.getAttribute('href') : null;
    const debugOpen=element.querySelector('[data-debug-disclosure]')?.open;
    const debugFocus=element.contains(focused) && focused.closest('[data-debug-disclosure]') ? (focused.hasAttribute('data-debug-session')?'select':focused.tagName==='SUMMARY'?'summary':focused.hasAttribute('data-debug-send')?'button':null) : null;
    const openReviews=new Set([...element.querySelectorAll('[data-review-details][open]')].map(e=>e.dataset.reviewDetails));
    const testInput=element.querySelector('[data-ci-test-search]'),selection=testInput&&document.activeElement===testInput?[testInput.selectionStart,testInput.selectionEnd]:null;
    const paneScroll=element.scrollTop;
    const graphScroll=[...element.querySelectorAll('.ci-pipeline-scroll')].map(node=>[node.scrollLeft,node.scrollTop]);
    const successOpen=[...element.querySelectorAll('.ci-pipeline-success')].map(node=>node.open);
    const logElement=element.querySelector('.ig-log');
    const logScroll=logElement?.scrollTop;
    const atEnd=logElement && logElement.scrollHeight-logElement.clientHeight-logElement.scrollTop<8;
    element.innerHTML=html;
    for(const review of element.querySelectorAll('[data-review-details]'))review.open=openReviews.has(review.dataset.reviewDetails);
    if(focusKey)[...element.querySelectorAll(`[${focusKey}]`)].find(e=>e.getAttribute(focusKey)===focusValue)?.focus({preventScroll:true});
    const debug=element.querySelector('[data-debug-disclosure]');if(debug && debugOpen)debug.open=true;
    if(debugFocus)debug?.querySelector(debugFocus)?.focus({preventScroll:true});
    if(reviewFocus)[...element.querySelectorAll('[data-review-details]')].find(e=>e.dataset.reviewDetails===reviewFocus)?.querySelector('summary')?.focus({preventScroll:true});
    if(linkFocus)[...element.querySelectorAll('a')].find(e=>e.getAttribute('href')===linkFocus)?.focus({preventScroll:true});
    if(selection){const input=element.querySelector('[data-ci-test-search]');input?.setSelectionRange(...selection);}
    element.scrollTop=paneScroll;
    [...element.querySelectorAll('.ci-pipeline-scroll')].forEach((node,i)=>{if(graphScroll[i]){node.scrollLeft=graphScroll[i][0];node.scrollTop=graphScroll[i][1];}});
    [...element.querySelectorAll('.ci-pipeline-success')].forEach((node,i)=>node.open=!!successOpen[i]);
    const nextLog=element.querySelector('.ig-log');
    if(nextLog && logScroll!=null)nextLog.scrollTop=atEnd?nextLog.scrollHeight:logScroll;
  }
  function tick() {
    for(const element of container.querySelectorAll('[data-integration-time]')) element.textContent=formatDuration(Math.max(0,Date.now()-Number(element.dataset.integrationTime)));
  }
  function draw() {
    if(disposed)return;
    el('[data-ig-error]').textContent=error;
    const health=integrationHealth(data,kind);
    const healthElement=el('[data-ig-health]');
    const healthKey=JSON.stringify([data?.status,data?.refreshing,data?.progress,data?.last_success_at,data?.checked_at,data?.error,data?.stale,data?.repo_errors,data?.project_mapping_error]);
    if(healthElement.dataset.signature!==healthKey){healthElement.dataset.signature=healthKey;morph(healthElement,health);}
    const all=entries(),filtered=selectIntegrations(all,state),pageSize=20;
    state.page=Math.min(state.page,Math.max(0,Math.ceil(filtered.length/pageSize)-1));
    // Clock advance can change membership even when every provider payload is unchanged.
    const windowMembership=isCi?all.map(entry=>ciRunInWindow(entry.record,state)):null;
    const nextSignature=JSON.stringify([state,data,reportData,performanceData,performanceError,performanceDays,windowMembership,[...reviewCache]]);
    if(isCi) {
      const observations=ciObservations();
      const model=ciInsightsModel(observations,state);
      const html=renderCiInsights(model,{loading:reportData===null&&!reportsError,error:reportsError,failurePage});
      if(insightsSignature!==html){
        const host=el('[data-ci-insights]');
        const focus=[...host.querySelectorAll('details')].findIndex(d=>d.querySelector('summary')===document.activeElement);
        const stranded=morph(host,html);
        // morph keeps an open disclosure open; only re-focus if a focused summary left the tree.
        if(focus>=0 && !host.contains(document.activeElement))[...host.querySelectorAll('details')][focus]?.querySelector('summary')?.focus({preventScroll:true});
        handOff(stranded,()=>null);
        insightsSignature=html;
      }
      for(const [selector,field,label] of [['[data-ci-repository]','repository','All repositories'],['[data-ci-branch]','branch','All branches']]){
        const select=el(selector),values=new Map();
        if(field==='repository')repositoryChoices.clear();
        const inventory=ciObservations({scoped:false});
        for(const r of [...inventory.pipelines,...records(inventory.reports),...records(performanceData?.cache_groups),...records(performanceData?.execution_trends),...(state.project?[state.project]:[])]) {
          const exact=!!r.provider_url&&r.provider_repo_id!=null&&!!r.repository;
          const value=field==='repository'?(exact?ciProjectKey(r):String(r.provider_repo_id??r.repository_id??r.repository??'')):text(r.branch);
          if(value){values.set(value,field==='repository'?`${r.repository||value}${exact?` · ${r.provider_url} #${r.provider_repo_id}`:` (${value})`}`:value);if(field==='repository')repositoryChoices.set(value,exact?{provider_url:r.provider_url,provider_repo_id:String(r.provider_repo_id),repository:r.repository}:null);}
        }
        const selectedValue=field==='repository'&&state.project?ciProjectKey(state.project):state[field];
        if(selectedValue)values.set(selectedValue,values.get(selectedValue)||selectedValue);
        const options=`<option value="">${label}</option>`+[...values].sort((a,b)=>a[1].localeCompare(b[1])).map(([value,label])=>`<option value="${esc(value)}">${esc(label)}</option>`).join('');
        if(select.innerHTML!==options){morph(select,options);select.value=selectedValue;}
      }
    }
    if(signature!==nextSignature) {
      signature=nextSignature;
      // Rows and charts are morphed (Task 19 patcher); motion pairs old and new nodes by key:
      // tab counts by node, rows by data-entry (state change = flash).
      // Motion only for data-driven changes: not before the reports have landed (CI counts include
      // retained observations) and not when the user changed the tab, a filter, the search or page.
      const settled=!!data&&(!isCi||reportData!==null||!!reportsError), view=JSON.stringify(state);
      const live=settled&&motionSettled&&view===motionView;
      motionSettled=settled;motionView=view;
      const tabCounts=live?snapshotText(container,'[data-tab] strong'):null;
      const rowStates=live?snapshotText(el('#ig-results'),'[data-entry] .ig-state strong',entryKey):null;
      for(const [key] of tabs) {
        const button=el(`[data-tab="${key}"]`),active=state.tab===key;
        button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;
        button.querySelector('strong').textContent=data ? all.filter(e=>selectIntegrations([e],{...state,tab:key,search:''}).length).length : '…';
      }
      if(isCi) {
        const chart=el('[data-ci-charts-body]');
        const html=data ? renderCiCharts(ciChartModel(selectIntegrations(all,{...state,tab:'all'}),{data:state.project&&performanceData?{...performanceData,...Object.fromEntries(['cache_groups','execution_trends','alerts'].map(key=>[key,records(performanceData[key]).filter(r=>ciProjectKey(r)===ciProjectKey(state.project))]))}:performanceData,filters:state,loading:performanceData===null&&!performanceError&&typeof client.getCiPerformance==='function',error:performanceError}),{
          retained:!!reportData,days:state.days,scope:reportData?.scope,stale:!!data.stale,partial:data.status==='partial'||Object.keys(data.repo_errors || {}).length>0,
        }) : '<p class="ci-chart-note">Waiting for CI reports…</p>';
        if(chartSignature!==html) {
          chartSignature=html;
          // Charts are rebuilt (not morphed): data-drawn marks must be cleared on later paints so
          // only the first paint draws in. The chartSignature gate already avoids quiet-poll writes.
          const disclosure=chart.querySelector('[data-ci-chart-data]');
          const open=disclosure?.open, focused=disclosure?.querySelector('summary')===document.activeElement;
          const focusedTable=chart.contains(document.activeElement)&&document.activeElement.classList.contains('ci-chart-table')?document.activeElement.getAttribute('aria-label'):null;
          chart.innerHTML=html;
          // Charts draw in on their first paint only; later refreshes replace them without motion.
          if(!chartsDrawn&&chart.querySelector('svg')){chartsDrawn=true;for(const mark of chart.querySelectorAll('.ci-chart-card svg :is(rect,polyline,circle)'))mark.setAttribute('data-drawn','');}
          const next=chart.querySelector('[data-ci-chart-data]');
          if(next && open)next.open=true;
          if(focused)next?.querySelector('summary')?.focus({preventScroll:true});
          if(focusedTable)[...chart.querySelectorAll('.ci-chart-table')].find(t=>t.getAttribute('aria-label')===focusedTable)?.focus({preventScroll:true});
        }
      }
      const groups=new Map();
      for(const entry of filtered.slice(state.page*pageSize,(state.page+1)*pageSize)) {
        const group=state.group==='project'?entry.project:'Most recent';
        if(!groups.has(group))groups.set(group,[]);groups.get(group).push(entry);
      }
      const result=el('#ig-results');result.setAttribute('aria-labelledby',`ig-tab-${state.tab}`);
      replace(result,[...groups].map(([name,rows])=>`<div class="ig-group"><h2>${esc(name)}</h2>${rows.map(e=>integrationRow(e,kind)).join('')}</div>`).join('') || `<div class="ig-group ig-empty">${!data ? 'Waiting for reports…' : isCi && data.available===false && !all.length ? 'No CI reports available. Connection details are shown above.' : `No ${state.tab==='current'?'current ':''}${isCi?'runs':'reviews'} match this view.`}</div>`);
      animateChanges(container,'[data-tab] strong',tabCounts);
      animateChanges(result,'[data-entry] .ig-state strong',rowStates,entryKey,{count:false,target:node=>node.closest('[data-entry]')});
      el('[data-ig-count]').textContent=data ? `${filtered.length} matching ${isCi?'runs':'reviews'}${isCi?reportData?' in retained observations':' in the recent pipeline window':data.has_more?` · Showing the latest ${records(data.reviews).length}${Number.isFinite(data.total)?` of ${data.total}`:''} reviews; older records are outside this window`:''}` : '';
      el('.ig-pages').hidden=filtered.length<=pageSize;
      el('[data-ig-page]').textContent=`Page ${state.page+1} of ${Math.max(1,Math.ceil(filtered.length/pageSize))}`;
      el('[data-page="previous"]').disabled=state.page===0;el('[data-page="next"]').disabled=(state.page+1)*pageSize>=filtered.length;
      if(isCi) {
        const runners=records(data?.agents);
        // morph keeps the runner-capacity disclosure open across refreshes.
        morph(el('[data-ig-runners]'),`<details class="ig-runners"><summary>Runner capacity · ${runners.length ? `${runners.length} reported` : 'not available'}</summary>${data?.agents_error ? `<p>${esc(short(data.agents_error,300))}</p>` : ''}${runners.map(r=>`<p>${esc(short(r.name)||'Unnamed runner')} · ${esc(short(r.platform))}${r.capacity!=null ? ` · Capacity ${esc(r.capacity)}` : ''}</p>`).join('') || '<p>The provider has not supplied runner capacity. Repository runs can still be inspected.</p>'}</details>`);
      }
    }
    const inspector=el('.ig-inspector');inspector.hidden=!selected;
    if(isCi){
      for(const key of ['health','runs','tests']){el(`#ci-workspace-${key}`).hidden=workspace!==key;const button=el(`[data-ci-workspace="${key}"]`);button.setAttribute('aria-selected',String(workspace===key));button.tabIndex=workspace===key?0:-1;}
      const target=workspace==='runs'?el('.ig-layout'):el('[data-health-pipeline-slot]');if(target && inspector.parentNode!==target)target.append(inspector);
      container.dataset.ciFocused=selected?'true':'false';
      el('.ci-report-filters').hidden=workspace==='health';
      el('[data-ci-insights]').hidden=false;el('[data-ci-charts]').hidden=false;
    }
    if(selected) {
      const runReports=ciSelectedRunReports(selectedReports?.reports,ciObservations().pipelines,pipeline);
      const runReportHtml=selectedReports===null?`<p class="ig-note" role="status">${esc(selectedReportsError||'Loading test reports for this run…')}</p>`:`${selectedReportsError?`<p class="ig-warning" role="status">${esc(selectedReportsError)} Showing retained test evidence (stale).</p>`:''}${renderCiRunReports(runReports,{search:testSearch,page:testPage})}`;
      const candidates=ciObservations().pipelines.filter(record=>sameCiRepository(pipeline,record)&&ciRunKey(record)!==selected.id).slice(0,100);
      const compared=candidates.find(record=>ciRunKey(record)===comparisonKey);
      const comparison=compared?ciComparisonModel(pipeline,{...observedDetails.get(comparisonKey),...compared,tasks:compared.tasks??observedDetails.get(comparisonKey)?.tasks}):null;
      const compareHtml=`<label class="ci-compare-control">Compare observed run<select data-ci-compare aria-label="Compare observed run"><option value="">No comparison</option>${candidates.map(record=>`<option value="${esc(ciRunKey(record))}" ${ciRunKey(record)===comparisonKey?'selected':''}>Run #${esc(record.run_number)} · ${esc(record.state||record.status||'Unknown')} · ${esc(record.branch||'Branch not reported')}</option>`).join('')}</select></label>${comparisonKey&&!compared?'<p class="ig-note">Compared run is no longer in retained observations.</p>':''}${renderCiComparison(comparison)}`;
      const next=JSON.stringify([collapseSuccess,zoom,compareHtml,testSearch,testPage,runReportHtml,pipeline,detailError,log,logError,step,actionMessage,debugSessions,debugSessionId,debugMessage,fixAvailable,fix,fixMessage,fixTerminal,fixTerminalError,related,relatedFailed,fixIsActive(fix)?Math.floor(Date.now()/5000):0]);
      if(detailSignature!==next){detailSignature=next;replace(inspector,'<div class="ig-active-pipeline">'+renderPipeline(pipeline,{loading:true,error:detailError,log,logError,step,actionMessage,debugSessions,debugSessionId,debugMessage,collapseSuccess,zoom,fixHtml:renderCiFix({available:fixAvailable,fix,message:fixMessage,terminal:fixTerminal,terminalError:fixTerminalError}),relatedHtml:related||relatedFailed?renderRelated(related,{error:relatedFailed?'failed':null}):''})+runReportHtml+'</div><div class="ig-comparison-panel">'+compareHtml+'</div>');healthView?.selectedRun(pipeline);}
    }
    tick();
  }
  async function request(key,fn,accept,onError) {
    if(disposed || document.hidden || pending.has(key))return;
    const controller=new AbortController();pending.set(key,controller);
    let timeout=false;const deadline=setTimeout(()=>{timeout=true;controller.abort();},15000);
    try {const response=await fn(controller.signal);if(!disposed && !controller.signal.aborted)accept(response);}
    catch(err){if(!disposed && (!controller.signal.aborted || timeout))onError(err);}
    finally {clearTimeout(deadline);if(pending.get(key)===controller)pending.delete(key);draw();}
  }
  function refreshRelated() {
    if(!selected || typeof client.related!=='function')return;
    const key=selected.id,id=`${selected.providerId}:${selected.number}`;
    request('related',signal=>client.related('ci',id,{signal}),response=>{if(selected?.id===key){related=response;relatedFailed=false;}},err=>{if(selected?.id===key){related=err?.status===404?{relations:{}}:null;relatedFailed=err?.status!==404;}});
  }
  function refreshDetail() {
    if(!selected)return;
    if(!selected.providerUrl){detailError='Exact CI provider identity is not reported. Live details, logs and actions are unavailable for this retained run.';draw();return;}
    const target={...selected},key=target.id,selectedStep=step;
    if(typeof client.getCiReports==='function')request('run-reports',signal=>client.getCiReports({signal,provider_url:target.providerUrl,provider_repo_id:target.providerId,repository:target.repository,run_number:target.number}),response=>{
      if(selected?.id!==key || selected?.repository!==target.repository)return;
      if(!Array.isArray(response?.reports))throw new Error('Invalid run report response');
      selectedReports=response;selectedReportsError='';
    },()=>{if(selected?.id===key)selectedReportsError='Test reports could not refresh.';});
    request('pipeline',signal=>client.getCiPipeline(target.providerId,target.number,{signal,providerUrl:target.providerUrl}),response=>{
      if(selected?.id!==key)return;
      if(!response?.pipeline)throw new Error('Pipeline response is unavailable');
      if(response.provider_url && canonicalProvider(response.provider_url)!==target.providerUrl)throw new Error('Pipeline provider identity mismatch');
      const reportedProvider=JSON.parse(ciRunKey(response.pipeline))[0];
      if(reportedProvider && reportedProvider!==target.providerUrl)throw new Error('Pipeline provider identity mismatch');
      const next=pipelineDetails(response.pipeline,{...pipeline,repository:target.repository,run_number:target.number});
      if(ciRunKey(next)!==key)throw new Error('Pipeline identity mismatch');
      pipeline={...next,diagnosis:response.diagnosis??null};detailError='';observedDetails.set(key,pipeline);if(observedDetails.size>30)observedDetails.delete(observedDetails.keys().next().value);
    },()=>{if(selected?.id===key)detailError='Pipeline details could not refresh.';});
    if(fixAvailable && integrationOutcome(pipeline)==='failed' && typeof client.listCiFix==='function'){
      const run=fixRunKey(target.providerId,target.number);
      request('fix',signal=>client.listCiFix({run,signal}),response=>{
        if(selected?.id!==key)return;
        const latest=latestFix(response);
        if(latest && (!fix || fix.id!==latest.id)){fixTerminal=null;fixTerminalError='';}
        fix=latest;
      },err=>{if(selected?.id===key && err?.status===404){fixAvailable=false;fix=null;}});
    }
    if(selectedStep)request('log',signal=>client.getCiLog(target.providerId,target.number,selectedStep,{signal,providerUrl:target.providerUrl}),response=>{
      if(response?.provider_url && canonicalProvider(response.provider_url)!==target.providerUrl)throw new Error('Log provider identity mismatch');
      if(selected?.id===key && step===selectedStep){log=response;logError='';}
    },()=>{if(selected?.id===key && step===selectedStep)logError='Step output could not refresh.';});
  }
  function refreshPerformance(reset=false) {
    if(!isCi || typeof client.getCiPerformance!=='function')return;
    if(reset){pending.get('performance')?.abort();pending.delete('performance');performanceData=null;performanceError='';}
    request('performance',signal=>client.getCiPerformance({days:performanceDays,branch:state.branch||undefined,signal}),response=>{
      if(response?.schema_version!==1 || response.days!==performanceDays || !Array.isArray(response.cache_groups) || !Array.isArray(response.execution_trends) || !Array.isArray(response.alerts))throw new Error('Invalid CI performance response');
      performanceData=response;performanceError='';
    },()=>{performanceError='Cache health could not refresh.';});
    if(reset)draw();
  }
  function refresh() {
    refreshPerformance();
    if(isCi && typeof client.getCiReports==='function')request('reports',signal=>client.getCiReports({signal,compact:true}),response=>{
      if(!Array.isArray(response?.reports)||!Array.isArray(response?.pipelines))throw new Error('Invalid CI history response');
      reportData=response;reportsError='';
    },()=>{reportsError=`CI history could not refresh.${reportData?' Showing last received reports.':''}`;});
    request('list',signal=>isCi ? client.getCi({signal}) : client.listReviews({signal}),response=>{
      if(!response || (isCi ? typeof response.available!=='boolean' : !Array.isArray(response.reviews)))throw new Error('Invalid integration response');
      data=response;error='';
    },()=>{error=`${title} could not refresh.${data?' Showing last received records.':' Retrying automatically.'}`;});
    refreshDetail();
  }
  function cancelDetail() {for(const key of ['pipeline','log','debug-sessions','fix','related','run-reports']){pending.get(key)?.abort();pending.delete(key);}}
  function focusInspector() {
    const inspector=el('.ig-inspector');inspector.querySelector('[data-close-inspector]')?.focus({preventScroll:true});
    inspector.scrollIntoView({block:'nearest',behavior:'smooth'});
  }
  function openCiRun(record){
    const entry=integrationEntries({pipelines:{history:[record]}})[0];
    if(!entry)return;
    if(selected?.id===entry.id&&selected.repository===record.repository){draw();focusInspector();return;}
    listScroll=window.scrollY;healthOriginKey=document.activeElement?.dataset.healthRun||healthOriginKey;testPage=0;testSearch='';comparisonKey='';cancelDetail();selectedReports=null;selectedReportsError='';
    selected={id:entry.id,repository:record.repository,providerId:record.provider_repo_id || record.repository,number:record.run_number,providerUrl:JSON.parse(entry.id)[0]};
    pipeline=selected.providerUrl?record:{...record,can_retry:false,can_cancel:false};debugSessions=null;debugSessionId='';debugMessage='';debugOperationId='';fixOperationId='';fix=null;fixMessage='';sandboxOperationId='';fixTerminal=null;fixTerminalError='';step='';log=null;logError='';detailError='';actionMessage='';detailSignature='';related=null;relatedFailed=false;
    // Start lazy I/O before rendering the synchronized run/health views.
    refreshDetail();refreshRelated();draw();focusInspector();
  }
  container.addEventListener('click',async event=>{
    if(disposed)return;
    const button=event.target.closest('button');if(!button)return;
    if(button.hasAttribute('data-ci-workspace')){workspace=button.dataset.ciWorkspace;draw();return;}
    if(button.hasAttribute('data-collapse-success')){collapseSuccess=!collapseSuccess;draw();return;}
    if(button.hasAttribute('data-pipeline-zoom')){zoom=pipelineZoom(button.dataset.pipelineZoom==='reset'?1:Math.round((zoom+(button.dataset.pipelineZoom==='in'?.25:-.25))*100)/100);draw();return;}
    if(button.dataset.ciTestPage){testPage=Math.max(0,testPage+(button.dataset.ciTestPage==='next'?1:-1));draw();}
    if(button.dataset.ciFailurePage){failurePage=Math.max(0,failurePage+(button.dataset.ciFailurePage==='next'?1:-1));draw();el(`[data-ci-failure-page="${button.dataset.ciFailurePage}"]`)?.focus();}
    if(button.dataset.tab){state.tab=button.dataset.tab;state.page=0;draw();}
    if(button.dataset.page){state.page+=button.dataset.page==='next'?1:-1;draw();}
    if(button.hasAttribute('data-run')) {testPage=0;testSearch='';
      const entry=entries().find(e=>e.id===button.dataset.run);if(!entry)return;
      healthView?.setLayout?.('pipeline');openCiRun(entry.record);
    }
    if(button.hasAttribute('data-close-inspector')){const lastRunId=selected?.id;cancelDetail();selected=null;pipeline=null;healthView?.selectedRun(null);healthView?.setLayout('split');draw();if(isCi && workspace==='health')[...container.querySelectorAll('[data-health-run]')].find(button=>button.dataset.healthRun===healthOriginKey)?.focus({preventScroll:true});window.scrollTo({top:listScroll});if(isCi && workspace==='runs')[...container.querySelectorAll('[data-run]')].find(button=>button.dataset.run===lastRunId)?.focus({preventScroll:true});}
    if(button.hasAttribute('data-step')){pending.get('log')?.abort();pending.delete('log');step=button.dataset.step;log=null;logError='';draw();refreshDetail();}
    if(button.hasAttribute('data-fix-sandbox') && selected?.providerUrl && !pending.has('fix-start')){
      const target={...selected};
      if(!sandboxOperationId)sandboxOperationId=newOperationId();
      const operationId=sandboxOperationId;fixMessage='Requesting a sandbox fix…';draw();
      request('fix-start',signal=>client.ciFix(target.providerId,target.number,operationId,{signal,providerUrl:target.providerUrl}),response=>{
        if(selected?.id!==target.id)return;
        fix=response;fixMessage='';sandboxOperationId='';fixTerminal=null;fixTerminalError='';
      },err=>{
        if(selected?.id!==target.id)return;
        if(err?.status===404)fixAvailable=false;
        fixMessage=fixError(err);
      });
      return;
    }
    if(button.hasAttribute('data-fix-cancel') && fix && !pending.has('fix-cancel')){
      const id=fix.id;
      request('fix-cancel',signal=>client.cancelCiFix(id,{signal}),response=>{if(fix?.id===id){fix=response;fixMessage='';}},err=>{if(fix?.id===id)fixMessage=err?.status===403?'Cancelling requires operator access.':'Cancel was not confirmed; try again.';});
      return;
    }
    if(button.hasAttribute('data-fix-terminal') && fix?.session_id && !pending.has('fix-terminal')){
      const id=fix.id,sessionId=fix.session_id;
      request('fix-terminal',()=>client.getTerminalAttach(sessionId),response=>{if(fix?.id===id){fixTerminal=response;fixTerminalError='';}},err=>{if(fix?.id===id){fixTerminal=null;fixTerminalError=attachError(err);}});
      return;
    }
    if(button.hasAttribute('data-fix-copy') && fixTerminal){
      const form=attachForms(fixTerminal)[Number(button.dataset.fixCopy)];
      if(form){
        try{await navigator.clipboard.writeText(form.command);}catch{const area=document.createElement('textarea');area.value=form.command;document.body.appendChild(area);area.select();document.execCommand('copy');area.remove();}
        const label=button.textContent;button.textContent='Copied';setTimeout(()=>{button.textContent=label;},1200);
      }
      return;
    }
    if((button.hasAttribute('data-debug-send') || button.hasAttribute('data-debug-fix')) && selected?.providerUrl && debugSessionId && !pending.has('debug')) {
      const fix=button.hasAttribute('data-debug-fix'),target={...selected},sessionId=debugSessionId;
      if(fix){if(!fixOperationId)fixOperationId=newOperationId();}else if(!debugOperationId)debugOperationId=newOperationId();
      const operationId=fix?fixOperationId:debugOperationId,noun=fix?'fix request':'debugging context';debugMessage=`Sending ${noun} to Pi…`;draw();
      request('debug',signal=>client.debugCi(target.providerId,target.number,sessionId,operationId,{signal,providerUrl:target.providerUrl,...(fix?{mode:'fix'}:{})}),response=>{
        if(selected?.id!==target.id || debugSessionId!==sessionId)return;
        debugMessage=response.status==='delivered'?`The ${noun} was delivered to the selected Pi agent.`:response.status==='queued'?`The ${noun} is queued for delivery.`:response.error || `Pi did not receive the ${noun}.`;
      },err=>{if(selected?.id===target.id && debugSessionId===sessionId)debugMessage=err.status===403?'Sending to Pi requires operator access.':'Delivery unconfirmed; retry checks the same operation.';});
    }
    if(button.dataset.ciAction && selected?.providerUrl && !pending.has('action')) {
      const target={...selected};actionMessage='Sending action to Woodpecker…';draw();
      request('action',signal=>client.ciAction(target.providerId,target.number,button.dataset.ciAction,{signal,providerUrl:target.providerUrl}),()=>{if(selected?.id===target.id)actionMessage='Action accepted by Woodpecker.';refresh();},err=>{if(selected?.id===target.id)actionMessage=err.status===403?'This action requires operator access.':'Woodpecker did not accept the action.';});
    }
  },{signal:listenerController.signal});
  container.addEventListener('toggle',event=>{
    if(disposed)return;
    if(!event.target.open){const cached=reviewCache.get(event.target.dataset.reviewDetails);if(cached?.error)reviewCache.delete(event.target.dataset.reviewDetails);return;}
    if(event.target.hasAttribute?.('data-review-details')) {
      const id=event.target.dataset.reviewDetails,summary=records(data?.reviews).find(r=>String(r.review_id)===id);
      if(!summary || summary.plan || summary.feedback)return;
      const version=reviewVersion(summary),cached=reviewCache.get(id);
      if(cached?.version===version && (cached.record || cached.loading || cached.error))return;
      if(pending.has(`review:${id}`))return;
      if(!reviewCache.has(id) && reviewCache.size>=30)reviewCache.delete(reviewCache.keys().next().value);
      const cache={version,record:null,error:'',loading:true};reviewCache.set(id,cache);
      request(`review:${id}`,signal=>client.getReview(id,{signal}),response=>{
        if(reviewCache.get(id)!==cache)return;
        const detail=response?.review || response;
        if(!detail || String(detail.review_id)!==id)throw new Error('Invalid review detail');
        cache.record=detail;cache.loading=false;cache.error='';
      },()=>{if(reviewCache.get(id)===cache){cache.error='Review details could not load.';cache.loading=false;}});
      return;
    }
    if(!event.target.hasAttribute?.('data-debug-disclosure') || debugSessions!==null || !selected)return;
    const key=selected.id;debugMessage='Loading active Pi agents…';
    request('debug-sessions',signal=>client.listSessions(undefined,{signal}),response=>{
      if(selected?.id!==key)return;
      debugSessions=activePiSessions(response.sessions);debugMessage='';{const onBranch=selected?.branch?debugSessions.filter(x=>x.branch===selected.branch):[];if(!debugSessionId && onBranch.length===1){debugSessionId=onBranch[0].session_id;debugOperationId='';fixOperationId='';}}
    },()=>{if(selected?.id===key){debugSessions=[];debugMessage='Pi agents could not load. Reopen this run to retry.';}});
  },{capture:true,signal:listenerController.signal});
  container.addEventListener('change',event=>{if(event.target.hasAttribute('data-ci-compare')){comparisonKey=event.target.value;draw();}if(event.target.hasAttribute('data-debug-session')){if(debugSessionId!==event.target.value){debugOperationId='';fixOperationId='';debugMessage='';}debugSessionId=event.target.value;draw();}},{signal:listenerController.signal});
  container.querySelector('.ig-tabs:not(.ci-workspace-tabs)').addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
    const index=tabs.findIndex(([key])=>key===state.tab);
    state.tab=tabs[event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:tabs.length-1))%tabs.length][0];state.page=0;event.preventDefault();draw();el(`[data-tab="${state.tab}"]`).focus();
  });
  if(isCi)for(const [selector,key] of [['[data-ci-repository]','repository'],['[data-ci-branch]','branch'],['[data-ci-period]','days']])el(selector).addEventListener('change',event=>{
    if(key==='repository') {
      const value=event.target.value,project=repositoryChoices.get(value)||null;
      if(observabilityView){observabilityView.setContext({project,repository:project?repository:value||repository});const context=observabilityView.getContext();healthView?.setObservabilityContext(context);applyCiContext(context);}
      else{state.project=project;state.repository=project?.repository||value;state.provider=project?.provider_url||provider;state.page=0;healthView?.setObservabilityContext({project});draw();}
      return;
    }
    state[key]=event.target.value;state.page=0;if(key==='days'||key==='branch'){refreshPerformance(true);const context={branch:state.branch,start:null,end:null,run_number:null,...(key==='days'?{mode:'days'}:{}),...([7,14,30].includes(Number(state.days))?{days:Number(state.days)}:{})};state.start=null;state.end=null;state.run_number=null;if(key==='days')state.mode='days';observabilityView?.setContext(context);healthView?.setObservabilityContext(context);}draw();});
  if(isCi)el('[data-ci-performance-window]').addEventListener('change',event=>{performanceDays=Number(event.target.value);refreshPerformance(true);});
  container.addEventListener('input',event=>{if(event.target.hasAttribute('data-ci-test-search')){testSearch=event.target.value;testPage=0;draw();}},{signal:listenerController.signal});
  el('[data-ig-search]').addEventListener('input',event=>{state.search=event.target.value;state.page=0;draw();});
  el('[data-ig-group]').addEventListener('change',event=>{state.group=event.target.value;draw();});
  function visibility() {
    clearInterval(timer);clearInterval(clock);
    if(document.hidden){for(const [key,controller] of pending){if(['debug','action','fix-start','fix-cancel'].includes(key))continue;controller.abort();pending.delete(key);}for(const cached of reviewCache.values())cached.loading=false;return;}
    refresh();for(const disclosure of container.querySelectorAll('details[open]'))disclosure.dispatchEvent(new Event('toggle'));timer=setInterval(refresh,5000);clock=setInterval(tick,1000);
  }
  draw();document.addEventListener('visibilitychange',visibility);visibility();
  return {refresh(){refresh();healthView?.refresh?.();observabilityView?.refresh();},dispose(){disposed=true;healthView?.dispose();observabilityView?.dispose();listenerController.abort();clearInterval(timer);clearInterval(clock);for(const controller of pending.values())controller.abort();pending.clear();document.removeEventListener('visibilitychange',visibility);}};
}
