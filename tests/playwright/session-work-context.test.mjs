// WC-004: Work Context on the session page, driven through the real app over a stub API.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,sleep} from './helpers/live.mjs';

const shots=path.resolve(import.meta.dirname,'../../docs/specs/herdr-work-command-center-wc001/screenshots');
const snap='2026-01-01T00:00:08Z';
const projection=(id,title,o={})=>({mission_key:`pi:${id}`,activity_snapshot_at:snap,host_id:'beast',mission:{id,title,status:'EXECUTING'},freshness:{state:'live',age_seconds:2},activity:{health:'active'},...o});
const sess=(id,o={})=>({session_id:id,host_id:'beast',status:'running',updated_at:snap,...o});
const SESSIONS=[
 sess('s-1',{display_name:'Parser worker',mission_key:'pi:MSN-1',capabilities:{attach_terminal:true}}),
 sess('s-2',{display_name:'<img src=x onerror="window.__xss=1">',mission_key:'pi:MSN-1'}),
 sess('s-solo',{display_name:'Solo agent'}),
 sess('s-stale',{display_name:'Stale ref',mission_key:'pi:GONE'}),
];
const API={
 '/api/v1/mission-activity':{activities:[projection('MSN-1','Repair parser',{activity:{health:'active',acceptanceCoverage:{completed:1,total:4}}})]},
 '/api/v1/missions':{missions:[]},
 '/api/v1/sessions':{sessions:SESSIONS},
 '/api/v1/sessions/s-1/terminal':{recommended:'local',commands:[{form:'local',label:'Local',command:'herdr attach s-1'}]},
};
const handler=p=>{
 const m=/^\/api\/v1\/sessions\/([^/]+)$/.exec(p);
 if(m){const s=SESSIONS.find(x=>x.session_id===m[1]);return s??{__status:404,json:{error:'not found'}};}
 return API[p]??{services:[],hosts:[],activities:[],events:[],items:[],reviews:[],available:true,components:[],summary:{},status:'ok',repositories:[],pipelines:[]};
};
const ctx='[data-work-context]';

test('overview row -> session shows Work Context with the parent mission link -> mission route',async()=>withPage(async(page,base)=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await boot(page,base,'/overview?keep=1',handler);
 await page.locator('[data-overview="week"] .wk-row').first().waitFor({timeout:8000});
 await page.goto(`${base}/index.html#/herd/session/s-1`);
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 const link=page.locator(`${ctx} [data-mission-link]`);
 assert.equal(await link.innerText(),'Repair parser');
 assert.equal(await page.locator(`${ctx} progress`).count(),1);
 assert.match(await page.locator(`${ctx} progress`).getAttribute('aria-label'),/1 \/ 4/);
 const sib=page.locator(`${ctx} [data-sibling-link]`);
 assert.equal(await sib.count(),1,'self excluded, one sibling');
 assert.equal(await page.evaluate(()=>window.__xss),undefined);
 assert.equal(await page.locator(`${ctx} img`).count(),0);
 await page.setViewportSize({width:1200,height:900});
 await page.locator(ctx).screenshot({path:path.join(shots,'wc004-session-context-desktop.png')});
 await page.setViewportSize({width:390,height:844});await sleep(100);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
 await page.locator(ctx).screenshot({path:path.join(shots,'wc004-session-context-mobile.png')});
 await link.click();
 await until(async()=>(await page.evaluate(()=>location.hash)).startsWith('#/missions/pi%3AMSN-1'),'mission route');
 assert.deepEqual(errors,[]);
}));

test('overview row opens the exact session, and browser back restores the overview address with its query',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview?keep=1',handler);
 await page.locator('[data-overview="week"] .wk-row').first().waitFor({timeout:8000});
 await page.locator('[data-overview="week"] a',{hasText:'Solo agent'}).click();
 await until(async()=>(await page.evaluate(()=>location.hash))==='#/herd/session/s-solo','session route');
 await page.goBack();
 await until(async()=>(await page.evaluate(()=>location.hash))==='#/overview?keep=1','overview address restored');
 await page.locator('[data-overview="week"] .wk-row').first().waitFor({timeout:8000});
}));

test('"← Agents" returns to the agents list on the tab for the session outcome',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/herd/session/s-1',handler);
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 assert.equal(await page.locator('a',{hasText:'← Agents'}).getAttribute('href'),'#/herd?tab=current');
}));

test('unlinked and stale-reference sessions show the explicit unlinked state, no mission link',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/herd/session/s-solo',handler);
 await page.locator(`${ctx}[data-state="unlinked"]`).waitFor({timeout:8000});
 assert.match(await page.locator(ctx).innerText(),/Not linked to a mission/);
 assert.equal(await page.locator(`${ctx} [data-mission-link]`).count(),0);
 await page.goto(`${base}/index.html#/herd/session/s-stale`);
 await page.locator(`${ctx}[data-state="unlinked"]`).waitFor({timeout:8000});
 assert.match(await page.locator(ctx).innerText(),/could not be confirmed/);
}));

test('an unknown session id gives the not-found page, not a Work Context',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/herd/session/nope',handler);
 await until(async()=>(await page.locator('#view').innerText()).length>0&&await page.locator(ctx).count()===0&&/not found|could not load/i.test(await page.locator('#view').innerText()),'not found');
}));

test('terminal attach stays a separate control that only offers a copyable command',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/herd/session/s-1',handler);
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 assert.equal(await page.locator(`${ctx} [data-terminal]`).count(),0,'attach is not inside Work Context');
 assert.equal(await page.locator('#session-terminal [data-terminal]').count(),1);
 await page.locator('#session-terminal [data-terminal]').click();
 await page.locator('#session-terminal [data-copy]').waitFor({timeout:5000});
 assert.match(await page.locator('#session-terminal').innerText(),/herdr attach s-1/);
 assert.match(await page.locator('#session-terminal').innerText(),/Run one of these in a terminal on your machine/);
}));
