// Task 16: calm mode and OS reduced motion. On every route (read from app.js's router), through a
// first load and live updates that change the data, nothing moves:
//  - no element is ever marked data-entering / data-leaving / data-flash (observed from first paint);
//  - document.getAnimations() is empty when sampled after each load and each refresh;
//  - a changed value is written at once (no count-up still running after the refresh).
// A separate test samples while every route's first-load skeleton is on screen: its shimmer must
// not run. Control runs with calm off prove each check can see the motion it forbids.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,sleep,T0} from './helpers/live.mjs';
import {appRoutes} from './helpers/routes.mjs';

const ROUTES=appRoutes();

// One body per round that every view accepts; each round adds records and moves the numbers, so
// views have rows to enter and values to count up and flash.
const payload=n=>{
 const ids=k=>Array.from({length:k},(_,i)=>i+1);
 const session=i=>({session_id:`s-${i}`,display_name:`Agent ${i}`,agent_role:`Role ${i}`,host_id:'h-1',status:'running',cwd:'/work/app',updated_at:T0,last_activity:T0});
 const service=i=>({service_id:`svc-${i}`,service_name:`Service ${i}`,name:`svc-${i}`,host_id:'h-1',status:'healthy',kind:'service',updated_at:T0,observed_at:T0,catalog:{scope:'herdr'}});
 const host=i=>({host_id:`h-${i}`,host_name:`h-${i}`,status:'healthy',last_seen:T0,updated_at:T0});
 const mission=i=>({mission_id:`m-${i}`,title:`Mission ${i}`,status:'EXECUTING',updated_at:T0});
 const event=i=>({event_id:`e${i}`,sequence:i,event_type:'MissionStageChanged',entity_type:'mission',entity_id:'m-1',source_timestamp:T0,payload:{title:'Mission'}});
 const k=n+1;
 return {missions:ids(k).map(mission),sessions:ids(k).map(session),services:ids(k).map(service),hosts:ids(k).map(host),activities:[],
  events:ids(k*2).map(event),timeline:[],available:true,reviews:[],items:[],groups:[],points:[],snapshots:[],analyses:[],repositories:[],issues:[],counts:{},pagination:{},status:'ready',
  components:[],summary:{},reports:[],pipelines:[],samples:[],total:k*10,completed:k*3,completion_rate:k*15,by_stage:{review:k,done:k*2},
  models:{},total_sessions:k,healthy:k,unavailable:n,mission_id:'m-1',session_id:'s-1',host_id:'h-1',host_name:'h-1',title:'Mission 1',
  display_name:'Agent 1',updated_at:T0,created_at:T0,stage:'review',sessions:[]};
};

// Records every motion marker from the first paint: set on an existing node or present on an added one.
const RECORD=()=>{
 window.__motion=[];
 const marked=n=>n.nodeType===1&&(n.matches('[data-entering],[data-leaving],[data-flash]')||n.querySelector('[data-entering],[data-leaving],[data-flash]'));
 new MutationObserver(rs=>{for(const r of rs){
  if(r.type==='attributes'&&r.target.hasAttribute(r.attributeName))window.__motion.push(`${location.hash} ${r.attributeName} ${r.target.outerHTML.slice(0,80)}`);
  for(const n of r.addedNodes)if(marked(n))window.__motion.push(`${location.hash} added ${n.outerHTML.slice(0,80)}`);
 }}).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['data-entering','data-leaving','data-flash']});
};

const animations=page=>page.evaluate(()=>document.getAnimations().map(a=>`${location.hash} ${a.animationName||a.transitionProperty||a.constructor.name} on ${a.effect?.target?.tagName||'?'}.${a.effect?.target?.className||''}`));
// The view's text without its clocks (relative ages tick as the fake clock runs).
const viewText=page=>page.evaluate(()=>{
 const view=document.getElementById('view').cloneNode(true);
 for(const el of view.querySelectorAll('herdr-freshness,herdr-mission-activity,time'))el.remove();
 return view.innerText.replace(/\d+(\.\d+)?\s?(ms|s|m|h|d|sec|min|hr)\b/g,'#');
});

// Visits every route by hash, lets it load, then pushes two rounds of changed data through live
// events and polls. Returns the motion markers and running animations seen, and the routes whose
// text was still changing after a refresh had been applied.
async function tour(page,base,init){
 let round=0;
 await page.addInitScript(RECORD);
 await init(page);
 await boot(page,base,ROUTES[0],p=>decodeURIComponent(p)==='/api/v1/missions/pi:msn-1'?{__status:404,json:{error:'not found'}}:payload(round));
 const seen={motion:[],animations:[],settling:[]};
 for(const hash of ROUTES){
  round=0;
  await page.evaluate(h=>{location.hash=h;},hash);
  await until(()=>page.evaluate(()=>!document.querySelector('#view [data-skeleton]')&&document.getElementById('view').children.length>0),`${hash} loaded`,8000);
  await page.clock.runFor(1000);await sleep(80);
  seen.animations.push(...await animations(page));
  for(const n of [1,2]){
   round=n;
   await page.evaluate(i=>window.__emit({event_id:`live-${i}-${Math.random()}`,event_type:'MissionStageChanged'}),n);
   // Past the bus throttle (event-driven views), then past the slowest poll (30 s, services). After
   // each, the text must already be final: a count-up would still be moving under the fake clock.
   for(const wait of [5100,31000]){
    await page.clock.runFor(wait);await sleep(250);
    const now=await viewText(page);
    seen.animations.push(...await animations(page));
    await page.clock.runFor(1500);await sleep(80);
    if(await viewText(page)!==now)seen.settling.push(hash);
    seen.animations.push(...await animations(page));
   }
  }
 }
 seen.motion=await page.evaluate(()=>window.__motion);
 return seen;
}

const calmOn=page=>page.addInitScript(()=>{try{localStorage.setItem('herdr.calm','on');}catch{}});
const reducedMotion=async page=>{await page.emulateMedia({reducedMotion:'reduce'});};

test('calm on: no route moves through first load and live updates',async()=>withPage(async(page,base)=>{
 const seen=await tour(page,base,calmOn);
 assert.deepEqual(seen.motion,[],'no motion markers');
 assert.deepEqual(seen.animations,[],'no running animations');
 assert.deepEqual(seen.settling,[],'values are final as soon as a refresh lands');
}));

test('OS reduced motion (calm auto): no route moves through first load and live updates',async()=>withPage(async(page,base)=>{
 const seen=await tour(page,base,reducedMotion);
 assert.equal(await page.evaluate(()=>document.documentElement.dataset.calm),undefined,'calm left on auto');
 assert.deepEqual(seen.motion,[],'no motion markers');
 assert.deepEqual(seen.animations,[],'no running animations');
 assert.deepEqual(seen.settling,[],'values are final as soon as a refresh lands');
}));

// Control: the same tour with calm off must see motion, or the two guards above prove nothing.
test('control: with calm off the same tour sees motion',async()=>withPage(async(page,base)=>{
 const seen=await tour(page,base,p=>p.addInitScript(()=>{try{localStorage.setItem('herdr.calm','off');}catch{}}));
 assert.ok(seen.motion.length>0,'calm off marks entering/flashing elements');
 assert.ok(seen.animations.length>0,'calm off runs animations');
 assert.ok(seen.settling.length>0,'calm off counts values up after a refresh lands');
}));

// While a skeleton is on screen (the API held back), its shimmer must not run under calm or OS
// reduced motion. Calm off is the control: there the shimmer runs.
async function skeletonMotion(init){
 const seen={running:[],names:new Set(),sampled:0};
 await withPage(async(page,base)=>{
  await init(page);
  await boot(page,base,ROUTES[0],()=>({__hang:true}));
  for(const hash of ROUTES){
   await page.evaluate(h=>{location.hash=h;},hash);
   await until(()=>page.evaluate(()=>!!document.querySelector('#view [data-skeleton]')),`${hash} skeleton`,8000);
   await sleep(60);
   const r=await page.evaluate(()=>({anims:document.getAnimations().map(a=>a.animationName||a.transitionProperty||'?'),
    names:[...document.querySelectorAll('#view .hd-skeleton')].map(e=>getComputedStyle(e).animationName)}));
   seen.sampled++;
   for(const a of r.anims)seen.running.push(`${hash} ${a}`);
   for(const n of r.names)seen.names.add(n);
  }
 });
 return seen;
}

test('calm on and OS reduced motion: a visible skeleton does not shimmer',async()=>{
 for(const init of [calmOn,reducedMotion]){
  const seen=await skeletonMotion(init);
  assert.equal(seen.sampled,ROUTES.length);
  assert.deepEqual(seen.running,[],'no animation while the skeleton shows');
  assert.deepEqual([...seen.names],['none'],'the skeleton rows have no animation');
 }
});

test('control: with calm off a visible skeleton shimmers',async()=>{
 const seen=await skeletonMotion(p=>p.addInitScript(()=>{try{localStorage.setItem('herdr.calm','off');}catch{}}));
 assert.ok(seen.running.some(r=>/hd-skeleton/.test(r)),'the shimmer runs');
 assert.ok(seen.names.has('hd-skeleton'));
});
