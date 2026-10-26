import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
import {ciChartStyles,ciPercentiles,renderCiSummaryCharts,renderCiSparkline,renderCiTestPareto} from './ci-charts.js';

const rows=value=>Array.isArray(value)?value.filter(v=>v&&typeof v==='object'&&!Array.isArray(v)):[];
const count=value=>Number.isFinite(value)&&value>=0?value:0;
const text=value=>typeof value==='string'?value.slice(0,500):'';
const rate=(numerator,denominator)=>denominator>0?numerator/denominator:null;
const percent=value=>value===null?'Not reported':`${Math.round(value*1000)/10}%`;
const duration=(value,scale=1000)=>value===null?'Insufficient data':formatDuration(value*scale);
export const ciProjectKey=project=>JSON.stringify([text(project.provider_url).replace(/\/+$/,''),String(project.provider_repo_id??''),text(project.repository)]);
function metrics(value={},minimum=5) {
  const success=count(value.success_count),failed=count(value.failure_count),completed=success+failed;
  return {success,failed,completed,passRate:rate(success,completed),failureRate:rate(failed,completed),active:count(value.active_count),duration:ciPercentiles(value.duration_seconds,minimum)};
}
export function ciObservabilityModel(data={},options={}) {
  const minimum=Math.max(5,count(data.scope?.minimum_percentile_samples));
  const all=rows(data.projects).map(p=>({...p,key:ciProjectKey(p),metrics:metrics(p,minimum)}));
  const terms=text(options.search).toLowerCase().trim().split(/\s+/).filter(Boolean);
  const projects=all.filter(p=>(!options.failuresOnly||p.metrics.failed>0)&&terms.every(t=>`${p.repository} ${p.provider_url} ${p.provider_repo_id}`.toLowerCase().includes(t)));
  projects.sort((a,b)=>options.sort==='failure'?(b.metrics.failureRate??-1)-(a.metrics.failureRate??-1)||a.key.localeCompare(b.key):options.sort==='duration'?(b.metrics.duration.p95??-1)-(a.metrics.duration.p95??-1)||a.key.localeCompare(b.key):a.repository.localeCompare(b.repository)||a.key.localeCompare(b.key));
  return {projects,selected:all.find(p=>p.key===options.projectKey)||null,projectKey:options.projectKey||'',kpis:metrics(data.kpis,minimum),projectCount:count(data.kpis?.project_count),scope:data.scope||{},available:data.available,charts:rows(data.charts),minimum,generatedAt:data.generated_at,refreshing:!!data.refreshing};
}
function scopeNote(scope={}) {
  return `${scope.days||14}-day observed window · ${scope.branch||'All branches'} · ${scope.scope||'all'} runs. ${scope.window?`${scope.window.start} → ${scope.window.end}. `:''}Retained observations, not a complete history. ${scope.partial?'Partial coverage. ':''}${scope.stale?'Stale snapshot. ':''}${scope.truncated?'Retention or response limit reached. ':''}${scope.memory_only?'Storage is memory-only. ':''}${scope.history_memory_only?'Provider backfill is memory-only. ':''}`;
}
function cards(model) {
  if(model.available===false)return '<p class="ci-chart-note" role="status">CI summary unavailable. Coverage is partial; observed metrics cannot be established.</p>';
  const m=model.kpis;
  const cards=[['Observed projects',model.projectCount,'Projects in this scope'],['Completed pass rate',percent(m.passRate),`${m.success} / ${m.completed} successful or failed runs`],['Failed runs',m.failed,`${m.completed} successful or failed runs`],['Duration p50 / p95',`${duration(m.duration.p50)} / ${duration(m.duration.p95)}`,`${m.duration.samples} measured runs · minimum ${model.minimum}`],['Active runs',m.active,'Running, queued, waiting or blocked']];
  return `<div class="ci-ob-kpis">${cards.map(([label,value,note])=>`<section class="ci-ob-card"><h3>${label}</h3><strong>${esc(value)}</strong><p>${esc(note)}</p></section>`).join('')}</div>`;
}
function rail(model,projectCharts=new Map()) {
  return `<div class="ci-ob-table" tabindex="0" role="region" aria-label="Project comparison"><table><thead><tr>${['Project / provider','Pass / fail','Pass rate','Duration p50 / p95','Failure trend'].map(t=>`<th scope="col">${t}</th>`).join('')}</tr></thead><tbody>${model.projects.map(p=>`<tr><th scope="row"><button type="button" data-ob-project="${esc(p.key)}" aria-pressed="${p.key===model.projectKey}">${esc(p.repository)}</button><small>${esc(p.provider_url)} · #${esc(p.provider_repo_id)}</small>${p.scope_unavailable?'<small>Default branch unavailable</small>':''}</th><td>${p.metrics.success} / ${p.metrics.failed}<small>${p.metrics.completed} completed</small></td><td>${percent(p.metrics.passRate)}</td><td>${duration(p.metrics.duration.p50)} / ${duration(p.metrics.duration.p95)}<small>${p.metrics.duration.samples} samples</small></td><td class="ci-ob-spark">${projectCharts.has(p.key)?renderCiSparkline(projectCharts.get(p.key),p.repository):'<span>Select to load trend</span>'}</td></tr>`).join('')}</tbody></table>${!model.projects.length?'<p>No projects match these filters.</p>':''}</div>`;
}
export function renderCiObservability(model,{projectCharts=new Map()}={}) {
  return `<p class="ci-chart-note">${esc(scopeNote(model.scope))}</p>${model.available===false?'<p role="status">CI summary unavailable. Retained observations may be incomplete.</p>':''}${cards(model)}${rail(model,projectCharts)}`;
}
export function ciTestClassification(row) {
  return row.classification==='proven_flaky'&&!row.identity_ambiguous?'Proven flaky · same-revision retry evidence':row.classification==='inconsistent_outcomes'||row.classification==='proven_flaky'?'Inconsistent outcomes observed':'No inconsistent outcomes observed';
}
export function renderCiTestHistory(data,{loading=false,error='',selected=false,sort='failure'}={}) {
  if(!data)return `<p class="ci-chart-note" role="status">${error?esc(error):loading?'Loading reported test history…':selected?'Test evidence unavailable.':'Select a project to load test history.'}</p>`;
  const tests=rows(data.tests).sort((a,b)=>sort==='duration'?(b.last_duration_ms??-1)-(a.last_duration_ms??-1):count(b.failure_count)-count(a.failure_count));
  const scope=data.scope||{},minimum=Math.max(5,count(scope.minimum_percentile_samples));
  const banner=`<p class="ci-chart-note">${esc(scopeNote(scope))} Test history is grouped by branch, using report timestamps and reported completed test attempts; skipped attempts are excluded. Event scope and selected buckets use exact run creation evidence; reports without a matching run are excluded from those selections.</p>${error?`<p role="status">${esc(error)} Showing retained test evidence.</p>`:''}<p class="ci-chart-note">${count(scope.not_reported_run_count)} runs: test evidence not reported. Missing reports are not passing tests.</p>`;
  if(!tests.length)return banner+'<p class="ci-chart-note">No validated test evidence in this selection.</p>';
  return `${banner}${renderCiTestPareto(tests)}<div class="ci-ob-table" tabindex="0" role="region" aria-label="Exact test history"><table><thead><tr>${['Test / branch','Failure rate','Evidence','First failure / last pass','Last / p50 / p95 duration','Representative runs'].map(v=>`<th scope="col">${v}</th>`).join('')}</tr></thead><tbody>${tests.map(t=>{const p=ciPercentiles(t.duration_ms,minimum);return `<tr><th scope="row">${esc(t.name||t.id)}<small>${esc(t.classname||t.suite||'')} · ${esc(t.file||'File not reported')} · ${esc(t.branch||'Branch not reported')}</small><small>${esc(t.id)}</small></th><td>${percent(rate(count(t.failure_count),count(t.completed_count)))}<small>${count(t.failure_count)} / ${count(t.completed_count)} reported completed test attempts</small></td><td>${ciTestClassification(t)}${t.identity_ambiguous?'<small>Reported test identity is ambiguous</small>':''}${t.failure_evidence?`<details data-ob-disclosure="${esc(JSON.stringify([t.id,t.branch]))}"><summary>Failure evidence</summary><pre>${esc(text(t.failure_evidence.message))}\n${esc(String(t.failure_evidence.trace||'').slice(0,2000))}</pre></details>`:''}</td><td>${esc(t.first_failure||'Not observed')}<small>${esc(t.last_pass||'Not observed')}</small></td><td>${Number.isFinite(t.last_duration_ms)?formatDuration(t.last_duration_ms):'Not reported'} / ${duration(p.p50,1)} / ${duration(p.p95,1)}<small>${p.samples} measured attempts</small></td><td>${rows(t.representative_runs).map(r=>`<button type="button" data-ob-run="${esc(JSON.stringify(r))}">Run #${esc(r.run_number)} · ${esc(r.revision||'Revision unavailable')}</button>`).join('')||'Not reported'}</td></tr>`;}).join('')}</tbody></table></div>`;
}
const styles=`<style>
.ci-ob{min-width:0;margin:18px 0 24px}.ci-ob h2{font-size:19px;margin:0 0 6px}.ci-ob h3{font-size:13px}.ci-ob-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(190px,100%),1fr));gap:12px;margin:16px 0}.ci-ob-card{padding:16px;border:1px solid var(--border);border-radius:var(--radius,8px);background:var(--surface);min-width:0}.ci-ob-card h3{margin:0 0 12px;color:var(--text-secondary);font-weight:500}.ci-ob-card strong{font-size:22px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.ci-ob-card p{font-size:11px;color:var(--text-secondary);line-height:1.6}.ci-ob-controls{display:flex;flex-wrap:wrap;gap:12px;align-items:end;margin:16px 0}.ci-ob-controls label{display:flex;flex-direction:column;gap:6px;font-size:12px;max-width:100%}.ci-ob input,.ci-ob select,.ci-ob button{font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm,4px);padding:8px;max-width:100%}.ci-ob button{cursor:pointer;min-height:36px}.ci-ob button[aria-pressed=true]{border-color:var(--accent);box-shadow:inset 3px 0 var(--accent)}.ci-ob :focus-visible{outline:2px solid var(--accent);outline-offset:2px}.ci-ob-table{max-width:100%;overflow:auto;border:1px solid var(--border);border-radius:var(--radius,8px);margin:12px 0}.ci-ob table{width:100%;border-collapse:collapse;text-align:left;font-size:12px}.ci-ob th,.ci-ob td{padding:12px;vertical-align:top;border-bottom:1px solid var(--border-subtle);min-width:100px;max-width:300px;overflow-wrap:anywhere}.ci-ob thead th{font-weight:500;color:var(--text-secondary);background:var(--surface)}.ci-ob small{display:block;font-weight:400;color:var(--text-secondary);margin-top:6px;line-height:1.5}.ci-ob-spark svg{width:180px;height:80px}.ci-ob pre{max-height:220px;max-width:280px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}.ci-ob [data-ob-status]{font-size:12px;line-height:1.6;color:var(--text-secondary)}.ci-ob .ci-chart-grid{grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr))}.ci-ob-chart-heading{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:space-between}.ci-ob [data-ob-tests],.ci-ob [data-ob-charts]{min-width:0}.ci-ob .ci-chart-table{max-width:100%}
</style>`;

export function mountCiObservability(host,client,{repository='',provider='',mission='',session='',search,onContext=()=>{},onSelectRun=()=>{}}={}) {
  const doc=host.ownerDocument,listeners=new AbortController();
  const storageKey=`herdr.ci-observability.v1:${JSON.stringify([repository,provider,mission,session])}`;
  const read=key=>{try{return JSON.parse(doc.defaultView.localStorage.getItem(key)||'null');}catch{return null;}};
  const health=read(`herdr.ci-health.v1:${JSON.stringify([repository,mission,session])}`);
  const saved=read(storageKey);
  const preferences=ciObservabilityPreferences({...health,days:health?.mode,...saved,...(typeof search==='string'?{search}:{})});
  let filters={days:preferences.days,scope:preferences.scope,branch:preferences.branch,repository,provider_url:provider||undefined},project=preferences.project,summary=null,detail=null,tests=null;
  let view={search:preferences.search,sort:preferences.sort,failuresOnly:preferences.failuresOnly},testSort=preferences.testSort,error='',detailError='',testsError='',disposed=false,timer=null,generation=0;
  const context=()=>({...filters,...view,project});
  function save() {try{doc.defaultView.localStorage.setItem(storageKey,JSON.stringify(ciObservabilityPreferences({...view,days:filters.days,scope:filters.scope,branch:filters.branch,project,testSort})));}catch{ /* Storage can be unavailable or full. */ }}
  const pending=new Map(),projectCharts=new Map();
  const unsupported=!!(mission||session);
  host.innerHTML=`${ciChartStyles}${styles}<section class="ci-ob" aria-label="CI command center"><h2>CI command center</h2><p class="ci-chart-note">Reliability and runtime across observed projects</p><div class="ci-ob-controls"><label>Window<select data-ob-days>${[7,14,30].map(n=>`<option value="${n}" ${n===14?'selected':''}>${n} days</option>`).join('')}</select></label><label>Scope<select data-ob-scope><option value="all">All runs</option><option value="default">Default branch</option><option value="pr">Pull requests</option></select></label><label>Branch<input data-ob-branch maxlength="500" placeholder="All branches"></label><label>Find projects<input data-ob-search type="search" maxlength="500" value="${esc(view.search)}"></label><label>Sort projects<select data-ob-sort><option value="failure">Failure rate</option><option value="duration">Duration p95</option><option value="name">Project name</option></select></label><label><span>Failures only</span><input data-ob-failures type="checkbox"></label><button type="button" data-ob-refresh>Refresh</button></div><p data-ob-status role="status" aria-live="polite"></p><div data-ob-summary></div><h3>Project comparison</h3><p class="ci-chart-note">Search, failures-only and sorting filter this comparison table. Summary cards describe the selected project, or all projects in the server scope.</p><div data-ob-projects></div><div class="ci-ob-chart-heading"><h3 data-ob-selection>All projects · selected window</h3><button type="button" data-ob-clear hidden>All projects</button></div><div data-ob-charts></div><div class="ci-ob-controls"><h3>Reported test history</h3><label>Sort tests<select data-ob-test-sort><option value="failure">Most failures</option><option value="duration">Slowest last duration</option></select></label></div><div data-ob-tests></div></section>`;
  const el=s=>host.querySelector(s);
  function syncControls() {
    for(const key of ['days','scope','branch'])el(`[data-ob-${key}]`).value=String(filters[key]);
    for(const key of ['search','sort'])el(`[data-ob-${key}]`).value=view[key];
    el('[data-ob-failures]').checked=view.failuresOnly;el('[data-ob-test-sort]').value=testSort;
  }
  syncControls();
  // Region patching keeps inputs, disclosures, focus and internal scroll stable.
  function replace(node,html) {
    if(node.dataset.rendered===html)return;
    const active=doc.activeElement,focused=node.contains(active);
    const attr=focused?['data-ob-project','data-ob-run'].find(a=>active.hasAttribute(a)):null;
    const value=attr?active.getAttribute(attr):null;
    const disclosure=focused?active.closest('[data-ob-disclosure]')?.dataset.obDisclosure:null;
    const open=new Set([...node.querySelectorAll('[data-ob-disclosure][open]')].map(n=>n.dataset.obDisclosure));
    const scroll=[...node.querySelectorAll('.ci-ob-table,.ci-chart-table')].map(n=>[n.scrollLeft,n.scrollTop,n===active]);
    node.innerHTML=html;node.dataset.rendered=html;
    for(const n of node.querySelectorAll('[data-ob-disclosure]')){n.open=open.has(n.dataset.obDisclosure);if(n.dataset.obDisclosure===disclosure)n.querySelector('summary')?.focus({preventScroll:true});}
    if(attr)[...node.querySelectorAll(`[${attr}]`)].find(n=>n.getAttribute(attr)===value)?.focus({preventScroll:true});
    [...node.querySelectorAll('.ci-ob-table,.ci-chart-table')].forEach((n,i)=>{if(scroll[i]){n.scrollLeft=scroll[i][0];n.scrollTop=scroll[i][1];if(scroll[i][2])n.focus({preventScroll:true});}});
  }
  function draw() {
    if(disposed)return;
    const model=ciObservabilityModel(summary||{}, {...view,projectKey:project?ciProjectKey(project):''});
    const selected=project?detail:summary;
    el('[data-ob-status]').textContent=unsupported?'Compact summaries are unavailable for mission/session context. Use the scoped run tree below.':`${error||''} ${detailError||''} ${summary?.available===false||detail?.available===false?'CI summary unavailable. Coverage is partial. ':''}${pending.size?'Refreshing observations… ':''}${summary?scopeNote(summary.scope):'CI summary unavailable or loading.'}${summary?.generated_at?` Last provider snapshot: ${summary.generated_at}.`:''}`.trim();
    replace(el('[data-ob-summary]'),selected?cards(ciObservabilityModel(selected)):'');
    replace(el('[data-ob-projects]'),summary?rail(model,projectCharts):'<p class="ci-chart-note">No project summary received.</p>');
    el('[data-ob-selection]').textContent=project?`${project.repository} · ${project.provider_url} #${project.provider_repo_id} · ${filters.days}-day window`:'All projects · selected window';
    el('[data-ob-clear]').hidden=!project;
    replace(el('[data-ob-charts]'),selected?`<p class="ci-chart-note">${esc(scopeNote(selected.scope))} Daily grouping uses run creation time (created_at) and UTC calendar days, including today. Selected heatmap buckets narrow these charts and run metrics.</p>${renderCiSummaryCharts(rows(selected.charts))}`:'<p class="ci-chart-note">No chart observations received for this selection.</p>');
    replace(el('[data-ob-tests]'),renderCiTestHistory(tests,{selected:!!project,loading:pending.has('tests'),error:testsError,sort:testSort}));
  }
  function notify() {save();onContext(context());}
  async function request(key,fetcher,accept,reject) {
    if(pending.has(key)||disposed)return;
    const controller=new AbortController(),version=generation;pending.set(key,controller);
    const timeout=setTimeout(()=>controller.abort(),15000);
    try {
      const aborted=new Promise((_,fail)=>controller.signal.addEventListener('abort',()=>fail(new Error('Refresh timed out')),{once:true}));
      const response=await Promise.race([fetcher(controller.signal),aborted]);
      if(!disposed&&version===generation)accept(response);
    }catch(reason){if(!disposed&&version===generation)reject(reason);}
    finally{clearTimeout(timeout);if(pending.get(key)===controller)pending.delete(key);if(!disposed&&version===generation)draw();}
  }
  async function refresh({selectionOnly=false}={}) {
    if(disposed||doc.hidden||unsupported){draw();return;}
    clearTimeout(timer);const version=generation;
    // Selection changes do not change the fleet scope. Poll/manual refresh still
    // refresh all three resources; a missing fleet snapshot must also be fetched.
    const jobs=[];
    if(!selectionOnly||!summary)jobs.push(request('summary',signal=>client.getCiSummary({...filters,signal}),data=>{if(!Array.isArray(data?.projects)||!data.kpis)throw Error('Invalid summary');summary=data;error='';},()=>{error=summary?'Summary refresh failed. Showing the last received snapshot (stale).':'CI summary unavailable.';}));
    if(project){
      const context={...filters,repository:project.repository,provider_url:project.provider_url,provider_repo_id:project.provider_repo_id};
      jobs.push(request('project',signal=>client.getCiSummary({...context,signal}),data=>{if(!Array.isArray(data?.charts)||!data.kpis)throw Error('Invalid project summary');detail=data;detailError='';projectCharts.set(ciProjectKey(project),rows(data.charts));},()=>{detailError=detail?'Project refresh failed. Retained charts are stale.':'Project charts unavailable.';}));
      jobs.push(request('tests',signal=>client.getCiTests({...context,signal}),data=>{if(!Array.isArray(data?.tests))throw Error('Invalid test history');tests=data;testsError='';},()=>{testsError='Test history refresh failed.';}));
    }
    draw();await Promise.all(jobs);
    if(!disposed&&!doc.hidden&&version===generation){clearTimeout(timer);timer=setTimeout(refresh,5000);}
  }
  function invalidate({all=false}={}) {
    generation++;for(const request of pending.values())request.abort();pending.clear();
    detail=null;tests=null;detailError='';testsError='';
    if(all){summary=null;error='';projectCharts.clear();}
  }
  function selectProject(next) {const bucket=filters.start!=null;filters.start=null;filters.end=null;invalidate({all:bucket});project=next;refresh({selectionOnly:!bucket});notify();}
  host.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button||!host.contains(button))return;
    if(button.hasAttribute('data-ob-refresh'))refresh();
    if(button.hasAttribute('data-ob-clear'))selectProject(null);
    if(button.hasAttribute('data-ob-project')){const next=rows(summary?.projects).find(p=>ciProjectKey(p)===button.dataset.obProject);if(next)selectProject(next);}
    if(button.hasAttribute('data-ob-run')){try{onSelectRun(JSON.parse(button.dataset.obRun));}catch{ /* Invalid evidence cannot navigate. */ }}
  },{signal:listeners.signal});
  function change(event) {
    const target=event.target;
    if(target.hasAttribute('data-ob-test-sort')){testSort=target.value;save();draw();return;}
    for(const key of ['search','sort','failures'])if(target.hasAttribute(`data-ob-${key}`)){view[key==='failures'?'failuresOnly':key]=key==='failures'?target.checked:text(target.value);notify();draw();return;}
    for(const key of ['days','scope','branch'])if(target.hasAttribute(`data-ob-${key}`)){filters[key]=key==='days'?Number(target.value):text(target.value);filters.start=null;filters.end=null;invalidate({all:true});notify();draw();refresh();return;}
  }
  host.addEventListener('change',change,{signal:listeners.signal});
  host.addEventListener('input',event=>{if(event.target.matches('[data-ob-search],[data-ob-branch]'))change(event);},{signal:listeners.signal});
  doc.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(doc.hidden){generation++;for(const request of pending.values())request.abort();pending.clear();}else refresh();},{signal:listeners.signal});
  draw();refresh();
  return {refresh,getContext:context,
    setContext(next={}) {
      if(disposed)return;
      const updated={...filters,...Object.fromEntries(['days','scope','branch','repository','start','end'].filter(key=>next[key]!==undefined).map(key=>[key,next[key]]))};
      const nextProject=next.project===undefined?project:next.project;
      if(next.start===undefined&&next.end===undefined&&(['days','scope','branch','repository'].some(key=>updated[key]!==filters[key])||(nextProject?ciProjectKey(nextProject):'')!==(project?ciProjectKey(project):''))){updated.start=null;updated.end=null;}
      const changed=JSON.stringify(updated)!==JSON.stringify(filters)||(nextProject?ciProjectKey(nextProject):'')!==(project?ciProjectKey(project):'');
      const scopeChanged=JSON.stringify(updated)!==JSON.stringify(filters);
      if(changed){invalidate({all:scopeChanged});filters=updated;project=nextProject;}
      for(const key of ['search','failuresOnly','sort'])if(next[key]!==undefined)view[key]=next[key];
      syncControls();save();
      if(changed)refresh({selectionOnly:!scopeChanged});else draw();
    },
    dispose(){disposed=true;generation++;clearTimeout(timer);for(const request of pending.values())request.abort();pending.clear();listeners.abort();}
  };
}


export function ciObservabilityPreferences(value={}) {
  const input=value&&typeof value==='object'&&!Array.isArray(value)?value:{};
  const project=input.project;
  const exact=project&&typeof project.provider_url==='string'&&project.provider_url.length<=500&&project.provider_url && ['string','number'].includes(typeof project.provider_repo_id)&&String(project.provider_repo_id).length<=500&&String(project.provider_repo_id) && typeof project.repository==='string'&&project.repository.length<=500&&project.repository;
  return {days:[7,14,30].includes(Number(input.days))?Number(input.days):14,
    scope:['all','default','pr'].includes(input.scope)?input.scope:'all',branch:text(input.branch),search:text(input.search),
    sort:['name','failure','duration'].includes(input.sort)?input.sort:'failure',failuresOnly:input.failuresOnly===true,
    testSort:input.testSort==='duration'?'duration':'failure',
    project:exact?{provider_url:project.provider_url,provider_repo_id:String(project.provider_repo_id),repository:project.repository}:null};
}
