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

// ---------------------------------------------------------------- Agents (sessions)
const agent=(id,extra={})=>({session_id:id,agent_role:'coder',host_id:'beast',status:'working',cwd:`/work/${id.startsWith('a')?'alpha':'beta'}`,updated_at:'2026-01-01T00:00:05Z',summary:`Doing ${id}`,...extra});
function sessionsApi(state){
 return p=>{
  if(p==='/api/v1/sessions')return {sessions:structuredClone(state.sessions)};
  if(p==='/api/v1/missions')return {missions:[]};
  return {};
 };
}

test('Agents: identical refresh writes nothing; one changed agent rewrites only its row; focus and filter survive',async()=>withPage(async(page,base)=>{
 const state={sessions:[agent('a1'),agent('a2'),agent('b1')]};
 const reads=await boot(page,base,'/herd',sessionsApi(state));
 await until(()=>page.locator('[data-session-id]').count().then(n=>n===3),'agent rows');
 await page.locator('#ss-host').selectOption('beast');
 await page.locator('#ss-search').fill('doing');await page.locator('#ss-search').evaluate(e=>e.blur());
 await page.locator('[data-session-id="a1"] [data-session-link]').focus();
 await liveRefresh(page,{event_id:'s0',event_type:'SessionUpdated'});
 await page.evaluate(()=>{window.__rows=[...document.querySelectorAll('[data-session-id]')];});

 const before=reads['/api/v1/sessions'];
 await observe(page);
 await liveRefresh(page,{event_id:'s1',event_type:'SessionUpdated'});
 assert.ok(reads['/api/v1/sessions']>before,'refresh fetched agents again');
 assert.deepEqual(await writes(page),[],'identical refresh performs no DOM writes');

 state.sessions[2].summary='Doing something else';
 await observe(page);
 await liveRefresh(page,{event_id:'s2',event_type:'SessionUpdated'});
 await until(()=>page.locator('[data-session-id="b1"] .ss-summary').innerText().then(t=>t==='Doing something else'),'changed summary');
 assert.equal(await page.evaluate(()=>[...document.querySelectorAll('[data-session-id]')].every((n,i)=>n===window.__rows[i])),true,'every row node survives');
 assert.deepEqual(await writes(page,'[data-session-id="b1"]'),[],'nothing outside the changed row was written');
 assert.equal(await page.evaluate(()=>document.activeElement?.closest('[data-session-id]')?.dataset.sessionId),'a1','focus stays on its row');
 assert.equal(await page.locator('#ss-search').inputValue(),'doing','typed filter survives');
 assert.equal(await page.locator('#ss-host').inputValue(),'beast','host filter survives');
}));

// ---------------------------------------------------------------- Services
const svc=(id,extra={})=>({service_id:id,service_name:`Service ${id}`,status:'healthy',host_id:'beast',port:8000,updated_at:'2026-01-01T00:00:05Z',catalog:{scope:'herdr',project:'alpha'},...extra});
function servicesApi(state){
 return p=>{
  if(p==='/api/v1/services')return {services:structuredClone(state.services)};
  const one=p.match(/^\/api\/v1\/services\/(.+)$/);
  if(one)return structuredClone(state.services.find(x=>x.service_id===decodeURIComponent(one[1])));
  return {};
 };
}

test('Services: a live event refreshes the list; identical data writes nothing; one change rewrites only its row',async()=>withPage(async(page,base)=>{
 const state={services:[svc('s1'),svc('s2'),svc('s3',{catalog:{scope:'herdr',project:'beta'}})]};
 const reads=await boot(page,base,'/services',servicesApi(state));
 await until(()=>page.locator('[data-service-id]').count().then(n=>n===3),'service rows');
 await page.locator('#sc-search').fill('service');await page.locator('#sc-search').evaluate(e=>e.blur());
 await page.evaluate(()=>{document.querySelector('.sc-group[data-group]').open=false;});
 const openBefore=await page.evaluate(()=>[...document.querySelectorAll('.sc-group')].map(d=>d.open));
 await page.evaluate(()=>{window.__rows=[...document.querySelectorAll('[data-service-id]')];});

 // The page polls every 30 s; a re-read within 6 s of an event comes from the refresh bus.
 const before=reads['/api/v1/services'];
 await observe(page);
 await liveRefresh(page,{event_id:'v1',event_type:'ServiceUpdated'});
 assert.ok(reads['/api/v1/services']>before,'a live event refreshed the services');
 assert.deepEqual(await writes(page),[],'identical refresh performs no DOM writes');
 assert.equal(await page.evaluate(()=>window.__spinners),0,'no loader on a live refresh');

 state.services[1].port=9000;
 await observe(page);
 await liveRefresh(page,{event_id:'v2',event_type:'ServiceUpdated'});
 await until(()=>page.locator('[data-service-id="s2"] .sc-location').textContent().then(t=>t.includes(':9000')),'changed port');
 assert.equal(await page.evaluate(()=>[...document.querySelectorAll('[data-service-id]')].every((n,i)=>n===window.__rows[i])),true,'every row node survives');
 assert.deepEqual(await writes(page,'[data-service-id="s2"]'),[],'nothing outside the changed row was written');
 assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('.sc-group')].map(d=>d.open)),openBefore,'open and closed groups stay as the user left them');
 assert.equal(await page.locator('#sc-search').inputValue(),'service','typed filter survives');
}));

test('Service detail: a live event refreshes it and identical data writes nothing',async()=>withPage(async(page,base)=>{
 const state={services:[svc('s1',{url:'http://dev.lan:8000'})]};
 const reads=await boot(page,base,'/services/s1',servicesApi(state));
 await until(()=>page.locator('[data-service-state]').count().then(n=>n===1),'service detail');
 await page.locator('[data-service-back]').focus();
 // The detail polls every 30 s; a read within 6 s of an event comes from the refresh bus.
 const before=reads['/api/v1/services']??0;
 await observe(page);
 await liveRefresh(page,{event_id:'d1',event_type:'ServiceUpdated'});
 assert.ok((reads['/api/v1/services']??0)>before,'a live event refreshed the detail');
 assert.deepEqual(await writes(page),[],'identical refresh performs no DOM writes');
 assert.equal(await page.evaluate(()=>document.activeElement?.hasAttribute('data-service-back')),true,'focus stays');
}));

// ---------------------------------------------------------------- Missions catalog
function projection(n,extra={}){
 return {contract_version:1,mission_key:`pi:M${n}`,host_id:'beast',observed_at:T0,snapshot_at:T0,activity_snapshot_at:T0,connection:'connected',
  mission:{id:`M${n}`,title:`Mission ${n}`,status:'EXECUTING'},
  freshness:{state:'live',age_seconds:1,stale_after_seconds:3600,disconnect_after_seconds:7200},
  activity:{health:'active',currentActivity:{summary:`Working ${n}`}},...extra};
}
function missionsApi(state){
 return p=>{
  if(p==='/api/v1/mission-activity')return {activities:structuredClone(state.projections)};
  if(p==='/api/v1/missions')return {missions:[]};
  return {};
 };
}

test('Missions: identical refresh writes nothing; tabs, counts and host filter keep their nodes and the typed filter',async()=>withPage(async(page,base)=>{
 const state={projections:[projection(1),projection(2),projection(3)]};
 const reads=await boot(page,base,'/missions',missionsApi(state));
 await until(()=>page.locator('[data-mission-key]').count().then(n=>n===3),'mission rows');
 await page.locator('#mc-host').selectOption('beast');
 await page.locator('#mc-search').fill('mission');await page.locator('#mc-search').evaluate(e=>e.blur());
 await liveRefresh(page,{event_id:'m0',event_type:'MissionUpdated'});
 await page.evaluate(()=>{window.__nodes=[...document.querySelectorAll('[data-mission-key],#mc-tabs [data-tab],#mc-counts [data-filter],#mc-host option')];});

 const before=reads['/api/v1/missions'];
 await observe(page);
 await liveRefresh(page,{event_id:'m1',event_type:'MissionUpdated'});
 assert.ok(reads['/api/v1/missions']>before,'a live event refreshed recorded missions');
 assert.deepEqual(await writes(page),[],'identical refresh performs no DOM writes');

 state.projections[1].activity.currentActivity.summary='Working on something new';
 await observe(page);
 await liveRefresh(page,{event_id:'m2',event_type:'MissionUpdated'});
 await until(()=>page.locator('[data-mission-key="pi:M2"]').innerText().then(t=>t.includes('something new')),'changed activity');
 assert.equal(await page.evaluate(()=>[...document.querySelectorAll('[data-mission-key],#mc-tabs [data-tab],#mc-counts [data-filter],#mc-host option')].every((n,i)=>n===window.__nodes[i])),true,'rows, tabs, counts and options keep their nodes');
 assert.deepEqual(await writes(page,'[data-mission-key="pi:M2"]'),[],'nothing outside the changed row was written');
 assert.equal(await page.locator('#mc-search').inputValue(),'mission','typed filter survives');
 assert.equal(await page.locator('#mc-host').inputValue(),'beast','host filter survives');
}));

// ---------------------------------------------------------------- Topology
function topologyApi(state){
 return p=>{
  if(p==='/api/v1/hosts')return {hosts:[{host_id:'beast',host_name:'Beast',status:'healthy',last_seen:T0}]};
  if(p==='/api/v1/services')return {services:structuredClone(state.services)};
  if(p==='/api/v1/sessions')return {sessions:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  if(p==='/api/v1/mission-activity')return {activities:[]};
  if(p==='/api/v1/ci')return {available:true,status:'connected',pipelines:{}};
  return {};
 };
}

test('Topology: identical refresh writes nothing; one changed service rewrites only its row; open groups, focus and search survive; Collapse still closes',async()=>withPage(async(page,base)=>{
 const state={services:[svc('s1'),svc('s2'),svc('s3')]};
 const reads=await boot(page,base,'/topology',topologyApi(state));
 await until(()=>page.locator('details[data-tx-disclosure="host:beast"]').count().then(n=>n===1),'beast group');
 await page.locator('details[data-tx-disclosure="host:beast"] > summary').click();
 // Service subgroups start closed; open the one under beast.
 await until(()=>page.locator('details.tx-subgroup').count().then(n=>n>0),'service subgroup');
 await page.locator('details.tx-subgroup > summary').first().click();
 await until(()=>page.locator('[data-topology-entry]').count().then(n=>n===3),'service rows');
 await page.locator('[data-tx-type]').selectOption('service');
 await page.locator('[data-topology-entry="service:s1"] [data-entry-link]').focus();
 await liveRefresh(page,{event_id:'t0',event_type:'ServiceUpdated'});
 await page.evaluate(()=>{window.__rows=[...document.querySelectorAll('[data-topology-entry],details[data-tx-disclosure]')];});

 const before=reads['/api/v1/services'];
 await observe(page);
 await liveRefresh(page,{event_id:'t1',event_type:'ServiceUpdated'});
 assert.ok(reads['/api/v1/services']>before,'refresh fetched services again');
 assert.deepEqual(await writes(page),[],'identical refresh performs no DOM writes');

 state.services[1].service_name='Service s2 renamed';
 await observe(page);
 await liveRefresh(page,{event_id:'t2',event_type:'ServiceUpdated'});
 await until(()=>page.locator('[data-topology-entry="service:s2"] [data-entry-link]').innerText().then(t=>t==='Service s2 renamed'),'renamed service');
 assert.equal(await page.evaluate(()=>[...document.querySelectorAll('[data-topology-entry],details[data-tx-disclosure]')].every((n,i)=>n===window.__rows[i])),true,'groups and rows keep their nodes');
 assert.deepEqual(await writes(page,'[data-topology-entry="service:s2"]'),[],'nothing outside the changed row was written');
 assert.equal(await page.evaluate(()=>document.activeElement?.closest('[data-topology-entry]')?.dataset.topologyEntry),'service:s1','focus stays on its row');
 assert.equal(await page.locator('[data-tx-type]').inputValue(),'service','type filter survives');
 assert.equal(await page.locator('details[data-tx-disclosure="host:beast"]').evaluate(d=>d.open),true,'the open group stays open');

 await page.locator('[data-tx-collapse]').click();
 await until(()=>page.locator('details[data-tx-disclosure="host:beast"]').evaluate(d=>!d.open),'Collapse groups closes the group');
}));
