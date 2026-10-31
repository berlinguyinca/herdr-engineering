// Router rule (Task 6): a same-route trigger (SSE event) refreshes the current
// view through the shared refresh bus and never replaces the page with a
// spinner; only a route change renders. A hidden tab pauses the bus.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';

const sleep=ms=>new Promise(r=>setTimeout(r,ms));

const events=Array.from({length:50},(_,i)=>({event_id:`evt-${i}`,event_type:i%2?'HostHeartbeat':'MissionStageChanged',sequence:i+1,
 source_timestamp:'2026-01-01T00:00:00Z',payload:{n:i}}));
const service={service_id:'svc-1',name:'web',host_id:'beast',status:'running',kind:'service',observed_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:00Z'};

// Stub API + a fake EventSource the test drives with window.__emit(event).
async function boot(page,base,hash){
 const reads={activity:0,services:0,analytics:0};
 await page.route('**/api/v1/**',route=>{
  const url=new URL(route.request().url());const p=url.pathname;
  if(p==='/api/v1/activity'){reads.activity++;return route.fulfill({json:{events}});}
  if(p==='/api/v1/services'){reads.services++;return route.fulfill({json:{services:[service]}});}
  if(p.startsWith('/api/v1/analytics/')){reads.analytics++;return route.fulfill({json:{total:1,completed:1,completion_rate:100,by_stage:{},models:{},total_sessions:0,healthy:1,unavailable:0}});}
  if(p==='/api/v1/missions')return route.fulfill({json:{missions:[]}});
  return route.fulfill({json:{}});
 });
 await page.addInitScript(()=>{
  const sources=[];
  window.EventSource=class{constructor(url){this.url=String(url);sources.push(this);}close(){}};
  window.__emit=e=>{for(const s of sources)if(s.url.includes('/events/stream'))s.onmessage?.({data:JSON.stringify(e)});};
  window.__setHidden=hidden=>{
   Object.defineProperty(document,'hidden',{configurable:true,get:()=>hidden});
   Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>hidden?'hidden':'visible'});
   document.dispatchEvent(new Event('visibilitychange'));
  };
 });
 await page.clock.install({time:new Date('2026-01-01T00:00:10Z')});
 await page.setViewportSize({width:900,height:500});
 await page.goto(`${base}/index.html#${hash}`);
 return reads;
}

async function until(fn,label,ms=5000){
 const end=Date.now()+ms;
 while(Date.now()<end){if(await fn())return;await sleep(25);}
 assert.fail(`timed out waiting for ${label}`);
}

// Records every loader (spinner or first-load skeleton) inserted under #view from now on.
const watchSpinners=page=>page.evaluate(()=>{
 window.__view=document.getElementById('view');window.__spinners=0;
 new MutationObserver(records=>{for(const r of records)for(const n of r.addedNodes)
  if(n.nodeType===1&&(n.matches('herdr-spinner,[data-skeleton]')||n.querySelector('herdr-spinner,[data-skeleton]')))window.__spinners++;})
  .observe(window.__view,{childList:true,subtree:true});
});

test('same-route SSE refresh keeps #view, scroll position and never shows a spinner',async()=>withPage(async(page,base)=>{
 const reads=await boot(page,base,'/activity');
 await until(()=>page.locator('.ac-row').count().then(n=>n===50),'activity rows');
 await until(async()=>reads.activity===1,'first activity read');
 await page.evaluate(()=>window.scrollTo(0,400));
 const scrolled=await page.evaluate(()=>window.scrollY);
 assert.ok(scrolled>0,'fixture must be scrollable');
 await watchSpinners(page);

 // The first event right after navigation waits out the throttle window.
 await page.evaluate(()=>window.__emit({event_id:'live-1',event_type:'HostHeartbeat'}));
 await page.clock.runFor(1000);await sleep(100);
 assert.equal(reads.activity,1,'no refresh inside the post-load quiet window');
 await page.clock.runFor(5000);
 await until(async()=>reads.activity===2,'one SSE-driven refresh');
 await until(()=>page.locator('.ac-row').count().then(n=>n===50),'rows after refresh');

 // A burst coalesces into a single refresh per throttle window.
 for(let i=0;i<20;i++)await page.evaluate(n=>window.__emit({event_id:`burst-${n}`,event_type:'HostHeartbeat'}),i);
 await page.clock.runFor(6000);
 await until(async()=>reads.activity===3,'burst refresh');
 await page.clock.runFor(6000);await sleep(100);
 assert.equal(reads.activity,3,'burst yields exactly one refresh');

 const after=await page.evaluate(()=>({same:document.getElementById('view')===window.__view,spinners:window.__spinners,y:window.scrollY,
  spinnerNow:!!document.querySelector('#view herdr-spinner, #view [data-skeleton]')}));
 assert.deepEqual(after,{same:true,spinners:0,y:scrolled,spinnerNow:false});
}));

test('a route change renders the new route',async()=>withPage(async(page,base)=>{
 const reads=await boot(page,base,'/activity');
 await until(()=>page.locator('.ac-row').count().then(n=>n===50),'activity rows');
 await page.evaluate(()=>{location.hash='#/analytics';});
 await until(()=>page.locator('#view h1').innerText().then(t=>t==='Analytics').catch(()=>false),'analytics page');
 assert.ok(reads.analytics>=3);
 assert.equal(await page.locator('.ac-row').count(),0);
}));

test('a hidden tab pauses refreshes; becoming visible refreshes once',async()=>withPage(async(page,base)=>{
 const reads=await boot(page,base,'/activity');
 await until(()=>page.locator('.ac-row').count().then(n=>n===50),'activity rows');
 await until(async()=>reads.activity===1,'first activity read');
 await page.evaluate(()=>window.__setHidden(true));
 for(let i=0;i<5;i++)await page.evaluate(n=>window.__emit({event_id:`hidden-${n}`,event_type:'HostHeartbeat'}),i);
 await page.clock.runFor(12000);await sleep(150);
 assert.equal(reads.activity,1,'no refresh while hidden');
 await watchSpinners(page);
 await page.evaluate(()=>window.__setHidden(false));
 await page.clock.runFor(100);
 await until(async()=>reads.activity===2,'refresh on becoming visible');
 await page.clock.runFor(6000);await sleep(100);
 assert.equal(reads.activity,2,'exactly one refresh on resume');
 assert.equal(await page.evaluate(()=>window.__spinners),0);
}));

test('Services refreshes in place through its own view on SSE',async()=>withPage(async(page,base)=>{
 const reads=await boot(page,base,'/services');
 await until(()=>page.locator('#sc-results').count().then(n=>n===1),'services page');
 const before=reads.services;
 await watchSpinners(page);
 await page.evaluate(()=>{window.__h1=document.querySelector('#view h1');});
 await page.evaluate(()=>window.__emit({event_id:'svc-live',event_type:'ServiceObserved'}));
 await page.clock.runFor(6000);
 await until(async()=>reads.services===before+1,'services refresh');
 assert.deepEqual(await page.evaluate(()=>({same:document.querySelector('#view h1')===window.__h1,spinners:window.__spinners})),{same:true,spinners:0});
}));

test('a malformed %-escape in the URL shows an error page instead of an uncaught rejection',async()=>withPage(async(page,base)=>{
 const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 await boot(page,base,'/missions/%E0%A4%A');
 await page.locator('#view herdr-error').waitFor();
 for(const hash of ['/llm/session/%ZZ','/services/%','/hosts/%E0']){
  await page.evaluate(h=>{location.hash=h;},hash);
  await page.locator('#view herdr-error').waitFor();
 }
 await sleep(200);
 assert.deepEqual(errors.filter(e=>/URI/.test(e)),[]);
}));
