// Task 19: the remaining views patch in place through the shared patcher (morph / patchList).
// Per view: a live refresh with identical data performs zero DOM writes; a refresh with one
// changed item writes only inside that item while every other row, an open <details>, focus and
// a typed filter survive; and a live event refreshes the view (it is registered with the bus).
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,liveRefresh,sleep,T0} from './helpers/live.mjs';

// Records DOM writes under #view as they happen, described at record time (a text node a later
// write detaches is still attributed to its element). Self-ticking ages are left out.
const observe=page=>page.evaluate(sel=>{
 window.__muts=[];window.__spinners=0;window.__obs?.disconnect();
 window.__obs=new MutationObserver(records=>{for(const r of records){
  const el=r.target.nodeType===1?r.target:r.target.parentElement||r.target.__parent;
  for(const n of r.addedNodes)if(n.nodeType===1&&(n.matches('herdr-spinner,[data-skeleton]')||n.querySelector('herdr-spinner,[data-skeleton]')))window.__spinners++;
  if(el?.closest('herdr-freshness,herdr-mission-activity'))continue;
  window.__muts.push({target:r.target,el,text:`${r.type}:${r.attributeName||''}:${r.oldValue??''}->${r.target.nodeType===3?r.target.nodeValue:''}:${el?.outerHTML?.slice(0,80)}`});
 }});
 window.__obs.observe(document.getElementById('view')||document.body,{childList:true,subtree:true,attributes:true,characterData:true,characterDataOldValue:true});
},null);

// Mutations recorded by observe(), minus self-ticking age spans (their text follows the clock).
const ticking='[data-gh-time],[data-topology-time],[data-overview-stamp],[data-service-age],[data-service-seen],[data-report],[data-freshness],[data-report-age],[data-recorded-age],[data-output-age],[data-wait-age],[data-integration-time]';
const writes=(page,inside=null)=>page.evaluate(([sel,inside])=>{
 const box=inside?document.querySelector(inside):null;
 return window.__muts.filter(m=>!m.el?.closest?.(sel)).filter(m=>!box||!(box.contains(m.target)||box.contains(m.el))).map(m=>m.text);
},[ticking,inside]);

// ---------------------------------------------------------------- GitHub
const ghRepo=(name,count)=>({github_id:name,repository_id:`github.com/lab/${name}`,name_with_owner:`lab/${name}`,open_issue_count:count,issues_loaded:2,issues_complete:true});
const ghIssue=(number,extra={})=>({github_id:String(number),repository_id:'github.com/lab/busy',number,title:`Issue ${number}`,state:'OPEN',updated_at:'2025-12-01T00:00:00Z',labels:['auto-implement','size:s'],assignees:[],autospec:{managed:true,state:'candidate',evidence_source:'github_labels',reasons:[]},...extra});
function githubApi(state){
 const snap=extra=>({available:true,status:'ready',refreshing:false,stale:false,partial:false,account:'operator',connection_generation:1,last_success_at:'2026-01-01T00:00:00Z',pagination:{page:1,per_page:20,total:3,has_next:false},...extra});
 return (p,url)=>{
  if(p==='/api/v1/github')return snap({repositories:structuredClone(state.repos)});
  if(p==='/api/v1/github/issues')return snap({issues:structuredClone(state.issues),counts:{all:state.issues.length,autospec:3,preparation:0,in_progress:0,blocked:0}});
  if(p==='/api/v1/github/issue'){const n=Number(url.searchParams.get('number'));return snap({checked_at:'2026-01-01T00:00:00Z',issue:{...structuredClone(state.issues.find(i=>i.number===n)),body:'Body text'}});}
  return {};
 };
}

test('GitHub: identical refresh writes nothing; one changed issue rewrites only its row; details, focus and filter survive',async()=>withPage(async(page,base)=>{
 const state={repos:[ghRepo('busy',3),ghRepo('small',1)],issues:[ghIssue(1),ghIssue(2),ghIssue(3)]};
 const reads=await boot(page,base,'/github',githubApi(state));
 await page.setViewportSize({width:1600,height:900});// wide: list and inspector side by side
 await until(()=>page.locator('[data-gh-issue-row]').count().then(n=>n===3),'issue rows');
 await page.locator('[data-gh-issue="github.com/lab/busy#2"]').click();
 await until(()=>page.locator('[data-gh-labels-details]').count().then(n=>n===1),'inspector labels');
 await page.evaluate(()=>{document.querySelector('[data-gh-labels-details]').open=true;});
 const search=page.locator('[data-gh-issue-search]');
 await search.fill('issue');await search.evaluate(e=>e.blur());
 await liveRefresh(page);await sleep(300);
 await page.evaluate(()=>{window.__rows=[...document.querySelectorAll('[data-gh-issue-row],[data-gh-repository]')];});

 const before=reads['/api/v1/github/issues'];
 await observe(page);
 await liveRefresh(page);
 assert.ok(reads['/api/v1/github/issues']>before,'refresh fetched issues again');
 assert.deepEqual(await writes(page),[],'identical refresh performs no DOM writes');

 state.issues[2].title='Issue 3 renamed';
 await observe(page);
 await liveRefresh(page);
 await until(()=>page.locator('[data-gh-issue="github.com/lab/busy#3"]').innerText().then(t=>t==='Issue 3 renamed'),'renamed issue');
 const result=await page.evaluate(()=>({
  same:[...document.querySelectorAll('[data-gh-issue-row],[data-gh-repository]')].every((n,i)=>n===window.__rows[i]),
  open:document.querySelector('[data-gh-labels-details]').open,
 }));
 assert.equal(result.same,true,'every row node survives the refresh');
 assert.equal(result.open,true,'the open label list stays open');
 assert.deepEqual(await writes(page,'[data-gh-issue-row="github.com/lab/busy#3"]'),[],'nothing outside the changed row was written');
 assert.equal(await search.inputValue(),'issue','typed issue filter survives');

 // Focus inside the inspector survives a refresh that changes the focused issue.
 await page.locator('[data-gh-refresh-issue]').focus();
 state.issues[1].title='Issue 2 renamed';state.issues[1].updated_at='2025-12-02T00:00:00Z';
 await liveRefresh(page);
 await until(()=>page.locator('[data-gh-inspector] h3').first().innerText().then(t=>t==='Issue 2 renamed'),'inspector updated');
 assert.equal(await page.evaluate(()=>document.activeElement?.hasAttribute('data-gh-refresh-issue')),true,'focus stays on the inspector control');
}));
