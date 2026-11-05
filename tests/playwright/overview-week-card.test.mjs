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
