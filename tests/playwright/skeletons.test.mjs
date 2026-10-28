// Task 15: every route's first load shows a skeleton ([data-skeleton]) in place of "Loading…" text,
// and when the data arrives the page does not jump: the page head stays where it was and the page
// height changes by no more than a fixed tolerance (small fixed payload; viewport 1200x700). The
// height is compared within the first screen (clamped to the viewport): growth below the fold
// moves nothing the user is looking at.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {appRoutes} from './helpers/routes.mjs';
import {boot,until,sleep,T0} from './helpers/live.mjs';

// Every route the router knows (app.js ROUTES), so none is missed.
const ROUTES=appRoutes();

// The head may move by at most HEAD_PX; the page height may change by at most HEIGHT_PX when the
// skeleton gives way to the (small, fixed) data.
const HEAD_PX=4,HEIGHT_PX=130;

const service=id=>({service_id:id,service_name:`Service ${id}`,name:id,host_id:'h-1',status:'healthy',kind:'service',updated_at:T0,observed_at:T0,catalog:{scope:'herdr'}});
const session=id=>({session_id:id,display_name:`Agent ${id}`,host_id:'h-1',status:'running',cwd:'/work/app',updated_at:T0,last_activity:T0});
const host=id=>({host_id:id,host_name:id,status:'healthy',last_seen:T0,updated_at:T0});
const event=n=>({event_id:`e${n}`,sequence:n,event_type:'MissionStageChanged',entity_type:'mission',entity_id:'m-1',source_timestamp:T0,payload:{title:'Mission'}});
const PAYLOAD={items:[],groups:[],snapshots:[],analyses:[],artifacts:[],versions:[],publications:[],nodes:[],edges:[],missions:[{mission_id:'m-1',title:'Mission one',status:'EXECUTING',updated_at:T0}],sessions:[session('s-1'),session('s-2')],
 services:[service('svc-1'),service('svc-2')],hosts:[host('h-1'),host('h-2')],activities:[],events:[event(1),event(2)],timeline:[],
 available:true,reviews:[],repositories:[],issues:[],counts:{},pagination:{},status:'ready',components:[],summary:{},reports:[],pipelines:[],samples:[],
 total:2,completed:1,completion_rate:50,by_stage:{review:1,done:1},models:{'qwen-27b':{sessions:2,messages:40},'flint':{sessions:1,messages:12},'codex':{sessions:1,messages:7}},total_sessions:2,healthy:2,unavailable:0,
 mission_id:'m-1',session_id:'s-1',host_id:'h-1',host_name:'h-1',title:'Mission one',display_name:'Agent one',updated_at:T0,created_at:T0,stage:'review',sessions:[]};

const measure=page=>page.evaluate(()=>{
 const view=document.getElementById('view');
 const head=view.querySelector('.hd-live-head');
 const rect=view.getBoundingClientRect();
 return {head:head?Math.round(head.getBoundingClientRect().top):null,height:Math.round(Math.min(rect.height,innerHeight-rect.top)),
  skeletons:view.querySelectorAll('[data-skeleton]').length,loadingText:/Loading[^\n]*…/.test(view.innerText),
  // a11y: a loading region is aria-busy, and a skeleton never puts a status region inside another one
  notBusy:[...view.querySelectorAll('[data-skeleton]')].filter(e=>!e.closest('[aria-busy="true"]')).map(e=>e.getAttribute('aria-label')||e.parentElement.outerHTML.slice(0,60)),
  nested:[...view.querySelectorAll('[role="status"] [data-skeleton][role="status"]')].map(e=>e.getAttribute('aria-label'))};
});

test('each route shows a skeleton on first load and does not jump when data arrives',async()=>{
 const report=[];
 for(const hash of ROUTES)await withPage(async(page,base)=>{
  let open;const gate=new Promise(r=>{open=r;});
  await boot(page,base,hash,async p=>{await gate;return decodeURIComponent(p)==='/api/v1/missions/pi:msn-1'?{__status:404,json:{error:'not found'}}:PAYLOAD;});
  await until(async()=>(await measure(page)).skeletons>0,`skeleton on ${hash}`,8000);
  await sleep(100);
  const before=await measure(page);
  assert.equal(before.loadingText,false,`${hash}: no "Loading…" text beside the skeleton`);
  assert.deepEqual(before.notBusy,[],`${hash}: every skeleton sits in an aria-busy region`);
  assert.deepEqual(before.nested,[],`${hash}: no status region nested in another`);
  open();
  await until(async()=>(await measure(page)).skeletons===0,`${hash}: skeleton replaced`,8000);
  await sleep(150);
  const after=await measure(page);
  assert.equal(await page.evaluate(()=>document.querySelector('#view [aria-busy="true"]')||document.getElementById('view').getAttribute('aria-busy')==='true'?'busy':''),'',`${hash}: nothing stays aria-busy once loaded`);
  report.push(`${hash} head ${before.head}->${after.head} height ${before.height}->${after.height}`);
  assert.ok(before.head!=null&&after.head!=null,`${hash}: page head present before and after`);
  assert.ok(Math.abs(after.head-before.head)<=HEAD_PX,`${hash}: head moved ${before.head}->${after.head}`);
  assert.ok(Math.abs(after.height-before.height)<=HEIGHT_PX,`${hash}: height ${before.height}->${after.height}`);
 });
 console.log(report.join('\n'));
});

test('a live refresh never shows a skeleton again',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/hosts',()=>PAYLOAD);
 await until(()=>page.locator('#view [data-host-id]').count().then(n=>n>0),'hosts');
 await page.evaluate(()=>{window.__sk=0;new MutationObserver(rs=>{for(const r of rs)for(const n of r.addedNodes)if(n.nodeType===1&&(n.matches('[data-skeleton]')||n.querySelector('[data-skeleton]')))window.__sk++;}).observe(document.getElementById('view'),{subtree:true,childList:true});});
 await page.evaluate(()=>window.__emit({event_id:'x1',event_type:'HostHeartbeat'}));
 await page.clock.runFor(6000);await sleep(200);
 assert.equal(await page.evaluate(()=>window.__sk),0);
}));

// A skeleton means "loading". Once the first load has failed (the chip is red), no skeleton may stay
// beside the error: the region shows its error text instead.
test('a failed first load leaves no skeleton on any route',async()=>{
 const stuck=[];
 for(const hash of ROUTES)await withPage(async(page,base)=>{
  await boot(page,base,hash,()=>({__status:500,json:{error:'backend down'}}));
  await until(()=>page.evaluate(()=>document.querySelector('#view herdr-freshness')?.dataset.state==='error'),`${hash}: red chip`,8000);
  await page.clock.runFor(1000);await sleep(300);
  const left=await page.evaluate(()=>[...document.querySelectorAll('#view [data-skeleton]')].map(e=>e.getAttribute('aria-label')));
  if(left.length)stuck.push(`${hash}: ${left.join(', ')}`);
 });
 assert.deepEqual(stuck,[]);
});
