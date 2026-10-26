import {escapeHtml as esc} from './herdr-web-components/util.js';
import {ciHealthModel,renderCiHeatmap,renderCiTree,ciHealthStyles} from './ci-health.js';
import {snapshotText,animateChanges} from './motion.js';

const modes=['hours','runs','7','14','30'];
const scopes=['default','pr','all'];
const rows=value=>Array.isArray(value)?value.filter(row=>row && typeof row==='object' && !Array.isArray(row)):[];
const bounded=value=>typeof value==='string'?value.slice(0,500):'';
const runKey=record=>JSON.stringify([String(record.provider_url || '').replace(/\/$/,''),String(record.provider_repo_id ?? record.id ?? record.repository_id ?? record.repository ?? ''),record.run_number ?? record.number ?? record.id]);

export function ciHealthPreferences(value={}) {
  const input=value && typeof value==='object' && !Array.isArray(value)?value:{};
  return {mode:modes.includes(input.mode)?input.mode:'hours',scope:scopes.includes(input.scope)?input.scope:'default',sort:['name','failures'].includes(input.sort)?input.sort:'name',search:bounded(input.search),branch:bounded(input.branch),failuresOnly:input.failuresOnly===true,expanded:Array.isArray(input.expanded)?input.expanded.filter(key=>typeof key==='string' && key.length<=2000).slice(0,500):[]};
}

export function ciHealthSelection(value) {
  if(!value || typeof value.projectKey!=='string' || !value.projectKey || value.projectKey.length>2000 || typeof value.bucketKey!=='string' || !value.bucketKey || value.bucketKey.length>4096)return null;
  return {projectKey:value.projectKey,bucketKey:value.bucketKey};
}

export function ciHealthUnavailable(history={},missingClient=false) {
  if(missingClient)return 'CI history unavailable. The provider history endpoint is required.';
  if(history.status==='syncing'&&history.refreshing)return 'Loading CI projects and history…';
  const reason=bounded(history.error) || bounded(history.status) || 'Provider history is not available';
  return `CI history unavailable: ${reason.replace(/[.]+$/,'')}.`;
}

// Persist user preferences per route context; repository/mission/session never become global filters.
export function mountCiHealth(host,client,{onSelectRun=()=>{},onSelectStep=()=>{},onLayout=()=>{},onContext=()=>{},repository='',mission='',session='',search}={}) {
  const doc=host.ownerDocument;
  let context={repository,mission,session},commandProject=null,state,expanded,selection=null,selected=null,selectedKey='',layout='tree';
  let history=null,model=null,orderSignature='',error='',disposed=false,timer=null,pending=null,generation=0;
  const listeners=new AbortController();
  const treeLimits=new Map();
  const storageKey=()=>`herdr.ci-health.v1:${JSON.stringify([context.repository || '',context.mission || '',context.session || ''])}`;
  function load() {
    let saved;try {saved=JSON.parse(doc.defaultView.localStorage.getItem(storageKey()) || 'null');} catch {saved=null;}
    state=ciHealthPreferences(saved);expanded=new Set(state.expanded);
    selection=ciHealthSelection(saved?.selection);
  }
  function save() {
    try {doc.defaultView.localStorage.setItem(storageKey(),JSON.stringify({...state,selection,expanded:[...expanded].slice(-500)}));} catch { /* Storage may be blocked or full. */ }
  }
  load();
  if(typeof search==='string')state.search=bounded(search);
  host.innerHTML=`<style>${ciHealthStyles}</style><style>
.ci-health-controls{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:14px 0;padding:14px;border:1px solid var(--border);border-radius:var(--radius);background:linear-gradient(135deg,color-mix(in srgb,var(--surface) 92%,var(--accent)),var(--surface))}.ci-health-controls [data-health-mode]{background:var(--surface);color:var(--text);border:1px solid var(--border);margin-right:3px}.ci-health-controls [data-health-mode][aria-pressed=true]{background:var(--accent);color:#fff;border-color:var(--accent)}.ci-health-controls label{display:flex;flex-direction:column;gap:5px;font-size:12px}.ci-health-controls input,.ci-health-controls select{max-width:100%;padding:8px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:var(--radius-sm)}.ci-health-controls input[type=checkbox]{margin:0}.ci-health-controls .ci-health-check{flex-direction:row;align-items:center;padding:8px 0}.ci-health-layout{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0}.ci-health-layout button{font-weight:650}.ci-health-layout [aria-selected=true]{outline:2px solid var(--accent);background:var(--accent);color:#fff}.ci-health-selection,.ci-health-status{font-size:12px;line-height:1.6;color:var(--text-secondary);overflow-wrap:anywhere}.ci-health-workflows{padding:12px;font-size:12px}.ci-health-workflows button{display:block;margin:6px 0;text-align:left}.ci-health-tree[hidden]{display:none}.ci-health-lower{display:grid;grid-template-columns:minmax(0,1fr);gap:18px;min-width:0}.ci-health-tree,.ci-health-pipeline-slot{min-width:0}.ci-health-pipeline-slot:empty,.ci-health-pipeline-slot:not(:has(.ig-inspector:not([hidden]))){display:none}[data-health-layout=map]>.ci-health-tree,[data-health-layout=map]>.ci-health-pipeline-slot{display:none}[data-health-layout=tree]>.ci-health-pipeline-slot{display:block}@media(min-width:1100px){.ci-health-lower[data-health-layout=tree]:has(.ig-inspector:not([hidden])){grid-template-columns:minmax(240px,.25fr) minmax(0,1fr)}}
</style><section aria-label="CI project health"><h2>Project health</h2><div class="ci-health-controls">
<div role="group" aria-label="History window"><button type="button" class="hd-btn" data-health-mode="hours">Last 24 hours</button><button type="button" class="hd-btn" data-health-mode="runs">Last 24 runs</button><button type="button" class="hd-btn" data-health-mode="7">7 days</button><button type="button" class="hd-btn" data-health-mode="14">14 days</button><button type="button" class="hd-btn" data-health-mode="30">30 days</button></div>
<label>Scope<select data-health-scope><option value="default">Default-branch builds</option><option value="pr">Pull requests</option><option value="all">All runs</option></select></label>
<label>Sort projects<select data-health-sort><option value="name">Project name</option><option value="failures">Failures in selected window</option></select></label>
<label>Search projects and runs<input type="search" data-health-search maxlength="500"></label><label>Branch<input data-health-branch maxlength="500" placeholder="All branches"></label><label class="ci-health-check"><input type="checkbox" data-health-failures>Projects with failures</label></div>
<p class="ci-health-status" role="status" data-health-status></p><div data-health-map></div><div class="ci-health-selection" data-health-selection></div>
<div class="ci-health-layout" role="tablist" aria-label="CI project views"><button type="button" class="hd-btn" role="tab" data-health-layout="map" aria-selected="false">Map</button><button type="button" class="hd-btn" role="tab" data-health-layout="tree" aria-selected="true">Tree</button></div><div class="ci-health-lower" data-health-lower><div class="ci-health-tree" data-health-tree></div><div class="ci-health-pipeline-slot" data-health-pipeline-slot></div></div></section>`;
  const el=selector=>host.querySelector(selector);
  function syncControls() {
    for(const button of host.querySelectorAll('button[data-health-mode]'))button.setAttribute('aria-pressed',String(button.dataset.healthMode===state.mode));
    for(const name of ['scope','sort','search','branch'])el(`[data-health-${name}]`).value=state[name];
    el('[data-health-failures]').checked=state.failuresOnly;
  }
  syncControls();
  function applyLayout() {
    host.dataset.healthLayout=layout;el('[data-health-lower]').dataset.healthLayout=layout;
    for(const button of host.querySelectorAll('button[data-health-layout]'))button.setAttribute('aria-selected',String(button.dataset.healthLayout===layout));
    el('[data-health-tree]').hidden=layout==='map';
    onLayout(layout);
  }
  applyLayout();
  function rememberDisclosures() {
    for(const node of host.querySelectorAll('[data-health-disclosure]')) {
      const key=node.dataset.healthDisclosure;if(node.open)expanded.add(key);else expanded.delete(key);
    }
  }
  function replace(element,html) {
    const active=doc.activeElement,inside=element.contains(active);
    const attributes=['data-health-run','data-health-project','data-health-bucket','data-health-step','data-health-tree-more'];
    const scrollFocus=inside?[...element.querySelectorAll('[data-health-grid-scroll]')].indexOf(active):-1;
    const focus=inside?attributes.filter(key=>active.hasAttribute(key)).map(key=>[key,active.getAttribute(key)]):[];
    const summary=inside && active.tagName==='SUMMARY'?active.closest('[data-health-disclosure]')?.dataset.healthDisclosure:null;
    const scroll=[...element.querySelectorAll('[data-health-grid-scroll]')].map(node=>[node.scrollLeft,node.scrollTop]);
    element.innerHTML=html;
    for(const node of element.querySelectorAll('[data-health-disclosure]'))node.open=expanded.has(node.dataset.healthDisclosure);
    if(focus.length)[...element.querySelectorAll(`[${focus[0][0]}]`)].find(node=>focus.every(([key,value])=>node.getAttribute(key)===value))?.focus({preventScroll:true});
    if(summary)[...element.querySelectorAll('[data-health-disclosure]')].find(node=>node.dataset.healthDisclosure===summary)?.querySelector('summary')?.focus({preventScroll:true});
    [...element.querySelectorAll('[data-health-grid-scroll]')].forEach((node,i)=>{if(scrollFocus===i)node.focus({preventScroll:true});if(scroll[i]){node.scrollLeft=scroll[i][0];node.scrollTop=scroll[i][1];}});
  }
  function contextCoverageUnknown() {
    return rows(history?.pipelines).some(record=>(context.mission && !['mission_key','mission_id','related_missions'].some(key=>Object.hasOwn(record,key))) || (context.session && !['session_id','related_sessions'].some(key=>Object.hasOwn(record,key))));
  }
  function scopedHistory() {
    const projected=ciHealthProjectHistory(history||{},commandProject);
    if(!context.mission && !context.session)return projected;
    return {...projected,...(contextCoverageUnknown()?{coverage:{},status:'partial'}:{}),pipelines:rows(projected.pipelines).filter(record=>(!context.mission || (record.mission_key || record.mission_id)===context.mission || rows(record.related_missions).some(row=>(row.mission_key || row.mission_id)===context.mission)) && (!context.session || record.session_id===context.session || rows(record.related_sessions).some(row=>row.session_id===context.session)))};
  }
  function selectedSteps() {
    if(!selected || !Array.isArray(selected.tasks))return '';
    const groups=new Map();for(const task of rows(selected.tasks)){const key=String(task.workflow ?? task.workflow_id ?? 'Steps');if(!groups.has(key))groups.set(key,[]);groups.get(key).push(task);}
    return `<section class="ci-health-workflows" aria-label="Selected run workflows"><h3>Run #${esc(String(selected.run_number ?? selected.number ?? ''))} · workflows and steps</h3>${[...groups].map(([name,tasks])=>`<details data-health-disclosure="${esc(JSON.stringify(['workflow',selectedKey,name]))}"><summary>${esc(name)}</summary>${tasks.map(task=>`<button type="button" class="hd-btn" data-health-step="${esc(String(task.id ?? ''))}">${esc(task.name || `Step ${task.id ?? '?'}`)} · ${esc(task.state || task.status || 'Unknown')}</button>`).join('')}</details>`).join('')}</section>`;
  }
  function selectedSlot() {
    if(!selection || !model)return '';
    const project=model.projects.find(p=>p.key===selection.projectKey),cell=project?.cells.find(c=>c.key===selection.bucketKey);
    if(!project || !cell)return '';
    const counts=cell.counts||{},runs=Array.isArray(cell.runs)?cell.runs:[];
    const runButtons=runs.slice(0,100).map(run=>`<button type="button" data-health-slot-run="${esc(String(run.runKey||run.run_number||''))}"><strong>Run #${esc(String(run.run_number??'unknown'))}</strong><br><small>${esc(run.state||run.status||'Unknown')} · ${esc(run.branch||'Branch unavailable')} · ${esc(run.revision||'Revision unavailable')}</small></button>`).join('');
    return `<section class="ci-slot-detail" aria-label="Selected CI slot detail"><header><div><h3>${esc(project.identity)}</h3><p class="ci-slot-note">${esc(cell.label||cell.key)} · ${cell.complete?'Complete coverage':'Incomplete coverage'}</p></div><strong>${esc(`${counts.failure||0} failed`)}</strong></header><dl><div><dt>Runs</dt><dd>${esc(counts.total??runs.length)}</dd></div><div><dt>Passed</dt><dd>${esc(counts.success||0)}</dd></div><div><dt>Failed</dt><dd>${esc(counts.failure||0)}</dd></div><div><dt>Active</dt><dd>${esc(counts.active||0)}</dd></div></dl>${runs.length?`<div class="ci-slot-runs">${runButtons}</div><p class="ci-slot-note">Select a run to open its failed steps, tests, logs and provider diagnosis.</p>`:'<p class="ci-slot-note">No run evidence is reported for this slot. Try a wider window or another scope.</p>'}</section>`;
  }
  const CELL='[data-health-project][data-health-bucket]';
  const cellKey=node=>`${node.dataset.healthProject}|${node.dataset.healthBucket}`;
  let shownCells=null;
  function draw(preserve=true) {
    if(disposed)return;
    if(preserve)rememberDisclosures();
    const previousOrder=new Map((model?.projects || []).map((project,index)=>[project.key,index]));
    const nextOrderSignature=JSON.stringify([state.mode,state.scope,state.sort,state.search,state.branch,state.failuresOnly,context]);
    const interacting=el('[data-health-map]').contains(doc.activeElement) || el('[data-health-tree]').contains(doc.activeElement) || host.querySelector('[data-health-disclosure][open]');
    model=ciHealthModel(scopedHistory(),{...state,...context,selection});
    if(preserve && interacting && orderSignature===nextOrderSignature)model.projects.sort((a,b)=>(previousOrder.get(a.key) ?? Infinity)-(previousOrder.get(b.key) ?? Infinity));
    orderSignature=nextOrderSignature;
    const unavailable=history?.available===false;
    const hideHistory=unavailable&&!rows(history?.repos).length;
    el('[data-health-status]').textContent=error || (!history?'Loading CI history…':unavailable?ciHealthUnavailable(history,history.missingClient):`${history.stale?'Stale history · ':''}${history.refreshing?'Refreshing history · ':''}${model.partial?'Partial history: coverage is incomplete. ':''}${state.mode==='runs'?'Last 24 matching runs per project; no date cutoff. ':'UTC timestamps. '}${contextCoverageUnknown()?'Context associations are unavailable for some runs; counts and coverage for this route are incomplete. ':context.mission || context.session?'Counts are limited to the current route context. ':''}Only completed success and failure outcomes contribute to pass/fail totals.`);
    // The heatmap is rebuilt with innerHTML; a cell whose outcome symbol changed (same project and
    // bucket) flashes. A mode, scope or filter change re-keys the buckets, so it never flashes.
    const cellsBefore=nextOrderSignature===shownCells?snapshotText(el('[data-health-map]'),CELL,cellKey):null;
    replace(el('[data-health-map]'),hideHistory?'':renderCiHeatmap(model));
    animateChanges(el('[data-health-map]'),CELL,cellsBefore,cellKey,{count:false});
    shownCells=nextOrderSignature;
    replace(el('[data-health-tree]'),hideHistory?'':selectedSlot()+renderCiTree(model,{selectedRunKey:selectedKey,expanded,limits:treeLimits})+selectedSteps());
    const selectionHtml=selection?`${!model.projects.some(project=>project.key===selection.projectKey && project.cells.some(cell=>cell.key===selection.bucketKey))?'Selected run or bucket is no longer present in this window.':'Selected project and time bucket · latest labels apply within this selection.'} <button type="button" class="hd-btn" data-health-clear>Clear selection</button>`:'';
    const chip=el('[data-health-selection]');if(chip.innerHTML!==selectionHtml)chip.innerHTML=selectionHtml;
    const cells=[...host.querySelectorAll('[data-health-project][data-health-bucket]')];
    const focused=cells.find(cell=>cell===doc.activeElement);
    cells.forEach((cell,index)=>cell.tabIndex=focused?cell===focused?0:-1:index===0?0:-1);
  }
  function schedule() {clearTimeout(timer);if(!disposed && !doc.hidden && typeof client.getCiHistory==='function')timer=setTimeout(refresh,5000);}
  async function refresh() {
    if(disposed || doc.hidden || pending)return;
    if(typeof client.getCiHistory!=='function'){history={available:false,status:'unavailable',missingClient:true,repos:[],pipelines:[],coverage:{}};draw();return;}
    const request=new AbortController(),version=++generation;pending=request;
    const timeout=setTimeout(()=>request.abort(),15000);
    try {
      // Race the abort as old clients may not honor the supplied signal.
      const aborted=new Promise((_,reject)=>request.signal.addEventListener('abort',()=>reject(new Error('CI history refresh timed out.')),{once:true}));
      const result=await Promise.race([client.getCiHistory({signal:request.signal}),aborted]);
      if(disposed || version!==generation)return;
      history=result;error='';draw();
    } catch(reason) {
      if(disposed || version!==generation)return;
      if(history)history={...history,stale:true};error=`CI history unavailable: ${reason?.message || 'Refresh failed'}. ${history?'Showing the last received snapshot.':''}`;draw();
    } finally {clearTimeout(timeout);if(pending===request)pending=null;if(!disposed && version===generation)schedule();}
  }
  function notifyContext() {
    const repo=selection?model?.projects.find(p=>p.key===selection.projectKey)?.repo:commandProject;
    const cell=selection?model?.projects.find(p=>p.key===selection.projectKey)?.cells.find(c=>c.key===selection.bucketKey):null;
    onContext({...state,run_number:state.mode==='runs'?cell?.runs[0]?.run_number??null:null,start:cell?.start!=null?new Date(cell.start).toISOString():null,end:cell?.end!=null?new Date(cell.end).toISOString():null,project:repo?{...repo,provider_repo_id:repo.provider_repo_id??repo.id}:null});
  }
  host.addEventListener('change',event=>{
    const target=event.target;
    for(const name of ['scope','sort'])if(target.matches(`[data-health-${name}]`)){state[name]=target.value;selection=null;state=ciHealthPreferences(state);save();draw();notifyContext();return;}
    if(target.matches('[data-health-failures]')){state.failuresOnly=target.checked;selection=null;save();draw();notifyContext();}
  },{signal:listeners.signal});
  host.addEventListener('input',event=>{for(const name of ['search','branch'])if(event.target.matches(`[data-health-${name}]`)){state[name]=bounded(event.target.value);selection=null;save();draw();notifyContext();}},{signal:listeners.signal});
  host.addEventListener('toggle',event=>{
    const node=event.target;if(disposed || !host.contains(node) || !node.matches('[data-health-disclosure]'))return;
    const key=node.dataset.healthDisclosure;if(expanded.has(key)===node.open)return;
    if(node.open)expanded.add(key);else expanded.delete(key);
    rememberDisclosures();save();draw();
  },{capture:true,signal:listeners.signal});
  host.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button || !host.contains(button))return;
    if(button.hasAttribute('data-health-mode')){state.mode=button.dataset.healthMode;selection=null;syncControls();save();draw();notifyContext();return;}
    if(button.hasAttribute('data-health-layout')){layout=button.dataset.healthLayout;applyLayout();return;}
    if(button.hasAttribute('data-health-clear')){selection=null;save();draw();notifyContext();return;}
    if(button.hasAttribute('data-health-bucket')){if(state.mode==='runs'&&!model?.projects.find(p=>p.key===button.dataset.healthProject)?.cells.find(c=>c.key===button.dataset.healthBucket)?.runs.length)return;selection={projectKey:button.dataset.healthProject,bucketKey:button.dataset.healthBucket};save();draw();notifyContext();return;}
    if(button.hasAttribute('data-health-tree-more')){const key=button.dataset.healthTreeMore;treeLimits.set(key,(treeLimits.get(key) || 100)+100);draw();return;}
    if(button.hasAttribute('data-health-slot-run')){const record=model?.selectedRows.find(row=>row.runKey===button.dataset.healthSlotRun)||model?.projects.flatMap(p=>p.cells.flatMap(c=>c.runs||[])).find(row=>row.runKey===button.dataset.healthSlotRun);if(record){selected=record;selectedKey=record.runKey;draw();onSelectRun(record);}return;}
    if(button.hasAttribute('data-health-run')){const record=model?.selectedRows.find(row=>row.runKey===button.dataset.healthRun);if(record){selected=record;selectedKey=record.runKey;draw();onSelectRun(record);}return;}
    if(button.hasAttribute('data-health-step') && selected)onSelectStep(selected,button.dataset.healthStep);
  },{signal:listeners.signal});
  host.addEventListener('keydown',event=>{
    const cell=event.target.closest('[data-health-project][data-health-bucket]');if(!cell || !['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
    const cells=[...host.querySelectorAll('[data-health-project][data-health-bucket]')],projects=[...new Set(cells.map(node=>node.dataset.healthProject))];
    const row=cells.filter(node=>node.dataset.healthProject===cell.dataset.healthProject),column=row.indexOf(cell),index=projects.indexOf(cell.dataset.healthProject);
    let next;
    if(event.key==='Home')next=row[0];else if(event.key==='End')next=row.at(-1);else if(event.key==='ArrowLeft')next=row[Math.max(0,column-1)];else if(event.key==='ArrowRight')next=row[Math.min(row.length-1,column+1)];else {const project=projects[index+(event.key==='ArrowUp'?-1:1)];next=cells.filter(node=>node.dataset.healthProject===project)[column];}
    event.preventDefault();if(next){cells.forEach(node=>node.tabIndex=node===next?0:-1);next.focus({preventScroll:true});next.scrollIntoView?.({block:'nearest',inline:'nearest'});}
  },{signal:listeners.signal});
  doc.addEventListener('visibilitychange',()=>{if(doc.hidden){clearTimeout(timer);generation++;pending?.abort();pending=null;}else refresh();},{signal:listeners.signal});
  draw();refresh();
  return {
    refresh,
    setObservabilityContext(next={}) {
      if(disposed)return;
      rememberDisclosures();
      if(next.start===null&&next.end===null&&next.run_number==null)selection=null;
      const nextProject=next.project===undefined?commandProject:next.project;
      if(JSON.stringify(commandProject)!==JSON.stringify(nextProject))selection=null;
      commandProject=nextProject;
      const updated={...state,mode:['runs','hours'].includes(next.mode)?next.mode:[7,14,30].includes(Number(next.days))?String(next.days):state.mode};
      for(const key of ['scope','branch','search','failuresOnly'])if(next[key]!==undefined)updated[key]=next[key];
      if(['mode','scope','branch','search','failuresOnly'].some(key=>updated[key]!==state[key]))selection=null;
      state=ciHealthPreferences(updated);syncControls();save();draw();
    },
    setLayout(value){if(disposed || !['map','tree'].includes(value))return;layout=value;applyLayout();},
    selectedRun(record){if(disposed)return;selected=record;selectedKey=record?runKey(record):'';draw();},
    setContext(next={}){if(disposed)return;rememberDisclosures();save();context={repository:next.repository || '',mission:next.mission || '',session:next.session || ''};load();for(const name of ['search','branch'])if(typeof next[name]==='string')state[name]=bounded(next[name]);selected=null;selectedKey='';treeLimits.clear();syncControls();draw(false);},
    dispose(){disposed=true;generation++;clearTimeout(timer);pending?.abort();pending=null;listeners.abort();},
  };
}


export function ciHealthProjectHistory(history,project) {
  if(!project)return history;
  const same=row=>String(row.provider_url||'').replace(/\/+$/,'')===String(project.provider_url||'').replace(/\/+$/,'') && String(row.provider_repo_id??row.id)===String(project.provider_repo_id) && row.repository===project.repository;
  return {...history,repos:rows(history.repos).filter(same),pipelines:rows(history.pipelines).filter(same)};
}
