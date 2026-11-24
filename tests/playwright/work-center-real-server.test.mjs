// WC-006: the Work Command Center against the REAL control-plane server (tests/playwright/server.py, in-memory repo).
// Real: health capability, missions, sessions, mission-outcomes (PR refs), mission story, session page, flag on/off.
// Stubbed: only /api/v1/github/projects (no GitHub in the fixture server); its single PR card matches the real recorded PR.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {withPage} from './helpers/fixture.mjs';
import {until,sleep} from './helpers/live.mjs';

const REPO=path.resolve(import.meta.dirname,'..','..');
const PY=process.env.PYTHON||(fs.existsSync(path.join(REPO,'.venv','bin','python'))?path.join(REPO,'.venv','bin','python'):'python3');

async function startServer(env){
 const proc=spawn(PY,[path.join(REPO,'tests','playwright','server.py')],{env:{...process.env,BURST:'0',INSPECTOR_SEED:'0',CI_OFFLINE:'1',MISSION_STORY_SEED:'1',...env},stdio:['ignore','pipe','pipe']});
 const port=await new Promise((resolve,reject)=>{
  let buf='';const timer=setTimeout(()=>reject(new Error('server start timeout')),30000);
  proc.stdout.on('data',d=>{buf+=d.toString();const m=buf.match(/PORT=(\d+)/);if(m){clearTimeout(timer);resolve(Number(m[1]));}});
  proc.on('exit',c=>reject(new Error(`server exited early (${c})`)));
  proc.stderr.on('data',d=>process.stderr.write(`[srv] ${d}`));
 });
 return {proc,port,base:`http://127.0.0.1:${port}`};
}
const json=async(base,p)=>(await fetch(`${base}${p}`)).json();

async function withServer(env,run){
 const srv=await startServer(env);
 try{await withPage(async page=>run(page,srv.base));}
 finally{srv.proc.kill('SIGTERM');}
}

test('real server, flag ON: health advertises work_center_v1; Overview week card -> mission; session Work Context -> mission; board PR -> Open in Herdr -> mission',async()=>{
 await withServer({WORK_CENTER:'1'},async(page,base)=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const health=await json(base,'/api/v1/health?probe=1');
  assert.ok(health.capabilities.includes('work_center_v1'));
  const {sessions}=await json(base,'/api/v1/sessions');
  const withMission=sessions.find(s=>s.mission_key);
  assert.ok(withMission,'the seed has an agent that reports a mission');
  const {outcomes}=await json(base,'/api/v1/mission-outcomes');
  const [missionKey]=Object.entries(outcomes).find(([,o])=>o.prs.some(p=>p.repository==='org/app'&&p.number===214));
  // Overview
  await page.goto(`${base}/index.html#/overview`);
  await page.locator('[data-overview="week"] .wk-row').first().waitFor({timeout:15000});
  const card=await page.locator('[data-overview="week"]').innerText();
  assert.match(card,/This week/);
  assert.match(card,/Partial data/,'real history is not proven, so the card says so');
  const rows=await page.locator('[data-overview="week"] .wk-row a').evaluateAll(a=>a.map(x=>x.getAttribute('href')));
  assert.ok(rows.some(h=>h.startsWith('#/missions/')||h.startsWith('#/herd/session/')),`rows link into existing routes: ${rows}`);
  const missionLink=page.locator('[data-overview="week"] .wk-row a[href^="#/missions/"]').first();
  assert.ok(await missionLink.count()>0,'the seeded missions appear as rows');
  await missionLink.click();
  await until(async()=>(await page.evaluate(()=>location.hash)).startsWith('#/missions/'),'mission route');
  await page.locator('#view h1').first().waitFor({timeout:10000});
  // Session Work Context from the real records
  await page.goto(`${base}/index.html#/herd/session/${encodeURIComponent(withMission.session_id)}`);
  await page.locator('[data-work-context]').waitFor({timeout:15000});
  await until(async()=>(await page.locator('[data-work-context]').getAttribute('data-state'))!=='loading','work context settled');
  const state=await page.locator('[data-work-context]').getAttribute('data-state');
  assert.equal(state,'linked','the seeded agent names its mission by exact key');
  assert.equal(await page.locator('[data-work-context] [data-mission-link]').getAttribute('href'),`#/missions/${encodeURIComponent(withMission.mission_key)}`);
  // Board: the only stub is the GitHub projects feed; the PR ref comes from the REAL /mission-outcomes.
  const board={project_id:'PVT_1',owner:'org',number:1,title:'Roadmap',url:'https://github.com/orgs/org/projects/1',closed:false,updated_at:'2026-10-24T00:00:00Z',item_count:2,status_options:[{id:'o1',name:'Todo'}],complete:true,error:null};
  const sync={available:true,status:'ready',connection_generation:1,projects_sync:{enabled:true,status:'ready',error:null,projects:1,deferred:0},revision:3};
  const item=(id,n,o={})=>({item_id:id,project_id:'PVT_1',type:'PULL_REQUEST',status_option:'o1',status:'Todo',title:`PR ${n}`,number:n,url:`https://github.com/org/app/pull/${n}`,repository:'org/app',state:'OPEN',position:n,...o});
  await page.route('**/api/v1/github/projects**',route=>{
   const u=new URL(route.request().url());
   route.fulfill({json:u.searchParams.get('project')?{...sync,project:board,items:[item('P-real',214),item('P-unknown',999)]}:{...sync,projects:[board]}});
  });
  await page.goto(`${base}/index.html#/github/projects?project=PVT_1`);
  await page.locator('[data-ghp-item="P-real"] [data-herdr-link]').waitFor({timeout:15000});
  assert.equal(await page.locator('[data-ghp-item="P-unknown"] [data-herdr-link]').count(),0);
  assert.equal(await page.locator('[data-ghp-item="P-real"] [data-herdr-link]').getAttribute('href'),`#/missions/${encodeURIComponent(missionKey)}`);
  await page.locator('[data-ghp-item="P-real"] [data-herdr-link]').click();
  await until(async()=>(await page.evaluate(()=>location.hash))===`#/missions/${encodeURIComponent(missionKey)}`,'real mission route');
  await page.locator('#view h1').first().waitFor({timeout:10000});
  assert.deepEqual(errors.filter(m=>!/Failed to load resource/.test(m)),[]);
 });
});

test('real server, flag OFF (the default): no capability, no This week card, no Work Context, no Open in Herdr',async()=>{
 await withServer({},async(page,base)=>{
  const health=await json(base,'/api/v1/health?probe=1');
  assert.ok(!health.capabilities.includes('work_center_v1'));
  await page.goto(`${base}/index.html#/overview`);
  await page.locator('[data-overview="sessions"]').waitFor({timeout:15000});
  await sleep(500);
  assert.equal(await page.locator('[data-overview="week"], .wk-row').count(),0);
  const {sessions}=await json(base,'/api/v1/sessions');
  await page.goto(`${base}/index.html#/herd/session/${encodeURIComponent(sessions[0].session_id)}`);
  await page.locator('#session-terminal').waitFor({timeout:15000});
  await sleep(500);
  assert.equal(await page.locator('[data-work-context]').count(),0);
  await page.route('**/api/v1/github/projects**',route=>route.fulfill({json:{available:true,status:'ready',projects:[],projects_sync:{enabled:true,status:'ready'},revision:1}}));
  await page.goto(`${base}/index.html#/github/projects`);
  await sleep(800);
  assert.equal(await page.locator('[data-herdr-link], [data-herdr-partial]').count(),0);
 });
});

test('G1 real server: Started/Finished come from real stored lifecycle events, coverage stays partial with a horizon',async()=>{
 await withServer({WORK_CENTER:'1',MISSION_STORY_SEED:'1'},async(page,base)=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const cardSel='[data-overview="week"]';
  const calls=[];
  page.on('request',r=>{if(r.url().includes('/api/v1/work/weekly-events'))calls.push(r.url());});
  await page.goto(`${base}/index.html#/overview`);
  await page.locator(`${cardSel} .wk-row`).first().waitFor({timeout:15000});
  await until(async()=>/history from/.test(await page.locator(cardSel).innerText()),'server history badge',15000);
  assert.equal(calls.length,1,'one weekly read for this load');
  // Read the same window the page asked for, straight from the real endpoint.
  const q=new URL(calls[0]).searchParams;
  const body=await json(base,`/api/v1/work/weekly-events?since=${encodeURIComponent(q.get('since'))}&until=${encodeURIComponent(q.get('until'))}&limit=500`);
  assert.equal(body.coverage.complete,false,'the server cannot prove emission, so never complete');
  assert.ok(body.coverage.horizon,'a horizon is reported');
  assert.equal(body.coverage.truncated,false);
  const distinct=kind=>new Set(body.events.filter(e=>e.kind===kind&&!(e.source==='session'&&e.mission_key)).map(e=>e.work_id));
  const startedIds=distinct('started'),finishedIds=distinct('finished');
  assert.ok(startedIds.size>=4,`seed has several started works: ${[...startedIds]}`);
  assert.equal(finishedIds.size,2,'Alpha and Delta finished; Gamma failed is not a finish');
  const metric=async label=>Number((await page.locator(`${cardSel} .wk-metric`,{hasText:label}).locator('strong').innerText()).trim());
  assert.equal(await metric('Finished'),2);
  assert.equal(await metric('Started'),startedIds.size);
  const badge=await page.locator(`${cardSel} .wk-metric`,{hasText:'Started'}).innerText();
  assert.match(badge,/at least \d+ \(partial\) · history from \d{4}-\d{2}-\d{2}/);
  assert.doesNotMatch(badge,/complete/);
  await page.setViewportSize({width:1200,height:900});
  await page.locator(cardSel).screenshot({path:path.join(REPO,'docs/specs/herdr-work-command-center-wc001/screenshots/wc-g1-real-server-overview.png')});
  assert.deepEqual(errors.filter(m=>!/Failed to load resource/.test(m)),[]);
 });
});

test('G1 real server, flag OFF: the weekly endpoint is 404 and the page never asks for it',async()=>{
 await withServer({},async(page,base)=>{
  const calls=[];
  page.on('request',r=>{if(r.url().includes('/api/v1/work/'))calls.push(r.url());});
  const res=await fetch(`${base}/api/v1/work/weekly-events?since=2026-01-01T00:00:00Z&until=2026-01-08T00:00:00Z`);
  assert.equal(res.status,404);
  await page.goto(`${base}/index.html#/overview`);
  await page.locator('[data-overview="sessions"]').waitFor({timeout:15000});
  await sleep(500);
  assert.deepEqual(calls,[]);
 });
});

// G9: Pi-reported next action and Pi issue links, from a real Pi-shaped activity snapshot ingested by the real server
// (tests/playwright/server.py PI_ACTIVITY_SEED=1). Only the GitHub projects feed is stubbed, as above.
const projectsStub=async(page,items)=>{
 const board={project_id:'PVT_1',owner:'org',number:1,title:'Roadmap',url:'https://github.com/orgs/org/projects/1',closed:false,updated_at:'2026-10-24T00:00:00Z',item_count:items.length,status_options:[{id:'o1',name:'Todo'}],complete:true,error:null};
 const sync={available:true,status:'ready',connection_generation:1,projects_sync:{enabled:true,status:'ready',error:null,projects:1,deferred:0},revision:3};
 await page.route('**/api/v1/github/projects**',route=>{
  const u=new URL(route.request().url());
  route.fulfill({json:u.searchParams.get('project')?{...sync,project:board,items}:{...sync,projects:[board]}});
 });
};
const issue=(id,n)=>({item_id:id,project_id:'PVT_1',type:'ISSUE',status_option:'o1',status:'Todo',title:`Issue ${n}`,number:n,url:`https://github.com/org/app/issues/${n}`,repository:'org/app',state:'OPEN',position:n});

test('G9 real server, flag ON: session page shows the Pi-reported next action and an issue card verifies through Pi issue_refs',async()=>{
 await withServer({WORK_CENTER:'1',PI_ACTIVITY_SEED:'1'},async(page,base)=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const {sessions}=await json(base,'/api/v1/sessions');
  const piSession=sessions.find(s=>s.mission_key==='pi:MSN-PI1');
  assert.ok(piSession,'the seed has an agent in the Pi mission');
  const refs=await json(base,'/api/v1/mission-outcomes');
  assert.deepEqual(refs.issue_refs,{'pi:MSN-PI1':[{repository:'org/app',number:321}]});
  await page.goto(`${base}/index.html#/herd/session/${encodeURIComponent(piSession.session_id)}`);
  await page.locator('[data-work-context]').waitFor({timeout:15000});
  await until(async()=>(await page.locator('[data-work-context]').getAttribute('data-state'))!=='loading','work context settled');
  assert.equal(await page.locator('[data-work-context]').getAttribute('data-state'),'linked');
  assert.equal(await page.locator('[data-work-context] [data-next-action]').innerText().then(t=>t.includes('Run the integration suite')),true);
  // Issue card: org/app#321 is Pi-linked to exactly one mission; #322 is not linked.
  await projectsStub(page,[issue('I-321',321),issue('I-322',322)]);
  await page.goto(`${base}/index.html#/github/projects?project=PVT_1`);
  await page.locator('[data-ghp-item="I-321"] [data-herdr-link]').waitFor({timeout:15000});
  assert.equal(await page.locator('[data-ghp-item="I-321"] [data-herdr-link]').getAttribute('href'),`#/missions/${encodeURIComponent('pi:MSN-PI1')}`);
  assert.equal(await page.locator('[data-ghp-item="I-322"] [data-herdr-link]').count(),0);
  await page.locator('[data-ghp-item="I-321"] [data-herdr-link]').click();
  await until(async()=>(await page.evaluate(()=>location.hash))===`#/missions/${encodeURIComponent('pi:MSN-PI1')}`,'mission route');
  assert.deepEqual(errors.filter(m=>!/Failed to load resource/.test(m)),[]);
 });
});

test('G9 real server, flag OFF with the same Pi data: no next action row and no Open in Herdr on the issue card',async()=>{
 await withServer({PI_ACTIVITY_SEED:'1'},async(page,base)=>{
  const {sessions}=await json(base,'/api/v1/sessions');
  const piSession=sessions.find(s=>s.mission_key==='pi:MSN-PI1');
  assert.ok(!(await json(base,'/api/v1/health?probe=1')).capabilities.includes('work_center_v1'));
  await page.goto(`${base}/index.html#/herd/session/${encodeURIComponent(piSession.session_id)}`);
  await page.locator('#session-terminal').waitFor({timeout:15000});
  await sleep(500);
  assert.equal(await page.locator('[data-work-context], [data-next-action]').count(),0);
  await projectsStub(page,[issue('I-321',321)]);
  await page.goto(`${base}/index.html#/github/projects?project=PVT_1`);
  await page.locator('[data-ghp-item="I-321"]').waitFor({timeout:15000});
  await sleep(500);
  assert.equal(await page.locator('[data-herdr-link], [data-herdr-partial]').count(),0);
 });
});
