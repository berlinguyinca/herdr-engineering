// Pure CI history projection. Coverage is evidence supplied by the provider cache.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
const list=value=>Array.isArray(value)?value.filter(row=>row&&typeof row==='object'):[];
const text=value=>value==null?'':String(value);
const time=value=>{if(value==null||value==='')return NaN;const n=typeof value==='number'?(value<1e12?value*1000:value):Date.parse(value);return Number.isFinite(n)?n:NaN;};
const instance=row=>text(row.provider_url).replace(/\/$/,'');
const repoId=row=>text(row.provider_repo_id??row.id??row.repository_id??row.repository);
const projectKey=row=>JSON.stringify([instance(row),repoId(row)]);
const outcome=row=>{const state=text(row.state||row.status).toLowerCase();if(['success','passed'].includes(state))return 'success';if(['failure','failed','error','errored','timed_out','crashed'].includes(state))return 'failure';if(['canceled','cancelled','killed','declined'].includes(state))return 'canceled';if(state==='skipped')return 'skipped';if(['running','started'].includes(state))return 'running';if(['pending','queued','waiting','blocked'].includes(state))return 'queued';return 'unknown';};
const counts=runs=>{const result={success:0,failure:0,canceled:0,skipped:0,running:0,queued:0,unknown:0,total:runs.length};for(const run of runs)result[outcome(run)]++;return result;};
const chronological=(a,b)=>(Number.isFinite(a.timestamp)?a.timestamp:-Infinity)-(Number.isFinite(b.timestamp)?b.timestamp:-Infinity)||Number(a.run_number)-Number(b.run_number)||a.runKey.localeCompare(b.runKey);
const iso=value=>new Date(value).toISOString();
const repositoryName=repo=>text(repo.repository||repo.full_name||repo.name)||'Repository unavailable';
function projectLabels(inventory) {
 const labels=new Map(),groups=new Map();
 for(const [key,repo] of inventory){const name=repositoryName(repo);labels.set(key,name);if(!groups.has(name))groups.set(name,[]);groups.get(name).push({key,repo});}
 for(const [name,group] of groups) {
  if(group.length<2)continue;
  const providers=group.map(row=>{const provider=instance(row.repo);try{const url=new URL(provider);return {...row,provider,host:url.host,path:`${url.host}${url.pathname.replace(/\/$/,'')}`};}catch{return {...row,provider,host:provider||'Provider unavailable',path:provider||'Provider unavailable'};}});
  for(const row of providers) {
   const peers=providers.filter(peer=>peer.key!==row.key),parts=row.host.split('.');
   let suffix=row.host;
   for(let size=Math.min(2,parts.length);size<=parts.length;size++) {
    const candidate=parts.slice(-size).join('.');
    if(peers.every(peer=>peer.host!==candidate&&!peer.host.endsWith(`.${candidate}`))){suffix=candidate;break;}
   }
   if(peers.some(peer=>peer.host===row.host)) {
    suffix=row.path;
    if(peers.some(peer=>peer.path===row.path&&peer.provider!==row.provider))suffix=row.provider;
    if(peers.some(peer=>peer.provider===row.provider))suffix+=` #${repoId(row.repo)}`;
   }
   labels.set(row.key,`${name} · ${suffix}`);
  }
 }
 return labels;
}
function pullRequest(row,repo) {
 let number=null;
 const explicit=row.pull_request_number??row.pr_number??(typeof row.pull_request==='object'?row.pull_request?.number:row.pull_request);
 if(/^\d+$/.test(text(explicit))&&Number(explicit)>0)number=Number(explicit);
 const ref=text(row.ref||row.ref_name),match=/^refs\/pull\/(\d+)\/(?:head|merge)$/.exec(ref)||/^refs\/merge-requests\/(\d+)\/(?:head|merge)$/.exec(ref);
 if(match&&Number(match[1])>0)number=Number(match[1]);
 // Only an exact repository-scoped URL on the reported forge can establish identity.
 for(const raw of [row.pull_request_url,row.pr_url,row.ref,row.forge_url]) {
  if(!raw||!repo.forge_url||!repo.repository)continue;
  try {const url=new URL(raw),forge=new URL(repo.forge_url);const root=forge.pathname.replace(/\/$/,'');const repoPath=`/${repo.repository}`;const prefix=`${root.endsWith(repoPath)?root:`${root}${repoPath}`}/`;if(url.origin!==forge.origin||url.username||url.password||url.search||url.hash||!url.pathname.startsWith(prefix))continue;const match=/^(?:pull|pulls|merge_requests|-\/merge_requests)\/(\d+)$/.exec(url.pathname.slice(prefix.length));if(match&&Number(match[1])>0)number=Number(match[1]);}catch{ /* Untrusted or relative URLs cannot identify a PR. */ }
 }
 const event=text(row.event||row.event_type).toLowerCase();
 return {isPr:number!==null||['pull_request','pull_request_target','pull_request_closed','pull_request_opened','pull_request_sync','merge_request'].includes(event)||event.startsWith('pull_request:'),pr:number===null?null:{number,key:JSON.stringify([projectKey(repo),'pr',number]),title:text(row.pull_request?.title||row.pr_title),source:text(row.pull_request?.source_branch||row.source_branch),target:text(row.pull_request?.target_branch||row.target_branch)}};
}
export function ciRunInScope(row,scope='all',repo={}) {
 const {isPr}=pullRequest(row,repo);
 return scope==='all'||scope==='pr'&&isPr||scope==='default'&&!!repo.default_branch&&!isPr&&row.branch===repo.default_branch;
}
export function ciRunInWindow(row,{days=0,start=null,end=null,mode='days'}={},now=Date.now()) {
 const stamp=time(row.created_at??row.created);
 if(mode==='runs'&&!Number.isFinite(stamp))return start==null&&end==null;
 const lower=mode==='runs'?-Infinity:mode==='hours'?now-86400000:Number(days)?Math.floor(now/86400000)*86400000-(Number(days)-1)*86400000:-Infinity;
 return (mode==='days'&&!Number(days)&&start==null&&end==null)||Number.isFinite(stamp)&&stamp>=lower&&stamp<=now&&(start==null||stamp>=time(start))&&(end==null||stamp<time(end));
}

export function ciSelectRunWindow(runs,filters={},now=Date.now()) {
 let selected=runs.filter(row=>ciRunInWindow(row,filters,now));
 if(filters.mode==='runs'&&filters.run_number==null) {
  const groups=new Map();
  for(const row of selected){const key=JSON.stringify([instance(row),repoId(row),row.repository]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);}
  selected=[...groups.values()].flatMap(group=>group.sort((a,b)=>(time(a.created_at)||-Infinity)-(time(b.created_at)||-Infinity)||Number(a.run_number)-Number(b.run_number)).slice(-24));
 }
 return filters.run_number==null?selected:selected.filter(row=>String(row.run_number)===String(filters.run_number));
}

function windows(mode,now) {
 if(mode==='runs')return Array.from({length:24},(_,i)=>({key:`run:${i}`,label:`${24-i} ${i===23?'(newest)':''}`.trim(),start:null,end:null}));
 const hour=3600000,day=86400000;
 if(mode==='hours') {
  // Stable UTC bounds travel with the selection; membership intersects the trailing window below.
  const start=Math.floor((now-day)/hour)*hour;
  return Array.from({length:25},(_,i)=>{const boundary=start+i*hour;return {key:`hour:${boundary}`,label:iso(boundary).slice(11,16),start:boundary,end:boundary+hour};});
 }
 const today=Math.floor(now/day)*day;
 // Selection carries stable calendar bounds; run membership is capped at now below.
 return Array.from({length:Number(mode)},(_,i)=>{const start=today-(Number(mode)-1-i)*day;return {key:`day:${iso(start).slice(0,10)}`,label:iso(start).slice(0,10),start,end:start+day};});
}
function makeCell(bucket,runs,complete) {
 const c=counts(runs),state=c.failure?(c.success?'mixed':'failure'):c.success?'success':c.running||c.queued?'active':runs.length?'neutral':complete?'empty':'incomplete';
 return {...bucket,runs,counts:c,state,active:c.running+c.queued>0,complete};
}
export function ciHealthModel(history={},options={},now=Date.now()) {
 const mode=['hours','runs','7','14','30'].includes(text(options.mode))?text(options.mode):'7';
 const scope=['default','pr','all'].includes(options.scope)?options.scope:'default',buckets=windows(mode,now);
 const inventory=new Map();for(const repo of list(history.repos))inventory.set(projectKey(repo),{...repo});
 for(const row of list(history.pipelines)) {const key=projectKey(row);if(!inventory.has(key))inventory.set(key,{id:row.provider_repo_id,provider_url:row.provider_url,repository:row.repository,default_branch:row.default_branch,forge_url:row.forge_url});}
 const labels=projectLabels(inventory);
 const dedup=new Map();for(const raw of list(history.pipelines)){const key=projectKey(raw),runKey=JSON.stringify([instance(raw),repoId(raw),raw.run_number??raw.id]);const normalized={...raw,projectKey:key,runKey,timestamp:time(raw.created_at??raw.created)};const old=dedup.get(runKey);if(!old||time(raw.updated_at)>=time(old.updated_at)||!Number.isFinite(time(old.updated_at)))dedup.set(runKey,normalized);}
 const terms=text(options.search).toLowerCase().trim().split(/\s+/).filter(Boolean),projects=[];
 for(const [key,repo] of inventory) {
  const repository=repositoryName(repo);
  if(options.repository&&![key,repository,repoId(repo),repo.repository_id].includes(text(options.repository)))continue;
  const coverage=history.coverage?.[key]??history.coverage?.[repoId(repo)]??{};
  const scopeUnavailable=scope==='default'&&!repo.default_branch;
  let runs=[...dedup.values()].filter(row=>row.projectKey===key).map(row=>({...row,...pullRequest(row,repo)})).filter(row=>ciRunInScope(row,scope,repo)&&(!options.branch||row.branch===options.branch));
  const projectText=[repository,instance(repo),repoId(repo)].join(' ').toLowerCase();
  runs=runs.filter(row=>terms.every(term=>[projectText,row.branch,row.run_number,row.revision,row.title,row.state,row.pr?.number].map(text).join(' ').toLowerCase().includes(term)));
  if(terms.length&&!runs.length&&!terms.every(term=>projectText.includes(term)))continue;
  runs.sort(chronological);
  const unknownTimestamps=runs.some(row=>!Number.isFinite(row.timestamp));
  if(mode==='runs')runs=runs.filter(row=>!Number.isFinite(row.timestamp)||row.timestamp<=now).slice(-24);
  else runs=runs.filter(row=>ciRunInWindow(row,{mode:mode==='hours'?'hours':'days',days:Number(mode)},now));
  if(options.failuresOnly&&!runs.some(row=>outcome(row)==='failure'))continue;
  const since=time(coverage.complete_since),exhausted=coverage.exhausted===true;
  const completeAt=start=>!scopeUnavailable&&!unknownTimestamps&&(exhausted||Number.isFinite(since)&&since<=start);
  const runComplete=!scopeUnavailable&&!unknownTimestamps&&(exhausted||runs.length>=24&&completeAt(runs[0].timestamp));
  const cells=buckets.map((bucket,i)=>{if(mode==='runs'){const offset=24-runs.length,row=runs[i-offset];return makeCell(row?{...bucket,key:`run:${row.runKey}`}:bucket,row?[row]:[],runComplete);}return makeCell(bucket,runs.filter(row=>row.timestamp>=bucket.start&&row.timestamp<bucket.end),completeAt(mode==='hours'?Math.max(bucket.start,now-86400000):bucket.start));});
  const identity=`${repository} · ${instance(repo)||'Provider unavailable'} #${repoId(repo)}`;
  projects.push({key,repo,repository,label:labels.get(key),identity,runs,cells,counts:counts(runs),complete:cells.every(cell=>cell.complete),scopeUnavailable,coverage,unknownTimestamps});
 }
 projects.sort(options.sort==='failures'?(a,b)=>b.counts.failure-a.counts.failure||a.label.localeCompare(b.label)||a.identity.localeCompare(b.identity):(a,b)=>a.label.localeCompare(b.label)||a.identity.localeCompare(b.identity));
 let selectedRows=projects.flatMap(project=>project.runs);const selection=options.selection||null;
 if(selection){const project=projects.find(row=>row.key===selection.projectKey),cell=project?.cells.find(row=>row.key===selection.bucketKey);selectedRows=cell?.runs||[];}
 selectedRows.sort(chronological);
 return {mode,scope,options,now,buckets,projects,selectedRows,selection,counts:counts(projects.flatMap(project=>project.runs)),partial:projects.some(project=>!project.complete)||history.status==='partial',stale:!!history.stale,status:history.status||'unknown',refreshing:!!history.refreshing,historyScope:history.scope||{}};
}
const describe=c=>`${c.total} runs · ${c.success} success · ${c.failure} failure · ${c.canceled} canceled · ${c.skipped} skipped · ${c.running} running · ${c.queued} queued · ${c.unknown} unknown`;
const duration=row=>{const start=time(row.started_at??row.started),end=time(row.finished_at??row.finished);return Number.isFinite(start)&&Number.isFinite(end)&&end>=start?formatDuration(end-start):'Duration unavailable';};
const range=cell=>cell.start===null?'Ordinal run position; no date cutoff':`${iso(cell.start)} – ${iso(cell.end)} UTC`;
const symbol={success:'✓',failure:'×',mixed:'±',active:'●',neutral:'–',empty:'·',incomplete:'?'};
export function renderCiHeatmap(model) {
 const issues=[];
 if(model.historyScope?.inventory_error)issues.push(`Repository inventory: ${text(model.historyScope.inventory_error)}`);
 for(const project of model.projects) {
  const reasons=[project.coverage.error,project.coverage.truncated?'Provider safety limit reached':'',project.scopeUnavailable?'Default branch unavailable':'',project.unknownTimestamps?'Some run timestamps unavailable':''].filter(Boolean);
  if(reasons.length)issues.push(`${project.identity}: ${reasons.join(' · ')}`);
 }
 const diagnostics=issues.length?`<details class="ci-health-exact" data-health-disclosure="coverage-issues"><summary>Coverage issues (${issues.length})</summary><ul>${issues.slice(0,500).map(issue=>`<li>${esc(issue)}</li>`).join('')}</ul>${issues.length>500?`<p>${issues.length-500} additional project issues; narrow the repository filter for details.</p>`:''}</details>`:'';
 const rowOf=project=>`<tr><th scope="row" class="ci-health-project" title="${esc(project.identity)}">${esc(project.label)}${project.scopeUnavailable?'<small>Default branch unavailable</small>':''}${project.unknownTimestamps?'<small>Some run timestamps unavailable</small>':''}</th>${project.cells.map(cell=>{const label=`${project.identity} · ${range(cell)} · ${describe(cell.counts)}${model.mode==='runs'&&cell.runs.length?` · Run #${cell.runs[0].run_number} · ${cell.runs[0].branch||'Branch unavailable'} · ${cell.runs[0].revision||'Revision unavailable'} · ${cell.runs[0].state||cell.runs[0].status||'unknown'} · ${duration(cell.runs[0])}`:''} · ${cell.complete?'Complete coverage':'Incomplete coverage'}`;return `<td><button type="button" class="ci-health-cell state-${cell.state}${cell.complete?'':' is-incomplete'}${model.selection?.projectKey===project.key&&model.selection?.bucketKey===cell.key?' is-selected':''}" data-health-project="${esc(project.key)}" data-health-bucket="${esc(cell.key)}" aria-pressed="${model.selection?.projectKey===project.key&&model.selection?.bucketKey===cell.key?'true':'false'}" aria-label="${esc(label)}" title="${esc(label)}" tabindex="0">${symbol[cell.state]}${cell.active&&cell.state!=='active'?'<span class="ci-health-active" aria-hidden="true">●</span>':''}</button></td>`;}).join('')}</tr>`;
 // A project with no run in this window and no coverage problem is noise: fold it away, one disclosure, nothing dropped.
 const isIdle=project=>!project.runs.length&&!project.coverage.error&&!project.coverage.truncated&&!project.scopeUnavailable&&!project.unknownTimestamps;
 const busy=model.projects.filter(project=>!isIdle(project)),idle=busy.length?model.projects.filter(isIdle):[];
 const grid=busy.length?busy.map(rowOf).join(''):model.projects.map(rowOf).join('');
 const head=`<thead><tr><th class="ci-health-project" scope="col">Project</th>${model.buckets.map(bucket=>`<th scope="col" title="${esc(bucket.label)}" aria-label="${esc(bucket.label)}">${esc(['hours','runs'].includes(model.mode)?bucket.label:bucket.label.slice(5))}</th>`).join('')}</tr></thead>`;
 const idleBlock=idle.length?`<details class="ci-health-exact" data-health-disclosure="idle-projects"><summary>${idle.length} project${idle.length===1?'':'s'} with no runs in this window</summary><div class="ci-health-grid-scroll" tabindex="0" role="region" aria-label="Projects with no runs"><table class="ci-health-grid" style="--health-columns:${model.buckets.length}">${head}<tbody>${idle.map(rowOf).join('')}</tbody></table></div></details>`:'';
 return `<section class="ci-health-overview" aria-label="CI project health"><p class="ci-health-note">UTC · ${model.mode==='runs'?'Last 24 filtered runs per project; no date cutoff; newest at right':`Trailing ${model.mode==='hours'?'24 hours':`${model.mode} calendar days including today`}`} · Scope: ${esc(model.scope)} · ${model.counts.success} successful / ${model.counts.success+model.counts.failure} successful or failed runs. ${model.partial?'Incomplete coverage. ':''}${model.stale?'Stale snapshot. ':''}${model.refreshing?'Refreshing history. ':''}✓ success · × failure · ± mixed · ● active · – other outcomes · · no runs · ? incomplete.</p>${diagnostics}<div class="ci-health-grid-scroll" data-health-grid-scroll tabindex="0" aria-label="Scrollable CI health grid"><table class="ci-health-grid" style="--health-columns:${model.buckets.length}"><thead><tr><th class="ci-health-project" scope="col">Project</th>${model.buckets.map(bucket=>`<th scope="col" title="${esc(bucket.label)}" aria-label="${esc(bucket.label)}">${esc(['hours','runs'].includes(model.mode)?bucket.label:bucket.label.slice(5))}</th>`).join('')}</tr></thead><tbody>${grid}</tbody></table></div>${idleBlock}${!model.projects.length?'<p>No projects match these filters.</p>':''}<details class="ci-health-exact" data-health-disclosure="exact-counts"><summary>Exact counts (accessible table)</summary><div class="ci-health-grid-scroll" data-health-grid-scroll><table><thead><tr><th>Project</th><th>UTC range / run position</th><th>Outcomes</th><th>Coverage</th></tr></thead><tbody>${model.projects.flatMap(project=>project.cells.map(cell=>`<tr><th scope="row">${esc(project.identity)}</th><td>${esc(cell.start===null?cell.label:range(cell))}</td><td>${esc(describe(cell.counts))}</td><td>${cell.complete?'Complete':'Incomplete'}${project.scopeUnavailable?' — default branch unavailable':''}</td></tr>`)).join('')}</tbody></table></div></details></section>`;
}
export const ciHealthStyles=`
.ci-health-overview,.ci-health-detail{min-width:0;max-width:100%}.ci-health-note{font-size:.8rem;color:var(--text-muted,#68758a);line-height:1.6}.ci-health-grid-scroll{overflow:auto;max-height:440px;max-width:100%;min-width:0;border:1px solid var(--border,#d6dce5);border-radius:6px}.ci-health-grid{border-collapse:separate;border-spacing:0;table-layout:fixed;width:100%;min-width:max(100%,calc(200px + var(--health-columns,24)*36px));font-size:.7rem}.ci-health-grid th,.ci-health-grid td{padding:2px;text-align:center;min-width:24px;height:32px}.ci-health-grid thead th{position:sticky;top:0;z-index:2;background:var(--surface,#fff);font-weight:500}.ci-health-grid .ci-health-project{position:sticky;left:0;z-index:1;background:var(--surface,#fff);text-align:left;width:200px;min-width:140px;max-width:220px;padding:4px 8px;overflow-wrap:anywhere}.ci-health-grid thead .ci-health-project{z-index:3}.ci-health-project small{display:block;color:var(--warn-text,#9b5d00)}.ci-health-cell{position:relative;display:block;width:100%;min-width:24px;min-height:28px;border:1px solid transparent;border-radius:3px;cursor:pointer;font-size:15px;color:var(--text);background:var(--surface-2)}.ci-health-cell.state-success{background:color-mix(in srgb,var(--status-ok) 28%,var(--surface));color:var(--text)}.ci-health-cell.state-failure{background:color-mix(in srgb,var(--status-failed) 30%,var(--surface));color:var(--text)}.ci-health-cell.state-mixed{background:color-mix(in srgb,var(--status-attention) 32%,var(--surface));color:var(--text)}.ci-health-cell.state-active{background:color-mix(in srgb,var(--status-active) 28%,var(--surface));color:var(--text)}.ci-health-cell.state-neutral{background:color-mix(in srgb,var(--status-waiting) 25%,var(--surface));color:var(--text)}.ci-health-cell.state-incomplete{background:repeating-linear-gradient(135deg,var(--surface-2),var(--surface-2) 4px,var(--border) 4px,var(--border) 6px)}.ci-health-cell.is-incomplete{border-bottom:2px dashed var(--text-muted)}.ci-health-active{position:absolute;right:1px;top:-3px;color:var(--status-active);font-size:10px}.ci-health-cell.is-selected{box-shadow:inset 0 0 0 2px var(--text)}.ci-health-cell:focus-visible,.ci-health-detail button:focus-visible,.ci-health-detail summary:focus-visible{outline:3px solid var(--status-active);outline-offset:2px}.ci-health-detail details{padding:4px 0 4px 12px;max-width:100%}.ci-health-detail summary{cursor:pointer;overflow-wrap:anywhere;font-size:.85rem;line-height:1.6}.ci-health-latest{display:inline-block;font-size:.75rem;margin-left:8px;color:var(--text-muted,#68758a)}.ci-health-detail ul{list-style:none;padding-left:14px;margin:4px 0}.ci-health-detail button{background:none;border:0;color:inherit;cursor:pointer;text-align:left;padding:6px;overflow-wrap:anywhere;max-width:100%}.ci-health-detail button[aria-current]{background:var(--accent-weak,#e0edff);border-radius:4px}.ci-health-exact{margin-top:8px;font-size:.8rem}.ci-health-exact td,.ci-health-exact th{padding:6px;text-align:left}.ci-health-grid-scroll{resize:vertical;min-height:80px}@media(max-width:700px){.ci-health-grid .ci-health-project{width:160px;min-width:140px}.ci-health-grid{width:max-content}.ci-health-grid td,.ci-health-grid thead th:not(.ci-health-project){width:28px;min-width:28px}.ci-health-latest{display:block;margin-left:0}}
`;
