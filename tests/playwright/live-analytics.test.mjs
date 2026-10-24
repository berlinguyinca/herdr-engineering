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
 await page.clock.runFor(400);// the changed counter counts up (about 300 ms) to its new value; the
 // count-up swaps the field's text node, so the replaced (now detached) node's record is the field's own write
 const r=await page.evaluate(()=>{const field=document.querySelector('[data-field="missions.completed"]');return {
  text:field.textContent,count:window.__muts.length,outside:window.__muts.filter(m=>!field.contains(m.target)&&m.target.isConnected).map(m=>`${m.type}:${m.target.nodeName}`)};});
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

test('a not-found mission page recovers on a live event once the mission exists, even after a hung retry',async()=>withPage(async(page,base)=>{
 let phase='missing';
 const mission={mission_id:'m2',title:'Late mission',status:'EXECUTING',stage:'implementation',created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z',source:'herdr',sessions:[]};
 const reads=await boot(page,base,'/missions/m2',p=>{
  if(p==='/api/v1/missions/m2')return phase==='missing'?{__raw:''}:phase==='hang'?{__hang:true}:structuredClone(mission);
  if(p==='/api/v1/missions/m2/timeline')return {timeline:[]};
  if(p==='/api/v1/events')return {events:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  return {};
 });
 await page.locator('herdr-error[message="Mission not found"]').waitFor();
 const emit=()=>page.evaluate(()=>window.__emit({event_id:`x-${Math.random()}`,event_type:'MissionStageChanged'}));
 // A retry whose request never settles must not wedge later retries.
 phase='hang';
 const before=reads['/api/v1/missions/m2'];
 await emit();await page.clock.runFor(6000);
 await until(async()=>reads['/api/v1/missions/m2']>before,'hung retry issued');
 phase='found';
 await page.clock.runFor(30000);
 await emit();await page.clock.runFor(6000);
 await until(()=>page.locator('#view h1').innerText().then(t=>t==='Late mission').catch(()=>false),'mission rendered without reload',8000);
}));

// Task 14: a changed cell flashes (data-flash) on a live refresh; unchanged cells and the first paint never do.
const watchFlash=page=>page.evaluate(()=>{
 window.__flashed=[];window.__fw?.disconnect();
 window.__fw=new MutationObserver(rs=>{for(const r of rs)if(r.target.hasAttribute?.('data-flash'))window.__flashed.push(r.target.closest('tr')?.dataset.key+'|'+r.target.cellIndex+'|'+(r.target.dataset.field||''));});
 window.__fw.observe(document.getElementById('view'),{subtree:true,attributes:true,attributeFilter:['data-flash']});
});

test('analytics: a changed table cell flashes, unchanged cells and the first paint do not; calm never flashes',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const state=analyticsState();
  state.tokens.by_project.web={requests:2,input_tokens:20,output_tokens:10,reasoning_tokens:0,failed:0};
  await page.addInitScript(m=>{localStorage.setItem('herdr.calm',m);},mode);
  await page.addInitScript(()=>{window.__early=[];new MutationObserver(rs=>{for(const r of rs)if(r.target.hasAttribute?.('data-flash'))window.__early.push(r.target.nodeName);}).observe(document,{subtree:true,attributes:true,attributeFilter:['data-flash']});});
  await boot(page,base,'/analytics/tokens',api(state));
  await page.locator('tr[data-key="herdr"]').first().waitFor();
  await sleep(100);
  assert.deepEqual(await page.evaluate(()=>window.__early),[],'first paint does not flash');
  await watchFlash(page);
  state.tokens.by_project.herdr.failed=4;
  await liveRefresh(page);
  const flashed=await page.evaluate(()=>[...new Set(window.__flashed)]);
  if(mode==='off')assert.deepEqual(flashed,['herdr|5|'],'only the changed cell flashes');
  else assert.deepEqual(flashed,[],'calm: no flash');
  await page.clock.runFor(400);
  assert.equal(await page.locator('tr[data-key="herdr"] td').nth(5).textContent(),'4');
 });
});

test('analytics dashboard: a changed counter flashes and counts up; a metric tile flashes',async()=>withPage(async(page,base)=>{
 const state=analyticsState();
 await page.addInitScript(()=>{localStorage.setItem('herdr.calm','off');});
 await boot(page,base,'/analytics',api(state));
 await page.locator('[data-field="missions.total"]').waitFor();
 await sleep(50);
 await watchFlash(page);
 state.missions.total=16;
 await liveRefresh(page);
 assert.deepEqual(await page.evaluate(()=>[...new Set(window.__flashed)]),['undefined|undefined|missions.total']);
 await page.clock.runFor(400);
 assert.equal(await page.locator('[data-field="missions.total"]').textContent(),'16');
 await page.evaluate(()=>{location.hash='/analytics/hosts';});
 await page.locator('herdr-metric[label="Healthy"] .hd-metric__value').waitFor();
 await sleep(50);
 await watchFlash(page);
 state.hosts.healthy=3;
 await liveRefresh(page);
 const metric=await page.evaluate(()=>window.__flashed.length);
 assert.ok(metric>=1,'the changed metric tile flashes');
}));
