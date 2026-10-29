import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright';
import test from 'node:test';
import path from 'node:path';
import assert from 'node:assert/strict';
const hf=async p=>{await p.locator('[data-health-filters]').waitFor({state:'attached',timeout:15000});await p.evaluate(()=>{const d=document.querySelector('[data-health-filters]');if(d)d.open=true;});};  // the Health filter bar is a closed disclosure by default
const root=path.resolve(import.meta.dirname,'../..');

test('CI health controller applies route search and preserves lazy drilldown through refresh',async()=>{
let calls=0;
const history={available:true,status:'ready',repos:[{id:1,repository:'org/app',provider_url:'https://ci.example',default_branch:'main'}],pipelines:[{provider_repo_id:1,repository:'org/app',provider_url:'https://ci.example',run_number:4,branch:'main',state:'failure',created_at:new Date().toISOString()}],coverage:{1:{exhausted:true}}};
const server=createServer(async(req,res)=>{try{if(req.url==='/history'){calls++;res.setHeader('content-type','application/json');res.end(JSON.stringify(history));}else if(req.url==='/'){res.setHeader('content-type','text/html');res.end(`<main id="health"></main><script type="module">import {mountCiHealth} from '/packages/ci-health-view.js';localStorage.setItem('herdr.ci-health.v1:[\"\",\"\",\"\"]',JSON.stringify({search:'wrong persisted search'}));window.health=mountCiHealth(document.querySelector('main'),{getCiHistory:({signal})=>fetch('/history',{signal}).then(r=>r.json())},{search:'org/app',onSelectRun:r=>window.selected=r,onSelectStep:(r,id)=>window.step=id});</script>`);}else{res.setHeader('content-type','text/javascript');res.end(await readFile(root+req.url));}}catch(e){res.statusCode=500;res.end(String(e));}});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const browser=await chromium.launch({headless:true});
try{const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(`http://127.0.0.1:${server.address().port}/`);await page.locator('[data-health-bucket]').first().waitFor();await hf(page);assert.equal(await page.locator('[data-health-search]').inputValue(),'org/app');assert.equal(await page.locator('[data-health-mode=hours]').getAttribute('aria-pressed'),'true');await hf(page);await page.locator('[data-health-mode=runs]').click();let cells=page.locator('[data-health-bucket]');await cells.last().click();assert.equal(await page.locator('[data-health-clear]').count(),1);await hf(page);await page.locator('[data-health-search]').fill('org/app ');await page.locator('[data-health-tree] [data-health-disclosure] summary').first().click();assert.equal(await page.locator('[data-health-tree] [data-health-disclosure]').first().evaluate(e=>e.open),true,'first tree click after editing search must expand');await hf(page);await page.locator('[data-health-branch]').fill('main');await page.locator('[data-health-tree] [data-health-disclosure] [data-health-disclosure] summary').first().click();assert.equal(await page.locator('[data-health-tree] [data-health-disclosure] [data-health-disclosure]').first().evaluate(e=>e.open),true,'first group click after editing branch must expand');await page.locator('[data-health-run]').click();assert.equal(await page.evaluate(()=>selected.run_number),4);await page.evaluate(()=>health.selectedRun({...selected,tasks:[{id:3,name:'test',workflow:'CI',state:'failure'}]}));assert.equal(await page.locator('[data-health-run]').getAttribute('aria-current'),'true');await page.locator('.ci-health-workflows summary').click();await page.locator('[data-health-step]').click();assert.equal(await page.evaluate(()=>window.step),'3');await page.evaluate(()=>health.refresh());await page.waitForTimeout(100);assert.equal(await page.locator('.ci-health-workflows details').getAttribute('open'),'');await cells.last().click();await page.locator('[data-health-clear]').click();await cells.first().focus();await page.keyboard.press('ArrowRight');assert.equal(await cells.nth(1).evaluate(n=>n===document.activeElement),true);await page.evaluate(()=>health.refresh());await page.waitForTimeout(100);assert.equal(await cells.nth(1).evaluate(n=>n===document.activeElement),true);history.available=false;history.stale=true;history.status='auth_error';history.error='Woodpecker rejected the credential (HTTP 401).';history.coverage={};await page.evaluate(()=>health.refresh());await page.waitForFunction(()=>document.querySelector('[data-health-status]')?.textContent.includes('HTTP 401'));assert.ok(await page.locator('[data-health-bucket]').count()>0,'provider outage should retain the stale heatmap');await page.evaluate(()=>health.dispose());const before=calls;await page.waitForTimeout(5200);assert.equal(calls,before);assert.deepEqual(errors,[]);}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}

});

test('CI health keeps exact retained evidence, focus and selection during pending and failed refreshes',async()=>{
 const history={available:true,status:'ready',repos:[{id:1,repository:'org/shared',provider_url:'https://ci.alpha.example',default_branch:'main'},{id:2,repository:'org/shared',provider_url:'https://ci.beta.example',default_branch:'main'}],pipelines:[{provider_repo_id:1,repository:'org/shared',provider_url:'https://ci.alpha.example',run_number:17,branch:'main',state:'failure',created_at:new Date().toISOString()}],coverage:{1:{exhausted:true},2:{exhausted:true}}};
 let calls=0,pendingResponse,requestStarted;
 const nextRequest=new Promise(resolve=>requestStarted=resolve);
 const server=createServer(async(req,res)=>{
  try {
   if(req.url==='/history') {
    calls++;
    if(calls===2){pendingResponse=res;requestStarted();return;}
    res.setHeader('content-type','application/json');
    res.end(JSON.stringify(calls===1?history:{...history,status:'partial',stale:true,refreshing:true,coverage:{1:{error:'Older history unavailable'},2:{exhausted:true}}}));
   }else if(req.url==='/') {
    res.setHeader('content-type','text/html');
    res.end(`<main></main><script type="module">import {mountCiHealth} from '/packages/ci-health-view.js';window.health=mountCiHealth(document.querySelector('main'),{getCiHistory:async({signal})=>{const response=await fetch('/history',{signal});if(!response.ok)throw new Error('History HTTP '+response.status);return response.json();}},{onSelectRun:run=>window.selected=run});</script>`);
   }else {res.setHeader('content-type','text/javascript');res.end(await readFile(root+req.url));}
  }catch(error){res.statusCode=500;res.end(String(error));}
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();
  // Keep the controller's five-second background poll from racing the explicit
  // refresh whose pending and failure states this test observes.
  const frozenAt=new Date();
  await page.clock.install({time:frozenAt});
  await page.clock.pauseAt(frozenAt);
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.locator('[data-health-bucket]').first().waitFor();
  assert.deepEqual(await page.locator('th[scope=row].ci-health-project').allTextContents(),['org/shared · alpha.example','org/shared · beta.example']);
  await hf(page);
  await page.locator('[data-health-mode=runs]').click();
  const cell=page.locator('[data-health-bucket]').nth(23);
  await cell.click();
  await page.locator('[data-health-tree] [data-health-disclosure] summary').first().click();
  await page.locator('[data-health-tree] [data-health-disclosure] [data-health-disclosure] summary').first().click();
  await page.locator('[data-health-run]').click();
  assert.equal(await page.evaluate(()=>selected.run_number),17);
  const evidence=await page.locator('[data-health-map]').innerHTML();
  await cell.focus();
  await page.evaluate(()=>{window.refreshDone=false;window.refreshPromise=health.refresh().then(()=>window.refreshDone=true);});
  await nextRequest;
  assert.equal(await page.evaluate(()=>window.refreshDone),false);
  assert.equal(await page.locator('[data-health-map]').innerHTML(),evidence,'pending refresh must retain the exact heatmap');
  assert.equal(await cell.evaluate(node=>node===document.activeElement),true);
  assert.equal(await cell.getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('[data-health-run]').getAttribute('aria-current'),'true');
  pendingResponse.writeHead(503);pendingResponse.end('Temporarily unavailable');
  await page.evaluate(()=>window.refreshPromise);
  assert.match(await page.locator('[data-health-status]').textContent(),/History HTTP 503.*Showing the last received snapshot/);
  assert.equal(await page.locator('[data-health-bucket]').count(),48);
  assert.equal(await cell.getAttribute('aria-pressed'),'true');
  assert.equal(await cell.evaluate(node=>node===document.activeElement),true);
  assert.equal(await page.locator('[data-health-run]').getAttribute('aria-current'),'true');
  assert.match(await page.locator('[data-health-map] .ci-health-note').textContent(),/0 successful \/ 1 successful or failed runs.*Stale snapshot/);
  assert.equal(await page.locator('[data-health-map] .state-failure').count(),1);
  await page.evaluate(()=>health.refresh());
  assert.match(await page.locator('[data-health-status]').textContent(),/Stale history.*Refreshing history.*Partial history/);
  assert.match(await page.locator('[data-health-map]').textContent(),/Older history unavailable/);
  assert.equal(await cell.getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('[data-health-run]').getAttribute('aria-current'),'true');
  assert.doesNotMatch(await page.locator('[data-health-map]').textContent(),/No projects match/);
  await page.evaluate(()=>health.dispose());
 }finally {
  pendingResponse?.end();
  await browser.close();
  await new Promise(resolve=>server.close(resolve));
 }
});
