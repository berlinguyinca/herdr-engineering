// Provider-reported dependencies only: array order is never an execution edge.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
import {reportTimestamp} from './record-state.js';

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
function workflowLane(group,selected) {
  const model=graph(group.tasks);
  let connectors='';
  if(model.positions) {
    const y=position=>(position.row+.5)*140;
    connectors=`<svg class="ci-pipeline-edges" viewBox="0 0 1000 ${model.height*140}" preserveAspectRatio="none" aria-hidden="true" focusable="false">${model.parents.flatMap((parents,i)=>parents.map(parent=>{
      const a=model.positions[parent],b=model.positions[i];
      const from=(a.col+1)/model.width*1000-18,to=b.col/model.width*1000+18;
      return `<path d="M ${from} ${y(a)} C ${(from+to)/2} ${y(a)}, ${(from+to)/2} ${y(b)}, ${to} ${y(b)}"/><path class="ci-pipeline-arrow" d="M ${to-7} ${y(b)-5} L ${to} ${y(b)} L ${to-7} ${y(b)+5}"/>`;
    })).join('')}</svg>`;
  }
  return `<section class="ci-pipeline-lane"><h3>${esc(group.name)}</h3>${group.workflow?`<p class="ci-pipeline-note">${group.workflow.state||group.workflow.status?`Workflow: ${esc(String(group.workflow.state||group.workflow.status))} · `:''}${dependencies(group.workflow.depends_on).length?`Workflow depends on: ${esc(dependencies(group.workflow.depends_on).join(', '))}`:''}</p>`:''}<p class="ci-pipeline-note">${model.note}</p><div class="ci-pipeline-scroll" tabindex="0" role="region" aria-label="${esc(group.name)} steps"><div class="ci-pipeline-canvas ${model.positions?'ci-pipeline-graph':'ci-pipeline-group'}"${model.positions?` style="--pipeline-columns:${model.width};--pipeline-rows:${model.height}"`:''}>${connectors}${group.tasks.map((task,i)=>stepButton(task,selected,model.positions?.[i])).join('')}</div></div></section>`;
}
export function renderCiPipeline(pipeline={}, {step=''}={}) {
  const tasks=rows(pipeline?.tasks);
  if(!tasks.length)return '<p class="ci-pipeline-note">No steps reported yet. Waiting for the CI provider.</p>';
  const groups=new Map(), workflows=rows(pipeline?.workflows);
  for(const task of tasks) {
    const key=String(task.workflow_id ?? task.workflow ?? 'ungrouped');
    if(!groups.has(key))groups.set(key,{name:text(task.workflow)||'Steps',workflow:workflows.find(w=>task.workflow_id!=null?String(w.id)===String(task.workflow_id):w.name===task.workflow),tasks:[]});
    groups.get(key).tasks.push(task);
  }
  return `<div class="ci-pipeline" aria-label="Pipeline workflows"><p class="ci-pipeline-note">Select a step to inspect its log. Status and timings reflect the latest provider report.</p>${[...groups.values()].map(group=>workflowLane(group,step)).join('')}</div>`;
}
export const ciPipelineStyles=`<style>
.ci-pipeline{min-width:0;max-width:100%}.ci-pipeline-lane{margin:18px 0;min-width:0}.ci-pipeline-lane h3{font-size:14px;margin:8px 0}.ci-pipeline-note{font-size:12px;color:var(--text-secondary);line-height:1.6;overflow-wrap:anywhere}.ci-pipeline-scroll{overflow:auto;max-width:100%;padding:3px}.ci-pipeline-canvas{position:relative;display:grid;gap:12px;align-items:center}.ci-pipeline-group{grid-template-columns:repeat(auto-fit,minmax(min(210px,100%),1fr))}.ci-pipeline-graph{grid-template-columns:repeat(var(--pipeline-columns),minmax(180px,1fr));grid-template-rows:repeat(var(--pipeline-rows),128px);gap:12px 36px;min-width:calc(var(--pipeline-columns)*216px - 36px)}.ci-pipeline-step{position:relative;z-index:1;display:flex;flex-direction:column;gap:7px;min-width:0;max-height:128px;overflow:auto;text-align:left;padding:12px;background:var(--surface);color:var(--text);border:1px solid var(--border);border-left:4px solid var(--text-secondary);border-radius:var(--radius,8px);font:inherit;cursor:pointer;overflow-wrap:anywhere}.ci-pipeline-step strong{font-size:13px}.ci-pipeline-step small{font-size:11px;color:var(--text-secondary)}.ci-pipeline-step[data-state="failure"]{border-left-color:#dc3545}.ci-pipeline-step[data-state="success"]{border-left-color:#238636}.ci-pipeline-step[data-state="running"]{border-left-color:var(--accent,#237bc2)}.ci-pipeline-step[data-state="queued"]{border-left-color:#bf8700}.ci-pipeline-step[data-state="blocked"]{border-left-color:#a56ae0}.ci-pipeline-step[data-state="skipped"]{border-left-style:dashed}.ci-pipeline-step[aria-pressed="true"]{outline:2px solid var(--accent);outline-offset:1px}.ci-pipeline-step:focus-visible,.ci-pipeline-scroll:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.ci-pipeline-state{font-size:12px;font-weight:600}.ci-pipeline-step[data-state="failure"] .ci-pipeline-state{color:#dc3545}.ci-pipeline-step[data-state="success"] .ci-pipeline-state{color:#238636}.ci-pipeline-edges{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}.ci-pipeline-edges path{fill:none;stroke:var(--text-secondary);stroke-width:1.5;vector-effect:non-scaling-stroke}
@media(max-width:650px){.ci-pipeline-graph,.ci-pipeline-group{grid-template-columns:minmax(0,1fr);grid-template-rows:none;min-width:0}.ci-pipeline-step{grid-column:1!important;grid-row:auto!important;max-height:none}.ci-pipeline-edges{display:none}}
</style>`;
