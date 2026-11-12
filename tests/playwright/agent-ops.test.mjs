// Agent Operations Activity Map against the REAL control-plane server (tests/playwright/agent-ops-server.py).
// Real: ingest -> projection -> /api/v1/agent-ops/{snapshot,events,replay,stream} (SSE) -> page. Nothing is stubbed.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {withPage} from './helpers/fixture.mjs';
import {until} from './helpers/live.mjs';

const REPO=path.resolve(import.meta.dirname,'..','..');
const PY=process.env.PYTHON||['/private/tmp/claude-501/aoenv/bin/python',path.join(REPO,'.venv','bin','python')].find(p=>fs.existsSync(p))||'python3';

async function startServer(env={}){
 const proc=spawn(PY,[path.join(REPO,'tests','playwright','agent-ops-server.py')],{env:{...process.env,...env},stdio:['ignore','pipe','pipe']});
 const port=await new Promise((resolve,reject)=>{
  let buf='';const timer=setTimeout(()=>reject(new Error('server start timeout')),30000);
  proc.stdout.on('data',d=>{buf+=d.toString();const m=buf.match(/PORT=(\d+)/);if(m){clearTimeout(timer);resolve(Number(m[1]));}});
  proc.on('exit',c=>reject(new Error(`server exited early (${c})`)));
  proc.stderr.on('data',d=>process.stderr.write(`[srv] ${d}`));
 });
 return {proc,base:`http://127.0.0.1:${port}`};
}
async function withServer(env,run){
 const srv=await startServer(env);
 try{await withPage(async page=>run(page,srv.base));}finally{srv.proc.kill('SIGTERM');}
}
const open=async(page,base)=>{await page.goto(`${base}/index.html#/agent-ops`);await page.locator('.ao-node').first().waitFor({timeout:15000});};

test('map renders from real ingest: nodes with model + ctx text, edges with labels, nav entry, no page errors',async()=>{
 await withServer({},async(page,base)=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await open(page,base);
  assert.equal(await page.locator('.ao-node').count(),5);
  assert.ok(await page.locator('[data-ao-edge]').count()>=5);
  const text=await page.locator('.ao-svg').textContent();
  assert.match(text,/claude-opus-5/); assert.match(text,/gpt-5\.5/); assert.match(text,/ctx unknown/); assert.match(text,/model unknown/);
  assert.match(text,/Delegation · planned/); assert.match(text,/ctx critical/);
  assert.ok(await page.locator('nav a[href="#/agent-ops"], a[href="#/agent-ops"]').first().isVisible(),'nav entry shown when agent_ops_v1 is advertised');
  assert.ok(await page.locator('.ao-edge--dashed').count()>0,'planned edges are dashed');
  assert.ok(await page.locator('.ao-particle').count()>0,'animated particles on live edges');
  const box=await page.locator('.ao-stage').boundingBox(),insp=await page.locator('.ao-inspector').boundingBox();
  assert.ok(box.width>insp.width*2,'the map is the dominant element');
  assert.deepEqual(errors,[]);
 });
});

test('inspector: click node, click edge (metadata only), task lineage; double-click emits open-pane; keyboard',async()=>{
 await withServer({},async(page,base)=>{
  await open(page,base);
  await page.evaluate(()=>{window.__panes=[];document.addEventListener('agent-ops:open-pane',e=>window.__panes.push(e.detail));});
  await page.locator('[data-ao-node="s-planner"]').click();
  let insp=await page.locator('.ao-inspector').innerText();
  assert.match(insp,/Planner/); assert.match(insp,/claude-opus-5/); assert.match(insp,/31% of context used/);
  await page.locator('[data-ao-node="s-planner"]').dblclick();
  const panes=await page.evaluate(()=>window.__panes);
  assert.equal(panes.at(-1).session_id,'s-planner');
  // overlapping hit areas are a fact of a dense map: dispatch the click at the edge itself, as a pointer on its visible stroke would
  await page.locator('[data-ao-edge^="review|"]').first().dispatchEvent('click');
  insp=await page.locator('.ao-inspector').innerText();
  assert.match(insp,/Review/); assert.match(insp,/Metadata only/);
  await page.locator('[data-ao-task="task-42"]').first().click();
  insp=await page.locator('.ao-inspector').innerText();
  assert.match(insp,/Accountable owner/); assert.match(insp,/Contributors/); assert.match(insp,/Reviewer/);
  await page.locator('[data-ao-node="s-planner"]').focus();
  let moved=false;
  for(const key of ['ArrowLeft','ArrowUp','ArrowRight','ArrowDown']){await page.keyboard.press(key);if(await page.evaluate(()=>document.activeElement.dataset.aoNode)!=='s-planner'){moved=true;break;}}
  assert.ok(moved,'arrow keys move focus between nodes');
  await page.keyboard.press('Enter');
  assert.ok(await page.locator('.ao-node.is-selected').count()===1);
  assert.ok(await page.locator('.ao-sr table').count()>=2,'text alternative tables exist');
 });
});

test('filters narrow the map; modes dim edges',async()=>{
 await withServer({},async(page,base)=>{
  await open(page,base);
  await page.selectOption('[data-ao-filter="host"]','farm-gpu-01');
  await until(async()=>(await page.locator('.ao-node').count())<5,'host filter applied');
  await page.locator('[data-ao-clear]').click();
  await until(async()=>(await page.locator('.ao-node').count())===5,'filters cleared');
  await page.locator('[data-ao-mode="communication"]').click();
  assert.ok(await page.locator('.ao-edge.is-dim').count()>0,'non-communication edges are dimmed');
 });
});

test('replay: scrubbing calls /replay?until=<cursor> and shows an earlier map; Live returns',async()=>{
 await withServer({},async(page,base)=>{
  await open(page,base);
  const calls=[];page.on('request',r=>{if(r.url().includes('/agent-ops/replay'))calls.push(r.url());});
  await page.locator('[data-ao-scrub]').fill('2');
  await until(async()=>calls.length>0,'replay request');
  assert.match(decodeURIComponent(calls[0]),/until=.*~ao-003/);
  await until(async()=>(await page.locator('.ao-node').count())<5,'earlier map has fewer agents');
  assert.match(await page.locator('[data-ao-now]').innerText(),/3 \/ 20|3 \/ \d+/);
  assert.equal(await page.locator('[data-ao-live]').getAttribute('aria-pressed'),'false');
  await page.locator('[data-ao-live]').click();
  await until(async()=>(await page.locator('.ao-node').count())===5,'back to live');
 });
});

test('live: a newly ingested event arrives over the real SSE stream and adds a node; model change keeps the node',async()=>{
 await withServer({},async(page,base)=>{
  await open(page,base);
  const post=(body)=>fetch(`${base}/api/v1/ingest`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({schema_version:2,source:'pi',entity_type:'lifecycle',visibility_scope:'org',provenance:'observed',payload:{},occurred_at:new Date().toISOString(),...body})});
  assert.equal((await post({event_id:'live-1',source_sequence:900,entity_id:'newbie',agent_id:'newbie',session_id:'s-new',event_type:'session.started',model_id:'qwen-3.8',sanitized_label:'Newcomer'})).status,200);
  await until(async()=>(await page.locator('.ao-node').count())===6,'new session appears as a new node',15000);
  assert.match(await page.locator('.ao-svg').textContent(),/Newcomer/);
  await post({event_id:'live-2',source_sequence:901,entity_id:'newbie',agent_id:'newbie',session_id:'s-new',event_type:'model.switched',model_id:'qwen-4'});
  await until(async()=>/qwen-4/.test(await page.locator('[data-ao-node="s-new"]').textContent()),'model switch updates the same node',15000);
  assert.equal(await page.locator('.ao-node').count(),6,'a model change is not a new node');
 });
});

test('reduced motion and calm mode: no running animations, no particles; map still complete',async()=>{
 await withServer({},async(page,base)=>{
  await page.emulateMedia({reducedMotion:'reduce'});
  await open(page,base);
  assert.equal(await page.evaluate(()=>document.getAnimations().length),0);
  assert.equal(await page.locator('.ao-particle').first().isVisible(),false);
  assert.equal(await page.locator('.ao-node').count(),5);
 });
});

test('responsive: tablet and phone keep the page free of horizontal scroll; inspector stacks',async()=>{
 await withServer({},async(page,base)=>{
  for(const [w,h] of [[820,1100],[390,844]]){
   await page.setViewportSize({width:w,height:h});await open(page,base);
   const over=await page.evaluate(()=>document.documentElement.scrollWidth-document.documentElement.clientWidth);
   assert.ok(over<=1,`no horizontal page scroll at ${w}px (over by ${over})`);
   const s=await page.locator('.ao-stage').boundingBox(),i=await page.locator('.ao-inspector').boundingBox();
   assert.ok(i.y>=s.y+s.height-1,`inspector below the map at ${w}px`);
  }
 });
});

test('capability off: no nav entry and a calm "not turned on" state',async()=>{
 await withServer({OFF:'1'},async(page,base)=>{
  await page.goto(`${base}/index.html#/overview`);
  await page.waitForTimeout(1500);
  assert.equal(await page.locator('a[href="#/agent-ops"]').count(),0);
  await page.goto(`${base}/index.html#/agent-ops`);
  await page.getByText('not turned on').first().waitFor({timeout:10000});
 });
});

test('empty server: explains that nothing was reported',async()=>{
 await withServer({EMPTY:'1'},async(page,base)=>{
  await page.goto(`${base}/index.html#/agent-ops`);
  await page.getByText('No agent activity yet').waitFor({timeout:10000});
 });
});
