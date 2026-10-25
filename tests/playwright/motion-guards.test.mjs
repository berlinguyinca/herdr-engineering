// Review fixes for the motion PR: only data-driven changes animate. A first load whose sources land
// one after another, and counts that change because the user filtered or searched, never flash.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,sleep,T0} from './helpers/live.mjs';

// Records every data-flash on the page from the very first paint.
async function recordFlashes(page){
 await page.addInitScript(()=>{
  try{localStorage.setItem('herdr.calm','off');}catch{}
  window.__flashes=[];
  new MutationObserver(rs=>{for(const r of rs)if(r.target.hasAttribute('data-flash'))window.__flashes.push(r.target.outerHTML.slice(0,90));})
   .observe(document,{subtree:true,attributes:true,attributeFilter:['data-flash']});
 });
}
const flashes=page=>page.evaluate(()=>window.__flashes);
const gate=()=>{let open;const p=new Promise(r=>{open=r;});return {wait:()=>p,open};};

const projection=n=>({contract_version:1,mission_key:`pi:M${n}`,host_id:'beast',observed_at:T0,snapshot_at:T0,activity_snapshot_at:T0,connection:'connected',
 mission:{id:`M${n}`,title:`Mission ${n}`,status:'EXECUTING'},freshness:{state:'live',age_seconds:1,stale_after_seconds:3600,disconnect_after_seconds:7200},
 activity:{health:'active',currentActivity:{summary:`Working ${n}`}}});
const recordedMission=n=>({mission_id:`R${n}`,mission_key:`R${n}`,title:`Recorded ${n}`,status:'running',updated_at:T0});

test('missions: counts settling during the first load and a filter switch do not flash',async()=>withPage(async(page,base)=>{
 const late=gate();
 await recordFlashes(page);
 await boot(page,base,'/missions',async p=>{
  if(p==='/api/v1/mission-activity')return {activities:[projection(1),projection(2),projection(3)]};
  if(p==='/api/v1/missions'){await late.wait();return {missions:[recordedMission(1),recordedMission(2)]};}
  return {};
 });
 await until(()=>page.locator('[data-tab="current"] span').textContent().then(t=>t==='3+'),'partial count 3+');
 late.open();
 await until(()=>page.locator('[data-tab="current"] span').textContent().then(t=>t==='5'),'settled count 5');
 await page.clock.runFor(5500);await sleep(50);
 await page.locator('[data-tab="finished"]').click();await page.locator('[data-tab="current"]').click();
 await page.locator('#mc-search').fill('Mission 1');
 await page.clock.runFor(5500);await sleep(50);
 assert.deepEqual(await flashes(page),[],'no flash on first load or on user filtering');
}));

test('topology: counts growing as sources land and changing with a search do not flash',async()=>withPage(async(page,base)=>{
 const late=gate();
 await recordFlashes(page);
 await boot(page,base,'/topology',async p=>{
  if(p==='/api/v1/sessions')return {sessions:[{session_id:'s1',agent_role:'One',status:'working',host_id:'beast',updated_at:T0},{session_id:'s2',agent_role:'Two',status:'working',host_id:'beast',updated_at:T0}]};
  if(p==='/api/v1/hosts')return {hosts:[{host_id:'beast',host_name:'beast',status:'healthy',last_seen:T0}]};
  if(p==='/api/v1/services'){await late.wait();return {services:[{service_id:'api',service_name:'API',status:'healthy',host_id:'beast',updated_at:T0,catalog:{scope:'herdr'}}]};}
  if(p==='/api/v1/missions')return {missions:[]};
  if(p==='/api/v1/mission-activity')return {activities:[]};
  if(p==='/api/v1/ci')return {available:true,pipelines:{}};
  return {};
 });
 await until(()=>page.locator('[data-tab-count="current"]').textContent().then(t=>t==='2'),'partial count');
 late.open();
 await until(()=>page.locator('[data-tab-count="current"]').textContent().then(t=>t==='3'),'settled count');
 await page.clock.runFor(5500);await sleep(50);
 await page.locator('[data-tx-search]').fill('One');
 await until(()=>page.locator('[data-tab-count="current"]').textContent().then(t=>t==='1'),'filtered count');
 await page.clock.runFor(5500);await sleep(50);
 assert.deepEqual(await flashes(page),[],'no flash on first load or on a search');
}));

test('topology: calm on writes final counts with no flash for a data change',async()=>withPage(async(page,base)=>{
 const s={sessions:[{session_id:'s1',agent_role:'One',status:'working',host_id:'beast',updated_at:T0}]};
 await page.addInitScript(()=>{localStorage.setItem('herdr.calm','on');window.__flashes=[];new MutationObserver(rs=>{for(const r of rs)if(r.target.hasAttribute('data-flash'))window.__flashes.push(1);}).observe(document,{subtree:true,attributes:true,attributeFilter:['data-flash']});});
 await boot(page,base,'/topology',p=>{
  if(p==='/api/v1/sessions')return {sessions:structuredClone(s.sessions)};
  if(p==='/api/v1/hosts')return {hosts:[]};
  if(p==='/api/v1/services')return {services:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  if(p==='/api/v1/mission-activity')return {activities:[]};
  if(p==='/api/v1/ci')return {available:true,pipelines:{}};
  return {};
 });
 await until(()=>page.locator('[data-tab-count="current"]').textContent().then(t=>t==='1'),'one record');
 s.sessions.push({session_id:'s2',agent_role:'Two',status:'working',host_id:'beast',updated_at:T0});
 await page.clock.runFor(5500);
 await until(()=>page.locator('[data-tab-count="current"]').textContent().then(t=>t==='2'),'two records');
 assert.deepEqual(await flashes(page),[]);
}));

const run=(n,state)=>({provider_repo_id:7,provider_url:'https://ci.example',repository:'org/app',run_number:n,state,branch:n%2?'main':'dev',created_at:T0,started_at:T0,finished_at:T0,updated_at:T0});

test('CI: counts settling when reports land and changing with a filter do not flash',async()=>withPage(async(page,base)=>{
 const late=gate();
 await recordFlashes(page);
 await boot(page,base,'/ci?tab=current',async p=>{
  if(p==='/api/v1/ci')return {available:true,status:'connected',pipelines:{'org/app':[run(1,'success'),run(2,'failure'),run(3,'running')]}};
  if(p==='/api/v1/ci/reports'){await late.wait();return {available:true,pipelines:[run(4,'failure'),run(5,'success')],reports:[]};}
  return {};
 });
 await page.locator('[data-ci-workspace="runs"]').click();
 await page.locator('.ig-row').first().waitFor();
 late.open();
 await page.clock.runFor(6000);await sleep(100);
 await page.locator('[data-ci-branch]').selectOption('dev').catch(()=>{});
 await page.locator('[data-ig-search], #ig-search, input[type=search]').first().fill('org').catch(()=>{});
 await page.clock.runFor(6000);await sleep(100);
 assert.deepEqual((await flashes(page)).filter(h=>!/ci-health-cell/.test(h)),[],'no flash on first load or on filtering');
}));

test('GitHub and Services: a first load does not flash',async()=>withPage(async(page,base)=>{
 await recordFlashes(page);
 const snap=extra=>({available:true,status:'ready',account:'operator',connection_generation:1,last_success_at:T0,pagination:{page:1,per_page:20,total:1,has_next:false},...extra});
 await boot(page,base,'/github',p=>{
  if(p==='/api/v1/github')return snap({repositories:[{repository_id:'github.com/lab/a',name_with_owner:'lab/a',open_issue_count:1}]});
  if(p==='/api/v1/github/issues')return snap({issues:[{github_id:'1',repository_id:'github.com/lab/a',number:1,title:'One',state:'OPEN',updated_at:T0}],counts:{all:1,autospec:0,preparation:0,in_progress:0,blocked:0}});
  if(p==='/api/v1/services')return {services:[{service_id:'api',service_name:'API',status:'healthy',updated_at:T0,catalog:{scope:'herdr'}}]};
  return {};
 });
 await page.locator('[data-gh-issue-row]').waitFor();
 await page.clock.runFor(5500);await sleep(50);
 await page.evaluate(()=>{location.hash='#/services';});
 await page.locator('[data-service-id]').waitFor();
 await page.clock.runFor(31000);await sleep(50);
 assert.deepEqual(await flashes(page),[]);
}));

// Positive control for the guards above: with the same recorder and calm off, a data-driven count
// change after the first load DOES flash. Without this, "no flash" could mean the recorder is blind.
test('control: a data-driven count change after load is recorded as a flash',async()=>withPage(async(page,base)=>{
 const s={sessions:[{session_id:'s1',agent_role:'One',status:'working',host_id:'beast',updated_at:T0}]};
 await recordFlashes(page);
 await boot(page,base,'/topology',p=>{
  if(p==='/api/v1/sessions')return {sessions:structuredClone(s.sessions)};
  if(p==='/api/v1/hosts')return {hosts:[]};
  if(p==='/api/v1/services')return {services:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  if(p==='/api/v1/mission-activity')return {activities:[]};
  if(p==='/api/v1/ci')return {available:true,pipelines:{}};
  return {};
 });
 await until(()=>page.locator('[data-tab-count="current"]').textContent().then(t=>t==='1'),'one record');
 await page.clock.runFor(5500);await sleep(50);
 assert.deepEqual(await flashes(page),[],'settled first load: nothing yet');
 s.sessions.push({session_id:'s2',agent_role:'Two',status:'working',host_id:'beast',updated_at:T0});
 await page.clock.runFor(5500);
 await until(()=>page.locator('[data-tab-count="current"]').textContent().then(t=>t==='2'),'two records');
 await page.clock.runFor(400);await sleep(50);
 assert.ok((await flashes(page)).length>0,'the recorder sees a real data-driven flash');
}));
