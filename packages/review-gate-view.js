// Review gate page: every gated PR, the review queue, main-branch status and recent notices.
// Every string from the API is untrusted; it is escaped and clipped before it reaches HTML.
import {escapeHtml as esc} from './herdr-web-components/util.js';

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

export function renderGateSummary(queue) {
 const gate=queue&&typeof queue==='object'&&queue.gate&&typeof queue.gate==='object'?queue.gate:{};
 const counts=gate.counts&&typeof gate.counts==='object'&&!Array.isArray(gate.counts)?gate.counts:{};
 const tiles=Object.entries(counts).filter(([,n])=>num(n)!==null).slice(0,20).map(([k,n])=>`<li class="rg-tile rg-${esc(stateClass(k))}"><strong>${n}</strong><span>${esc(stateLabel(k))}</span></li>`).join('');
 const tick=when(gate.last_tick_at);
 return `<ul class="rg-tiles" aria-label="Pull requests by state">${tiles||'<li class="rg-note">No counts reported yet.</li>'}</ul><p class="rg-note">${tick?`Last gate tick ${esc(tick)}`:'Gate has not ticked yet'}</p>`;
}

export function renderMainStatus(main) {
 const repos=list(main&&main.repos);
 if(!repos.length)return empty('No repositories have main verification on.');
 const rows=repos.slice(0,100).map(r=>{
  const culprits=Array.isArray(r.culprits)?r.culprits.slice(0,20).map(c=>`#${esc(short(String(c),12))}`).join(', '):'';
  const green=shortSha(r.last_green_sha);
  return `<tr><td data-label="Repository">${esc(short(r.repo,120)||'unknown')}<small> ${esc(short(r.branch,60))}</small></td><td data-label="State"><span class="rg-badge rg-main-${esc(['green','red','pending','unknown'].includes(r.state)?r.state:'unknown')}">${esc(short(r.state,24)||'unknown')}</span></td><td data-label="Head"><code>${esc(shortSha(r.head_sha))||'-'}</code></td><td data-label="Last green"><code>${esc(green)||'-'}</code></td><td data-label="Failures">${num(r.failures)??'-'}${num(r.range_total)!==null?` of ${r.range_total} commits`:''}</td><td data-label="Culprit PRs">${culprits||'-'}</td></tr>`;
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
