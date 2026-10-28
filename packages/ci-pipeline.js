// Provider-reported dependencies only: array order is never an execution edge.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
import {reportTimestamp} from './record-state.js';
import {ciTestClassification} from './ci-observability-view.js';
import {ciRunKey} from './ci-performance.js';

const rows=value=>Array.isArray(value)?value.filter(v=>v && typeof v==='object' && !Array.isArray(v)):[];
const text=value=>typeof value==='string'?value:'';
const dependencies=value=>Array.isArray(value)?value.filter(v=>typeof v==='string'||typeof v==='number').map(String):[];
const states={success:['success','Passed'],passed:['success','Passed'],failure:['failure','Failed'],failed:['failure','Failed'],error:['failure','Error'],killed:['failure','Killed'],running:['running','Running'],started:['running','Running'],pending:['queued','Queued'],queued:['queued','Queued'],waiting:['queued','Waiting'],skipped:['skipped','Skipped'],blocked:['blocked','Blocked'],canceled:['skipped','Canceled'],cancelled:['skipped','Canceled']};
function timing(task) {
  const start=new Date(reportTimestamp(task.started_at ?? task.started)).getTime();
  const end=new Date(reportTimestamp(task.finished_at ?? task.finished)).getTime();
  if(Number.isFinite(start)&&Number.isFinite(end)&&end>=start)return formatDuration(end-start);
  if(Number.isFinite(start)&&['running','started'].includes(task.state))return `Elapsed <span data-integration-time="${start}">${esc(formatDuration(Math.max(0,Date.now()-start)))}</span>`;
  return 'Duration not reported';
}
function graph(tasks) {
  const names=new Map(), ids=new Map();
  tasks.forEach((task,index)=>{
    const name=text(task.name), id=String(task.id ?? '');
    if(name)names.set(name,names.has(name)?null:index);
    if(id)ids.set(id,ids.has(id)?null:index);
  });
  const reported=tasks.some(task=>Array.isArray(task.depends_on));
  if(!reported)return {note:'Dependencies not reported. Steps are grouped by workflow; their position does not imply execution order.'};
  // Missing dependency arrays mean unknown, including when siblings have metadata.
  if(tasks.some(task=>!Array.isArray(task.depends_on)||task.depends_on.some(value=>!(typeof value==='string'&&value.trim())&&!(typeof value==='number'&&Number.isFinite(value)))))return {note:'Dependency graph is incomplete or ambiguous. Showing reported workflow groups.'};
  const parents=tasks.map(task=>dependencies(task.depends_on).map(key=>{
    const byName=names.get(key), byId=ids.get(key);
    return byName !== undefined && byId !== undefined && byName !== byId ? null : byName !== undefined ? byName : byId;
  }));
  if(parents.some(list=>list.some(index=>index==null)))return {note:'Dependency graph is incomplete or ambiguous. Showing reported workflow groups.'};
  const columns=[], visiting=new Set();
  function column(index) {
    if(columns[index]!==undefined)return columns[index];
    if(visiting.has(index))throw new Error('cycle');
    visiting.add(index);
    columns[index]=parents[index].length?1+Math.max(...parents[index].map(column)):0;
    visiting.delete(index);
    return columns[index];
  }
  try {tasks.forEach((_,i)=>column(i));} catch {return {note:'Dependency graph is incomplete or ambiguous. Showing reported workflow groups.'};}
  const counts=new Map();
  const positions=columns.map(col=>{const row=counts.get(col)||0;counts.set(col,row+1);return {col,row};});
  return {parents,positions,width:Math.max(...columns)+1,height:Math.max(...counts.values()),note:'Arrows show reported dependencies. Steps in the same column can run in parallel when dependencies permit.'};
}
function stepButton(task,selected,position) {
  const raw=text(task.state || task.status).toLowerCase();
  const [state,label]=states[raw] || ['unknown',raw?`Unknown (${raw})`:'Unknown'];
  const deps=dependencies(task.depends_on);
  return `<button type="button" class="ci-pipeline-step" data-step="${esc(String(task.id ?? ''))}" aria-pressed="${String(task.id ?? '')===String(selected)}" data-state="${state}"${position?` data-column="${position.col}" data-row="${position.row}" style="grid-column:${position.col+1};grid-row:${position.row+1}"`:''}><strong>${esc(text(task.name)||`Step ${task.id ?? '?'}`)}</strong><span class="ci-pipeline-state">${esc(label)}</span><small>${timing(task)}</small>${deps.length?`<small>Depends on: ${esc(deps.join(', '))}</small>`:''}${task.error?`<small class="ci-pipeline-error">${esc(String(task.error))}</small>`:''}</button>`;
}
function workflowLane(group,selected,collapseSuccess,zoom) {
  const successful=collapseSuccess?group.tasks.filter(task=>['success','passed'].includes(text(task.state||task.status).toLowerCase()) && String(task.id)!==String(selected)):[];
  const tasks=group.tasks.filter(task=>!successful.includes(task));
  const model=successful.length?{note:'Successful steps collapsed. Expand all stages to inspect reported dependency arrows.'}:graph(tasks);
  let connectors='';
  if(model.positions) {
    const y=position=>(position.row+.5)*140;
    connectors=`<svg class="ci-pipeline-edges" viewBox="0 0 1000 ${model.height*140}" preserveAspectRatio="none" aria-hidden="true" focusable="false">${model.parents.flatMap((parents,i)=>parents.map(parent=>{
      const a=model.positions[parent],b=model.positions[i];
      const from=(a.col+1)/model.width*1000-18,to=b.col/model.width*1000+18;
      return `<path d="M ${from} ${y(a)} C ${(from+to)/2} ${y(a)}, ${(from+to)/2} ${y(b)}, ${to} ${y(b)}"/><path class="ci-pipeline-arrow" d="M ${to-7} ${y(b)-5} L ${to} ${y(b)} L ${to-7} ${y(b)+5}"/>`;
    })).join('')}</svg>`;
  }
  return `<section class="ci-pipeline-lane"><h3>${esc(group.name)}</h3>${group.workflow?`<p class="ci-pipeline-note">${group.workflow.state||group.workflow.status?`Workflow: ${esc(String(group.workflow.state||group.workflow.status))} · `:''}${dependencies(group.workflow.depends_on).length?`Workflow depends on: ${esc(dependencies(group.workflow.depends_on).join(', '))}`:''}</p>`:''}<p class="ci-pipeline-note">${model.note}</p><div class="ci-pipeline-scroll" tabindex="0" role="region" aria-label="${esc(group.name)} steps"><div class="ci-pipeline-canvas ${model.positions?'ci-pipeline-graph':'ci-pipeline-group'}" style="zoom:${zoom};${model.positions?`--pipeline-columns:${model.width};--pipeline-rows:${model.height}`:''}">${connectors}${tasks.map((task,i)=>stepButton(task,selected,model.positions?.[i])).join('')}</div></div>${successful.length?`<details class="ci-pipeline-success"><summary>${successful.length} successful steps</summary><div class="ci-pipeline-canvas ci-pipeline-group">${successful.map(task=>stepButton(task,selected)).join('')}</div></details>`:''}</section>`;
}
export function renderCiPipeline(pipeline={}, {step='',collapseSuccess=false,zoom=1,diagnosis=null}={}) {
  zoom=pipelineZoom(zoom);
  const tasks=rows(pipeline?.tasks);
  if(!tasks.length)return renderCiDiagnosis(diagnosis)+'<p class="ci-pipeline-note">No steps reported yet. Waiting for the CI provider.</p>';
  const groups=new Map(), workflows=rows(pipeline?.workflows);
  for(const task of tasks) {
    const key=String(task.workflow_id ?? task.workflow ?? 'ungrouped');
    if(!groups.has(key))groups.set(key,{name:text(task.workflow)||'Steps',workflow:workflows.find(w=>task.workflow_id!=null?String(w.id)===String(task.workflow_id):w.name===task.workflow),tasks:[]});
    groups.get(key).tasks.push(task);
  }
  return `${renderCiDiagnosis(diagnosis)}<div class="ci-pipeline" aria-label="Pipeline workflows"><p class="ci-pipeline-note">Select a step to inspect its log. Status and timings reflect the latest provider report.</p>${[...groups.values()].map(group=>workflowLane(group,step,collapseSuccess,zoom)).join('')}</div>`;
}
export const ciPipelineStyles=`<style>
.ci-pipeline{min-width:0;max-width:100%}.ci-pipeline-lane{margin:18px 0;min-width:0}.ci-pipeline-lane h3{font-size:14px;margin:8px 0}.ci-pipeline-note{font-size:12px;color:var(--text-secondary);line-height:1.6;overflow-wrap:anywhere}.ci-pipeline-scroll{overflow:auto;max-width:100%;padding:3px}.ci-pipeline-canvas{position:relative;display:grid;gap:12px;align-items:center}.ci-pipeline-group{grid-template-columns:repeat(auto-fit,minmax(min(210px,100%),1fr))}.ci-pipeline-graph{grid-template-columns:repeat(var(--pipeline-columns),minmax(180px,1fr));grid-template-rows:repeat(var(--pipeline-rows),128px);gap:12px 36px;min-width:calc(var(--pipeline-columns)*216px - 36px)}.ci-pipeline-step{position:relative;z-index:1;display:flex;flex-direction:column;gap:7px;min-width:0;max-height:128px;overflow:auto;text-align:left;padding:12px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-left:4px solid var(--text-secondary);border-radius:var(--radius,8px);font:inherit;cursor:pointer;overflow-wrap:anywhere}.ci-pipeline-step strong{font-size:13px}.ci-pipeline-step small{font-size:11px;color:var(--text-secondary)}.ci-pipeline-step[data-state="failure"]{border-left-color:var(--ci-fail)}.ci-pipeline-step[data-state="success"]{border-left-color:var(--ci-pass)}.ci-pipeline-step[data-state="running"]{border-left-color:var(--accent,#237bc2)}.ci-pipeline-step[data-state="queued"]{border-left-color:var(--ci-queued)}.ci-pipeline-step[data-state="blocked"]{border-left-color:var(--ci-blocked)}.ci-pipeline-step[data-state="skipped"]{border-left-style:dashed}.ci-pipeline-step[aria-pressed="true"]{outline:2px solid var(--accent);outline-offset:1px}.ci-pipeline-step:focus-visible,.ci-pipeline-scroll:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.ci-pipeline-state{font-size:12px;font-weight:600}.ci-pipeline-step[data-state="failure"] .ci-pipeline-state{color:var(--ci-fail)}.ci-pipeline-step[data-state="success"] .ci-pipeline-state{color:var(--ci-pass)}.ci-pipeline-edges{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}.ci-pipeline-edges path{fill:none;stroke:var(--text-secondary);stroke-width:1.5;vector-effect:non-scaling-stroke}
@media(max-width:650px){.ci-pipeline-graph,.ci-pipeline-group{grid-template-columns:minmax(0,1fr);grid-template-rows:none;min-width:0}.ci-pipeline-step{grid-column:1!important;grid-row:auto!important;max-height:none}.ci-pipeline-edges{display:none}}
</style>`;

export function pipelineZoom(value) {
  return Number.isFinite(Number(value))?Math.max(.75,Math.min(1.5,Number(value))):1;
}
export function sameCiRepository(a,b) {
  const left=JSON.parse(ciRunKey(a)),right=JSON.parse(ciRunKey(b));
  return !!left[0] && !!left[1] && left[0]===right[0] && left[1]===right[1];
}
const measuredDuration=row=>{
 const start=new Date(reportTimestamp(row.started_at??row.started)).getTime(),end=new Date(reportTimestamp(row.finished_at??row.finished)).getTime();
 return Number.isFinite(start)&&Number.isFinite(end)&&end>=start?end-start:null;
};
export function ciComparisonModel(active,other) {
 if(!active || !other || !sameCiRepository(active,other) || ciRunKey(active)===ciRunKey(other))return null;
 const a=measuredDuration(active),b=measuredDuration(other);
 const key=task=>JSON.stringify([task.workflow??task.workflow_id??'',text(task.name)]);
 const group=tasks=>{const map=new Map();for(const task of rows(tasks)){const k=key(task);map.set(k,map.has(k)?null:task);}return map;};
 const left=group(active.tasks),right=group(other.tasks),keys=[...new Set([...left.keys(),...right.keys()])];
 const steps=keys.slice(0,200).map(k=>{
  const l=left.get(k),r=right.get(k),ld=l?measuredDuration(l):null,rd=r?measuredDuration(r):null;
  const state=(map,row)=>map.has(k)&&row===null?'Ambiguous step':row?text(row.state||row.status)||'Unknown':'Not reported';
  return {name:text(l?.name||r?.name)||'Unnamed step',workflow:String(l?.workflow??r?.workflow??''),active:state(left,l),other:state(right,r),durationDeltaMs:ld!==null&&rd!==null?ld-rd:null};
 });
 return {active,other,durationDeltaMs:a!==null&&b!==null?a-b:null,steps,truncated:keys.length>steps.length};
}
export function renderCiComparison(model) {
 if(!model)return '';
 const {active,other}=model,delta=value=>value===null?'Duration not reported':`${value>=0?'+':'−'}${formatDuration(Math.abs(value))}`;
 return `<section class="ci-comparison" aria-label="Observed run comparison"><h3>Observed run comparison</h3><p class="ig-note">Active #${esc(active.run_number)} compared with #${esc(other.run_number)}. Logs and actions apply to the active run only. Missing evidence is not a change.</p><div class="ci-comparison-scroll" tabindex="0" role="region" aria-label="Run comparison"><table><thead><tr><th>Evidence</th><th>Active #${esc(active.run_number)}</th><th>Compared #${esc(other.run_number)}</th></tr></thead><tbody><tr><th>State</th><td>${esc(active.state||active.status||'Unknown')}</td><td>${esc(other.state||other.status||'Unknown')}</td></tr><tr><th>Revision</th><td>${esc(active.revision||'Not reported')}</td><td>${esc(other.revision||'Not reported')}</td></tr><tr><th>Duration</th><td>${measuredDuration(active)===null?'Duration not reported':esc(formatDuration(measuredDuration(active)))}</td><td>${measuredDuration(other)===null?'Duration not reported':esc(formatDuration(measuredDuration(other)))}</td></tr></tbody></table></div><p class="ig-note">Active duration difference: ${esc(delta(model.durationDeltaMs))}</p><p class="ig-note">Steps match by reported workflow and name. ${!Array.isArray(active.tasks)||!Array.isArray(other.tasks)?'Step details not reported for one or both runs.':''}</p><div class="ci-comparison-scroll" tabindex="0" role="region" aria-label="Step differences"><table><thead><tr><th>Step</th><th>Active</th><th>Compared</th><th>Duration difference</th></tr></thead><tbody>${model.steps.map(step=>`<tr><th>${esc(step.workflow)} · ${esc(step.name)}</th><td>${esc(step.active)}</td><td>${esc(step.other)}</td><td>${esc(delta(step.durationDeltaMs))}</td></tr>`).join('')}</tbody></table></div>${!model.steps.length?'<p class="ig-note">No step details reported.</p>':''}${model.truncated?'<p class="ig-note">Showing the first 200 observed step identities.</p>':''}</section>`;
}


export function renderCiDiagnosis(diagnosis) {
  if(!diagnosis)return '';
  const bounded=(value,limit=500)=>esc(String(value??'').slice(0,limit));
  const tests=rows(diagnosis.failed_tests).slice(0,25);
  return `<section class="ci-diagnosis" aria-label="Failure diagnosis"><h3>Failure diagnosis</h3>
    ${rows(diagnosis.failed_workflows).length?`<p class="ci-pipeline-note">Failed workflows: ${rows(diagnosis.failed_workflows).slice(0,50).map(w=>bounded(w.name||w.id)).join(', ')}</p>`:''}
    ${rows(diagnosis.failed_steps).slice(0,50).map(s=>`<p class="ci-pipeline-note"><button type="button" class="hd-btn" data-step="${bounded(s.id)}">${bounded(s.name||s.id)} · Open step log</button> ${bounded(s.state||s.status)}${s.exit_code!=null?` · Exit ${bounded(s.exit_code)}`:''} · ${s.dependency_evidence==='reported'?`Reported dependencies: ${bounded(dependencies(s.depends_on).join(', ')||'none')}`:'Ordering not reported'}${s.error?` · ${bounded(s.error)}`:''}</p>`).join('')}
    <p class="ci-pipeline-note">${bounded(diagnosis.step_test_reason||'Test-to-step association is not reported.')} Logs load on step selection in the existing log pane; log text is not structured test evidence.</p>
    ${diagnosis.evidence_availability!=='reported'?`<p class="ci-pipeline-note">Test evidence unavailable: ${bounded(diagnosis.reason||'No validated report was supplied.')}</p>`:tests.length?tests.map(t=>`<article><h4>${bounded(t.name||t.id)}</h4><p class="ci-pipeline-note">${bounded(t.classname)} · ${bounded(t.file||'File not reported')} · ${Number.isFinite(t.duration_ms)?esc(formatDuration(t.duration_ms)):'Duration not reported'}</p><pre class="ig-log">${bounded(t.message)}\n${bounded(t.trace,1000)}</pre>${t.history?`<p class="ci-pipeline-note">${ciTestClassification({...t.history,identity_ambiguous:t.identity_ambiguous})} · ${bounded(t.history.failure_count)} / ${bounded(t.history.completed_count)} reported completed attempts · First failure: ${bounded(t.history.first_failure||'Not observed')} · Last pass: ${bounded(t.history.last_pass||'Not observed')}</p>`:''}</article>`).join(''):'<p class="ci-pipeline-note">Validated reports contain no reported failing test cases.</p>'}
    ${diagnosis.history_scope?`<p class="ci-pipeline-note">Recurrence uses retained ${bounded(diagnosis.history_scope.days)}-day report history; coverage is partial.</p>`:''}${diagnosis.truncated?'<p class="ci-pipeline-note">Evidence is bounded; additional failures or text were truncated.</p>':''}</section>`;
}
