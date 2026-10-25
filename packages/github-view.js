// GitHub owns issue state. AutoSpec labels express intent, never permission to run work.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration,formatTimestamp} from './herdr-web-components/format.js';
import {safeServiceUrl} from './services-view.js';
import {snapshotText,animateChanges,skeleton} from './motion.js';
import {freshnessSources} from './freshness.js';
import {mountRelated} from './related-panel.js';

const text=value=>typeof value==='string'?value:'';
const short=(value,max=160)=>text(value).replace(/\s+/g,' ').trim().slice(0,max);
const records=value=>Array.isArray(value)?value.filter(v=>v&&typeof v==='object'&&!Array.isArray(v)):[];
const strings=value=>Array.isArray(value)?value.filter(v=>typeof v==='string'):[];
const integer=value=>Number.isInteger(value)&&value>=0;
const human=value=>text(value).replaceAll('_',' ');
const issueKey=issue=>`${issue.repository_id}#${issue.number}`;
const tabs=[['all','All open issues'],['autospec','AutoSpec candidates'],['preparation','Needs preparation'],['in_progress','In progress'],['blocked','Blocked']];
const stamp=value=>typeof value==='number'?value>0?(value<1e12?value*1000:value):0:Date.parse(value)||0;
const age=(value,label)=>{const date=stamp(value);return date?`${esc(label)} <span data-gh-time="${date}">${esc(formatDuration(Math.max(0,Date.now()-date)))}</span> ago`:`${esc(label)} time not reported`;};
const link=(url,label)=>{const safe=safeServiceUrl(url);return safe?`<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>`:'';};

export function rankGithubRepositories(repositories) {
 return records(repositories).slice().sort((a,b)=>(integer(b.open_issue_count)?b.open_issue_count:-1)-(integer(a.open_issue_count)?a.open_issue_count:-1)||text(a.name_with_owner).localeCompare(text(b.name_with_owner)));
}
export function githubIssueLabel(issue={}) {
 const a=issue.autospec||{};
 if(a.eligibility==='blocked'||a.state==='blocked')return 'Blocked';
 if(a.state==='in_progress')return 'In progress · reported by labels';
 if(a.state==='preparation')return 'Needs preparation';
 if(a.managed)return 'AutoSpec candidate · readiness not verified';
 return 'Open issue';
}
// failed: the first read failed (its message shows beside the list), so no skeleton is left behind.
export function renderGithubHealth(snapshot,{failed=false}={}) {
 if(!snapshot)return failed?'':skeleton('GitHub connection',{rows:1,height:20,inStatus:true});
 const messages={not_configured:'GitHub connection needs server configuration.',auth_error:'GitHub authentication failed. The server credential needs attention.',rate_limited:'GitHub is rate limited. Synchronization will resume when requests are available.',unavailable:'GitHub could not be reached.'};
 const progress=snapshot.progress||{},issues=progress.phase==='issues'||Number(progress.issue_repositories_complete)>0;
 const complete=issues?progress.issue_repositories_complete:progress.repositories_seen;
 const total=issues?progress.issue_repositories_total:progress.repositories_total;
 const measured=integer(complete)&&integer(total)&&total>0;
 const busy=snapshot.refreshing||snapshot.status==='syncing';
 const account=text(snapshot.account?.login||snapshot.account);
 return `${messages[snapshot.status]?`<p class="gh-warning" role="status">${esc(messages[snapshot.status])}</p>`:''}${snapshot.error?`<p class="gh-warning">${esc(short(snapshot.error,400))}</p>`:''}
  ${busy?`<div class="gh-progress" role="status"><span>${issues?'Reading open issues':'Discovering repositories'}${measured?` · ${Math.min(complete,total)} of ${total} repositories`:''}${progress.current_repository?` · ${esc(short(progress.current_repository))}`:''}</span><progress ${measured?`value="${Math.min(complete,total)}" max="${total}"`:''} aria-label="${issues?'Repositories checked for issues':'Repository discovery'}"></progress>${integer(progress.issues_seen)?`<span>${progress.issues_seen} open issues observed so far</span>`:''}</div>`:''}
  ${snapshot.partial?'<p class="gh-warning">Partial coverage: some repositories or issues have not been read. Counts describe the available catalog.</p>':''}${snapshot.stale?'<p class="gh-warning">Showing previously synchronized data. Issue age does not determine whether an issue is still open.</p>':''}
  <p class="gh-note">${account?`${esc(account)} · `:''}${age(snapshot.last_success_at,'Last successful sync')}${integer(snapshot.rate_limit?.remaining)?` · ${snapshot.rate_limit.remaining} GraphQL points remaining`:''}</p>`;
}
// The chip's note for the sync snapshot: status (when not ready), stale, partial and deferred
// repositories (array or count). Any of them keeps the chip amber: a sync problem never reads as live.
export function githubSyncNote(snapshot) {
 if(!snapshot)return {text:'',warn:false};
 const deferred=Array.isArray(snapshot.deferred_repos)?snapshot.deferred_repos.length:integer(snapshot.deferred_repos)?snapshot.deferred_repos:0;
 const status=text(snapshot.status);
 const parts=[status&&status!=='ready'&&status!=='ok'?human(status):'',snapshot.stale===true?'stale':'',snapshot.partial===true&&status!=='partial'?'partial':'',deferred?`${deferred} ${deferred===1?'repository':'repositories'} deferred`:''].filter(Boolean);
 return {text:parts.join(' · '),warn:parts.length>0};
}
export function renderGithubRepository(repo={},selected=false) {
 const count=integer(repo.open_issue_count)?`${repo.open_issue_count} open ${repo.open_issue_count===1?'issue':'issues'}`:'Count not reported';
 const coverage=repo.issues_complete?'Issue list read':integer(repo.issues_loaded)?`${repo.issues_loaded} loaded`:'Issues not read yet';
 return `<li><button type="button" class="gh-repo" data-gh-repository="${esc(repo.repository_id||'')}" aria-pressed="${selected}"><strong>${esc(short(repo.name_with_owner||repo.repository_id))}</strong><span>${count}</span><small>${esc(coverage)}${repo.private?' · Private':''}${repo.archived?' · Archived':''}${repo.issues_enabled===false?' · Issues disabled':''}</small>${repo.error?`<small class="gh-warning">${esc(short(repo.error,180))}</small>`:''}</button></li>`;
}
export function renderGithubIssue(issue={},selected=false) {
 const labels=strings(issue.labels),assigned=strings(issue.assignees);
 return `<article class="gh-issue" data-gh-issue-row="${esc(issueKey(issue))}"><button type="button" class="gh-issue-title" data-gh-issue="${esc(issueKey(issue))}" aria-pressed="${selected}">${esc(short(issue.title,220)||'Untitled issue')}</button>
  <p class="gh-note">${esc(issue.repository_id||'')} · #${esc(issue.number??'?')}${assigned.length?` · ${esc(assigned.slice(0,3).join(', '))}`:''}</p>
  <p class="gh-state">${esc(githubIssueLabel(issue))}</p>${labels.length?`<div class="gh-labels">${labels.slice(0,6).map(label=>`<span>${esc(short(label,80))}</span>`).join('')}${labels.length>6?`<span>+${labels.length-6} labels</span>`:''}</div>`:''}
  <p class="gh-note">Issue updated ${esc(formatTimestamp(issue.updated_at))}</p></article>`;
}
export function renderGithubDetail(response,{summary,loading=false,error=''}={}) {
 const issue=response?.issue||summary;
 if(!issue)return '<p class="gh-note">Choose an issue to inspect its description and AutoSpec labels.</p>';
 const autospec=issue.autospec||{},reasons=strings(autospec.reasons),body=text(issue.body),full=!!response?.issue;
 return `<div class="gh-heading"><h2>Issue #${esc(issue.number??'?')}</h2><button type="button" class="hd-btn" data-gh-close><span class="gh-close-label">Close</span><span class="gh-back-label">Back to issues</span></button></div><h3>${esc(short(issue.title,300))}</h3><p class="gh-note">${esc(issue.repository_id||'')} · ${esc(human(issue.state)||'State not reported')}</p>
  <div class="gh-links">${link(issue.url,'Open on GitHub')}<button type="button" class="hd-btn" data-gh-refresh-issue aria-busy="${loading}">Refresh issue</button></div>
  ${error||response?.error?`<p class="gh-warning" role="status">${esc(error||short(response.error,300))}${full?' Showing the last received issue.':''}</p>`:''}${loading?'<p class="gh-note" role="status">Reading issue details…</p>':''}${response?.stale?'<p class="gh-warning">These issue details come from an earlier synchronization.</p>':''}
  <p class="gh-state">${esc(githubIssueLabel(issue))}</p>${autospec.evidence_source==='github_labels'?'<p class="gh-note">AutoSpec state comes from GitHub labels. Execution readiness has not been verified.</p>':''}${reasons.length?`<ul class="gh-reasons">${reasons.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul>`:''}
  ${[autospec.context,autospec.reasoning,autospec.language].some(v=>text(v))?`<dl class="gh-metadata">${[['Context',autospec.context],['Reasoning',autospec.reasoning],['Language',autospec.language]].filter(([,value])=>text(value)).map(([key,value])=>`<dt>${key}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`:''}
  ${strings(issue.labels).length?`<details class="gh-all-labels" data-gh-labels-details><summary>All labels · ${strings(issue.labels).length}</summary><div class="gh-labels">${strings(issue.labels).map(label=>`<span>${esc(short(label,160))}</span>`).join('')}</div></details>`:''}
  ${full?`<h3>Description</h3><pre class="gh-body" data-gh-body tabindex="0">${esc(body.slice(0,60000))||'No description provided.'}</pre>${body.length>60000?'<p class="gh-note">Description display truncated. Open GitHub for the full issue.</p>':''}<p class="gh-note">${age(response.checked_at,'Details checked')}</p>`:''}`;
}
const styles=`<style>
.gh-intro,.gh-note{color:var(--text-secondary);font-size:13px;line-height:1.6}.gh-intro{max-width:86ch}.gh-note{font-size:12px;margin:7px 0}.gh-warning{color:var(--status-attention);font-size:12px;line-height:1.6;overflow-wrap:anywhere}.gh-progress{display:flex;flex-direction:column;gap:7px;font-size:12px;margin:14px 0}.gh-progress progress{width:min(100%,600px);height:7px;accent-color:var(--accent)}.gh-layout{display:grid;grid-template-columns:minmax(180px,240px) minmax(0,1fr);gap:18px;align-items:start;margin-top:20px}.gh-repositories,.gh-inspector,.gh-issue-list{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);min-width:0;overflow:hidden}.gh-repositories>summary{padding:14px;font-weight:600;font-size:14px;cursor:pointer}.gh-repository-content{padding:0 12px 12px}.gh-controls label{display:flex;flex-direction:column;gap:6px;font-size:12px;margin:12px 0}.gh-controls input{padding:9px;font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);width:100%;min-width:0;box-sizing:border-box}.gh-repo-list{list-style:none;padding:0;margin:0}.gh-repo{display:flex;flex-direction:column;gap:5px;width:100%;text-align:left;background:none;border:0;border-top:1px solid var(--border-subtle);color:var(--text);padding:12px 8px;cursor:pointer;overflow-wrap:anywhere;font:inherit}.gh-repo strong{font-size:13px}.gh-repo span,.gh-repo small{font-size:12px;color:var(--text-secondary)}.gh-repo[aria-pressed=true]{background:var(--bg);box-shadow:inset 3px 0 var(--accent)}.gh-all-repos{width:100%;margin:7px 0}.gh-tabs{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}.gh-tabs button{font:inherit;font-size:12px;padding:9px 10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);cursor:pointer}.gh-tabs button[aria-selected=true]{border-color:var(--accent);box-shadow:inset 0 -2px var(--accent)}.gh-tabs small{margin-left:6px;color:var(--text-secondary)}.gh-main{min-width:0}.gh-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.gh-heading h2{font-size:17px;margin:0;overflow-wrap:anywhere}.gh-issue-layout{display:grid;grid-template-columns:minmax(0,1fr);gap:14px;align-items:start}.gh-issue{padding:14px;border-top:1px solid var(--border-subtle);overflow-wrap:anywhere}.gh-issue:first-child{border-top:0}.gh-issue-title{border:0;background:none;padding:0;text-align:left;font:inherit;font-weight:600;font-size:14px;line-height:1.6;color:var(--accent);cursor:pointer}.gh-issue-title[aria-pressed=true]{text-decoration:underline}.gh-state{font-size:12px;line-height:1.6;color:var(--text);margin:8px 0}.gh-labels{display:flex;flex-wrap:wrap;gap:5px;margin:8px 0}.gh-labels span{padding:3px 6px;border:1px solid var(--border-subtle);border-radius:var(--radius-sm);font-size:11px;color:var(--text-secondary);overflow-wrap:anywhere;max-width:100%}.gh-pages{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;margin:12px 0;font-size:12px;color:var(--text-secondary)}.gh-pages[hidden]{display:none}.gh-pages button:disabled{opacity:.45;cursor:default}.gh-inspector{padding:15px;overflow-wrap:anywhere;grid-row:1}.gh-inspector h3{font-size:14px;line-height:1.6;margin:14px 0 8px}.gh-links{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:12px}.gh-body{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:12px;border:1px solid var(--border-subtle);border-radius:var(--radius-sm);font:inherit;font-size:13px;line-height:1.7;max-height:450px;overflow:auto;margin:8px 0}.gh-reasons{padding-left:20px;font-size:12px;line-height:1.7}.gh-metadata{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 12px;font-size:12px}.gh-metadata dt{color:var(--text-secondary)}.gh-metadata dd{margin:0}.gh-all-labels{font-size:12px;margin:12px 0}.gh-all-labels summary{cursor:pointer}.gh-empty{padding:22px;color:var(--text-secondary);font-size:13px;line-height:1.6}.gh-section-label{font-size:12px;color:var(--text-secondary);line-height:1.6}.gh-inspector[hidden]{display:none}
.gh-back-label{display:none}
@media(min-width:1250px){
.gh-layout{height:calc(100dvh - var(--gh-workspace-top,220px) - 24px);min-height:240px;align-items:stretch}
.gh-repositories,.gh-main{position:sticky;top:16px;height:100%;min-height:0;box-sizing:border-box}
.gh-repository-content{display:flex;flex-direction:column;height:calc(100dvh - var(--gh-workspace-top,220px) - var(--gh-repository-heading,48px) - 26px);box-sizing:border-box;min-height:0}
.gh-repository-content>.gh-controls,.gh-repository-content>.gh-all-repos,.gh-repository-content>.gh-note,.gh-repository-content>.gh-pages{flex-shrink:0}
.gh-repo-list{min-height:0;overflow:auto;flex:1;overscroll-behavior:contain;scrollbar-gutter:stable}
.gh-main{display:flex;flex-direction:column}.gh-main>.gh-heading,.gh-main>.gh-tabs,.gh-main>.gh-controls,.gh-main>.gh-section-label,.gh-main>.gh-warning,.gh-main>.gh-pages{flex-shrink:0}
.gh-issue-layout{flex:1;min-height:0;align-items:stretch}.gh-issue-list,.gh-inspector{overflow:auto;min-height:0;overscroll-behavior:contain;scrollbar-gutter:stable}
.gh-inspector .gh-body{max-height:none;overflow:visible}
.gh-issue-layout:has(.gh-inspector:not([hidden])){grid-template-columns:minmax(0,1fr) minmax(280px,1fr)}.gh-inspector{grid-row:auto;grid-column:2}}
@media(min-width:2200px){.gh-layout{grid-template-columns:minmax(280px,360px) minmax(0,1fr)}.gh-issue-layout:has(.gh-inspector:not([hidden])){grid-template-columns:minmax(520px,.8fr) minmax(600px,1.2fr)}}
@media(max-width:1249px){
.gh-layout.gh-detail-open{grid-template-columns:minmax(0,1fr)}
.gh-detail-open>.gh-repositories,.gh-detail-open .gh-main>.gh-heading,.gh-detail-open .gh-main>.gh-tabs,.gh-detail-open .gh-main>.gh-controls,.gh-detail-open .gh-main>.gh-section-label,.gh-detail-open .gh-main>.gh-warning,.gh-detail-open .gh-main>.gh-pages,.gh-detail-open .gh-issue-list{display:none}
.gh-inspector{overflow:visible}.gh-inspector>.gh-heading{position:sticky;top:0;background:var(--surface);padding:12px 0;z-index:1}.gh-inspector .gh-body{max-height:none;overflow:visible}.gh-close-label{display:none}.gh-back-label{display:inline}
}
@media(max-width:850px){.gh-layout{grid-template-columns:minmax(0,1fr)}.gh-repo-list{max-height:280px;overflow:auto}.gh-tabs button{padding:8px}.gh-body{max-height:350px}}
</style>`;

export function mountGithub(container,client) {
 const state={repository:'',repositoryLabel:'All repositories',repositorySearch:'',issueSearch:'',tab:'all',repositoryPage:1,issuePage:1};
 let disposed=false,repositories=null,issues=null,repoError='',issueError='',selected=null,detail=null,detailError='',detailLoading=false;
 let clock=null,pollTimer=null,repositoryDebounce=null,issueDebounce=null;
 let account=null,connectionGeneration=null,listScroll=null;
 const requests=new Map(),cache=new Map(),signatures=new Map(),listeners=new AbortController();
 const chipEl=()=>container.querySelector('herdr-freshness'),chip=freshnessSources(chipEl);
 container.innerHTML=`${styles}<div class="hd-live-head"><h1>GitHub projects</h1><herdr-freshness interval="5000"></herdr-freshness></div><p class="gh-intro">Repositories ranked by open issues. Find work to inspect and review its AutoSpec labels, preparation needs and reported progress.</p><div data-gh-health role="status"></div><p data-gh-repo-error class="gh-warning" role="status"></p>
  <div class="gh-layout"><details class="gh-repositories" ${window.matchMedia('(min-width:851px)').matches?'open':''}><summary>Repositories · most open issues first</summary><div class="gh-repository-content"><div class="gh-controls"><label>Search repositories<input type="search" data-gh-repo-search placeholder="Owner or repository"></label></div><button type="button" class="hd-btn gh-all-repos" data-gh-all-repositories>All repositories</button><p class="gh-note" data-gh-repo-count></p><ul class="gh-repo-list" data-gh-repositories></ul><div class="gh-pages" data-gh-repo-pages><button type="button" class="hd-btn" data-gh-repo-page="previous">Previous</button><span data-gh-repo-page-label></span><button type="button" class="hd-btn" data-gh-repo-page="next">Next</button></div></div></details>
  <section class="gh-main"><div class="gh-heading"><h2 data-gh-scope>All repositories</h2></div><div data-gh-related></div><div class="gh-tabs" role="tablist" aria-label="Issue classification">${tabs.map(([key,label])=>`<button type="button" role="tab" id="gh-tab-${key}" data-gh-tab="${key}" aria-controls="gh-issue-panel" aria-selected="${key==='all'}" tabindex="${key==='all'?0:-1}">${label}<small data-gh-tab-count="${key}">…</small></button>`).join('')}</div><div class="gh-controls"><label>Search open issues<input type="search" data-gh-issue-search placeholder="Issue title or number"></label></div><p class="gh-section-label" data-gh-issue-count role="status"></p><p class="gh-warning" data-gh-issue-error role="status"></p><div class="gh-issue-layout"><div class="gh-issue-list" id="gh-issue-panel" role="tabpanel" aria-labelledby="gh-tab-all"></div><aside class="gh-inspector" data-gh-inspector aria-label="Issue inspector" hidden></aside></div><div class="gh-pages" data-gh-issue-pages><button type="button" class="hd-btn" data-gh-issue-page="previous">Previous</button><span data-gh-issue-page-label></span><button type="button" class="hd-btn" data-gh-issue-page="next">Next</button></div></section></div>`;
 const el=selector=>container.querySelector(selector);
 const narrowViewport=window.matchMedia('(max-width:1249px)');
 const narrow=()=>narrowViewport.matches;
 // A selected repository links to its agents, missions, CI runs and pull requests (cross-linking).
 let disposeRelated=null;
 function showProject(){disposeRelated?.();disposeRelated=null;const slot=el('[data-gh-related]');if(slot)slot.replaceChildren();if(state.repository&&slot&&typeof client.related==='function')disposeRelated=mountRelated(slot,client,'project',state.repository,{footer:`<nav class="hd-inline" style="font-size:13px"><a href="#/ci?repository=${encodeURIComponent(state.repository)}">All CI for this repository</a></nav>`});}
 function sizeWorkspace(){const layout=el('.gh-layout');layout.style.setProperty('--gh-workspace-top',`${Math.max(16,layout.getBoundingClientRect().top+window.scrollY)}px`);layout.style.setProperty('--gh-repository-heading',`${el('.gh-repositories>summary').getBoundingClientRect().height}px`);}
 window.addEventListener('resize',sizeWorkspace,{signal:listeners.signal});
 narrowViewport.addEventListener('change',event=>{
  if(!selected)return;
  if(!event.matches){listScroll=null;return;}
  // Measure the return position in the new document layout before focusing detail.
  const layout=el('.gh-layout'),inspector=el('[data-gh-inspector]');inspector.hidden=true;layout.classList.remove('gh-detail-open');
  [...container.querySelectorAll('[data-gh-issue]')].find(node=>node.dataset.ghIssue===selected.key)?.scrollIntoView({block:'center'});
  listScroll=window.scrollY;layout.classList.add('gh-detail-open');inspector.hidden=false;
  el('[data-gh-inspector]').scrollIntoView({block:'start'});el('[data-gh-close]').focus({preventScroll:true});
  sizeWorkspace();
 },{signal:listeners.signal});
 const cancel=key=>{requests.get(key)?.abort();requests.delete(key);};
 function replace(element,html,key,signature) {
  if(signatures.get(key)===signature)return;signatures.set(key,signature);
  const focused=document.activeElement,within=element.contains(focused);
  const attr=within?['data-gh-repository','data-gh-issue','data-gh-close','data-gh-refresh-issue'].find(name=>focused.hasAttribute(name)):null;
  const value=attr?focused.getAttribute(attr):null,href=within&&focused.tagName==='A'?focused.getAttribute('href'):null;
  const labelsOpen=element.querySelector('[data-gh-labels-details]')?.open;
  const labelsFocused=within&&focused.tagName==='SUMMARY';
  const body=element.querySelector('[data-gh-body]'),bodyScroll=body?.scrollTop,bodyFocused=within&&focused.hasAttribute('data-gh-body');
  const scrollTop=element.scrollTop;
  element.innerHTML=html;element.scrollTop=scrollTop;
  if(attr)[...element.querySelectorAll(`[${attr}]`)].find(node=>node.getAttribute(attr)===value)?.focus({preventScroll:true});
  if(href)[...element.querySelectorAll('a')].find(node=>node.getAttribute('href')===href)?.focus({preventScroll:true});
  const labels=element.querySelector('[data-gh-labels-details]');if(labels&&labelsOpen)labels.open=true;if(labelsFocused)labels?.querySelector('summary')?.focus({preventScroll:true});
  const nextBody=element.querySelector('[data-gh-body]');if(nextBody&&bodyScroll!=null)nextBody.scrollTop=bodyScroll;if(bodyFocused)nextBody?.focus({preventScroll:true});
 }
 function pages(kind,snapshot,current) {
  const pagination=snapshot?.pagination||{},total=integer(pagination.total)?pagination.total:null;
  const next=pagination.has_next===true;
  el(`[data-gh-${kind}-pages]`).hidden=current===1&&!next;
  el(`[data-gh-${kind}-page="previous"]`).disabled=current===1;
  const nextButton=el(`[data-gh-${kind}-page="next"]`),focusedNext=document.activeElement===nextButton;
  nextButton.disabled=snapshot?!next:false;
  if(snapshot&&!next&&focusedNext)(el(kind==='repo'?'[data-gh-repository]':'[data-gh-issue]')||el(kind==='repo'?'[data-gh-repo-search]':'[data-gh-issue-search]')).focus({preventScroll:true});
  el(`[data-gh-${kind}-page-label]`).textContent=`Page ${current}${total!==null?` · ${total} total`:''}`;
 }
 function tick(){for(const node of container.querySelectorAll('[data-gh-time]'))node.textContent=formatDuration(Math.max(0,Date.now()-Number(node.dataset.ghTime)));}
 let motionView='';
 function draw() {
  if(disposed)return;
  replace(el('[data-gh-health]'),renderGithubHealth(repositories,{failed:!!repoError}),'health',JSON.stringify([!repositories&&!!repoError,repositories&&[repositories.status,repositories.refreshing,repositories.progress,repositories.partial,repositories.stale,repositories.account,repositories.last_success_at,repositories.error,repositories.rate_limit]]));
  el('[data-gh-repo-error]').textContent=repoError;
  el('[data-gh-issue-error]').textContent=[issueError,issues?.error,issues?.partial?'Issue coverage is partial; additional open issues may not have been read.':'',issues?.stale?'Showing previously synchronized issue summaries.':''].filter(Boolean).join(' ');
  // Lists are rebuilt with innerHTML (Task 19 moves them onto the patcher); motion pairs old and new
  // nodes by key: a repository or issue whose text changed flashes, tab counts count up.
  const repoKey=node=>node.dataset.ghRepository,issueRowKey=node=>node.dataset.ghIssueRow;
  // Only data-driven changes animate: not the first load, not a change of repository, tab, search or page.
  const view=JSON.stringify(state),live=!!repositories&&!!issues&&motionView===view;motionView=repositories&&issues?view:'';
  const reposBefore=live?snapshotText(el('[data-gh-repositories]'),'[data-gh-repository]',repoKey):null,issuesBefore=live?snapshotText(el('#gh-issue-panel'),'[data-gh-issue-row]',issueRowKey):null,countsBefore=live?snapshotText(container,'[data-gh-tab-count]'):null;
  const repos=rankGithubRepositories(repositories?.repositories);
  replace(el('[data-gh-repositories]'),repos.map(repo=>renderGithubRepository(repo,repo.repository_id===state.repository)).join('')||(!repositories?(repoError?'<li class="gh-empty">Repositories could not be loaded yet; retrying automatically.</li>':`<li>${skeleton('repositories',{rows:3,height:52})}</li>`):`<li class="gh-empty">${repositories.available===false?'Repository data is unavailable. Connection details appear above.':'No repositories match this search.'}</li>`),'repos',JSON.stringify([repositories,state.repository,!repositories&&!!repoError]));
  animateChanges(el('[data-gh-repositories]'),'[data-gh-repository]',reposBefore,repoKey,{count:false});
  el('[data-gh-repo-count]').textContent=repositories?`${repos.length} repositories on this page${integer(repositories.pagination?.total)?` · ${repositories.pagination.total} matching`:''}`:'';
  el('[data-gh-all-repositories]').setAttribute('aria-pressed',String(!state.repository));
  el('[data-gh-scope]').textContent=state.repositoryLabel;
  for(const [key] of tabs){const button=el(`[data-gh-tab="${key}"]`);button.setAttribute('aria-selected',String(state.tab===key));button.tabIndex=state.tab===key?0:-1;el(`[data-gh-tab-count="${key}"]`).textContent=integer(issues?.counts?.[key])?issues.counts[key]:'…';}
  const rows=records(issues?.issues);
  el('#gh-issue-panel').setAttribute('aria-labelledby',`gh-tab-${state.tab}`);
  replace(el('#gh-issue-panel'),rows.map(issue=>renderGithubIssue(issue,selected?.key===issueKey(issue))).join('')||(!issues?(issueError?'<p class="gh-empty">Issues could not be loaded yet; retrying automatically.</p>':skeleton('open issues',{rows:2,height:64})):`<p class="gh-empty">${issues.available===false?'Issue data is unavailable.':'No open issues match this view.'}${issues?.partial?' Synchronization is incomplete.':''}</p>`),'issues',JSON.stringify([issues,selected?.key,!issues&&!!issueError]));
  animateChanges(el('#gh-issue-panel'),'[data-gh-issue-row]',issuesBefore,issueRowKey,{count:false});
  animateChanges(container,'[data-gh-tab-count]',countsBefore);
  el('[data-gh-issue-count]').textContent=issues?`${rows.length} issues on this page${integer(issues.pagination?.total)?` · ${issues.pagination.total} matching open issues`:''}`:'';
  pages('repo',repositories,state.repositoryPage);pages('issue',issues,state.issuePage);
  el('.gh-layout').classList.toggle('gh-detail-open',!!selected);
  const inspector=el('[data-gh-inspector]');inspector.hidden=!selected;
  if(selected)replace(inspector,renderGithubDetail(detail,{summary:selected.summary,loading:detailLoading,error:detailError}),'detail',JSON.stringify([selected,detail,detailLoading,detailError]));
  tick();sizeWorkspace();
 }
 async function request(key,read,accept,reject) {
  if(disposed||document.hidden||requests.has(key))return;
  const controller=new AbortController();requests.set(key,controller);let timeout=false;
  const deadline=setTimeout(()=>{timeout=true;controller.abort();},15000);
  try{const response=await read(controller.signal);if(!disposed&&!controller.signal.aborted&&requests.get(key)===controller)accept(response);}
  catch(error){if(!disposed&&requests.get(key)===controller&&(!controller.signal.aborted||timeout))reject(error);}
  finally{clearTimeout(deadline);if(requests.get(key)===controller)requests.delete(key);draw();}
 }
 function loadDetail(force=false) {
  if(!selected)return;
  const current=selected,stored=cache.get(current.key);
  if(!force&&stored?.version===current.summary.updated_at){detail=stored.response;detailError='';detailLoading=false;draw();return;}
  cancel('detail');detailLoading=true;detailError='';draw();
  request('detail',signal=>client.getGithubIssue(current.summary.repository_id,current.summary.number,{signal}),response=>{
   if(selected?.key!==current.key)return;
   if((connectionGeneration!==null&&response?.connection_generation!==undefined&&response.connection_generation!==connectionGeneration)||(account&&text(response?.account)&&account!==response.account)){clearSelection();cache.clear();issues=null;issueError='GitHub connection changed. Refreshing account context.';readRepositories();return;}
   if(!response?.issue||issueKey(response.issue)!==current.key)throw new Error('Invalid issue response');
   detail=response;detailLoading=false;
   if(!cache.has(current.key)&&cache.size>=20)cache.delete(cache.keys().next().value);
   cache.set(current.key,{version:current.summary.updated_at,response});
  },()=>{if(selected?.key===current.key){detailLoading=false;detailError='Issue details could not refresh. Use Refresh issue to retry.';}});
 }
 function readRepositories(){request('repos',signal=>client.getGithub({page:state.repositoryPage,per_page:20,search:state.repositorySearch,signal}),response=>{
  if(!Array.isArray(response?.repositories))throw new Error('Invalid repositories response');
  const nextAccount=text(response.account?.login||response.account)||null,nextGeneration=response.connection_generation;
  const changed=(account&&nextAccount&&account!==nextAccount)||(connectionGeneration!==null&&nextGeneration!==undefined&&connectionGeneration!==nextGeneration);
  const disconnected=['not_configured','auth_error'].includes(response.status);
  if(changed||disconnected){clearSelection();cache.clear();cancel('issues');issues=null;issueError='';state.repository='';state.repositoryLabel='All repositories';state.issuePage=1;showProject();}
  account=disconnected?null:nextAccount;if(nextGeneration!==undefined)connectionGeneration=nextGeneration;
  repositories=response;repoError='';if(changed||disconnected)readIssues();
  const note=githubSyncNote(response);chipEl()?.setDetail(note.text,{warn:note.warn});chip.ok('repos');
 },error=>{chip.fail('repos',`Repositories: ${error?.message||'request failed'}`);repoError=`Repositories could not refresh.${repositories?' Showing last received repositories.':' Retrying automatically.'}`;});}
 function readIssues(){request('issues',signal=>client.getGithubIssues(state.repository||undefined,{tab:state.tab,search:state.issueSearch,page:state.issuePage,per_page:20,signal}),response=>{
  if(!Array.isArray(response?.issues))throw new Error('Invalid issues response');
  if((connectionGeneration!==null&&response.connection_generation!==undefined&&response.connection_generation!==connectionGeneration)||(account&&text(response.account)&&account!==response.account)){clearSelection();cache.clear();issues=null;issueError='GitHub connection changed. Refreshing account context.';readRepositories();return;}
  issues=response;issueError='';chip.ok('issues');
  const latest=selected&&records(response.issues).find(issue=>issueKey(issue)===selected.key);
  if(latest&&latest.updated_at!==selected.summary.updated_at){selected={key:selected.key,summary:latest};loadDetail();}
 },error=>{chip.fail('issues',`Open issues: ${error?.message||'request failed'}`);issueError=`Open issues could not refresh.${issues?' Showing last received issues.':' Retrying automatically.'}`;});}
 function refresh(){readRepositories();readIssues();}
 function clearSelection(){cancel('detail');selected=null;el('.gh-layout').classList.remove('gh-detail-open');detail=null;detailError='';detailLoading=false;el('[data-gh-inspector]').innerHTML='';el('[data-gh-inspector]').hidden=true;signatures.delete('detail');}
 function changeIssues(){cancel('issues');issues=null;issueError='';readIssues();draw();}
 container.addEventListener('click',event=>{
  if(disposed)return;const button=event.target.closest('button');if(!button)return;
  if(button.hasAttribute('data-gh-all-repositories')||button.hasAttribute('data-gh-repository')){
   state.repository=button.dataset.ghRepository||'';state.repositoryLabel=repositories?.repositories?.find(r=>r.repository_id===state.repository)?.name_with_owner||state.repository||'All repositories';state.issuePage=1;clearSelection();showProject();changeIssues();
  }
  if(button.dataset.ghTab){state.tab=button.dataset.ghTab;state.issuePage=1;changeIssues();}
  if(button.dataset.ghRepoPage){state.repositoryPage+=button.dataset.ghRepoPage==='next'?1:-1;cancel('repos');repositories=null;readRepositories();draw();}
  if(button.dataset.ghIssuePage){state.issuePage+=button.dataset.ghIssuePage==='next'?1:-1;changeIssues();}
  if(button.hasAttribute('data-gh-issue')){
   const summary=records(issues?.issues).find(issue=>issueKey(issue)===button.dataset.ghIssue);if(!summary)return;
   if(narrow())listScroll=window.scrollY;
   cancel('detail');selected={key:issueKey(summary),summary};detail=cache.get(selected.key)?.response||null;loadDetail();
   if(narrow()){el('[data-gh-inspector]').scrollIntoView({block:'start'});el('[data-gh-close]').focus({preventScroll:true});}
  }
  if(button.hasAttribute('data-gh-close')){const key=selected?.key,restore=narrow()?listScroll:null;clearSelection();draw();[...container.querySelectorAll('[data-gh-issue]')].find(node=>node.dataset.ghIssue===key)?.focus({preventScroll:true});if(restore!==null)window.scrollTo(0,restore);listScroll=null;}
  if(button.hasAttribute('data-gh-refresh-issue')&&!detailLoading)loadDetail(true);
 },{signal:listeners.signal});
 el('[data-gh-repo-search]').addEventListener('input',event=>{state.repositorySearch=event.target.value;state.repositoryPage=1;cancel('repos');clearTimeout(repositoryDebounce);repositoryDebounce=setTimeout(()=>{repositories=null;readRepositories();draw();},250);},{signal:listeners.signal});
 el('[data-gh-issue-search]').addEventListener('input',event=>{state.issueSearch=event.target.value;state.issuePage=1;cancel('issues');clearTimeout(issueDebounce);issueDebounce=setTimeout(changeIssues,250);},{signal:listeners.signal});
 container.querySelector('.gh-tabs').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const i=tabs.findIndex(([key])=>key===state.tab);state.tab=tabs[event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(event.key==='ArrowRight'?1:tabs.length-1))%tabs.length][0];state.issuePage=1;event.preventDefault();changeIssues();el(`[data-gh-tab="${state.tab}"]`).focus();},{signal:listeners.signal});
 function visibility(){clearInterval(clock);clearInterval(pollTimer);if(document.hidden){for(const controller of requests.values())controller.abort();requests.clear();detailLoading=false;return;}refresh();if(selected&&!detail)loadDetail();clock=setInterval(tick,1000);pollTimer=setInterval(refresh,5000);}
 document.addEventListener('visibilitychange',visibility,{signal:listeners.signal});draw();visibility();
 return {refresh,dispose(){disposed=true;disposeRelated?.();listeners.abort();clearInterval(clock);clearInterval(pollTimer);clearTimeout(repositoryDebounce);clearTimeout(issueDebounce);for(const controller of requests.values())controller.abort();requests.clear();}};
}
