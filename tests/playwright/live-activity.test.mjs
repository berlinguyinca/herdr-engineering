// Task 8: Activity is a mounted view. New events prepend in place, selection is
// kept by event_id, the active filter persists, and a user scrolled away from the
// top sees an "N new" pill instead of a jumping list.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,observe,liveRefresh,sleep} from './helpers/live.mjs';

const ev=(i,type='HostHeartbeat')=>({event_id:`ev-${i}`,event_type:type,sequence:i,source_timestamp:`2026-01-01T00:00:0${i%10}Z`,payload:{n:i}});

function api(state){
 return p=>{
  if(p==='/api/v1/activity')return {events:structuredClone(state.events)};
  if(p==='/api/v1/missions')return {missions:[]};
  return {};
 };
}

test('a new event prepends without recreating rows and the selection follows its event_id',async()=>withPage(async(page,base)=>{
 const state={events:[ev(3,'MissionStageChanged'),ev(2),ev(1,'MissionStageChanged')]};
 await boot(page,base,'/activity',api(state));
 await until(()=>page.locator('.ac-row').count().then(n=>n===3),'rows');
 await page.locator('.ac-row[data-event-id="ev-2"] > summary').click();
 await until(()=>page.locator('[data-activity-selected]').getAttribute('data-event-id').then(v=>v==='ev-2'),'ev-2 selected');
 await page.evaluate(()=>{window.__rows=[...document.querySelectorAll('.ac-row')];});

 await observe(page);
 await liveRefresh(page);
 assert.equal(await page.evaluate(()=>window.__muts.length),0,'identical refresh writes nothing');

 await page.evaluate(()=>{window.__entering=[];new MutationObserver(rs=>{for(const r of rs)for(const n of r.addedNodes)if(n.nodeType===1&&n.matches('.ac-row'))window.__entering.push([n.dataset.eventId,n.hasAttribute('data-entering')]);})
  .observe(document.querySelector('.ac-list'),{childList:true});});
 state.events.unshift(ev(4));
 await liveRefresh(page);
 const r=await page.evaluate(()=>{const rows=[...document.querySelectorAll('.ac-row')];return {
  ids:rows.map(x=>x.dataset.eventId),kept:window.__rows.every(x=>rows.includes(x)),entering:window.__entering,
  selected:document.querySelector('[data-activity-selected]').dataset.eventId,
  open:document.querySelector('.ac-row[data-event-id="ev-2"]').open,
  count:document.querySelector('.ac-stream-head span').textContent};});
 assert.deepEqual(r.ids,['ev-4','ev-3','ev-2','ev-1']);
 assert.equal(r.kept,true,'existing row nodes are reused');
 assert.deepEqual(r.entering,[['ev-4',true]]);
 assert.equal(r.selected,'ev-2','selection stays on the same event after rows shift');
 assert.equal(r.open,true);
 assert.equal(r.count,'4 events');
}));

test('the active filter persists across a refresh and applies to new rows',async()=>withPage(async(page,base)=>{
 const state={events:[ev(3,'MissionStageChanged'),ev(2),ev(1,'MissionStageChanged')]};
 await boot(page,base,'/activity',api(state));
 await until(()=>page.locator('.ac-row').count().then(n=>n===3),'rows');
 await page.locator('[data-event-filter="MissionStageChanged"]').click();
 assert.equal(await page.locator('.ac-row:visible').count(),2);
 state.events.unshift(ev(5,'MissionStageChanged'),ev(4));
 await liveRefresh(page);
 const r=await page.evaluate(()=>({
  visible:[...document.querySelectorAll('.ac-row')].filter(x=>!x.hidden).map(x=>x.dataset.eventId),
  pressed:[...document.querySelectorAll('[data-event-filter]')].filter(b=>b.getAttribute('aria-pressed')==='true').map(b=>b.dataset.eventFilter),
  status:document.querySelector('.ac-filter-status').textContent,
  count:document.querySelector('[data-event-filter="MissionStageChanged"] strong').textContent}));
 assert.deepEqual(r,{visible:['ev-5','ev-3','ev-1'],pressed:['MissionStageChanged'],status:'Showing 3 MissionStageChanged events',count:'3'});
}));

test('a user scrolled away from the top sees "N new" and the list does not jump',async()=>withPage(async(page,base)=>{
 const state={events:Array.from({length:40},(_,i)=>ev(100-i))};
 await boot(page,base,'/activity',api(state));
 await until(()=>page.locator('.ac-row').count().then(n=>n===40),'rows');
 await page.evaluate(()=>window.scrollTo(0,900));
 const y=await page.evaluate(()=>window.scrollY);
 assert.ok(y>0);
 await page.evaluate(()=>{window.__first=document.querySelector('.ac-row');});
 state.events.unshift(ev(102),ev(101));
 await liveRefresh(page);
 const r=await page.evaluate(()=>({y:window.scrollY,first:document.querySelector('.ac-row')===window.__first,rows:document.querySelectorAll('.ac-row').length,
  pill:document.querySelector('[data-activity-new]')?.hidden===false?document.querySelector('[data-activity-new]').textContent.trim():null}));
 assert.deepEqual(r,{y,first:true,rows:40,pill:'2 new'});

 await page.locator('[data-activity-new]').click();
 await until(()=>page.evaluate(()=>window.scrollY===0),'scrolled to top');
 await sleep(50);
 const after=await page.evaluate(()=>({ids:[...document.querySelectorAll('.ac-row')].slice(0,3).map(x=>x.dataset.eventId),hidden:document.querySelector('[data-activity-new]').hidden}));
 assert.deepEqual(after,{ids:['ev-102','ev-101','ev-100'],hidden:true});
}));

test('rows are keyed per principal and events without an id keep a stable key across prepends',async()=>withPage(async(page,base)=>{
 const shared=(actor,n)=>({...ev(n),event_id:'same-id',actor:{id:actor,kind:'agent'},payload:{actor}});
 const noId={event_type:'HostHeartbeat',sequence:7,source_timestamp:'2026-01-01T00:00:07Z',payload:{anon:true}};
 const state={events:[noId,shared('alice',2),shared('bob',1)]};
 await boot(page,base,'/activity',api(state));
 await until(()=>page.locator('.ac-row').count().then(n=>n===3),'three rows, including two principals sharing an event_id');
 await page.evaluate(()=>{window.__rows=[...document.querySelectorAll('.ac-row')];});
 state.events.unshift(ev(9));
 await liveRefresh(page);
 const r=await page.evaluate(()=>{const rows=[...document.querySelectorAll('.ac-row')];return {n:rows.length,kept:window.__rows.every(x=>rows.includes(x)),
  first:rows[0].dataset.eventId,second:rows[1]===window.__rows[0]};});
 assert.deepEqual(r,{n:4,kept:true,first:'ev-9',second:true});

 // Scrolled away: a new id-less event counts toward the pill.
 state.events=[...Array.from({length:40},(_,i)=>ev(200-i)),...state.events];
 await liveRefresh(page);
 await page.evaluate(()=>window.scrollTo(0,900));
 state.events.unshift({...noId,sequence:8,source_timestamp:'2026-01-01T00:00:08Z',payload:{anon:2}});
 await liveRefresh(page);
 assert.equal(await page.locator('[data-activity-new]').textContent(),'1 new');
}));
