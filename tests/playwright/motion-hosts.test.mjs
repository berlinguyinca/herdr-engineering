// Task 10: motion on Hosts, Host detail and Overview. Changed values count up and flash,
// unchanged data does nothing, and calm mode (or OS reduced motion) writes the final value at once.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,observe,liveRefresh,sleep,T0} from './helpers/live.mjs';

const fresh='2026-01-01T00:00:05Z';
const calm=(page,mode)=>page.addInitScript(m=>{try{localStorage.setItem('herdr.calm',m);}catch{}},mode);

function state(){
 const host=(id,cpu,extra={})=>({host_id:id,host_name:id,status:'healthy',tailnet_ip:'100.64.0.1',last_seen:fresh,
  latest:{at:fresh,cpu,mem:40,disk:50,uptime_s:3600},problems:[],...extra});
 return {
  hosts:[host('beast',10),host('zapp',20),host('old',30,{last_seen:'2025-12-31T23:30:00Z',latest:{at:'2025-12-31T23:30:00Z',cpu:30,mem:40,disk:50}})],
  inventory:{hosts:[{host:'beast',collected_at:fresh},{host:'zapp',collected_at:fresh}],summary:{drifted:0,missing:0},
   components:[{name:'herdr',group:'tools',reference:'1.0',hosts:{beast:{version:'1.0',state:'ok'},zapp:{version:'1.0',state:'ok'}}}]},
  samples:[{at:'2026-01-01T00:00:00Z',cpu:10,mem:20,disk:30},{at:'2026-01-01T00:00:05Z',cpu:20,mem:30,disk:30}],
 };
}
function api(s){
 return p=>{
  if(p==='/api/v1/hosts')return {hosts:structuredClone(s.hosts)};
  if(p==='/api/v1/fleet/inventory')return structuredClone(s.inventory);
  const detail=p.match(/^\/api\/v1\/hosts\/([^/]+)$/);
  if(detail)return structuredClone(s.hosts.find(h=>h.host_id===decodeURIComponent(detail[1]))||{__status:404});
  if(/\/telemetry$/.test(p))return {samples:structuredClone(s.samples)};
  return {};
 };
}

// Records every text written to the elements matching sel, and whether data-flash ever appeared.
const record=(page,sel)=>page.evaluate(sel=>{
 window.__texts=[];window.__flashes=0;window.__flashed=[];
 window.__rec?.disconnect();
 window.__rec=new MutationObserver(rs=>{for(const r of rs){
  const el=r.target.nodeType===1?r.target:r.target.parentElement;
  if(r.type==='attributes'&&r.attributeName==='data-flash'&&el.hasAttribute('data-flash')){window.__flashes++;window.__flashed.push(el);}
  const v=el?.closest(sel);if(v&&r.type!=='attributes')window.__texts.push(v.textContent);
 }});
 window.__rec.observe(document.getElementById('view'),{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['data-flash']});
},sel);
const recorded=page=>page.evaluate(()=>({texts:[...new Set(window.__texts)],flashes:window.__flashes}));

test('host detail: a changed value counts up and flashes; calm writes it at once with no flash',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s=state();
  await calm(page,mode);
  await boot(page,base,'/hosts/beast',api(s));
  await until(()=>page.locator('[data-section=cpu] .hv-value').textContent().then(t=>t==='10%'),'cpu 10%');
  await record(page,'[data-section=cpu] .hv-value');
  s.hosts[0].latest.cpu=20;
  await liveRefresh(page);
  await page.clock.runFor(400);await sleep(30);
  const r=await recorded(page);
  assert.equal(await page.locator('[data-section=cpu] .hv-value').textContent(),'20%',`${mode}: final value`);
  if(mode==='off'){
   assert.ok(r.texts.some(t=>t!=='20%'),`calm off counts up through earlier values, saw ${r.texts}`);
   assert.ok(r.flashes>0,'calm off flashes the changed value');
  }else{
   assert.deepEqual(r.texts,['20%'],'calm on writes the final value only');
   assert.equal(r.flashes,0,'calm on never flashes');
  }
 });
});

test('host detail: identical data neither flashes nor redraws; the sparkline is drawn once',async()=>withPage(async(page,base)=>{
 const s=state();
 await calm(page,'off');
 await boot(page,base,'/hosts/beast',api(s));
 await page.locator('[data-series=cpu] path.hv-spark__line').waitFor();
 const line=page.locator('[data-series=cpu] path.hv-spark__line');
 assert.equal(await line.getAttribute('data-drawn'),'','first paint marks the line drawn');
 await page.evaluate(()=>{window.__line=document.querySelector('[data-series=cpu] path.hv-spark__line');});
 await record(page,'.hv-value');
 await observe(page);
 await liveRefresh(page);
 assert.deepEqual(await recorded(page),{texts:[],flashes:0},'identical refresh: no text, no flash');
 assert.equal(await page.evaluate(()=>window.__muts.length),0,'identical refresh performs no DOM writes');

 s.samples.push({at:'2026-01-01T00:00:08Z',cpu:60,mem:30,disk:30});
 await observe(page);
 await liveRefresh(page);
 const after=await page.evaluate(()=>({same:document.querySelector('[data-series=cpu] path.hv-spark__line')===window.__line,
  drawnWrites:window.__muts.filter(m=>m.type==='attributes'&&m.attributeName==='data-drawn').length,
  drawn:window.__line.getAttribute('data-drawn')}));
 assert.deepEqual(after,{same:true,drawnWrites:0,drawn:''},'a refresh never re-runs the draw-in');
}));

test('hosts: a changed meter flashes (not under calm); stale hosts read "no report for N min" and tick',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s=state();
  await calm(page,mode);
  await boot(page,base,'/hosts',api(s));
  await until(()=>page.locator('[data-host-card]').count().then(n=>n===3),'host cards');
  const old=page.locator('[data-host-card][data-host-id="old"]');
  assert.match(await old.innerText(),/no report for 30 min/);
  await record(page,'[data-host-id="beast"] [data-meter=cpu]');
  s.hosts[0].latest.cpu=55;
  await liveRefresh(page);
  const r=await recorded(page);
  if(mode==='off')assert.ok(r.flashes>0,'changed meter flashes');
  else assert.equal(r.flashes,0,'calm: no flash');
  // The flash stays inside the changed meter (it is removed again after ~600 ms).
  assert.equal(await page.evaluate(()=>window.__flashed.every(el=>el.closest('[data-host-id="beast"] [data-meter=cpu]'))),true);
  await page.clock.runFor(61000);await sleep(30);
  assert.match(await old.innerText(),/no report for 31 min/,'the stale label ticks without a refresh');
 });
});

test('hosts: the fleet matrix header is sticky and a cell that starts to differ flashes',async()=>withPage(async(page,base)=>{
 const s=state();
 await calm(page,'off');
 await boot(page,base,'/hosts',api(s));
 await page.locator('.hd-fleet td[data-host-id="zapp"]').waitFor();
 assert.equal(await page.locator('.hd-fleet thead th').first().evaluate(el=>getComputedStyle(el).position),'sticky');
 await record(page,'.hd-fleet');
 s.inventory.components[0].hosts.zapp={version:'2.0',state:'drifted'};s.inventory.summary.drifted=1;
 await liveRefresh(page);
 const flashed=await page.evaluate(()=>window.__flashed.map(el=>`${el.localName}:${el.getAttribute('data-host-id')}`));
 assert.deepEqual(flashed,['td:zapp'],'only the cell that started to differ flashes');
}));

function overviewApi(s){
 return p=>{
  if(p==='/api/v1/sessions')return {sessions:structuredClone(s.sessions)};
  if(p==='/api/v1/mission-activity')return {activities:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  if(p==='/api/v1/services')return {services:[]};
  if(p==='/api/v1/hosts')return {hosts:[]};
  if(p==='/api/v1/fleet/inventory')return {hosts:[],components:[],summary:{drifted:0,missing:0}};
  if(p==='/api/v1/activity')return {events:[]};
  if(p==='/api/v1/health')return {status:'ok'};
  if(p==='/api/v1/ci')return {available:true,pipelines:[]};
  if(p==='/api/v1/reviews')return {reviews:[]};
  return {};
 };
}
const agent=(id,status='running')=>({session_id:id,display_name:id,status,host_id:'beast',updated_at:T0,last_activity:T0});

test('overview: skeleton cards before data, no "Loading" text',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',()=>({__hang:true}));
 await page.locator('[data-overview=sessions] .hd-skeleton').first().waitFor();
 assert.doesNotMatch(await page.locator('#view').innerText(),/Loading [^,]*…/,'no "Loading …" placeholder text');
 assert.ok(await page.locator('[data-overview=sessions] [role=status][aria-label]').count()>0,'skeleton is announced');
}));

test('overview: metric tiles count up and flash; a new critical item pulses; calm does neither',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s={sessions:[agent('a'),agent('b')]};
  await calm(page,mode);
  await boot(page,base,'/overview',overviewApi(s));
  const tile=page.locator('[data-overview=sessions] .ov-metric strong');
  await until(()=>tile.textContent().then(t=>t==='2').catch(()=>false),'2 agents');
  await record(page,'[data-overview=sessions] .ov-metric strong');
  s.sessions.push(agent('c'),agent('d'),agent('e'),agent('f'),agent('g'),agent('h'),agent('i'),agent('j'));
  s.sessions.push(agent('broken','failed'));
  await page.clock.runFor(5500);
  await until(()=>page.locator('[data-attention-key="session:broken"]').count().then(n=>n===1),'critical item');
  const pulse=await page.locator('[data-attention-key="session:broken"] .ov-alert').getAttribute('data-pulse');
  await page.clock.runFor(400);await sleep(30);
  const r=await recorded(page);
  assert.equal(await tile.textContent(),'10');
  if(mode==='off'){
   assert.ok(r.texts.some(t=>t!=='2'&&t!=='10'),`counts up through intermediate values, saw ${r.texts}`);
   assert.ok(r.flashes>0,'tile flashes');
   assert.notEqual(pulse,null,'new critical item pulses');
  }else{
   assert.equal(r.flashes,0);assert.equal(pulse,null,'calm: no pulse');
   assert.ok(r.texts.every(t=>t==='10'),`calm: final value only, saw ${r.texts}`);
  }
 });
});

test('overview: critical items present on first load do not pulse',async()=>withPage(async(page,base)=>{
 await calm(page,'off');
 await boot(page,base,'/overview',overviewApi({sessions:[agent('broken','failed')]}));
 await page.locator('[data-attention-key="session:broken"]').waitFor();
 await sleep(100);
 assert.equal(await page.locator('[data-attention-key="session:broken"] .ov-alert').getAttribute('data-pulse'),null);
}));

test('sparkline: scaled 2× it is a solid line once drawn, with and without calm (no dash fragments)',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s=state();
  await calm(page,mode);
  await boot(page,base,'/hosts/beast',api(s));
  const line=page.locator('[data-series=cpu] path.hv-spark__line');
  await line.waitFor();
  const scale=await line.evaluate(el=>{el.ownerSVGElement.style.width='600px';return el.ownerSVGElement.getBoundingClientRect().width/el.ownerSVGElement.viewBox.baseVal.width;});
  assert.ok(scale>=2,`rendered at ${scale}× its viewBox`);
  await page.clock.runFor(1000);await sleep(30);
  const style=await line.evaluate(el=>({dash:getComputedStyle(el).strokeDasharray,animation:getComputedStyle(el).animationName}));
  assert.equal(style.dash,'none',`${mode}: the line is not dashed`);
 });
});

test('snapshotText during a running tween reads its target, so an identical redraw does not flash',async()=>withPage(async(page,base)=>{
 await page.goto(`${base}/__blank`);
 const r=await page.evaluate(async()=>{
  const m=await import('/motion.js');
  m.setCalm('off');
  document.body.innerHTML='<div id="box"><b>10</b></div>';
  const box=document.getElementById('box');
  let before=m.snapshotText(box,'b',()=>'k');
  box.innerHTML='<b>20</b>';
  m.animateChanges(box,'b',before,()=>'k');
  await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
  const mid=box.querySelector('b').textContent;
  box.querySelector('b').removeAttribute('data-flash');
  before=m.snapshotText(box,'b',()=>'k');// mid-tween: must read the target, not the in-between number
  box.innerHTML='<b>20</b>';
  const changed=m.animateChanges(box,'b',before,()=>'k');
  return {mid,snap:before.get('k'),changed:changed.length,flash:box.querySelector('b').hasAttribute('data-flash')};
 });
 assert.notEqual(r.mid,'20','the tween is still running');
 assert.deepEqual({snap:r.snap,changed:r.changed,flash:r.flash},{snap:'20',changed:0,flash:false});
}));
