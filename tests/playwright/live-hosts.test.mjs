// Task 7: Hosts and Host detail are mounted views. A live refresh patches only
// what changed, keeps open disclosures, and the range tabs swap the series in place.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,observe,mutationCount,liveRefresh,pollRefresh,sleep} from './helpers/live.mjs';

// Hosts never get a HostHeartbeat event in production (routine heartbeats are memory-only), so
// these tests refresh by the view's poll alone, with no SSE traffic.
const POLL=16000;

const fresh='2026-01-01T00:00:05Z';
function hostsState(){
 const host=(id,cpu,extra={})=>({host_id:id,host_name:id,status:'healthy',tailnet_ip:'100.64.0.1',last_seen:fresh,
  latest:{at:fresh,cpu,mem:40,disk:50,uptime_s:3600},problems:[],...extra});
 return {
  hosts:[host('beast',10),host('zapp',20),host('old',30,{last_seen:'2025-12-31T20:00:00Z',latest:{at:'2025-12-31T20:00:00Z',cpu:30,mem:40,disk:50}})],
  inventory:{hosts:[{host:'beast',collected_at:fresh},{host:'zapp',collected_at:fresh}],summary:{drifted:0,missing:0},
   components:[{name:'herdr',group:'tools',reference:'1.0',hosts:{beast:{version:'1.0',state:'ok'},zapp:{version:'1.0',state:'ok'}}}]},
 };
}

function api(state){
 return p=>{
  if(p==='/api/v1/hosts')return {hosts:structuredClone(state.hosts)};
  if(p==='/api/v1/fleet/inventory')return structuredClone(state.inventory);
  const detail=p.match(/^\/api\/v1\/hosts\/([^/]+)$/);
  if(detail)return structuredClone(state.hosts.find(h=>h.host_id===decodeURIComponent(detail[1]))||{__status:404});
  if(/\/telemetry$/.test(p))return {samples:state.samples||[]};
  if(p==='/api/v1/missions')return {missions:[]};
  return {};
 };
}

test('a live refresh with one changed host cpu mutates only that card\'s meter and value',async()=>withPage(async(page,base)=>{
 const state=hostsState();
 const reads=await boot(page,base,'/hosts',api(state));
 await until(()=>page.locator('[data-host-card]').count().then(n=>n===3),'host cards');
 await page.evaluate(()=>{window.__cards=[...document.querySelectorAll('[data-host-card]')];});

 // Identical data: zero DOM writes.
 await observe(page);
 await pollRefresh(page,POLL);
 assert.ok(reads['/api/v1/hosts']>=2,'refresh fetched hosts again');
 assert.equal(await mutationCount(page),0,'identical refresh performs no DOM writes');

 state.hosts[0].latest.cpu=55;
 await observe(page);
 await pollRefresh(page,POLL);
 const result=await page.evaluate(()=>{
  const beast=document.querySelector('[data-host-id="beast"][data-host-card]');
  const meter=beast.querySelector('[data-meter=cpu]');
  return {
   same:[...document.querySelectorAll('[data-host-card]')].every((c,i)=>c===window.__cards[i]),
   outside:window.__muts.filter(r=>!meter.contains(r.target)).map(r=>`${r.type}:${r.target.nodeName}`),
   count:window.__muts.length,
   now:meter.querySelector('[role=meter]').getAttribute('aria-valuenow'),
   text:meter.lastElementChild.textContent,
   spinners:window.__spinners,
  };
 });
 assert.equal(result.same,true,'every card node survives the refresh');
 assert.ok(result.count>0,'the changed meter was written');
 assert.deepEqual(result.outside,[],'nothing outside beast\'s cpu meter changed');
 assert.equal(result.now,'55');
 assert.equal(result.text,'55%');
 assert.equal(result.spinners,0);
}));

test('a stale host is marked data-stale and fleet cells carry data-host-id',async()=>withPage(async(page,base)=>{
 const state=hostsState();
 await boot(page,base,'/hosts',api(state));
 await until(()=>page.locator('[data-host-card]').count().then(n=>n===3),'host cards');
 assert.equal(await page.locator('[data-host-card][data-host-id="old"]').getAttribute('data-stale'),'');
 assert.equal(await page.locator('[data-host-card][data-host-id="beast"]').getAttribute('data-stale'),null);
 assert.equal(await page.locator('.hd-fleet td[data-host-id="zapp"]').count(),1);
}));

function detailState(){
 const state=hostsState();
 state.hosts[0].updates={manager:'apt',outdated:2,security:1,reboot_required:false,failed_units:[],packages:[{name:'curl',from:'1',to:'2'},{name:'git',from:'1',to:'2'}]};
 state.samples=[{at:'2026-01-01T00:00:00Z',cpu:10,mem:20,disk:30},{at:'2026-01-01T00:00:05Z',cpu:20,mem:30,disk:30}];
 return state;
}

test('host detail keeps an open outdated-packages disclosure across a live refresh',async()=>withPage(async(page,base)=>{
 const state=detailState();
 await boot(page,base,'/hosts/beast',api(state));
 await page.locator('[data-section=updates] details > summary').waitFor();
 await page.locator('[data-section=updates] details > summary').click();
 await page.evaluate(()=>{window.__details=document.querySelector('[data-section=updates] details');window.__h1=document.querySelector('#view h1');window.scrollTo(0,200);});
 const y=await page.evaluate(()=>window.scrollY);

 await observe(page);
 await pollRefresh(page,POLL);
 assert.equal(await mutationCount(page),0,'identical refresh performs no DOM writes');

 state.hosts[0].latest.cpu=77;state.hosts[0].updates.outdated=3;
 await pollRefresh(page,POLL);
 const after=await page.evaluate(()=>({
  same:document.querySelector('[data-section=updates] details')===window.__details,
  open:window.__details.open,h1:document.querySelector('#view h1')===window.__h1,
  cpu:document.querySelector('[data-section=cpu] .hv-value').textContent,
  outdated:document.querySelector('[data-field=outdated]').textContent,y:window.scrollY,
 }));
 assert.deepEqual(after,{same:true,open:true,h1:true,cpu:'77%',outdated:'3',y});
}));

test('switching range keeps the page and swaps the series without a spinner',async()=>withPage(async(page,base)=>{
 const state=detailState();
 const seen=[];
 const handler=api(state);
 await boot(page,base,'/hosts/beast',(p,url)=>{
  if(/\/telemetry$/.test(p)){const r=url.searchParams.get('range');seen.push(r);
   return {samples:r==='24h'?[{at:'2026-01-01T00:00:00Z',cpu:90,mem:20,disk:30},{at:'2026-01-01T00:00:05Z',cpu:5,mem:30,disk:30},{at:'2026-01-01T00:00:06Z',cpu:50,mem:30,disk:30}]:state.samples};}
  return handler(p,url);
 });
 await page.locator('[data-series=cpu] svg').waitFor();
 const before=await page.locator('[data-series=cpu] svg').getAttribute('aria-label');
 await page.evaluate(()=>{window.__h1=document.querySelector('#view h1');});
 await observe(page);
 await page.locator('.hv-range a',{hasText:'24h'}).click();
 await until(()=>page.locator('[data-series=cpu] svg').getAttribute('aria-label').then(l=>l!==before),'24h series');
 await sleep(50);
 const after=await page.evaluate(()=>({h1:document.querySelector('#view h1')===window.__h1,spinners:window.__spinners,
  hash:location.hash,current:document.querySelector('.hv-range a[aria-current=true]')?.textContent}));
 assert.deepEqual(after,{h1:true,spinners:0,hash:'#/hosts/beast?range=24h',current:'24h'});
 assert.deepEqual(seen,['1h','24h']);

 // A later live refresh keeps the selected range.
 await pollRefresh(page,POLL);
 assert.equal(seen.at(-1),'24h');
}));

test('a quiet fleet with no events still turns a silent host stale (poll, no SSE traffic)',async()=>withPage(async(page,base)=>{
 const state=hostsState();
 const reads=await boot(page,base,'/hosts',api(state));
 await until(()=>page.locator('[data-host-card]').count().then(n=>n===3),'host cards');
 assert.equal(await page.locator('[data-host-card][data-host-id="beast"]').getAttribute('data-stale'),null);
 // beast stops reporting: the server keeps answering with its last report and emits nothing.
 await page.clock.fastForward(16*60*1000);
 await pollRefresh(page,POLL);
 assert.equal(await page.locator('[data-host-card][data-host-id="beast"]').getAttribute('data-stale'),'','silent host marked stale');
 assert.ok(reads['/api/v1/hosts']>=2);
}));

test('any live event refreshes the Hosts page, not only host event types',async()=>withPage(async(page,base)=>{
 const state=hostsState();
 await boot(page,base,'/hosts',api(state));
 await until(()=>page.locator('[data-host-card]').count().then(n=>n===3),'host cards');
 state.hosts[1].latest.cpu=66;
 await liveRefresh(page,{event_id:'x1',event_type:'MissionUpdated'});
 assert.equal(await page.locator('[data-host-id="zapp"][data-host-card] [data-meter=cpu] [role=meter]').getAttribute('aria-valuenow'),'66');
}));

test('host detail refreshes on its own poll with no SSE traffic',async()=>withPage(async(page,base)=>{
 const state=detailState();
 await boot(page,base,'/hosts/beast',api(state));
 await page.locator('[data-section=cpu] .hv-value').waitFor();
 state.hosts[0].latest.cpu=42;
 await pollRefresh(page,POLL);
 assert.equal(await page.locator('[data-section=cpu] .hv-value').textContent(),'42%');
}));
