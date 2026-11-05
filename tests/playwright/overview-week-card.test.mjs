// WC-003: the "This week" card on #/overview, driven through the real app over a stub API.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {withPage} from './helpers/fixture.mjs';
import {withFlag} from './helpers/work-center.mjs';
import {boot,until,sleep,T0} from './helpers/live.mjs';

const shots=path.resolve(import.meta.dirname,'../../docs/specs/herdr-work-command-center-wc001/screenshots');
const snap='2026-01-01T00:00:08Z';
const projection=(id,title,o={})=>({mission_key:`pi:${id}`,activity_snapshot_at:snap,host_id:'beast',mission:{id,title,status:'EXECUTING'},freshness:{state:'live',age_seconds:2},activity:{health:'active'},...o});
const API={
 '/api/v1/mission-activity':{activities:[
  projection('MSN-1','Repair parser',{activity:{health:'active',acceptanceCoverage:{completed:1,total:4}}}),
  projection('MSN-2','Stuck deploy',{activity:{health:'blocked'}}),
  projection('MSN-3','<img src=x onerror="window.__xss=1">hostile',{})]},
 '/api/v1/missions':{missions:[{mission_id:'MSN-9',title:'Archived survey',created_at:'2025-12-30T10:00:00Z',updated_at:'2025-12-31T10:00:00Z'}]},
 '/api/v1/sessions':{sessions:[{session_id:'s-1',host_id:'beast',status:'running',updated_at:snap,mission_key:'pi:MSN-1'},{session_id:'s-solo',host_id:'beast',status:'running',updated_at:snap,display_name:'Solo agent'}]},
};
const rawHandler=p=>API[p]??{services:[],hosts:[],activities:[],events:[],items:[],reviews:[],available:true,components:[],summary:{},status:'ok',repositories:[],pipelines:[]};
const card='[data-overview="week"]';

const handler=withFlag(rawHandler);
test('overview shows the This week card with honest coverage and measured progress only',async()=>withPage(async(page,base)=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await boot(page,base,'/overview',handler);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 const text=await page.locator(card).innerText();
 assert.match(text,/This week/);
 assert.match(text,/Partial data/);
 assert.match(text,/snapshot/);
 assert.equal(await page.locator(`${card} progress`).count(),1,'only the mission with acceptance evidence has a progress bar');
 assert.match(await page.locator(`${card} progress`).getAttribute('aria-label'),/Repair parser/);
 assert.equal(await page.evaluate(()=>window.__xss),undefined,'hostile title did not execute');
 assert.equal(await page.locator(`${card} img`).count(),0);
 // Below Attention, and existing cards still render.
 const order=await page.evaluate(()=>[...document.querySelectorAll('[data-overview]')].map(e=>e.dataset.overview));
 assert.equal(order[0],'attention');assert.equal(order[1],'week');assert.ok(order.includes('sessions')&&order.includes('missions'));
 assert.deepEqual(errors,[]);
}));

test('a row opens the existing mission and session routes in one click, by keyboard too',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',handler);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await page.locator(`${card} a`,{hasText:'Repair parser'}).click();
 await until(async()=>(await page.evaluate(()=>location.hash)).startsWith('#/missions/pi%3AMSN-1'),'mission route');
 await page.goBack();
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 const link=page.locator(`${card} a`,{hasText:'Solo agent'});
 await link.focus();
 assert.equal(await link.evaluate(el=>el===document.activeElement),true,'row link is keyboard focusable');
 await page.keyboard.press('Enter');
 await until(async()=>(await page.evaluate(()=>location.hash)).startsWith('#/herd/session/s-solo'),'session route');
}));

test('an unchanged refresh keeps the card and its focus, and a failing source shows a stale note',async()=>withPage(async(page,base)=>{
 let broken=false;
 await boot(page,base,'/overview',p=>p==='/api/v1/sessions'&&broken?{__status:500,json:{error:'down'}}:handler(p));
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 const link=page.locator(`${card} a`,{hasText:'Repair parser'});
 await link.focus();
 await page.clock.runFor(6000);await sleep(100);
 assert.equal(await link.evaluate(el=>el===document.activeElement),true,'focus survives refresh');
 broken=true;await page.clock.runFor(31000);
 await until(async()=>/Could not refresh agents/.test(await page.locator(card).innerText()),'stale note');
 assert.ok(await page.locator(`${card} .wk-row`).count()>0,'last data stays visible');
}));

test('desktop and mobile layouts: no horizontal scroll, screenshots saved',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',handler);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await page.setViewportSize({width:1200,height:900});
 await page.locator(card).screenshot({path:path.join(shots,'wc003-overview-week-desktop.png')});
 await page.setViewportSize({width:390,height:844});
 await sleep(100);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'no horizontal scroll on mobile');
 await page.locator(card).screenshot({path:path.join(shots,'wc003-overview-week-mobile.png')});
}));

// ---- G1: server weekly history (stub API) ----
const ev=(kind,id,at,o={})=>({kind,work_id:`mission:${id}`,source:'mission',entity_id:id,occurred_at:at,evidence:{ref:`e-${kind}-${id}`,event_type:'X'},...o});
const WEEKLY='/api/v1/work/weekly-events';
const weeklyBody=(coverage={})=>({window:{interval:'[since,until)'},next_cursor:null,events:[
 ev('started','pi:MSN-1','2025-12-31T12:00:00Z',{mission_key:'pi:MSN-1'}),
 ev('started','pi:MSN-2','2025-12-30T12:00:00Z',{mission_key:'pi:MSN-2'}),
 ev('started','pi:GONE','2025-12-30T13:00:00Z',{mission_key:'pi:GONE'}),ev('finished','pi:GONE','2025-12-31T13:00:00Z',{mission_key:'pi:GONE'}),
 ev('started','pi:LAST-MONTH','2025-11-30T13:00:00Z')],
 coverage:{complete:false,horizon:'2025-12-01T12:00:00Z',truncated:false,reasons:['start events are written when the control plane first sees the work'],...coverage}});

test('G1: one weekly-events read per load feeds Started/Finished; partial shows the history horizon',async()=>withPage(async(page,base)=>{
 const calls=[];
 const h=withFlag((p,url)=>{if(p===WEEKLY){calls.push(url.searchParams.toString());return weeklyBody();}return rawHandler(p);});
 await boot(page,base,'/overview',h);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await until(async()=>/history from/.test(await page.locator(card).innerText()),'history badge');
 const started=page.locator(`${card} .wk-metric`,{hasText:'Started'});
 assert.match(await started.innerText(),/^4\s/);                                   // MSN-1, MSN-2, GONE from the server + MSN-9 from the snapshot; the last-month start is outside the window
 assert.match(await started.innerText(),/at least 4 \(partial\) · history from 2025-12-01/);
 assert.match(await page.locator(`${card} .wk-metric`,{hasText:'Finished'}).innerText(),/^1\s/);
 assert.match(await page.locator(`${card} .wk-banner`).innerText(),/stored start and finish events since 2025-12-01/);
 await page.clock.runFor(95000);await sleep(100);                                    // three poll cycles
 assert.equal(calls.length,1,'one read per overview load, no polling');
 const q=new URLSearchParams(calls[0]);
 assert.match(q.get('since'),/Z$/);assert.match(q.get('until'),/Z$/);assert.equal(q.get('limit'),'500');
 assert.equal(Date.parse(q.get('until'))-Date.parse(q.get('since')),7*86400e3,'a 7-day (DST-free) half-open window');
 await page.setViewportSize({width:1200,height:900});
 await page.locator(card).screenshot({path:path.join(shots,'wc-g1-overview-week-partial-desktop.png')});
 await page.setViewportSize({width:390,height:844});await sleep(100);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
 await page.locator(card).screenshot({path:path.join(shots,'wc-g1-overview-week-partial-mobile.png')});
}));

test('G1: complete is shown only when the server says complete; a failed read keeps the snapshot-only partial card',async()=>withPage(async(page,base)=>{
 const h=withFlag((p)=>p===WEEKLY?weeklyBody({complete:true}):rawHandler(p));
 await boot(page,base,'/overview',h);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await until(async()=>/complete/.test(await page.locator(`${card} .wk-metric`,{hasText:'Started'}).innerText()),'complete badge');
 assert.match(await page.locator(`${card} .wk-metric`,{hasText:'Finished'}).innerText(),/complete/);
 assert.doesNotMatch(await page.locator(card).innerText(),/Started and Finished only count|stored start and finish events/);
 await page.locator(card).screenshot({path:path.join(shots,'wc-g1-overview-week-server-complete.png')});
}));
test('G1: failed weekly read falls back to the snapshot and says so',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',withFlag((p)=>p===WEEKLY?{__status:500,json:{error:'down'}}:rawHandler(p)));
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await until(async()=>/weekly history could not be read/i.test(await page.locator(card).innerText()),'fallback note');
 assert.doesNotMatch(await page.locator(card).innerText(),/history from/);
 assert.match(await page.locator(`${card} .wk-metric`,{hasText:'Started'}).getAttribute('aria-label'),/partial|unknown/);
}));

// ---- G4: time zone and week start controls ----
const weekQuery=[];
const g4Handler=withFlag((p,url)=>{if(p===WEEKLY){weekQuery.push(url.searchParams.get('since'));return {events:[],coverage:{complete:false,horizon:'2025-12-01T00:00:00Z',truncated:false,reasons:['x']}};}return rawHandler(p);});
test('G4: zone and week start controls persist, re-render the card, re-read the window and survive reload',async()=>withPage(async(page,base)=>{
 weekQuery.length=0;
 await boot(page,base,'/overview',g4Handler);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 assert.match(await page.locator(card).innerText(),/week starts Monday/i,'default unchanged');
 const before=weekQuery.length;
 await page.locator(`${card} [data-week-zone]`).selectOption('Pacific/Auckland');
 await page.locator(`${card} [data-week-start]`).selectOption('sunday');
 await until(async()=>/Pacific\/Auckland/.test(await page.locator(`${card} .ov-note`).last().innerText()),'zone shown');
 assert.match(await page.locator(card).innerText(),/week starts Sunday/i);
 assert.ok(weekQuery.length>before,'weekly window re-read after a change');
 assert.equal(await page.evaluate(()=>JSON.parse(localStorage.getItem('herdr.overview.week.v1')).timeZone),'Pacific/Auckland');
 await page.reload();
 await page.locator(`${card} [data-week-zone]`).waitFor({timeout:8000});
 assert.equal(await page.locator(`${card} [data-week-zone]`).inputValue(),'Pacific/Auckland');
 assert.equal(await page.locator(`${card} [data-week-start]`).inputValue(),'sunday');
}));
test('G4: an invalid stored zone is ignored and a blocked localStorage still renders the card',async()=>withPage(async(page,base)=>{
 await page.addInitScript(()=>{localStorage.setItem('herdr.overview.week.v1',JSON.stringify({timeZone:'Mars/Base',weekStart:'friday'}));});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await boot(page,base,'/overview',g4Handler);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 assert.match(await page.locator(card).innerText(),/week starts Monday/i);
 assert.notEqual(await page.locator(`${card} [data-week-zone]`).inputValue(),'Mars/Base');
 // Storage that throws on every call: choice still applies for this page.
 await page.evaluate(()=>{Storage.prototype.setItem=()=>{throw new Error('full');};});
 await page.locator(`${card} [data-week-start]`).selectOption('saturday');
 await until(async()=>/week starts Saturday/i.test(await page.locator(card).innerText()),'saturday applied without storage');
 assert.deepEqual(errors,[]);
}));
