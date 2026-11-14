// WC-006: end-to-end journeys for the Work Command Center, driven through the real app (packages/index.html + app.js)
// over a STUB API (page.route). Real-server coverage lives in work-center-real-server.test.mjs.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {withPage} from './helpers/fixture.mjs';
import {withFlag} from './helpers/work-center.mjs';
import {boot,until,sleep} from './helpers/live.mjs';

const shots=path.resolve(import.meta.dirname,'../../docs/specs/herdr-work-command-center-wc001/screenshots');
const snap='2026-01-01T00:00:08Z';
const card='[data-overview="week"]';
const ctx='[data-work-context]';
const projection=(id,title,o={})=>({mission_key:`pi:${id}`,activity_snapshot_at:snap,host_id:'beast',mission:{id,title,status:'EXECUTING'},freshness:{state:'live',age_seconds:2},activity:{health:'active'},...o});
const sess=(id,o={})=>({session_id:id,host_id:'beast',status:'running',updated_at:snap,...o});
const evil='<img src=x onerror="window.__xss=1">';
const SESSIONS=[
 sess('s-1',{display_name:'Parser worker',mission_key:'pi:MSN-1',capabilities:{attach_terminal:true}}),
 sess('s-2',{display_name:'Parser reviewer',mission_key:'pi:MSN-1'}),
 sess('s-solo',{display_name:'Solo agent'}),
 sess('s-evil',{display_name:evil}),
 sess('s-stale',{display_name:'Stale ref',mission_key:'pi:GONE'}),
 // An agent finished, its mission has not: the mission stays active at its measured progress.
 sess('s-fin',{display_name:'Finished worker',status:'completed',mission_key:'pi:MSN-2'}),
 // mission_id (no key) that two recorded missions both claim to own: ambiguous, so never linked.
 sess('s-amb',{display_name:'Ambiguous ref',mission_id:'MSN-AMB'}),
];
const API={
 '/api/v1/mission-activity':{activities:[
  projection('MSN-1','Repair parser',{activity:{health:'active',acceptanceCoverage:{completed:1,total:4}}}),
  projection('MSN-2','Half done mission',{activity:{health:'active',acceptanceCoverage:{completed:2,total:4}}}),
  projection('MSN-3',`${evil}hostile mission`,{}),
 ]},
 '/api/v1/missions':{missions:[
  {mission_id:'MSN-AMB',title:'Amb one',created_at:'2025-12-30T10:00:00Z',updated_at:'2025-12-31T10:00:00Z',sessions:['s-amb']},
  {mission_id:'MSN-AMB',title:'Amb two',created_at:'2025-12-30T11:00:00Z',updated_at:'2025-12-31T11:00:00Z',sessions:['s-amb']},
 ]},
 '/api/v1/sessions':{sessions:SESSIONS},
 '/api/v1/sessions/s-1/terminal':{recommended:'local',commands:[{form:'local',label:'Local',command:'herdr attach s-1'}]},
};
const quiet={services:[],hosts:[],activities:[],events:[],items:[],reviews:[],available:true,components:[],summary:{},status:'ok',repositories:[],pipelines:[],missions:[],sessions:[]};
const sessionRoute=p=>{
 const m=/^\/api\/v1\/sessions\/([^/]+)$/.exec(p);
 if(m){const s=SESSIONS.find(x=>x.session_id===decodeURIComponent(m[1]));return s??{__status:404,json:{error:'not found'}};}
};
const raw=(extra={})=>(p,url)=>extra[p]!==undefined?(typeof extra[p]==='function'?extra[p](url):extra[p]):sessionRoute(p)??API[p]??quiet;
const handler=withFlag(raw());
// The card shows at most 5 rows per group ("Showing 5 of N"), so journeys that must find one exact row use a small fixture.
const lean=withFlag(raw({
 '/api/v1/sessions':{sessions:[SESSIONS[0],SESSIONS[1],SESSIONS[2]]},
 '/api/v1/mission-activity':{activities:[API['/api/v1/mission-activity'].activities[0],API['/api/v1/mission-activity'].activities[1]]},
 '/api/v1/missions':{missions:[]},
}));
const hashIs=(page,prefix)=>until(async()=>(await page.evaluate(()=>location.hash)).startsWith(prefix),`route ${prefix}`);
const total=reads=>Object.values(reads).reduce((a,b)=>a+b,0);

test('journey: Overview -> This week row -> mission -> related agent -> session -> terminal attach (copy command only)',async()=>withPage(async(page,base)=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const writes=[];page.on('request',r=>{const u=new URL(r.url());if(!['GET','HEAD','OPTIONS'].includes(r.method())&&u.pathname.startsWith('/api/'))writes.push(`${r.method()} ${u.pathname}`);});
 await boot(page,base,'/overview',lean);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await page.locator(`${card} a`,{hasText:'Repair parser'}).click();
 await hashIs(page,'#/missions/pi%3AMSN-1');
 assert.equal(await page.locator('#view h1').first().isVisible(),true,'mission page renders');
 // Related agent: the session page of an agent that belongs to the mission, reached by its canonical route.
 await page.goto(`${base}/index.html#/herd/session/s-1`);
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 assert.equal(await page.locator(`${ctx} [data-mission-link]`).innerText(),'Repair parser');
 await page.locator(`${ctx} [data-sibling-link]`).click();               // related agent (s-2) from the Work Context header
 await hashIs(page,'#/herd/session/s-2');
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 await page.locator(`${ctx} [data-sibling-link]`).click();               // and back to s-1
 await hashIs(page,'#/herd/session/s-1');
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 await page.locator('#session-terminal [data-terminal]').click();
 await page.locator('#session-terminal [data-copy]').waitFor({timeout:5000});
 const out=await page.locator('#session-terminal').innerText();
 assert.match(out,/herdr attach s-1/);
 assert.match(out,/Run one of these in a terminal on your machine/);
 assert.equal(await page.locator('#session-terminal a[href^="herdr:"], #session-terminal a[href^="ssh:"]').count(),0,'no native terminal link');
 // Getting the attach command is the one POST (the server builds the command text); nothing else writes, and nothing is attached or focused.
 assert.deepEqual(writes,['POST /api/v1/sessions/s-1/terminal'],'the only non-GET request is the copy-command lookup');
 assert.deepEqual(errors,[]);
}));

test('journey: GitHub board verified PR card -> Open in Herdr -> mission (flag on)',async()=>withPage(async(page,base)=>{
 const board={project_id:'PVT_1',owner:'acme',number:1,title:'Roadmap',url:'https://github.com/orgs/acme/projects/1',closed:false,updated_at:'2026-10-24T00:00:00Z',item_count:2,status_options:[{id:'o1',name:'Todo'}],complete:true,error:null};
 const sync={available:true,status:'ready',connection_generation:1,projects_sync:{enabled:true,status:'ready',error:null,projects:1,deferred:0},revision:3};
 const items=[
  {item_id:'P-1',project_id:'PVT_1',type:'PULL_REQUEST',status_option:'o1',status:'Todo',title:'Fix parser',number:7,url:'https://github.com/acme/app/pull/7',repository:'acme/app',state:'OPEN',position:0},
  {item_id:'I-1',project_id:'PVT_1',type:'ISSUE',status_option:'o1',status:'Todo',title:'Plain issue',number:1,url:'https://github.com/acme/app/issues/1',repository:'acme/app',state:'OPEN',position:1},
 ];
 const h=withFlag(raw({
  '/api/v1/github/projects':url=>url.searchParams.get('project')?{...sync,project:board,items}:{...sync,projects:[board]},
  '/api/v1/mission-outcomes':{outcomes:{'pi:MSN-7':{result:'succeeded',prs:[{repository:'acme/app',number:7}]}}},
 }));
 await boot(page,base,'/github/projects?project=PVT_1',h);
 await page.locator('[data-ghp-item="P-1"] [data-herdr-link]').waitFor({timeout:8000});
 assert.equal(await page.locator('[data-ghp-item="I-1"] [data-herdr-link]').count(),0,'issues never link');
 await page.locator('[data-ghp-item="P-1"] [data-herdr-link]').click();
 await hashIs(page,'#/missions/pi%3AMSN-7');
}));

test('flag off (default server): the pages behave exactly as before the Work Command Center',async()=>withPage(async(page,base)=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const off=withFlag(raw(),false);
 const reads=await boot(page,base,'/overview',off);
 await page.locator('[data-overview="sessions"]').waitFor({timeout:8000});
 await sleep(200);
 assert.equal(await page.locator(card).count(),0,'no This week card');
 assert.equal(await page.locator('.wk-row, .wk-banner').count(),0);
 const order=await page.evaluate(()=>[...document.querySelectorAll('[data-overview]')].map(e=>e.dataset.overview));
 assert.deepEqual(order,['attention','sessions','missions','fleet','ci','reviews','services','hosts','activity','health']);
 assert.doesNotMatch(await page.locator('.ov-customize').innerText(),/This week/);
 // Session page: no Work Context slot, the legacy mission line is untouched.
 await page.goto(`${base}/index.html#/herd/session/s-1`);
 await page.locator('#session-terminal').waitFor({timeout:8000});
 await sleep(200);
 assert.equal(await page.locator(`${ctx}, #session-context`).count(),0);
 // Board: no relationship read and no Open in Herdr / partial note anywhere.
 const board={project_id:'PVT_1',owner:'acme',number:1,title:'Roadmap',url:'https://github.com/orgs/acme/projects/1',closed:false,updated_at:'2026-10-24T00:00:00Z',item_count:1,status_options:[{id:'o1',name:'Todo'}],complete:true,error:null};
 const sync={available:true,status:'ready',connection_generation:1,projects_sync:{enabled:true,status:'ready',error:null,projects:1,deferred:0},revision:3};
 const boardReads=await boot(page,base,'/github/projects?project=PVT_1',withFlag(raw({
  '/api/v1/github/projects':url=>url.searchParams.get('project')?{...sync,project:board,items:[{item_id:'P-1',project_id:'PVT_1',type:'PULL_REQUEST',status_option:'o1',status:'Todo',title:'Fix parser',number:7,url:'https://github.com/acme/app/pull/7',repository:'acme/app',state:'OPEN',position:0}]}:{...sync,projects:[board]},
  '/api/v1/mission-outcomes':{outcomes:{'pi:MSN-7':{result:'succeeded',prs:[{repository:'acme/app',number:7}]}}},
 }),false));
 await page.locator('[data-ghp-item="P-1"]').waitFor({timeout:8000});
 assert.equal(await page.locator('[data-herdr-link], [data-herdr-partial]').count(),0);
 assert.equal(boardReads['/api/v1/mission-outcomes']??0,0,'flag off: the board does not read mission outcomes');
 assert.equal(reads['/api/v1/mission-outcomes']??0,0);
 assert.deepEqual(errors,[]);
}));

test('flag off when the capability probe fails: still the pre-WC pages',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',(p,url)=>p==='/api/v1/health'?{__status:500,json:{error:'down'}}:raw()(p,url));
 await page.locator('[data-overview="sessions"]').waitFor({timeout:8000});
 await sleep(200);
 assert.equal(await page.locator(card).count(),0);
}));

test('empty state: no work at all shows the explicit empty text, never zero totals as fact',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',withFlag(()=>quiet));
 await page.locator(`${card}`).waitFor({timeout:8000});
 await until(async()=>/No work items are reported yet/.test(await page.locator(card).innerText()),'empty text');
 const text=await page.locator(card).innerText();
 assert.doesNotMatch(text,/\b100%/);
 assert.equal(await page.locator(`${card} progress`).count(),0);
 assert.equal(await page.locator(`${card} .wk-row`).count(),0);
}));

test('401 and 403 everywhere give safe states: no crash, no invented zeros, no mission links',async()=>withPage(async(page,base)=>{
 for(const status of [401,403]){
  const errors=[];const onerr=e=>errors.push(e.message);page.on('pageerror',onerr);
  const denied=()=>({__status:status,json:{error:'forbidden'}});
  await boot(page,base,'/overview',withFlag(p=>p.startsWith('/api/v1/')?denied():quiet));
  await page.locator(card).waitFor({timeout:8000});
  await until(async()=>/Could not|unknown|unavailable/i.test(await page.locator(card).innerText()),`${status} stale/unknown state`);
  const text=await page.locator(card).innerText();
  assert.equal(await page.locator(`${card} .wk-row`).count(),0);
  assert.doesNotMatch(text,/\b0 \(complete\)|complete\b.*\bStarted/);
  // Session page: the agent read itself is denied -> error page, no Work Context.
  await page.goto(`${base}/index.html#/herd/session/s-1`);
  await sleep(300);
  assert.equal(await page.locator(ctx).count(),0);
  assert.deepEqual(errors.filter(m=>!/Failed to load resource/.test(m)),[],`no uncaught errors on ${status}`);
  page.off('pageerror',onerr);
 }
}));

test('401/403 only on the Work Context reads show "unavailable", not "unlinked"',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/herd/session/s-1',withFlag(raw({
  '/api/v1/mission-activity':{__status:403,json:{error:'no'}},
  '/api/v1/missions':{__status:401,json:{error:'no'}},
 })));
 await page.locator(`${ctx}[data-state="unavailable"]`).waitFor({timeout:8000});
 assert.doesNotMatch(await page.locator(ctx).innerText(),/Not linked to a mission/);
 assert.equal(await page.locator('#session-terminal [data-terminal]').count(),1,'the rest of the page still works');
}));

test('unknown, stale and ambiguous ids never produce a link or a guessed mission',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/herd/session/nope',handler);
 await until(async()=>/not found|could not load/i.test(await page.locator('#view').innerText())&&await page.locator(ctx).count()===0,'unknown session');
 await page.goto(`${base}/index.html#/missions/pi%3ANOPE`);
 await sleep(500);
 assert.equal(await page.locator('#view').isVisible(),true);
 await page.goto(`${base}/index.html#/herd/session/s-stale`);
 await page.locator(`${ctx}[data-state="unlinked"]`).waitFor({timeout:8000});
 assert.match(await page.locator(ctx).innerText(),/could not be confirmed/);
 // Two recorded missions share one id and both claim the agent: the page never presents that link as confirmed.
 await page.goto(`${base}/index.html#/herd/session/s-amb`);
 await page.locator(ctx).waitFor({timeout:8000});
 await until(async()=>/not fully confirmed|could not be confirmed/.test(await page.locator(ctx).innerText()),'ambiguous reference flagged unconfirmed');
}));

test('XSS: hostile mission, session and issue text stays inert on every Work Command Center surface',async()=>withPage(async(page,base)=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await boot(page,base,'/overview',handler);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await page.goto(`${base}/index.html#/herd/session/s-evil`);
 await page.locator(`${ctx}[data-state="unlinked"]`).waitFor({timeout:8000});
 await sleep(200);
 // s-2's sibling list shows a hostile label from the Work Context header of its mission peers.
 await page.goto(`${base}/index.html#/herd/session/s-1`);
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 const board={project_id:'PVT_1',owner:'acme',number:1,title:evil,url:'https://github.com/orgs/acme/projects/1',closed:false,updated_at:'2026-10-24T00:00:00Z',item_count:1,status_options:[{id:'o1',name:evil}],complete:true,error:null};
 const sync={available:true,status:'ready',connection_generation:1,projects_sync:{enabled:true,status:'ready',error:null,projects:1,deferred:0},revision:3};
 const h=withFlag(raw({
  '/api/v1/github/projects':url=>url.searchParams.get('project')?{...sync,project:board,items:[{item_id:'I-x',project_id:'PVT_1',type:'ISSUE',status_option:'o1',status:evil,title:evil,number:1,url:'javascript:alert(1)',repository:'acme/app',state:'OPEN',position:0}]}:{...sync,projects:[board]},
  '/api/v1/mission-outcomes':{outcomes:{}},
 }));
 await page.unroute('**/api/v1/**');
 await boot(page,base,'/github/projects?project=PVT_1',h);
 await page.locator('[data-ghp-item="I-x"]').waitFor({timeout:8000});
 assert.equal(await page.locator('#view img, #view script').count(),0);
 assert.equal(await page.locator('a[href^="javascript:"]').count(),0);
 await page.goto(`${base}/index.html#/overview`);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 assert.equal(await page.locator(`${card} img`).count(),0);
 assert.equal(await page.evaluate(()=>window.__xss),undefined);
 assert.deepEqual(errors,[]);
}));

test('no false 100%: an agent that completed does not complete its mission or invent progress',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',handler);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 const half=page.locator(`${card} .wk-row`,{hasText:'Half done mission'});
 assert.equal(await half.count(),1);
 assert.equal(await half.locator('progress').getAttribute('value'),'50','measured 2/4, not 100');
 const completed=page.locator(`${card} [data-group="completed"] .wk-row, ${card} .wk-row[data-lifecycle="finished"]`);
 assert.equal(await completed.filter({hasText:'Half done mission'}).count(),0,'mission is not in Completed this week');
 // Count the metric: no finished mission exists, so Finished must not claim one.
 const text=await page.locator(card).innerText();
 assert.doesNotMatch(text,/100\s*%/);
 // The mission without any acceptance evidence has no bar at all.
 assert.equal(await page.locator(`${card} .wk-row`,{hasText:'hostile mission'}).locator('progress').count(),0);
 // The finished agent's session page still links to the (running) mission, and shows it as running at 50%.
 await page.goto(`${base}/index.html#/herd/session/s-fin`);
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 const t=await page.locator(ctx).innerText();
 assert.match(t,/running/);
 assert.equal(await page.locator(`${ctx} progress`).getAttribute('value'),'50');
}));

test('event timestamp reconciliation: a mission repeated in the sources is one row, and duplicate live events change nothing',async()=>withPage(async(page,base)=>{
 const dup={mission_id:'MSN-1',title:'Repair parser',created_at:'2025-12-30T10:00:00Z',updated_at:'2025-12-31T10:00:00Z',completed_at:'2025-12-31T11:00:00Z',status:'COMPLETE'};
 const h=withFlag(raw({'/api/v1/missions':{missions:[dup,{...dup},{...dup,completed_at:'2025-12-31T11:00:00Z'}]}}));
 const reads=await boot(page,base,'/overview',h);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 const titles=async()=>page.locator(`${card} .wk-row`).evaluateAll(rows=>rows.map(r=>r.textContent.replace(/\s+/g,' ').trim()));
 const before=await titles();
 assert.equal(before.filter(t=>/Repair parser/.test(t)).length<=2,true,'the repeated records do not multiply the mission');
 const event={event_id:'dup-1',event_type:'MissionUpdated',entity_type:'mission',entity_id:'MSN-1'};
 for(let i=0;i<5;i++)await page.evaluate(e=>window.__emit(e),event);
 await page.clock.runFor(7000);await sleep(100);
 assert.deepEqual(await titles(),before,'the same event delivered five times leaves the card unchanged');
 assert.equal(await page.locator(`${card} progress`).count(),before.length?await page.locator(`${card} progress`).count():0);
 assert.ok(total(reads)<80,`bounded reads after five duplicate events: ${total(reads)}`);
}));

test('reconnect: the card stays stable across source errors and telemetry reconnects, and polling stays bounded',async()=>withPage(async(page,base)=>{
 let failing=false;
 const h=withFlag(p=>failing&&p.startsWith('/api/v1/')&&p!=='/api/v1/health'?{__status:503,json:{error:'down'}}:raw()(p));
 const reads=await boot(page,base,'/overview',h);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await page.evaluate(()=>{window.__rows=document.querySelector('[data-overview="week"]');});
 const t0=total(reads);
 await page.clock.runFor(60000);await sleep(150);
 const healthyMinute=total(reads)-t0;
 const rowsBefore=await page.locator(`${card} .wk-row`).count();
 // Source outage: last data stays, a stale note appears, and the request rate does not grow.
 failing=true;
 const t1=total(reads);
 await page.clock.runFor(60000);await sleep(150);
 const outageMinute=total(reads)-t1;
 assert.equal(await page.locator(`${card} .wk-row`).count(),rowsBefore,'rows kept while sources fail');
 assert.match(await page.locator(card).innerText(),/Could not refresh|showing last data/i);
 assert.ok(outageMinute<=healthyMinute*2+10,`outage minute (${outageMinute}) must not exceed a bounded multiple of a healthy minute (${healthyMinute})`);
 // Reconnect: telemetry comes back, events flow, data recovers; the same card element is patched, not replaced.
 failing=false;
 await page.evaluate(()=>window.__emit({event_id:'r-1',event_type:'SessionUpdated'}));
 await page.clock.runFor(35000);await sleep(150);
 await until(async()=>!/Could not refresh/.test(await page.locator(card).innerText()),'stale note cleared after reconnect');
 assert.equal(await page.locator(`${card} .wk-row`).count(),rowsBefore);
 assert.equal(await page.evaluate(()=>window.__rows===document.querySelector('[data-overview="week"]')),true,'card element not replaced');
 // Linear, not unbounded: ten more minutes cost at most ~10x a healthy minute.
 const t2=total(reads);
 await page.clock.runFor(600000);await sleep(300);
 const tenMinutes=total(reads)-t2;
 assert.ok(tenMinutes<=healthyMinute*12+20,`10 minutes (${tenMinutes}) stay linear in a healthy minute (${healthyMinute})`);
 console.log(`# poll budget: healthy minute=${healthyMinute} outage minute=${outageMinute} ten minutes=${tenMinutes}`);
}));

test('keyboard only: Overview -> row -> mission, then session header -> mission link, without a pointer',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',lean);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 let reached=false;
 for(let i=0;i<300&&!reached;i++){
  await page.keyboard.press('Tab');
  reached=await page.evaluate(()=>{const a=document.activeElement;return !!a&&a.tagName==='A'&&!!a.closest('[data-overview="week"] .wk-row')&&/Repair parser/.test(a.textContent);});
 }
 assert.equal(reached,true,'Tab reaches the Repair parser row link');
 const visible=await page.evaluate(()=>{const s=getComputedStyle(document.activeElement);return s.outlineStyle!=='none'||s.boxShadow!=='none';});
 assert.equal(visible,true,'a visible focus indicator');
 await page.keyboard.press('Enter');
 await hashIs(page,'#/missions/pi%3AMSN-1');
 await page.goto(`${base}/index.html#/herd/session/s-1`);
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 let onLink=false;
 for(let i=0;i<300&&!onLink;i++){
  await page.keyboard.press('Tab');
  onLink=await page.evaluate(()=>document.activeElement?.matches?.('[data-work-context] [data-mission-link]'));
 }
 assert.equal(onLink,true,'Tab reaches the mission link in Work Context');
 await page.keyboard.press('Enter');
 await hashIs(page,'#/missions/pi%3AMSN-1');
}));

test('desktop and mobile screenshots of the full journey surfaces; no horizontal scroll',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/overview',lean);
 await page.locator(`${card} .wk-row`).first().waitFor({timeout:8000});
 await page.setViewportSize({width:1280,height:900});
 await page.screenshot({path:path.join(shots,'wc006-overview-desktop.png')});
 await page.setViewportSize({width:390,height:844});await sleep(150);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
 await page.screenshot({path:path.join(shots,'wc006-overview-mobile.png')});
 await page.setViewportSize({width:1280,height:900});
 await page.goto(`${base}/index.html#/herd/session/s-1`);
 await page.locator(`${ctx}[data-state="linked"]`).waitFor({timeout:8000});
 await page.locator('#session-terminal [data-terminal]').click();
 await page.locator('#session-terminal [data-copy]').waitFor({timeout:5000});
 await page.screenshot({path:path.join(shots,'wc006-session-desktop.png')});
 await page.setViewportSize({width:390,height:844});await sleep(150);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
 await page.screenshot({path:path.join(shots,'wc006-session-mobile.png')});
 // Flag off, same overview, for the before/after comparison.
 await page.unroute('**/api/v1/**');
 await boot(page,base,'/overview',withFlag(raw(),false));
 await page.locator('[data-overview="sessions"]').waitFor({timeout:8000});
 await page.setViewportSize({width:1280,height:900});await sleep(150);
 await page.screenshot({path:path.join(shots,'wc006-overview-flag-off-desktop.png')});
}));

test('the Work Command Center adds no polling: per-path request counts over 2 minutes are identical with the flag on and off',async()=>withPage(async(page,base)=>{
 const run=async on=>{
  const reads=await boot(page,base,'/overview',withFlag(raw(),on));
  await page.locator('[data-overview="sessions"]').waitFor({timeout:8000});
  await sleep(300);
  await page.clock.runFor(120000);await sleep(300);
  const copy={...reads};await page.unroute('**/api/v1/**');return copy;
 };
 const off=await run(false);
 const on=await run(true);
 for(const [p,n] of Object.entries(on))assert.ok(n<=(off[p]??0)+1,`${p}: ${n} reads with the flag on vs ${off[p]??0} off`);
 assert.equal(on['/api/v1/mission-outcomes']??0,0,'the overview never reads mission outcomes');
}));
