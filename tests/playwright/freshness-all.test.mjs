// Task 13: every routed page shows one <herdr-freshness> chip. A failed poll turns it red with the
// error; a healthy page touches it fresh; a multi-source page stays red while any source fails.
// GitHub's chip also carries the sync snapshot (status, stale, partial, deferred_repos).
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {appRoutes} from './helpers/routes.mjs';
import {boot,until,sleep} from './helpers/live.mjs';

// Every route the router knows (app.js ROUTES), so none is missed.
const ROUTES=appRoutes();

const service={service_id:'svc-1',name:'web',host_id:'h-1',status:'running',kind:'service',observed_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'};
// One body that satisfies every view's response validator (lists empty, objects well-formed).
const OK={items:[],groups:[],snapshots:[],analyses:[],artifacts:[],versions:[],publications:[],nodes:[],edges:[],missions:[],sessions:[],services:[service],hosts:[],activities:[],events:[],timeline:[],available:true,reviews:[],
 repositories:[],issues:[],counts:{},pagination:{},status:'ready',components:[],summary:{},reports:[],pipelines:[],samples:[],
 total:0,completed:0,completion_rate:0,by_stage:{},models:{},total_sessions:0,healthy:0,unavailable:0,
 mission_id:'m-1',session_id:'s-1',host_id:'h-1',title:'Mission',display_name:'Agent'};

const chips=page=>page.evaluate(()=>[...document.querySelectorAll('#view herdr-freshness')].map(c=>({state:c.dataset.state,text:c.textContent,last:c._last})));

test('every route shows exactly one chip that turns red when the API fails',async()=>{
 for(const hash of ROUTES)await withPage(async(page,base)=>{
  await boot(page,base,hash,()=>({__status:500,json:{error:'backend down'}}));
  await until(async()=>(await chips(page)).some(c=>c.state==='error'),`red chip on ${hash}`,8000);
  await sleep(100);
  const all=await chips(page);
  assert.equal(all.length,1,`${hash}: one chip, got ${all.length}`);
  assert.equal(all[0].state,'error',hash);
  assert.match(all[0].text,/^error · /,hash);
 });
});

test('every route touches its chip fresh when the API answers',async()=>{
 for(const hash of ROUTES)await withPage(async(page,base)=>{
  await boot(page,base,hash,p=>decodeURIComponent(p)==='/api/v1/missions/pi:msn-1'?{__status:404,json:{error:'not found'}}:OK);
  await until(async()=>(await chips(page)).some(c=>c.last!=null),`fresh chip on ${hash}`,8000);
  const all=await chips(page);
  assert.equal(all.length,1,`${hash}: one chip, got ${all.length}`);
  assert.notEqual(all[0].state,'error',`${hash}: ${all[0].text}`);
  assert.match(all[0].text,/updated \d+s ago/,hash);
 });
});

test('a multi-source page stays red while one source fails, and recovers when it answers',async()=>{
 for(const [hash,failing] of [['/overview','/api/v1/hosts'],['/topology','/api/v1/hosts'],['/ci','/api/v1/ci'],['/herd','/api/v1/missions'],['/missions','/api/v1/missions']])
  await withPage(async(page,base)=>{
   let broken=true;
   await boot(page,base,hash,p=>p===failing&&broken?{__status:500,json:{error:'one source down'}}:OK);
   await until(async()=>(await chips(page))[0]?.state==='error',`red chip on ${hash}`,8000);
   // Healthy sources keep answering on their own polls; the failing one must keep the chip red.
   for(let i=0;i<3;i++){await page.clock.runFor(6000);await sleep(80);assert.equal((await chips(page))[0].state,'error',`${hash} stays red`);}
   assert.match((await chips(page))[0].text,/one source down/,hash);
   broken=false;
   await page.clock.runFor(31000);
   await until(async()=>(await chips(page))[0]?.state==='fresh',`${hash} recovers`,8000);
  });
});

test('a Pi mission whose live activity fails turns red',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/missions/pi%3Amsn-1',p=>decodeURIComponent(p)==='/api/v1/missions/pi:msn-1'?{__status:404,json:{error:'not found'}}:p.includes('activity')?{__status:500,json:{error:'telemetry down'}}:OK);
 await until(async()=>(await chips(page))[0]?.state==='error','red chip',8000);
 assert.equal((await chips(page)).length,1);
 assert.match((await chips(page))[0].text,/telemetry down/);
}));

test('the GitHub chip shows sync status, stale, partial and deferred repositories',async()=>withPage(async(page,base)=>{
 let snapshot={status:'partial',stale:true,partial:true,deferred_repos:['acme/a','acme/b']};
 await boot(page,base,'/github',p=>p==='/api/v1/github'?{...OK,...snapshot}:p.startsWith('/api/v1/github')?{...OK}:OK);
 await until(async()=>/deferred/.test((await chips(page))[0]?.text||''),'github detail',8000);
 let [chip]=await chips(page);
 assert.match(chip.text,/partial/);assert.match(chip.text,/stale/);assert.match(chip.text,/2 repositories deferred/);
 assert.equal(chip.state,'stale','a sync problem never reads as plain live');
 assert.match(chip.text,/^stale · updated \d+s ago · /,'the amber chip says stale, not live');
 snapshot={status:'ready',stale:false,partial:false,deferred_repos:3};
 await page.clock.runFor(6000);
 await until(async()=>/3 repositories deferred/.test((await chips(page))[0]?.text||''),'numeric deferred',8000);
 snapshot={status:'ready',stale:false,partial:false};
 await page.clock.runFor(6000);
 await until(async()=>(await chips(page))[0]?.text.startsWith('live · ')&&!/·.*·/.test((await chips(page))[0].text),'plain live',8000);
 [chip]=await chips(page);
 assert.equal(chip.state,'fresh');
}));
