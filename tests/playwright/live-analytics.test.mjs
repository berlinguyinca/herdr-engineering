// Task 9: Analytics pages and non-Pi mission detail refresh in place. Identical
// data writes nothing, a changed counter writes only its node, stage bars are a
// share of the total, and mission detail keeps open disclosures without a spinner.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,observe,mutationCount,liveRefresh,sleep} from './helpers/live.mjs';

function analyticsState(){
 return {
  missions:{total:6,completed:2,completion_rate:33,by_stage:{implementation:1,review:3,done:2}},
  models:{models:{'gpt-x':{sessions:3,messages:10},'qwen':{sessions:1,messages:2}},total_sessions:4},
  hosts:{total:3,healthy:2,unavailable:1},
  tokens:{total:{requests:10,input_tokens:1000,output_tokens:500},by_project:{herdr:{requests:10,input_tokens:1000,output_tokens:500,reasoning_tokens:0,failed:0}},by_model:{},by_day:{}},
 };
}
const api=state=>p=>{
 const m=p.match(/^\/api\/v1\/analytics\/(\w+)$/);
 if(m)return structuredClone(state[m[1]]);
 if(p==='/api/v1/missions')return {missions:[]};
 return {};
};

test('every analytics page refreshes with zero DOM writes when the payload is identical',async()=>withPage(async(page,base)=>{
 const state=analyticsState();
 await boot(page,base,'/analytics',api(state));
 for(const [hash,title] of [['/analytics','Analytics'],['/analytics/tokens','Token usage'],['/analytics/models','Model analytics'],['/analytics/hosts','Host analytics'],['/analytics/missions','Analytics']]){
  await page.evaluate(h=>{if(location.hash!=='#'+h)location.hash=h;},hash);
  await until(()=>page.locator('#view h1').first().innerText().then(t=>t===title).catch(()=>false),`${hash} page`);
  await until(()=>page.locator('#view herdr-freshness').count().then(n=>n===1),`${hash} chip`);
  await sleep(50);
  await observe(page);
  await liveRefresh(page);
  assert.equal(await mutationCount(page),0,`${hash}: identical refresh writes nothing`);
  assert.equal(await page.evaluate(()=>window.__spinners),0);
 }
}));

test('one changed counter mutates only its node; stage bars are a share of the total',async()=>withPage(async(page,base)=>{
 const state=analyticsState();
 await boot(page,base,'/analytics',api(state));
 await page.locator('.an-stage .hd-stage-bar').first().waitFor();
 const widths=await page.locator('.an-stage .hd-stage-bar > *').evaluateAll(bars=>bars.map(b=>parseFloat(b.style.width)));
 assert.equal(widths.length,3);
 const expected=[1/6*100,3/6*100,2/6*100];
 widths.forEach((w,i)=>assert.ok(Math.abs(w-expected[i])<0.2,`bar ${i}: ${w} vs ${expected[i]}`));
 assert.ok(Math.abs(widths.reduce((a,b)=>a+b,0)-100)<0.5,'bars sum to 100%');

 await sleep(50);
 state.missions.completed=3;
 await observe(page);
 await liveRefresh(page);
 const r=await page.evaluate(()=>{const field=document.querySelector('[data-field="missions.completed"]');return {
  text:field.textContent,count:window.__muts.length,outside:window.__muts.filter(m=>!field.contains(m.target)).map(m=>`${m.type}:${m.target.nodeName}`)};});
 assert.equal(r.text,'3');
 assert.ok(r.count>0);
 assert.deepEqual(r.outside,[]);
}));

test('non-Pi mission detail refreshes without a spinner and keeps open disclosures',async()=>withPage(async(page,base)=>{
 const mission={mission_id:'m1',title:'Ship it',status:'EXECUTING',stage:'implementation',created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z',source:'herdr',sessions:[]};
 const reads=await boot(page,base,'/missions/m1',p=>{
  if(p==='/api/v1/missions/m1')return structuredClone(mission);
  if(p==='/api/v1/missions/m1/timeline')return {timeline:[{at:'2026-01-01T00:00:00Z',stage:'implementation'}]};
  if(p==='/api/v1/events')return {events:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  return {};
 });
 await page.locator('[data-history-panel=brief] > summary').waitFor();
 await page.locator('[data-history-panel=brief] > summary').click();
 await page.evaluate(()=>{window.__brief=document.querySelector('[data-history-panel=brief]');window.__h1=document.querySelector('#view h1');});
 await sleep(100);
 await observe(page);
 const refreshed=async()=>{const n=reads['/api/v1/missions/m1'];await page.evaluate(()=>window.__emit({event_id:`x-${Math.random()}`,event_type:'MissionStageChanged'}));
  await page.clock.runFor(6000);await until(async()=>reads['/api/v1/missions/m1']>n,'mission refetch');await sleep(150);};
 await refreshed();
 assert.equal(await mutationCount(page),0,'identical refresh writes nothing');
 mission.stage='review';
 await refreshed();
 const r=await page.evaluate(()=>({brief:document.querySelector('[data-history-panel=brief]')===window.__brief,open:window.__brief.open,
  h1:document.querySelector('#view h1')===window.__h1,spinners:window.__spinners,sub:document.querySelector('#mission-history .hd-card__sub').textContent}));
 assert.deepEqual({brief:r.brief,open:r.open,h1:r.h1,spinners:r.spinners},{brief:true,open:true,h1:true,spinners:0});
 assert.match(r.sub,/review/);
}));
