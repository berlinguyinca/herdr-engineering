// Review gate page: every gated PR, the review queue, main-branch status and recent notices.
// Every string from the API is untrusted; it is escaped and clipped before it reaches HTML.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {skeleton} from './motion.js';
import {freshnessSources} from './freshness.js';

const text=v=>typeof v==='string'?v:'';
const short=(v,max=160)=>text(v).replace(/\s+/g,' ').trim().slice(0,max);
const list=v=>Array.isArray(v)?v.filter(x=>x&&typeof x==='object'&&!Array.isArray(x)):[];
const num=v=>Number.isFinite(v)?v:null;
const KEY=/^github\.com\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+#[0-9]+$/;
const STATES={ci_pending:'Waiting for CI',ci_green:'CI green',review_queued:'Review queued',reviewing:'In review',changes_requested:'Changes requested',approved:'Approved',merged:'Merged',ci_red:'CI failing',superseded:'Superseded',escalated:'Escalated'};
const when=v=>{const n=num(v);if(n===null||n<=0)return '';const d=new Date(n<1e12?n*1000:n);return Number.isNaN(d.getTime())?'':d.toISOString().slice(0,16).replace('T',' ')+' UTC';};

export const shortSha=sha=>text(sha).slice(0,12);
export function resumePath(key) {
 if(typeof key!=='string'||!KEY.test(key))return null;
 const [repo,number]=key.split('#');
 return `/api/v1/review-gate/prs/${repo}/${number}/resume`;
}
export function stateLabel(state) {
 if(typeof state!=='string'||!state)return 'Unknown';
 return Object.hasOwn(STATES,state)?STATES[state]:'Unknown state';
}

const empty=msg=>`<p class="rg-note">${esc(msg)}</p>`;
const stateClass=s=>Object.hasOwn(STATES,s)?s:'unknown';

const TICK=[['repos','repos checked'],['prs','PRs seen'],['enqueued','reviews enqueued'],['merged','merged'],['errors','errors'],['skipped_drafts','drafts skipped'],['main_errors','main check errors']];
export function renderGateSummary(queue, prs) {
 const gate=queue&&typeof queue==='object'&&queue.gate&&typeof queue.gate==='object'?queue.gate:{};
 const counts=gate.counts&&typeof gate.counts==='object'&&!Array.isArray(gate.counts)?gate.counts:{};
 const byState=new Map();
 for(const p of list(prs)){const k=typeof p.state==='string'&&Object.hasOwn(STATES,p.state)?p.state:'unknown';byState.set(k,(byState.get(k)||0)+1);}
 const tiles=[...byState].map(([k,n])=>`<li class="rg-tile rg-${esc(k)}"><strong>${n}</strong><span>${esc(stateLabel(k))}</span></li>`).join('');
 const tick=when(gate.last_tick_at);
 const ticks=TICK.filter(([k])=>num(counts[k])!==null).map(([k,label])=>`${counts[k]} ${label}`).join(', ');
 return `<ul class="rg-tiles" aria-label="Pull requests by state">${tiles||'<li class="rg-note">No pull requests seen yet.</li>'}</ul><p class="rg-note">${tick?`Last gate tick ${esc(tick)}`:'Gate has not ticked yet'}${ticks?`. Last tick: ${esc(ticks)}`:''}</p>`;
}

function failureList(f) {
 const all=Array.isArray(f)?f.filter(x=>typeof x==='string'&&x):[];
 if(!all.length)return '-';
 return `<ul class="rg-failures">${all.slice(0,5).map(x=>`<li>${esc(short(x,120))}</li>`).join('')}${all.length>5?`<li>+${all.length-5} more</li>`:''}</ul>`;
}
export function renderMainStatus(main) {
 const repos=list(main&&main.repos);
 if(!repos.length)return empty('No repositories have main verification on.');
 const rows=repos.slice(0,100).map(r=>{
  const culprits=Array.isArray(r.culprits)?r.culprits.slice(0,20).map(c=>`#${esc(short(String(c),12))}`).join(', '):'';
  const green=shortSha(r.last_green_sha);
  return `<tr><td data-label="Repository">${esc(short(r.repo,120)||'unknown')}<small> ${esc(short(r.branch,60))}</small></td><td data-label="State"><span class="rg-badge rg-main-${esc(['green','red','pending','unknown'].includes(r.state)?r.state:'unknown')}">${esc(short(r.state,24)||'unknown')}</span></td><td data-label="Head"><code>${esc(shortSha(r.head_sha))||'-'}</code></td><td data-label="Last green"><code>${esc(green)||'-'}</code></td><td data-label="Failures">${failureList(r.failures)}${num(r.range_total)!==null?`<small>${r.range_total} commits in range</small>`:''}</td><td data-label="Culprit PRs">${culprits||'-'}</td></tr>`;
 }).join('');
 return `<table class="rg-table"><thead><tr><th scope="col">Repository</th><th scope="col">State</th><th scope="col">Head</th><th scope="col">Last green</th><th scope="col">Failures</th><th scope="col">Culprit PRs</th></tr></thead><tbody>${rows}</tbody></table>`;
}

export function renderPrTable(prs, jobsByKey, selectedKey='') {
 const rows=list(prs);
 if(!rows.length)return empty('No pull requests have been seen by the gate.');
 const jobs=jobsByKey&&typeof jobsByKey==='object'?jobsByKey:{};
 const body=rows.slice(0,300).map(p=>{
  const key=text(p.key),job=Object.hasOwn(jobs,key)?jobs[key]:null;
  const jobState=job&&typeof job==='object'?short(job.state,20):'';
  return `<tr class="rg-row rg-${esc(stateClass(p.state))}"><td data-label="Pull request"><button type="button" class="rg-select" data-rg-select="${esc(key.slice(0,300))}" aria-pressed="${key!==''&&key===selectedKey}">${esc(short(key,120)||'unknown')}</button></td><td data-label="State"><span class="rg-badge rg-${esc(stateClass(p.state))}">${esc(stateLabel(p.state))}</span>${jobState?`<small> job ${esc(jobState)}</small>`:''}</td><td data-label="Rounds">review ${num(p.review_rounds)??0} · ci fix ${num(p.ci_fix_rounds)??0}</td><td data-label="Head"><code>${esc(shortSha(p.head_sha))||'-'}</code></td><td data-label="Reason">${esc(short(p.reason,160))||'-'}</td></tr>`;
 }).join('');
 return `<table class="rg-table"><thead><tr><th scope="col">Pull request</th><th scope="col">State</th><th scope="col">Rounds</th><th scope="col">Head</th><th scope="col">Reason</th></tr></thead><tbody>${body}</tbody></table>`;
}

export function renderPrDetail(pr, {busy=false,error=''}={}) {
 if(!pr||typeof pr!=='object'||Array.isArray(pr))return empty('Select a pull request to see its findings.');
 const key=text(pr.key),findings=list(pr.last_findings).slice(0,20);
 const items=findings.map(f=>`<li class="rg-finding"><strong>${esc(short(f.severity,20)||'finding')}</strong> <code>${esc(short(f.path,160))}${num(f.line)!==null?`:${f.line}`:''}</code><p>${esc(short(f.message,400))}</p>${text(f.required_change)?`<p class="rg-note">Required change: ${esc(short(f.required_change,300))}</p>`:''}</li>`).join('');
 const canResume=pr.state==='escalated'&&resumePath(key)!==null;
 return `<h3>${esc(short(key,120)||'Pull request')}</h3><p class="rg-note">${esc(stateLabel(pr.state))}${text(pr.reason)?` · ${esc(short(pr.reason,200))}`:''}${shortSha(pr.approved_sha)?` · approved <code>${esc(shortSha(pr.approved_sha))}</code>`:''}</p>
  ${canResume?`<p><button type="button" class="hd-btn" data-rg-resume="${esc(key)}"${busy?' disabled aria-busy="true"':''}>${busy?'Resuming…':'Resume review'}</button></p>`:''}${error?`<p class="rg-warning" role="status">${esc(short(error,300))}</p>`:''}
  ${items?`<ul class="rg-findings">${items}</ul>${findings.length<list(pr.last_findings).length?'<p class="rg-note">More findings not shown.</p>':''}`:empty('No findings recorded.')}`;
}

export function renderQueue(queue) {
 const q=queue&&typeof queue==='object'?queue:{};
 const jobs=list(q.jobs),claimed=jobs.filter(j=>j.state==='claimed').length;
 const lastError=q.gate&&typeof q.gate==='object'?short(q.gate.last_error,300):'';
 const rows=jobs.slice(0,100).map(j=>`<tr><td data-label="Pull request">${esc(short(j.key,120))}</td><td data-label="Head"><code>${esc(shortSha(j.sha))||'-'}</code></td><td data-label="State">${esc(short(j.state,20))}</td><td data-label="Author model">${esc(short(j.author_model,60))||'-'}</td><td data-label="Enqueued">${esc(when(j.enqueued_at))||'-'}</td></tr>`).join('');
 return `<p class="rg-note">Depth ${num(q.depth)??jobs.length} · ${claimed} claimed · ${jobs.length-claimed} queued</p>${lastError?`<p class="rg-warning" role="status">Gate error: ${esc(lastError)}</p>`:''}
  ${rows?`<table class="rg-table"><thead><tr><th scope="col">Pull request</th><th scope="col">Head</th><th scope="col">State</th><th scope="col">Author model</th><th scope="col">Enqueued</th></tr></thead><tbody>${rows}</tbody></table>`:empty('The review queue is empty.')}`;
}

export function renderNotices(notices) {
 const rows=list(notices);
 if(!rows.length)return empty('No notices yet.');
 return `<ul class="rg-notices">${rows.slice(0,100).map(n=>`<li><strong>${esc(short(n.kind,40))}</strong> via ${esc(short(n.channel,20))} · ${n.ok===true?'delivered':'failed'} · ${esc(short(n.key,120))} <code>${esc(shortSha(n.sha))}</code>${when(n.at)?` · ${esc(when(n.at))}`:''}</li>`).join('')}</ul>`;
}

const styles=`<style>
.rg-note{color:var(--text-secondary);font-size:12px;line-height:1.6;margin:7px 0;overflow-wrap:anywhere}.rg-warning{color:var(--status-attention);font-size:12px;line-height:1.6;overflow-wrap:anywhere}
.rg-panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px;margin:16px 0;min-width:0;overflow-x:auto}.rg-panel h2{font-size:15px;margin:0 0 8px}
.rg-tiles{display:flex;flex-wrap:wrap;gap:10px;list-style:none;padding:0;margin:0}.rg-tile{border:1px solid var(--border-subtle);border-radius:var(--radius-sm);padding:8px 12px;display:flex;flex-direction:column;min-width:90px}.rg-tile strong{font-size:20px}.rg-tile span{font-size:12px;color:var(--text-secondary)}
.rg-table{width:100%;border-collapse:collapse;font-size:13px}.rg-table th{text-align:left;font-size:12px;color:var(--text-secondary);font-weight:600}.rg-table th,.rg-table td{padding:8px;border-top:1px solid var(--border-subtle);vertical-align:top;overflow-wrap:anywhere}.rg-table small{color:var(--text-secondary)}
.rg-select{font:inherit;background:none;border:0;padding:0;color:var(--accent);cursor:pointer;text-align:left}.rg-select[aria-pressed=true]{text-decoration:underline;font-weight:600}
.rg-badge{display:inline-block;padding:2px 7px;border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px}.rg-escalated,.rg-ci_red,.rg-main-red{color:var(--status-attention)}
.rg-failures{margin:0;padding-left:16px}.rg-findings{padding-left:18px;font-size:13px}.rg-finding p{margin:4px 0;overflow-wrap:anywhere}.rg-notices{padding-left:18px;font-size:13px;line-height:1.7;overflow-wrap:anywhere}
.rg-layout{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:16px;align-items:start}
@media(max-width:700px){.rg-layout{grid-template-columns:minmax(0,1fr)}.rg-table thead{position:absolute;left:-9999px}.rg-table,.rg-table tbody,.rg-table tr,.rg-table td{display:block}.rg-table tr{border:1px solid var(--border-subtle);border-radius:var(--radius-sm);margin:8px 0;padding:6px}.rg-table td{border:0;padding:4px 8px}.rg-table td::before{content:attr(data-label) ": ";color:var(--text-secondary);font-size:12px}}
</style>`;

const errorText=e=>e&&e.name==='TimeoutError'?'request timed out':short(e&&e.body&&typeof e.body.error==='string'?e.body.error:e&&e.message,300)||'request failed';
const SOURCES=[['prs','/api/v1/review-gate/prs'],['queue','/api/v1/review-gate/queue'],['notices','/api/v1/review-gate/notices'],['main','/api/v1/review-gate/main']];

// The dashboard route owns the page head (title and freshness chip): it passes title:false and
// freshness, a getter for that chip. The four reads are the chip's four sources.
export function mountReviewGate(container,client,{intervalMs=5000,timers={setInterval,clearInterval},requestTimeoutMs=10000,timeout=ms=>AbortSignal.timeout(ms),title=true,freshness=()=>null}={}) {
 const chip=freshnessSources(freshness);
 const h1=title?'<h1>Review gate</h1>':'';
 const panels={};for(const [name] of SOURCES)panels[name]={data:null,at:0,error:''};
 const linked=(a,b)=>{if(typeof AbortSignal.any==='function')return AbortSignal.any([a,b]);const c=new AbortController();for(const x of [a,b]){if(x.aborted)c.abort(x.reason);else x.addEventListener('abort',()=>c.abort(x.reason),{once:true});}return c.signal;};
 let epoch=0;
 let disposed=false,disabled=false,inFlight=false,again=false,controller=null,timer=null,selected='';
 const resuming=new Map(),resumeErrors=new Map();

 // A panel keeps its last good data; a failed read only adds a visible warning.
 const warn=p=>!p.error?'':p.data!==null?`<p class="rg-warning" role="status">Showing data from ${esc(when(p.at))||'an earlier read'}: ${esc(p.error)}</p>`:`<p class="rg-warning" role="status">Could not load: ${esc(p.error)}</p>`;
 const panel=(title,p,body)=>`<section class="rg-panel"><h2>${esc(title)}</h2>${warn(p)}${p.data!==null?body:p.error?'':skeleton(title.toLowerCase(),{rows:2})}</section>`;
 let lastHtml=null;
 function write(html){if(html===lastHtml)return false;lastHtml=html;container.innerHTML=html;return true;}
 function draw() {
  if(disposed)return;
  const focus=typeof document!=='undefined'&&document.activeElement&&container.contains?.(document.activeElement)?document.activeElement.dataset:null;
  if(disabled){write(`${styles}${h1}<p class="rg-note" role="status">Review gate is not enabled on this control plane.</p>`);return;}
  const {prs,queue,notices,main}=panels;
  const rows=list(prs.data),jobs={};
  for(const j of list(queue.data&&queue.data.jobs))if(typeof j.key==='string')jobs[j.key]=j;
  const pr=rows.find(p=>p.key===selected);
  const changed=write(`${styles}${h1}
   ${panel('Overview',queue,renderGateSummary(queue.data,rows))}
   ${panel('Main branch',main,renderMainStatus(main.data))}
   <div class="rg-layout">${panel('Pull requests',prs,renderPrTable(rows,jobs,selected))}<section class="rg-panel" aria-label="Pull request detail">${pr?renderPrDetail(pr,{busy:resuming.has(selected),error:resumeErrors.get(selected)||''}):renderPrDetail(null)}</section></div>
   ${panel('Review queue',queue,renderQueue(queue.data))}
   ${panel('Recent notices',notices,renderNotices(notices.data))}`);
  if(changed&&focus&&container.querySelectorAll){const attr=focus.rgSelect!==undefined?'data-rg-select':focus.rgResume!==undefined?'data-rg-resume':'';const v=focus.rgSelect??focus.rgResume;if(attr)[...container.querySelectorAll(`[${attr}]`)].find(n=>n.getAttribute(attr)===v)?.focus?.({preventScroll:true});}
 }
 function stop(){if(timer!==null){timers.clearInterval(timer);timer=null;}}

 async function poll() {
  if(disposed||disabled)return;
  if(inFlight){again=true;return;}
  inFlight=true;again=false;
  const ctl=controller=new AbortController();
  const gen=epoch;
  const results=await Promise.allSettled(SOURCES.map(([,path])=>client.request(path,{signal:linked(ctl.signal,timeout(requestTimeoutMs))})));
  inFlight=false;
  if(disposed||ctl.signal.aborted)return;
  results.forEach((r,i)=>{
   const p=panels[SOURCES[i][0]];
   const source=SOURCES[i][0];
   if(r.status==='fulfilled'){chip.ok(source);if(i===0&&gen!==epoch)return;p.data=r.value??(i===0||i===2?[]:{});p.at=Date.now();p.error='';}
   // A 404 means the gate is turned off here: the server answered as configured, not a failed read.
   else if(r.reason&&r.reason.status===404){disabled=true;chip.ok(source);}
   else{p.error=errorText(r.reason);chip.fail(source,`Review gate ${source}: ${p.error}`);}
  });
  for(const p of list(panels.prs.data))if(p.state!=='escalated')resumeErrors.delete(p.key);
  if(disabled)stop();
  draw();
  if(again&&!disabled)poll();
 }

 async function resume(key) {
  const pr=list(panels.prs.data).find(p=>p.key===key),path=resumePath(key);
  if(!pr||pr.state!=='escalated'||!path||resuming.has(key))return;
  const ask=typeof window!=='undefined'&&window.confirm?window.confirm:()=>false;
  if(!ask(`Resume review for ${short(key,120)}? Its round counters reset and the current head is reviewed again.`))return;
  resuming.set(key,true);resumeErrors.delete(key);draw();
  try{
   const snap=await client.request(path,{method:'POST',body:{}});
   resumeErrors.delete(key);epoch++;
   if(snap&&typeof snap==='object'&&snap.key===key&&Array.isArray(panels.prs.data))panels.prs.data=panels.prs.data.map(p=>p&&p.key===key?snap:p);
  }
  catch(e){
   resumeErrors.set(key,e&&(e.status===401||e.status===403)?'Only an operator key can resume a pull request.':e&&e.status===404?'Pull request not found, or the review gate is disabled.':`Resume refused: ${errorText(e)}`);
  }
  resuming.delete(key);
  if(disposed)return;
  draw();poll();
 }

 const onClick=event=>{
  const t=event.target&&event.target.closest;if(!t)return;
  const pick=event.target.closest('[data-rg-select]');
  if(pick){selected=text(pick.dataset.rgSelect);draw();return;}
  const res=event.target.closest('[data-rg-resume]');
  if(res)resume(text(res.dataset.rgResume));
 };
 container.addEventListener('click',onClick);
 draw();
 timer=timers.setInterval(poll,intervalMs);
 poll();
 return {dispose(){
  if(disposed)return;disposed=true;stop();controller?.abort();container.removeEventListener('click',onClick);
 }};
}
