// The tests of one repository's latest runs: a list to search, filter and sort, and for one test the log excerpt (or the stored
// failure output) with ways to keep investigating. Pure: state in, markup out; integrations-view owns the fetches and events.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';

export const ROW_LIMIT=200;
const bad=s=>s==='failed'||s==='errored';
const STATUS_WORD={failed:'Failed',errored:'Errored',passed:'Passed',skipped:'Skipped'};
export const testSource=report=>report?.source==='log_parse'?'log':'junit';

// reports: [{label, report}] with full report bodies (tests included). Every test remembers where it came from.
export function explorerTests(entries) {
 const out=[];
 for(const {label,report} of entries||[]){
  if(!report)continue;
  const source=testSource(report),stepNames=new Map((report.steps||[]).map(x=>[String(x.step_id),x.step]));
  for(const t of Array.isArray(report.tests)?report.tests:[])out.push({...t,step:t.step||stepNames.get(String(t.step_id))||'',key:`${report.run_number}:${t.id}`,run_number:report.run_number,branch:report.branch||'',group:label||'',source,runners:report.runners||[],provider_url:report.provider_url,provider_repo_id:report.provider_repo_id,repository:report.repository});
 }
 return out;
}
export function filterExplorerTests(tests,{search='',status='all',sort='failures'}={}) {
 const terms=String(search).toLowerCase().trim().split(/\s+/).filter(Boolean);
 const keep=tests.filter(t=>(status==='all'||(status==='failing'?bad(t.status):t.status===status))&&terms.every(w=>`${t.name} ${t.suite||''} ${t.step||''}`.toLowerCase().includes(w)));
 const by={failures:(a,b)=>bad(b.status)-bad(a.status)||(a.line_start||0)-(b.line_start||0),
  duration:(a,b)=>(b.duration_ms||0)-(a.duration_ms||0),name:(a,b)=>String(a.name).localeCompare(String(b.name))}[sort]||(()=>0);
 return keep.map((t,i)=>[t,i]).sort((a,b)=>by(a[0],b[0])||a[1]-b[1]).map(x=>x[0]);
}
const dur=t=>t.source==='log'&&!t.timed?'—':formatDuration(t.duration_ms||0);
const where=t=>t.source==='log'?`parsed from the CI log${t.step?`, step ${t.step}`:''}, lines ${t.line_start}–${t.line_end}`:'from the uploaded JUnit report';
function counts(reports) {
 const total=reports.reduce((n,r)=>n+(Number(r.summary?.total)||0),0),listed=reports.reduce((n,r)=>n+(Array.isArray(r.tests)?r.tests.length:0),0);
 return {total,listed};
}
function renderRow(t,selected,branches) {
 return `<li><button type="button" class="cx-row" data-cx-test="${esc(t.key)}" data-test-status="${esc(t.status)}" aria-pressed="${String(selected===t.key)}" title="${esc(`${t.name} — ${where(t)}`)}"><span class="cx-dot" aria-hidden="true"></span><span class="cx-name">${esc(t.name)}<small>${esc([STATUS_WORD[t.status]||t.status,t.suite,t.step,branches?`${t.branch||'no branch'} #${t.run_number}`:''].filter(Boolean).join(' · '))}</small></span><span class="cx-dur">${esc(dur(t))}</span><span class="cx-src" data-cx-source="${t.source}">${t.source==='log'?'CI log':'JUnit'}</span></button></li>`;
}
function renderSlice(ex,t) {
 if(ex.sliceError)return `<p class="ig-warning" role="status">${esc(ex.sliceError)}</p>`;
 if(ex.slice===null)return '<p class="ig-note" role="status">Reading the live step log…</p>';
 const slice=ex.slice.slice;
 if(!slice)return `<p class="ig-warning" role="status" data-cx-slice-gone>${esc(ex.slice.reason||ex.slice.error||'The log excerpt is unavailable.')}</p>`;
 const first=Number(slice.first)||1,last=Number(slice.last)||first,start=Number(slice.start),end=Number(slice.end),totalLines=Number(ex.slice.total_lines)||0;
 const lines=slice.lines.map((text,i)=>{const n=first+i,hit=n>=start&&n<=end;return `<span class="cx-l${hit?' cx-hit':''}"><i>${n}</i>${esc(text)}</span>`;}).join('\n');
 return `<pre class="cx-log" tabindex="0" aria-label="Log lines ${first} to ${last}">${lines}</pre><p class="ig-note">Lines ${first}–${last} of ${totalLines}; the test is highlighted. Read live from CI and redacted${ex.slice.truncated?'; the log is larger than HerdR reads, so only its beginning is available':''}.</p>`;
}
function renderHistory(ex) {
 if(ex.history===null)return '';
 if(ex.history==='loading')return '<p class="ig-note" role="status">Looking this test up in earlier runs…</p>';
 if(ex.historyError)return `<p class="ig-warning" role="status">${esc(ex.historyError)}</p>`;
 if(!ex.history.length)return '<p class="ig-note">No earlier run has a test with this name.</p>';
 return `<table class="cx-history" aria-label="This test in earlier runs"><thead><tr><th scope="col">Run</th><th scope="col">Branch</th><th scope="col">Outcome</th><th scope="col">Source</th></tr></thead><tbody>${ex.history.map(h=>`<tr><td><button type="button" class="hd-btn hd-btn--secondary hd-btn--sm" data-cx-open-run="${esc(h.run_number)}">#${esc(h.run_number)}</button></td><td>${esc(h.branch||'—')}</td><td data-test-status="${esc(h.status||'')}">${esc(h.status?STATUS_WORD[h.status]||h.status:h.unindexed?'Test list no longer kept for this older run':'No such test')}</td><td>${h.source==='log'?'CI log':h.source==='junit'?'JUnit':'—'}</td></tr>`).join('')}</tbody></table>`;
}
export function renderTestInspector(ex,t) {
 const body=t.source==='log'?renderSlice(ex,t):`${t.message?`<p class="cx-msg">${esc(t.message)}</p>`:''}<pre class="cx-log" tabindex="0">${esc(t.trace||t.message||(bad(t.status)?'No failure output was reported for this test.':'This test passed; JUnit reports carry no output for passing tests.'))}</pre>`;
 return `<section class="cx-inspect" aria-label="Test detail" data-cx-inspector><div class="cx-inspect-head"><h4>${esc(t.name)}</h4><button type="button" class="hd-btn hd-btn--secondary hd-btn--sm" data-cx-close>Close</button></div><p class="ig-note"><span data-test-status="${esc(t.status)}">${esc(STATUS_WORD[t.status]||t.status)}</span> · ${esc(t.suite||'no suite')} · ${esc(dur(t))} · run #${esc(t.run_number)}${t.branch?` on ${esc(t.branch)}`:''} · ${esc(where(t))}</p>${body}<div class="ci-investigate" role="group" aria-label="Investigate this test"><span>Investigate</span><button type="button" class="hd-btn hd-btn--secondary hd-btn--sm" data-cx-history>History across runs</button><button type="button" class="hd-btn hd-btn--secondary hd-btn--sm" data-cx-open-run="${esc(t.run_number)}">Open run #${esc(t.run_number)}</button>${t.step_id?'<button type="button" class="hd-btn hd-btn--secondary hd-btn--sm" data-cx-open-step>Open this step</button>':''}</div>${renderHistory(ex)}</section>`;
}
// ex: {repo, label, reports:[{label,report}]|null, error, search, status, sort, selected, slice, sliceError, history, historyError}
export function renderTestExplorer(ex) {
 if(!ex||!ex.repo)return '';
 const head=`<article class="ci-insight-card cx" aria-label="Tests in ${esc(ex.label||'this repository')}" data-ci-explorer>`;
 if(ex.error)return `${head}<h3>Tests in ${esc(ex.label)}</h3><p class="ig-warning" role="status">${esc(ex.error)}</p></article>`;
 if(ex.reports===null)return `${head}<h3>Tests in ${esc(ex.label)}</h3><p class="ig-note" role="status">Loading the tests of the latest runs…</p></article>`;
 const reports=ex.reports.map(e=>e.report).filter(Boolean),{total,listed}=counts(reports);
 const tests=explorerTests(ex.reports),rows=filterExplorerTests(tests,ex);
 const logs=reports.filter(r=>testSource(r)==='log'),uploads=reports.length-logs.length;
 const runners=[...new Set(logs.flatMap(r=>r.runners||[]))];
 const bits=[logs.length?`${logs.length===reports.length?'All counts':'Some counts'} are parsed from CI logs${runners.length?` (${runners.join(', ')})`:''}; names come from what the runner printed`:'',uploads?`${uploads} ${uploads===1?'report is':'reports are'} an uploaded JUnit report with exact results`:''].filter(Boolean).join('. ');
 const gaps=[];
 const dropped=logs.filter(r=>r.index_dropped).length;
 if(dropped)gaps.push(`The test list is no longer kept for ${dropped} older ${dropped===1?'run':'runs'} (only the newest runs keep one), so ${dropped===1?'it shows':'they show'} counts and failing tests only.`);
 if(total>listed&&!dropped)gaps.push(listed===0?`These runners print only totals, so ${total} tests are counted but no list is available. Counts only.`:`The list names ${listed} of ${total} counted tests; the runner does not print the rest per test.`);
 for(const r of logs)for(const s of r.skipped_steps||[])gaps.push(s.reason==='read_failed'?`Step ${s.step} is not counted yet: its log could not be read, and it will be tried again.`:s.reason==='junit_report_exists'?'':`Step ${s.step} is not counted: its log is too large to read.`);
 const selected=tests.find(t=>t.key===ex.selected);
 const shown=rows.slice(0,ROW_LIMIT);
 return `${head}<h3>Tests in ${esc(ex.label)} · ${total} counted</h3><p class="ig-note">${esc(bits)}${bits?'.':''} ${esc(gaps.filter(Boolean).join(" "))}</p>${listed?`<div class="cx-controls"><label>Find a test<input type="search" data-cx-search maxlength="200" value="${esc(ex.search||'')}" placeholder="name, suite or step"></label><label>Show<select data-cx-status>${[['all','All'],['failing','Failing'],['passed','Passed'],['skipped','Skipped']].map(([v,l])=>`<option value="${v}"${(ex.status||'all')===v?' selected':''}>${l}</option>`).join('')}</select></label><label>Sort<select data-cx-sort>${[['failures','Failures first'],['duration','Slowest first'],['name','Name']].map(([v,l])=>`<option value="${v}"${(ex.sort||'failures')===v?' selected':''}>${l}</option>`).join('')}</select></label></div><p class="ig-note" role="status">${rows.length} ${rows.length===1?'test':'tests'} match${rows.length>ROW_LIMIT?`; showing the first ${ROW_LIMIT}, narrow the search to see others`:''}.</p><ul class="cx-list">${shown.map(t=>renderRow(t,ex.selected,reports.length>1)).join('')}</ul>`:''}${selected?renderTestInspector(ex,selected):''}</article>`;
}
export const testExplorerStyles=`<style>
.cx-controls{display:flex;flex-wrap:wrap;gap:12px;align-items:end;margin:12px 0}.cx-controls label{display:flex;flex-direction:column;gap:4px;font-size:12px;min-width:140px}.cx-controls input,.cx-controls select{padding:8px;border:1px solid var(--border);border-radius:var(--control-radius);background:var(--surface);color:var(--text);font:inherit;min-height:var(--control-h)}
.cx-list{list-style:none;margin:0;padding:0;display:grid;gap:4px;max-height:420px;overflow:auto}.cx-row{display:grid;grid-template-columns:12px minmax(0,1fr) auto auto;gap:12px;align-items:center;width:100%;min-height:var(--control-h);padding:4px 8px;font:inherit;font-size:13px;color:var(--text);background:transparent;border:1px solid transparent;border-radius:var(--control-radius);cursor:pointer;text-align:left}.cx-row:hover{background:var(--surface-hover)}.cx-row[aria-pressed=true]{border-color:var(--accent);box-shadow:inset 0 -2px var(--accent)}.cx-row:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.cx-dot{width:8px;height:8px;border-radius:50%;background:var(--status-success)}.cx-row[data-test-status=failed] .cx-dot,.cx-row[data-test-status=errored] .cx-dot{background:var(--status-failed)}.cx-row[data-test-status=skipped] .cx-dot{background:var(--text-muted)}
.cx-name{overflow-wrap:anywhere}.cx-name small{display:block;font-size:11px;color:var(--text-secondary)}.cx-dur{font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums}.cx-src{font-size:11px;color:var(--text-secondary);border:1px solid var(--border);border-radius:var(--control-radius);padding:0 8px}
.cx-inspect{margin-top:16px;padding-top:12px;border-top:1px solid var(--border)}.cx-inspect-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline}.cx-inspect h4{font-size:14px;margin:0;overflow-wrap:anywhere}.cx-msg{font-size:13px;overflow-wrap:anywhere}
.cx-log{max-height:360px;overflow:auto;font-size:11px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--surface-2);border-radius:var(--control-radius);padding:8px}.cx-l{display:block}.cx-l i{display:inline-block;min-width:48px;margin-right:8px;font-style:normal;color:var(--text-muted);text-align:right;user-select:none}.cx-hit{background:var(--surface);box-shadow:inset 2px 0 var(--accent)}
.cx-history{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}.cx-history th,.cx-history td{text-align:left;padding:8px;border-bottom:1px solid var(--border-subtle)}[data-test-status=failed],[data-test-status=errored]{color:var(--status-failed)}.cx-row[data-test-status]{color:var(--text)}
@media(max-width:700px){.cx-row{grid-template-columns:12px minmax(0,1fr) auto}.cx-src{display:none}}
</style>`;
