// Deployments page (deploy-4 Task 4) against a real control plane: rows grouped by stage, the coordinator's plan, the
// drill-down timeline, hostile host evidence rendered inert, and the no-definitions state.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import path from 'node:path';
import {chromium} from 'playwright';
import {cachedChromiums,withPage} from './helpers/fixture.mjs';

const root=path.resolve(import.meta.dirname,'../..');
const HOSTILE='<img src=x onerror="window.__pwned=1">boom';
// Waits name their step, so a timeout says which one (the page loads under a real server and a real browser).
const WAIT=30000;
async function step(name,fn){try{return await fn();}catch(error){error.message=`step "${name}": ${error.message}`;throw error;}}

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
  const failed=[];page.on('response',r=>{if(r.status()>=400)failed.push(`${r.status()} ${r.url()}`);});
  try{await run(page,`http://127.0.0.1:${port}`);}
  catch(error){
   // Say what the page showed when a step failed, not just that it timed out.
   const shown=await page.evaluate(()=>document.getElementById('view')?.innerText.slice(0,600)).catch(()=>'(page gone)');
   error.message+=`\n#view: ${JSON.stringify(shown)}\nfailed responses: ${failed.join(', ')||'none'}\nserver stderr: ${stderr.slice(-800)}`;
   throw error;
  }
  assert.deepEqual(errors,[]);
 }finally{await browser?.close();server.kill();}
}

test('rows are grouped by stage with the plan, a drill-down timeline, and hostile evidence stays text',async()=>withServer('defined',async(page,base)=>{
 await step('open the page',()=>page.goto(`${base}/#/deployments`));
 await step('rows loaded',()=>page.locator('[data-deploy-stage="Stage 2"] [data-deploy-row="whiteale-gateway"]').waitFor({timeout:WAIT}));
 // A tab of the CI section, not a sidebar link.
 await step('CI section tab strip',()=>page.locator('#subnav:not([hidden]) a[aria-current=page]',{hasText:'Deployments'}).waitFor({timeout:WAIT}));
 assert.equal(await page.locator('herdr-app-shell a.hd-shell__link.active').textContent(),'CI');
 assert.equal(await page.locator('herdr-app-shell a.hd-shell__link',{hasText:'Deployments'}).count(),0);
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
 // Task 8 drill-down: the escalation with its repair rounds and the stored host report (an unknown failure is a host
 // finding with no automatic repair; the report keeps the redacted evidence as text).
 const escalation=timeline.locator('[data-deploy-escalation]');
 assert.equal(await escalation.count(),1);
 assert.match(await escalation.locator('[data-deploy-repair]').innerText(),/round 1: host \(fallback\) → none/);
 assert.match(await escalation.innerText(),/issue: none/);
 assert.equal(await escalation.locator('[data-deploy-report-evidence]').innerText(),HOSTILE);
 assert.equal(await escalation.locator('[data-deploy-report-source]').innerText(),'fallback');
 assert.equal(await escalation.locator('[data-deploy-report-link]').getAttribute('href'),'#'+await escalation.locator('[data-deploy-report]').getAttribute('id'));
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

test('without definitions the tab shows its empty state with the config hint',async()=>withServer('empty',async(page,base)=>{
 await step('open the page',()=>page.goto(`${base}/#/deployments`));
 await step('empty state',()=>page.locator('[data-deploy-empty]').waitFor({timeout:WAIT}));
 assert.match(await page.locator('#deployments').innerText(),/control_plane\.deployment_definitions/);
 assert.equal(await page.locator('[data-deploy-stage]').count(),0);
 // The tab is still listed under CI (it is always there; the page explains what is missing).
 await step('CI section tab strip',()=>page.locator('#subnav:not([hidden]) a[aria-current=page]',{hasText:'Deployments'}).waitFor({timeout:WAIT}));
 assert.equal(await page.locator('herdr-app-shell a.hd-shell__link',{hasText:'Deployments'}).count(),0);
 assert.notEqual(await page.locator('#view herdr-freshness').getAttribute('data-state'),'error');
}));

// Render level: every string the server can send (definition errors and warning, Hold/Halt reasons, track names, component
// names, commits, steps, hosts, instances, evidence) carries markup and an attribute breakout. All of it stays text. A
// prototype key ('__proto__' as an own JSON key, 'constructor' with no row) is looked up with Object.hasOwn: the own row renders
// (escaped), the missing one renders empty instead of reading the prototype chain.
test('hostile strings in every server field render inert',async()=>withPage(async(page,base)=>{
 await page.goto(`${base}/__blank`);
 const result=await page.evaluate(async()=>{
  const {renderDeployments}=await import('/deployments-view.js');
  const H=(k)=>`" onfocus="window.__pwned='${k}'" x="'><img src=x onerror="window.__pwned='${k}'"><b class="canary">${k}</b>`;
  const event={outcome:H('outcome'),step:H('step'),from:H('from'),to:H('to'),received_at:H('received'),host_ts:H('host_ts'),reported_by:H('reported_by'),host:H('host'),instance:H('instance'),evidence:H('evidence')};
  const raw=JSON.parse(JSON.stringify({
   components:{[H('component')]:{running:H('running'),target:H('target'),step:H('step'),health:H('health'),last_result:{outcome:H('lr'),received_at:H('lr_at')},timeline:[event]},__proto__X:{step:H('proto_step'),timeline:[]}},
   definitions:{configured:true,warning:H('warning'),errors:[H('error1'),H('error2')],groups:[{stage:H('stage'),order:0,components:[H('component'),'__proto__','constructor']}]},
   plans:[{track:{repo:H('repo'),branch:H('branch'),ci_context:H('ctx')},generation:H('gen'),plan_commit:H('plan'),target_commit:H('tc'),
    current:{type:'Halt',stage:H('hstage'),reason:H('halt_reason')},permitted:[H('permitted')],missing:[H('missing')],
    actions:[{type:'Hold',reason:H('hold_reason')}],components:{[H('component')]:{state:H('state'),outcome:H('coutcome'),attempts:H('attempts'),hold_remaining_s:12}}},
   {track:{repo:'r',branch:'b'},current:{type:'Hold',stage:1,reason:H('hold_reason'),remaining_s:5},components:{}}],
   coordinator:{error:H('tick_error'),stale:true,evidence:'unknown'},
   escalations:[{component:H('component'),release:H('release'),open:true,pr:H('pr'),
    issue:{state:H('issue_state'),number:H('issue_number'),url:'javascript:alert(1)',reason:H('issue_reason')},
    assistant:{state:H('assistant_state')},
    rounds:[{index:H('index'),attempt:H('attempt'),finding:{kind:H('kind'),source:'model',summary:H('esc_summary')},repair:{action:H('action'),reason:H('esc_reason'),rollback_to:H('rollback_to')}}],
    report:{finding_source:'model',what_changed:H('what_changed'),current_state:H('current_state'),tried:[H('tried')],next_step:H('next_step'),evidence:H('report_evidence')}}]}).replace('"__proto__X"','"__proto__"'));
  const root=document.createElement('div');document.body.append(root);
  Object.prototype.step='POLLUTED';   // a polluted prototype must not reach a row with no own data
  try{root.innerHTML=renderDeployments(raw)(new Set([raw.definitions.groups[0].components[0]]));}finally{delete Object.prototype.step;}
  for(const el of root.querySelectorAll('[hidden]'))el.hidden=false;
  for(const el of root.querySelectorAll('*'))el.dispatchEvent(new FocusEvent('focus'));
  await new Promise(r=>setTimeout(r,100));
  const rows=[...root.querySelectorAll('[data-deploy-row]')].map(r=>r.getAttribute('data-deploy-row'));
  const source=root.querySelector('[data-deploy-report-source]')?.innerText;
  return {source,jslinks:root.querySelectorAll('a[href^="javascript"]').length,imgs:root.querySelectorAll('img').length,canaries:root.querySelectorAll('.canary').length,handlers:root.querySelectorAll('[onfocus],[onerror]').length,
   pwned:window.__pwned,text:root.textContent,rows,proto:root.querySelector('[data-deploy-row="__proto__"]')?.innerText,ctor:root.querySelector('[data-deploy-row="constructor"]')?.innerText};
 });
 assert.match(result.source,/model output/);assert.equal(result.jslinks,0);assert.equal(result.imgs,0);assert.equal(result.canaries,0);assert.equal(result.handlers,0);assert.equal(result.pwned,undefined);
 for(const k of ['warning','error1','error2','halt_reason','hold_reason','repo','branch','component','evidence','tick_error','missing','instance','reported_by',
  'esc_summary','esc_reason','issue_reason','what_changed','tried','next_step','report_evidence','pr'])
  assert.ok(result.text.includes(`<b class="canary">${k}</b>`),`${k} shown as text`);
 assert.deepEqual(result.rows.slice(1),['__proto__','constructor']);
 assert.ok(result.proto.includes('<b class="canary">proto_step</b>'),'the own __proto__ row renders as text');
 assert.deepEqual(result.ctor.split('\t'),['constructor','—','—','—','unknown','—','—','—'],'a key with no own row renders empty');
}));
