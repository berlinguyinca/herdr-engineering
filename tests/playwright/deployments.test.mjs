// Deployments page (deploy-4 Task 4) against a real control plane: rows grouped by stage, the coordinator's plan, the
// drill-down timeline, hostile host evidence rendered inert, and the no-definitions state.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import path from 'node:path';
import {chromium} from 'playwright';
import {cachedChromiums} from './helpers/fixture.mjs';

const root=path.resolve(import.meta.dirname,'../..');
const HOSTILE='<img src=x onerror="window.__pwned=1">boom';

async function launch(){
 try{return await chromium.launch({headless:true});}
 catch(error){for(const executablePath of cachedChromiums()){try{return await chromium.launch({headless:true,executablePath});}catch{/* next */}}throw error;}
}

async function withServer(mode,run){
 const server=spawn(process.env.PYTHON||'python3',[path.join(root,'tests/playwright/deployments-server.py'),mode],{cwd:root,stdio:['ignore','pipe','pipe']});
 let stderr='';server.stderr.on('data',d=>stderr+=d);
 let browser;
 try{
  const port=await new Promise((resolve,reject)=>{let out='';const t=setTimeout(()=>reject(new Error(stderr||'server timeout')),25000);
   server.stdout.on('data',d=>{out+=d;const m=out.match(/PORT=(\d+)/);if(m){clearTimeout(t);resolve(m[1]);}});
   server.once('exit',code=>{clearTimeout(t);reject(new Error(`server exit ${code}: ${stderr}`));});});
  browser=await launch();
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await run(page,`http://127.0.0.1:${port}`);
  assert.deepEqual(errors,[]);
 }finally{await browser?.close();server.kill();}
}

test('rows are grouped by stage with the plan, a drill-down timeline, and hostile evidence stays text',async()=>withServer('defined',async(page,base)=>{
 await page.goto(`${base}/#/deployments`);
 await page.locator('[data-deploy-row="hive-gateway"]').waitFor({timeout:15000});
 // Advertised by /api/v1/health once definitions are configured: the sidebar lists the page.
 await page.locator('herdr-app-shell a.hd-shell__link',{hasText:'Deployments'}).waitFor({timeout:10000});
 // Grouped by stage, in roll order.
 assert.deepEqual(await page.locator('[data-deploy-stage]').evaluateAll(s=>s.map(e=>e.getAttribute('data-deploy-stage'))),['Stage 1','Stage 2']);
 assert.deepEqual(await page.locator('[data-deploy-stage="Stage 1"] [data-deploy-row]').evaluateAll(r=>r.map(e=>e.getAttribute('data-deploy-row'))),['hive-gateway','hive-nodes']);
 assert.deepEqual(await page.locator('[data-deploy-stage="Stage 2"] [data-deploy-row]').evaluateAll(r=>r.map(e=>e.getAttribute('data-deploy-row'))),['whiteale-gateway']);
 // Per component: running, target, step, health, last result, hold left.
 const gw=page.locator('[data-deploy-row="hive-gateway"]');
 assert.match(await gw.innerText(),/c0ffee111111/);
 assert.match(await gw.locator('[data-deploy-health]').innerText(),/healthy/);
 assert.match(await gw.locator('[data-deploy-hold]').innerText(),/^\d+m \d+s$/);
 assert.match(await page.locator('[data-deploy-row="hive-nodes"] [data-deploy-health]').innerText(),/failed/);
 assert.match(await page.locator('[data-deploy-row="whiteale-gateway"] [data-deploy-health]').innerText(),/pending/);
 // The coordinator's current action: stage 1 halted, only the earlier stages (none) permitted.
 assert.match(await page.locator('[data-deploy-current]').innerText(),/Halt stage 1: stage 1 halted: hive-nodes: failed/);
 assert.match(await page.locator('[data-deploy-plan]').innerText(),/Permitted now: nothing/);
 // Drill-down: the timeline is hidden until asked for, then shows every event, newest first.
 const timeline=page.locator('[data-deploy-timeline="hive-nodes"]');
 assert.equal(await timeline.isHidden(),true);
 await page.locator('[data-deploy-toggle="hive-nodes"]').click();
 assert.equal(await timeline.isVisible(),true);
 assert.equal(await page.locator('[data-deploy-toggle="hive-nodes"]').getAttribute('aria-expanded'),'true');
 assert.equal(await timeline.locator('[data-deploy-event]').count(),2);
 assert.match(await timeline.locator('[data-deploy-event]').first().innerText(),/failed/);
 // Hostile evidence: the exact string as text, no element made from it, nothing executed.
 assert.equal(await timeline.locator('[data-deploy-evidence]').innerText(),HOSTILE);
 assert.equal(await page.locator('#view img').count(),0);
 assert.equal(await page.evaluate(()=>window.__pwned),undefined);
 // Toggling again closes it; another component's timeline opens on its own.
 await page.locator('[data-deploy-toggle="hive-nodes"]').click();
 assert.equal(await timeline.isHidden(),true);
 await page.locator('[data-deploy-toggle="hive-gateway"]').click();
 assert.match(await page.locator('[data-deploy-timeline="hive-gateway"]').innerText(),/probe ok: 3 models answered/);
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
}));

test('without definitions the page shows its empty state and the sidebar does not list it',async()=>withServer('empty',async(page,base)=>{
 await page.goto(`${base}/#/deployments`);
 await page.locator('[data-deploy-empty]').waitFor({timeout:15000});
 assert.match(await page.locator('#deployments').innerText(),/control_plane\.deployment_definitions/);
 assert.equal(await page.locator('[data-deploy-stage]').count(),0);
 await page.locator('herdr-app-shell a.hd-shell__link',{hasText:'CI'}).waitFor({timeout:10000});
 assert.equal(await page.locator('herdr-app-shell a.hd-shell__link',{hasText:'Deployments'}).count(),0);
 assert.notEqual(await page.locator('#view herdr-freshness').getAttribute('data-state'),'error');
}));
