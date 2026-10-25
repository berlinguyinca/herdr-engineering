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

// ---------------------------------------------------------------- Overview
function overviewApi(state){
 return p=>{
  if(p==='/api/v1/sessions')return {sessions:structuredClone(state.sessions)};
  if(p==='/api/v1/hosts')return {hosts:[{host_id:'beast',host_name:'Beast',status:'healthy',last_seen:T0}]};
  if(p==='/api/v1/fleet/inventory')return {hosts:[],components:[],summary:{drifted:0,missing:0}};
  if(p==='/api/v1/services')return {services:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  if(p==='/api/v1/mission-activity')return {activities:[]};
  if(p==='/api/v1/events'||p==='/api/v1/activity')return {events:[]};
  if(p==='/api/v1/health')return {status:'ok'};
  if(p==='/api/v1/ci')return {available:true,status:'connected',pipelines:{}};
  if(p==='/api/v1/reviews')return {reviews:[]};
  return {};
 };
}

test('Overview: identical refresh writes nothing; one changed agent rewrites only its row; focus and the open Customize panel survive',async()=>withPage(async(page,base)=>{
 const state={sessions:[agent('a1'),agent('a2'),agent('b1')]};
 const reads=await boot(page,base,'/overview',overviewApi(state));
 await until(()=>page.locator('[data-overview="sessions"] .ov-row').count().then(n=>n===3),'agent rows');
 await page.locator('.ov-customize > summary').click();
 await page.locator('[data-overview="sessions"] .ov-row a').first().focus();
 const focused=await page.evaluate(()=>document.activeElement.getAttribute('href'));
 await liveRefresh(page,{event_id:'o0',event_type:'SessionUpdated'});
 await page.evaluate(()=>{window.__rows=[...document.querySelectorAll('.ov-panel,.ov-row')];});

 const before=reads['/api/v1/sessions'];
 await observe(page);
 await liveRefresh(page,{event_id:'o1',event_type:'SessionUpdated'});
 assert.ok(reads['/api/v1/sessions']>before,'refresh fetched agents again');
 assert.deepEqual(await writes(page),[],'identical refresh performs no DOM writes');

 state.sessions[2].cwd='/work/gamma';
 await observe(page);
 await liveRefresh(page,{event_id:'o2',event_type:'SessionUpdated'});
 await until(()=>page.locator('[data-overview="sessions"] .ov-row[data-key="b1"]').innerText().then(t=>t.includes('/work/gamma')),'changed agent');
 assert.equal(await page.evaluate(()=>[...document.querySelectorAll('.ov-panel,.ov-row')].every((n,i)=>n===window.__rows[i])),true,'panels and rows keep their nodes');
 assert.deepEqual(await writes(page,'[data-overview="sessions"] .ov-row[data-key="b1"]'),[],'nothing outside the changed row was written');
 assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('href')),focused,'focus stays on its link');
 assert.equal(await page.locator('.ov-customize').evaluate(d=>d.open),true,'Customize stays open');
}));

test('Overview: a resolved attention row kept for a text selection goes once the selection is released',async()=>withPage(async(page,base)=>{
 const state={sessions:[],projections:[projection(1,{activity:{health:'blocked',currentActivity:{summary:'Stuck'}}})]};
 const api=overviewApi(state);
 await boot(page,base,'/overview',p=>p==='/api/v1/mission-activity'?{activities:structuredClone(state.projections)}:api(p));
 const row=page.locator('[data-overview="attention"] [data-attention-key]');
 await until(()=>row.count().then(n=>n===1),'attention row');
 await page.evaluate(()=>{const r=document.createRange();r.selectNodeContents(document.querySelector('[data-overview="attention"] [data-attention-key] a'));getSelection().removeAllRanges();getSelection().addRange(r);});
 state.projections[0].activity.health='active';
 await liveRefresh(page,{event_id:'a1',event_type:'MissionUpdated'});
 assert.equal(await row.count(),1,'the selected row stays while the text is selected');
 await page.evaluate(()=>getSelection().removeAllRanges());
 await page.clock.runFor(2000);
 await until(()=>row.count().then(n=>n===0),'the resolved row is removed once the selection is gone',3000);
}));

// ---------------------------------------------------------------- Reviews (integrations view)
const review=(id,extra={})=>({review_id:id,title:`Review ${id}`,kind:'plan',status:'pending',repository:'lab/alpha',updated_at:'2026-01-01T00:00:05Z',...extra});
function reviewsApi(state){
 return p=>{
  if(p==='/api/v1/reviews')return {reviews:structuredClone(state.reviews)};
  const one=p.match(/^\/api\/v1\/reviews\/(.+)$/);
  if(one)return {review:{...state.reviews.find(r=>r.review_id===decodeURIComponent(one[1])),plan:'Plan text'}};
  return {};
 };
}

test('Reviews: identical refresh writes nothing; one changed review rewrites only its row; open details, focus and search survive',async()=>withPage(async(page,base)=>{
 const state={reviews:[review('r1'),review('r2'),review('r3')]};
 const reads=await boot(page,base,'/reviews',reviewsApi(state));
 await until(()=>page.locator('[data-entry]').count().then(n=>n===3),'review rows');
 await page.locator('[data-ig-search]').fill('review');await page.locator('[data-ig-search]').evaluate(e=>e.blur());
 const first=page.locator('[data-entry]').first();
 await first.locator('[data-review-details] > summary').click();
 await first.locator('[data-review-details] > summary').focus();
 await liveRefresh(page,{event_id:'r0',event_type:'ReviewUpdated'});
 await page.evaluate(()=>{window.__rows=[...document.querySelectorAll('[data-entry],.ig-group')];});

 const before=reads['/api/v1/reviews'];
 await observe(page);
 await liveRefresh(page,{event_id:'r1',event_type:'ReviewUpdated'});
 assert.ok(reads['/api/v1/reviews']>before,'refresh fetched reviews again');
 assert.deepEqual(await writes(page),[],'identical refresh performs no DOM writes');

 const target=await page.locator('[data-entry]').nth(2).getAttribute('data-entry');
 const id=state.reviews.find(r=>target.includes(r.review_id))?.review_id||'r3';
 state.reviews.find(r=>r.review_id===id).title='Renamed review';
 await observe(page);
 await liveRefresh(page,{event_id:'r2',event_type:'ReviewUpdated'});
 await until(()=>page.locator(`[data-entry="${target}"]`).innerText().then(t=>t.includes('Renamed review')),'changed review');
 const result=await page.evaluate(()=>({same:[...document.querySelectorAll('[data-entry]')].every(n=>window.__rows.includes(n)),open:document.querySelector('[data-entry] [data-review-details]').open,focus:document.activeElement?.tagName}));
 assert.equal(result.same,true,'rows keep their nodes');
 assert.equal(result.open,true,'the open review stays open');
 assert.equal(result.focus,'SUMMARY','focus stays on the summary');
 assert.equal(await page.locator('[data-ig-search]').inputValue(),'review','typed search survives');
}));

// ---------------------------------------------------------------- CI (integrations view + health map)
const ago=ms=>new Date(Date.parse(T0)-ms).toISOString();
const ciRun=(n,state)=>({provider_repo_id:7,provider_url:'https://ci.example',repository:'org/app',run_number:n,state,branch:'main',created_at:ago(3600000+n*60000),started_at:ago(3500000+n*60000),finished_at:state==='running'?null:ago(3000000+n*60000),updated_at:ago(1000)});
function ciApi(state){
 return p=>{
  if(p==='/api/v1/ci')return {available:true,status:'connected',pipelines:{'org/app':structuredClone(state.runs)}};
  if(p==='/api/v1/ci/history')return {available:true,repos:[{id:7,provider_url:'https://ci.example',repository:'org/app',default_branch:'main'}],pipelines:structuredClone(state.runs),coverage:{}};
  if(p==='/api/v1/ci/reports')return {available:true,pipelines:[],reports:[]};
  if(p==='/api/v1/ci/pipeline')return {provider_url:'https://ci.example',pipeline:{...structuredClone(state.runs[0]),tasks:[{id:1,pid:1,name:'build',state:'success'}]}};
  return {};
 };
}

test('CI: with a run open in the inspector, an identical refresh writes nothing (list, charts, health map, inspector)',async()=>withPage(async(page,base)=>{
 const state={runs:[ciRun(1,'success'),ciRun(2,'failure'),ciRun(3,'success')]};
 const reads=await boot(page,base,'/ci?tab=finished',ciApi(state));
 await page.setViewportSize({width:1600,height:900});
 await page.locator('[data-ci-workspace="runs"]').click();
 await until(()=>page.locator('[data-run]').count().then(n=>n===2),'finished runs');
 await page.locator('[data-run]').first().click();
 await until(()=>page.locator('.ig-inspector:not([hidden]) .ig-active-pipeline').count().then(n=>n===1),'inspector');
 await liveRefresh(page,{event_id:'c0',event_type:'CiUpdated'});
 await liveRefresh(page,{event_id:'c1',event_type:'CiUpdated'});
 const before=reads['/api/v1/ci/pipeline'];
 await observe(page);
 await liveRefresh(page,{event_id:'c2',event_type:'CiUpdated'});
 assert.ok(reads['/api/v1/ci/pipeline']>before,'the open pipeline was re-read');
 // The hours heatmap slides with the clock: its two edge buckets (the one leaving the 24 h window
 // and the current hour) restate their bounds. That is time passing, not a refresh.
 const edge=w=>/ci-health-cell[^"]*is-incomplete/.test(w)||/ UTC->/.test(w);
 assert.deepEqual((await writes(page)).filter(w=>!edge(w)),[],'identical refresh performs no DOM writes');
}));

// ---------------------------------------------------------------- Collaboration room
// The real collab-view.js over a mocked collab API whose room state always reports "changed", so
// every long-poll round re-applies the same data (as a live event's re-ask does).
test('Collab: repeated identical room state writes nothing; one changed task rewrites only its row',async()=>withPage(async(page,base)=>{
 const T='2026-10-23T10:00:00+00:00';
 const task=(id,status)=>({task_id:id,target_agent_id:'agent_1',target_name:'coder',text:`Task ${id}`,status,created_at:T});
 const m={tasks:[task('t1','pending'),task('t2','pending')],reads:0,hang:null,fail:0};
 await page.route('**/api/v1/collab/**',async route=>{
  const url=new URL(route.request().url());const path=url.pathname.replace('/api/v1/collab','');
  let body={};
  if(path==='/config')body={client_id:'test',hosted_ui_domain:'test',public_url:'https://example.test',operator:{user_id:'owner',email:'owner@example.test'}};
  else if(path==='/rooms')body={rooms:[{room_id:'room-one',name:'Review',role:'owner'}]};
  else if(path==='/admin/users')body={users:[]};
  else if(path==='/rooms/room-one')body={room_id:'room-one',name:'Review',role:'owner',members:[]};
  else if(path==='/rooms/room-one/state'){
   m.reads++;await new Promise(r=>setTimeout(r,100));
   if(m.hang)await m.hang;
   if(m.fail>0){m.fail--;return route.fulfill({status:503,json:{detail:'busy'}});}
   const cursor=Number(url.searchParams.get('cursor')||0);
   body={changed:true,seq:1,next_cursor:1,agents:[{agent_id:'agent_1',name:'coder',harness_type:'pi',owner_user_id:'owner',actions:['execute']}],tasks:structuredClone(m.tasks),
    events:cursor<1?[{sequence:1,event_id:'e1',event_type:'message',created_at:T,room_id:'room-one',sender:{type:'user',id:'bob',owner_user_id:'bob'},text:'first'}]:[],flow:{nodes:[],edges:[],tasks:[]}};
  }
  await route.fulfill({json:body});
 });
 await page.goto(`${base}/__blank`);
 await page.evaluate(async()=>{
  document.documentElement.dataset.calm='off';
  await Promise.all(['/motion.css','/collab.css'].map(href=>new Promise(resolve=>{const link=document.createElement('link');link.rel='stylesheet';link.href=href;link.onload=resolve;document.head.append(link);})));
  document.body.innerHTML='<div id="host"></div>';
  const {mountCollab}=await import('/collab-view.js');window.collab=mountCollab(document.querySelector('#host'));
 });
 await page.locator('[data-room="room-one"]').click();
 await page.locator('#hcn-log .cx-m').first().waitFor();
 await page.locator('[data-sub="tasks"]').click();
 await until(()=>page.locator('#hcn-tasks li').count().then(n=>n===2),'task rows');
 await sleep(400);
 const watch=()=>page.evaluate(()=>{
  window.__muts=[];window.__obs?.disconnect();
  window.__obs=new MutationObserver(rs=>window.__muts.push(...rs));
  window.__obs.observe(document.getElementById('host'),{subtree:true,childList:true,attributes:true,characterData:true});
  window.__rows=[...document.querySelectorAll('#hcn-tasks li')];
 });
 const outside=sel=>page.evaluate(sel=>{const box=sel&&document.querySelector(sel);return window.__muts.filter(r=>!box||!box.contains(r.target)).map(r=>`${r.type}:${r.attributeName||''}:${(r.target.nodeType===1?r.target:r.target.parentElement)?.outerHTML.slice(0,80)}`);},sel);

 await watch();
 const before=m.reads;
 await sleep(1200);
 assert.ok(m.reads>before+2,'the room re-applied its state several times');
 assert.deepEqual(await outside(null),[],'identical room state performs no DOM writes');

 m.tasks[1].status='accepted';
 await until(()=>page.locator('#hcn-tasks li[data-key="t2"]').innerText().then(t=>/accepted/i.test(t)),'changed task');
 await sleep(300);
 assert.equal(await page.evaluate(()=>[...document.querySelectorAll('#hcn-tasks li')].every((n,i)=>n===window.__rows[i])),true,'task rows keep their nodes');
 assert.deepEqual(await outside('#hcn-tasks li[data-key="t2"]'),[],'nothing outside the changed task was written');

 // Live events must not abort the held long-poll: it already returns on change, and every
 // aborted waiter keeps a server slot until its deadline (MAX_STATE_WAITERS per user).
 let release;m.hang=new Promise(r=>{release=r;});
 await sleep(400);const held=m.reads;
 for(let i=0;i<5;i++)await page.evaluate(()=>window.collab.refresh());
 await sleep(500);
 assert.equal(m.reads,held,'live events leave the held long-poll alone');
 m.hang=null;release();
 // A loop sleeping in error backoff is woken by a live event.
 m.fail=3;
 await until(()=>m.fail===0,'three failed reads',15000);
 const failed=m.reads;await sleep(300);
 assert.equal(m.reads,failed,'the loop is sleeping in backoff');
 await page.evaluate(()=>window.collab.refresh());
 await until(()=>m.reads>failed,'a live event wakes the backoff sleep',1500);
 assert.deepEqual(await page.evaluate(()=>window.collab.interests),['SessionCreated','SessionStarted','SessionUpdated'],'collab only hears agent events');
 // A task row kept for a text selection after its task left is removed once the selection goes,
 // although the room state (and so the panel markup) no longer changes.
 await page.evaluate(()=>{const r=document.createRange();r.selectNodeContents(document.querySelector('#hcn-tasks li[data-key="t1"] .grow'));getSelection().removeAllRanges();getSelection().addRange(r);});
 m.tasks=m.tasks.filter(t=>t.task_id!=='t1');
 await sleep(600);
 assert.equal(await page.locator('#hcn-tasks li[data-key="t1"]').count(),1,'kept while selected');
 await page.evaluate(()=>getSelection().removeAllRanges());
 await until(()=>page.locator('#hcn-tasks li[data-key="t1"]').count().then(n=>n===0),'removed once the selection is released',3000);
 await page.evaluate(()=>window.collab.dispose());
}));
