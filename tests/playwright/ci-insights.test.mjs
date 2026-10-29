import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {existsSync,mkdirSync} from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';
const hf=async p=>{await p.locator('[data-health-filters]').waitFor({state:'attached',timeout:15000});await p.evaluate(()=>{const d=document.querySelector('[data-health-filters]');if(d)d.open=true;});};  // the Health filter bar is a closed disclosure by default
const root=path.resolve(import.meta.dirname,'../..');

test('CI reports publish over HTTP and drive history, pipeline logs and responsive layout',async()=>{
 const server=spawn(process.env.PYTHON||'python3',[path.join(root,'tests/playwright/ci-insights-server.py')],{cwd:root,stdio:['ignore','pipe','pipe']});
 let browser;let stderr='';server.stderr.on('data',d=>stderr+=d);
 try {
  const port=await new Promise((resolve,reject)=>{let text='';const timeout=setTimeout(()=>reject(new Error(`CI server startup timeout: ${stderr}`)),20000);server.stdout.on('data',d=>{text+=d;const match=text.match(/PORT=(\d+)/);if(match){clearTimeout(timeout);resolve(match[1]);}});server.once('exit',code=>{clearTimeout(timeout);reject(new Error(`CI server exited ${code}: ${stderr}`));});});
  const base=`http://127.0.0.1:${port}`;
  for(const [n,total,bad,branch] of [[1,2,false,'main'],[2,3,true,'main'],[3,4,true,'main'],[4,10,false,'feature']]) {
   const xml=`<testsuite name="api">${Array.from({length:total},(_,i)=>`<testcase classname="api" name="test_${i}" time="${i+1}">${bad&&i===0?'<failure message="expected 200">AssertionError &lt;unsafe&gt;</failure>':''}</testcase>`).join('')}</testsuite>`;
   const response=await fetch(`${base}/api/v1/ci/reports`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repository:'org/app',provider_repo_id:n===3?undefined:17,run_number:n,revision:'a'.repeat(40),branch,reported_at:new Date(Date.now()-(4-n)*86400000).toISOString(),xml})});
   assert.equal(response.status,200);const report=await response.json();assert.equal(report.summary.total,total);
  }
  const options={headless:true,args:['--no-sandbox']};
  if(process.env.PLAYWRIGHT_CHROMIUM)options.executablePath=process.env.PLAYWRIGHT_CHROMIUM;
  try{browser=await chromium.launch(options);}catch(error){const exe='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';if(!existsSync(exe))throw error;browser=await chromium.launch({...options,executablePath:exe});}
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[],reportReads=[];page.on('pageerror',e=>errors.push(e.message));
  page.on('request',request=>{const url=new URL(request.url());if(request.method()==='GET'&&url.pathname==='/api/v1/ci/reports')reportReads.push(url);});
  await page.goto(`${base}/#/ci`);
  assert.equal(await page.locator('[data-ci-workspace="health"]').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('.ci-report-filters').isVisible(),false,'Health owns its own history filters');
  assert.equal(await page.locator('[data-ci-workspace="health"]').getAttribute('aria-controls'),'ci-workspace-health');
  await page.locator('[data-ci-workspace="health"]').focus();await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('[data-ci-workspace="runs"]').getAttribute('aria-selected'),'true');
  assert.equal(await page.evaluate(()=>document.activeElement?.dataset.ciWorkspace),'runs');
  await page.locator('[data-ci-workspace="tests"]').click();
  const insights=page.locator('[data-ci-insights]');
  await page.waitForFunction(()=>document.querySelector('[data-ci-insights]')?.textContent.includes('4 test report attempts'));
  const projectOption=await page.locator('[data-ci-repository] option').filter({hasText:'org/app'}).first().getAttribute('value');
  await page.locator('[data-ci-repository]').selectOption(projectOption);
  await page.locator('[data-ci-branch]').selectOption('main');
  assert.match(await insights.innerText(),/3 retained runs · 3 test report attempts/);
  assert.match(await insights.innerText(),/33.3%/);
  assert.match(await insights.innerText(),/Select a project.*test history/);
  const testHistory=page.locator('[data-ob-tests]');
  await testHistory.locator('tbody tr').first().waitFor();
  assert.match(await testHistory.innerText(),/2 \/ 3 reported completed test attempts/);
  assert.match(await testHistory.innerText(),/First failure \/ last pass/);
  assert.equal(await insights.locator('svg[role="img"]').count(),1);
  await page.locator('[data-ci-period]').selectOption('7');
  assert.match(await insights.innerText(),/3 retained runs/);
  mkdirSync(path.join(root,'artifacts/ci-insights'),{recursive:true});
  await page.screenshot({path:path.join(root,'artifacts/ci-insights/overview.png'),fullPage:true});
  await page.locator('[data-ci-workspace="runs"]').click();
  await page.getByRole('tab',{name:/^Failed/}).click();
  await page.getByRole('button',{name:'Run #3',exact:true}).click();
  const inspector=page.locator('.ig-inspector');
  await inspector.locator('.ci-pipeline-step[data-step="12"]').waitFor();
  await page.locator('[data-ci-workspace="tests"]').click();
  assert.equal(await insights.isVisible(),true);
  await page.locator('[data-ci-workspace="health"]').click();
  assert.equal(await inspector.locator('.ci-pipeline-step[data-step="12"]').evaluate(e=>getComputedStyle(e).borderLeftColor),'rgb(220, 53, 69)');
  await inspector.getByRole('heading',{name:'Test results · 4 tests',exact:true}).waitFor();
  assert.ok(reportReads.some(url=>url.searchParams.get('compact')==='1'));
  assert.ok(reportReads.some(url=>url.searchParams.get('run_number')==='3'));
  assert.ok(reportReads.every(url=>url.searchParams.get('compact')==='1'||['provider_url','provider_repo_id','repository','run_number'].every(key=>url.searchParams.has(key))));
  const box=await inspector.boundingBox();assert.ok(box.width>900,`Pipeline inspector too narrow: ${box.width}`);
  await inspector.locator('.ci-pipeline-step[data-step="12"]').focus();await page.keyboard.press('Enter');
  await page.waitForFunction(()=>document.querySelector('.ig-log[aria-label="Step output"]')?.textContent.includes('expected 200, got 500'));
  assert.equal(await inspector.locator('.ci-pipeline-step[data-step="12"]').getAttribute('aria-pressed'),'true');
  await inspector.locator('[data-collapse-success]').click();
  assert.equal(await inspector.locator('.ci-pipeline-success').count(),1);
  assert.equal(await inspector.locator('.ci-pipeline-step[data-step="12"]').isVisible(),true);
  await inspector.locator('.ci-pipeline-success summary').click();
  assert.equal(await inspector.locator('[data-step="11"]').isVisible(),true);
  await inspector.locator('[data-pipeline-zoom="in"]').click();
  await inspector.locator('[data-pipeline-zoom="in"]').click();
  assert.equal(await inspector.locator('[data-pipeline-zoom="in"]').isDisabled(),true);
  assert.match(await inspector.locator('[data-pipeline-zoom="reset"]').innerText(),/150%/);
  await inspector.locator('[data-pipeline-zoom="reset"]').click();
  const compare=inspector.locator('[data-ci-compare]');
  const compareKey=await compare.locator('option').filter({hasText:'Run #2'}).getAttribute('value');
  assert.ok(compareKey);await compare.selectOption(compareKey);
  assert.match(await inspector.locator('.ci-comparison').innerText(),/Active #3 compared with #2/);
  assert.match(await inspector.locator('.ci-comparison').innerText(),/Step details not reported/);
  assert.equal(await inspector.locator('.ci-pipeline-step[data-step="12"]').getAttribute('aria-pressed'),'true');
  assert.match(await inspector.locator('.ig-log[aria-label="Step output"]').innerText(),/expected 200, got 500/);
  await page.locator('[data-ci-workspace="runs"]').click();
  for(const width of [3840,2560,1920,768,390]){
   await page.setViewportSize({width,height:1000});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`Overflow at ${width}`);
   if(width>=1920){
    assert.equal(await inspector.evaluate(node=>getComputedStyle(node).position),'sticky');
    const rail=await page.locator('#ig-results').boundingBox(),detail=await inspector.boundingBox();assert.ok(rail.x+rail.width<=detail.x,`Run rail should sit beside inspector at ${width}`);
   }else if(width===390){assert.equal(await page.locator('#ig-results').isVisible(),false);assert.equal(await inspector.locator('.ig-back-label').isVisible(),true);}
  }
  await page.setViewportSize({width:1440,height:1000});
  await inspector.locator('[data-collapse-success]').click();
  await page.locator('[data-ci-workspace="health"]').click();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  mkdirSync(path.join(root,'artifacts/ci-insights'),{recursive:true});
  await page.screenshot({path:path.join(root,'artifacts/ci-insights/desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.equal(await inspector.locator('.ci-pipeline-edges').evaluate(e=>getComputedStyle(e).display),'none');
  await page.screenshot({path:path.join(root,'artifacts/ci-insights/mobile.png'),fullPage:true});
  const largeXml=`<testsuite name="api">${Array.from({length:120},(_,i)=>`<testcase name="case_${i}"/>`).join('')}</testsuite>`;
  const upload=await fetch(`${base}/api/v1/ci/reports`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repository:'org/app',provider_repo_id:17,run_number:3,branch:'main',revision:'a'.repeat(40),xml:largeXml})});
  assert.equal(upload.status,200);
  await page.waitForFunction(()=>document.querySelector('.ig-inspector')?.textContent.includes('Test results · 120 tests'),null,{timeout:10000});
  await inspector.locator('[data-ci-test-page="next"]').click();
  assert.match(await inspector.innerText(),/Page 2 of 3/);
  const testSearch=inspector.locator('[data-ci-test-search]');await testSearch.fill('case_119');
  assert.match(await inspector.innerText(),/1 matching tests/);
  assert.equal(await testSearch.evaluate(e=>e===document.activeElement),true);
  await page.waitForTimeout(5200);
  assert.match(await inspector.innerText(),/1 matching tests/);
  assert.equal(await testSearch.evaluate(e=>e===document.activeElement),true);
  await inspector.locator('[data-close-inspector]').click();
  await page.locator('[data-ci-workspace="tests"]').click();
  assert.equal(await insights.isVisible(),true);
  await page.locator('[data-ci-workspace="runs"]').click();
  const search=page.locator('[data-ig-search]');await search.fill('main');await search.focus();
  await page.waitForTimeout(5200);assert.equal(await search.evaluate(e=>e===document.activeElement),true);await search.fill('');
  await page.locator('[data-ci-workspace="tests"]').click();
  await page.locator('[data-ci-branch]').selectOption('feature');
  assert.match(await insights.innerText(),/1 retained runs · 1 test report attempts/);
  assert.deepEqual(errors,[]);
 } finally {await browser?.close();server.kill();}
});

// Task 6: real local HTTP API and opt-in retained-observation scale fixture.
// Each case owns a fresh browser context; no preferences leak between cases.
async function commandCenter(run,{invalidateAssetTransport=false,selectionContract=false,oldRun=false}={}) {
 const server=spawn(process.env.PYTHON||'python3',[path.join(root,'tests/playwright/ci-insights-server.py'),'--117-projects',...(selectionContract?['--selection-contract']:[]),...(oldRun?['--old-selection-run']:[])],{cwd:root,stdio:['ignore','pipe','pipe']});
 let browser,stderr='';server.stderr.on('data',d=>stderr+=d);
 try {
  const port=await new Promise((resolve,reject)=>{let output='';const timer=setTimeout(()=>reject(Error(`Fixture timeout: ${stderr}`)),20000);server.stdout.on('data',d=>{output+=d;const match=output.match(/PORT=(\d+)/);if(match){clearTimeout(timer);resolve(match[1]);}});server.once('exit',code=>{clearTimeout(timer);reject(Error(`Fixture exited ${code}: ${stderr}`));});});
  const base=`http://127.0.0.1:${port}`;
  const upload=await fetch(`${base}/api/v1/ci/reports`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repository:'org/app',provider_repo_id:17,run_number:3,revision:'a'.repeat(40),branch:'main',xml:'<testsuite name="api"><testcase classname="api" name="test_status" time="1"><failure message="expected 200">AssertionError &lt;unsafe&gt;</failure></testcase></testsuite>'})});
  assert.equal(upload.status,200);
  browser=await chromium.launch({headless:true,args:['--no-sandbox'],...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  // Fault injection stays below the asset transport boundary: no host networking changes.
  if(invalidateAssetTransport)await context.route(`${base}/**/*.js`,route=>route.abort('internetdisconnected'));
  const assetReads=[];
  // Loopback assets must not depend on Chromium's host network-change notifier.
  // Fetch real HTTP bytes once, retaining failures/status/headers; never retry or
  // substitute source files. API and document traffic keep their browser transport.
  await context.route(url=>url.origin===base&&/\.(?:js|css)$/.test(url.pathname),async route=>{
   assetReads.push(route.request().url());
   const response=await route.fetch({maxRetries:0,maxRedirects:0});
   try{await route.fulfill({response});}finally{await response.dispose();}
  });
  const page=await context.newPage(),errors=[],requests=[],failedRequests=[],httpErrors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('requestfailed',r=>failedRequests.push({url:r.url(),error:r.failure()?.errorText}));
  page.on('response',r=>{if(r.status()>=400)httpErrors.push({url:r.url(),status:r.status()});});
  page.on('request',r=>{if(r.url().includes('/api/v1/ci/'))requests.push(new URL(r.url()).pathname);});
  try {
   await page.goto(`${base}/#/ci`);
   await page.waitForFunction(()=>document.querySelectorAll('[data-ob-project]').length===117);
   await run({page,context,base,requests,assetReads});
  }
  catch(error){
   console.error('CI_BROWSER_FAILURE',JSON.stringify({errors,failedRequests,httpErrors,state:await page.evaluate(()=>({url:location.href,projects:document.querySelectorAll('[data-ob-project]').length,status:document.querySelector('[data-ob-status]')?.textContent,body:document.body.innerText.slice(0,1500)}))}));
   throw error;
  }
  assert.deepEqual(errors,[]);
 }finally{await browser?.close();server.kill();}
}
// A sample ends only after selected KPIs, charts and test history have all
// committed to the DOM and had a paint opportunity. Text from one fast request
// must not hide a slow/failed project summary or be canceled by the next sample.
async function selectedReady(page) {
 await page.waitForFunction(()=>{
  const status=document.querySelector('[data-ob-status]')?.textContent||'';
  return document.querySelector('[data-ob-project][aria-pressed="true"]') &&
   !/Refreshing|refresh failed|unavailable/i.test(status) &&
   document.querySelector('[data-ob-summary] .ci-ob-card') &&
   document.querySelector('[data-ob-charts] svg') &&
   document.querySelector('[data-ob-tests]')?.textContent.includes('test_status') &&
   !/Loading|refresh failed/i.test(document.querySelector('[data-ob-tests]').textContent);
 });
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
}
function evidenceResponses(page,status=200,{fleet=true}={}) {
 return Promise.all([
  ...(fleet?[page.waitForResponse(r=>{const u=new URL(r.url());return u.pathname==='/api/v1/ci/summary'&&!u.searchParams.has('provider_repo_id')&&r.status()===status;})]:[]),
  page.waitForResponse(r=>{const u=new URL(r.url());return u.pathname==='/api/v1/ci/summary'&&u.searchParams.get('provider_repo_id')==='17'&&r.status()===status;}),
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/ci/tests'&&r.status()===status),
 ]).then(responses=>Promise.all(responses.map(async r=>assert.equal(await r.finished(),null,`response completed: ${r.url()}`))));
}
async function selectApp(page) {
 await page.locator('[data-ob-search]').fill('org/app');
 assert.equal(await page.locator('[data-ob-project]').count(),1);
 const responses=evidenceResponses(page,200,{fleet:false});
 await page.locator('[data-ob-project]').focus();await page.keyboard.press('Enter');
 await responses;await selectedReady(page);
}
async function openFailure(page) {
 await page.locator('[data-ci-workspace="runs"]').click();
 await page.getByRole('tab',{name:/^Failed/}).click();
 await page.getByRole('button',{name:'Run #3',exact:true}).click();
 await page.locator('.ci-diagnosis article').waitFor();
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
}
const p95=values=>[...values].sort((a,b)=>a-b)[Math.ceil(values.length*.95)-1];

test('117-project summary and selection meet cached response and interaction budgets without eager details',()=>commandCenter(async({page,base,requests})=>{
 assert.equal(requests.filter(p=>/\/(pipeline|log)$/.test(p)).length,0);
 const summaryMs=[],bytes=[];
 for(let i=0;i<20;i++){
  const start=performance.now(),response=await fetch(`${base}/api/v1/ci/summary`),body=await response.text();
  summaryMs.push(performance.now()-start);bytes.push(Buffer.byteLength(body));
  assert.equal(response.status,200);assert.equal(JSON.parse(body).projects.length,117);
 }
 const renderMs=[];
 for(let i=0;i<20;i++){
  // HTTP through the real client; readiness includes all project buttons and a paint.
  const start=performance.now();
  await page.reload();await page.waitForFunction(()=>document.querySelectorAll('[data-ob-project]').length===117);
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  renderMs.push(performance.now()-start);
 }
 const selectionMs=[];
 for(let i=0;i<20;i++){
  await page.locator('[data-ob-project]').filter({hasText:'org/app'}).focus();
  const responses=evidenceResponses(page,200,{fleet:false});
  const start=performance.now();await page.keyboard.press('Enter');
  await responses;await selectedReady(page);
  selectionMs.push(performance.now()-start);
  await page.locator('[data-ob-clear]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-ob-project][aria-pressed="true"]')&&!document.querySelector('[data-ob-status]').textContent.includes('Refreshing'));
 }
 assert.equal(requests.filter(p=>/\/(pipeline|log)$/.test(p)).length,0,'summary refresh and project selection never fetch pipeline details');
 const detailMs=[];
 await selectApp(page);
 for(let i=0;i<20;i++){
  const start=performance.now();await openFailure(page);detailMs.push(performance.now()-start);
  await page.locator('[data-close-inspector]').click();
 }
 console.log('CI_BUDGET_EVIDENCE',JSON.stringify({samples:20,projects:117,summaryP95Ms:p95(summaryMs),summaryBytes:[Math.min(...bytes),Math.max(...bytes)],renderP95Ms:p95(renderMs),projectSelectionP95Ms:p95(selectionMs),runSelectionP95Ms:p95(detailMs),browserRequests:requests.reduce((out,p)=>(out[p]=(out[p]||0)+1,out),{}),measuredSummaryRequests:20}));
 assert.ok(p95(summaryMs)<2000,'cached summary p95 < 2s');
 assert.ok(p95(renderMs)<2000,'local render readiness p95 < 2s (verification target)');
 assert.ok(p95(selectionMs)<1000,'project selection p95 < 1s');
 assert.ok(p95(detailMs)<1000,'run selection p95 < 1s, excluding log reads');
 assert.equal(requests.filter(p=>p.endsWith('/log')).length,0);
}));

for(const slow of ['summary','tests'])test(`project readiness waits for delayed selected ${slow} before clearing`,()=>commandCenter(async({page})=>{
 let release,started;
 const gate=new Promise(resolve=>release=resolve),requested=new Promise(resolve=>started=resolve);
 const pattern=`**/api/v1/ci/${slow}*`;
 await page.route(pattern,async route=>{
  const url=new URL(route.request().url());
  if(slow==='summary'&&!url.searchParams.has('provider_repo_id'))return route.continue();
  started();await gate;await route.continue();
 });
 const aborted=[];page.on('requestfailed',request=>{if(/\/ci\/(summary|tests)/.test(request.url()))aborted.push(request.url());});
 await page.locator('[data-ob-search]').fill('org/app');
 await page.locator('[data-ob-project]').click();await requested;
 let complete=false;
 const ready=selectedReady(page).then(()=>complete=true);
 if(slow==='summary')await page.waitForFunction(()=>document.querySelector('[data-ob-tests]').textContent.includes('test_status'));
 else await page.locator('[data-ob-charts] svg').first().waitFor();
 // Give the fast region and the readiness observer a full paint opportunity.
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))));
 assert.equal(complete,false,'one ready region cannot complete the selection sample');
 assert.match(await page.locator('[data-ob-status]').innerText(),/Refreshing/);
 release();await ready;await page.unroute(pattern);
 assert.match(await page.locator('[data-ob-summary]').innerText(),/Observed projects/);
 assert.ok(await page.locator('[data-ob-charts] svg').count()>0);
 assert.match(await page.locator('[data-ob-tests]').innerText(),/test_status/);
 await page.locator('[data-ob-clear]').click();
 assert.deepEqual(aborted,[],'completed selection must not leave an aborted summary/test request');
}));

test('filtered failing run exposes exact test evidence and keyboard-opened step output and provider link',()=>commandCenter(async({page,context,requests})=>{
 await selectApp(page);await openFailure(page);
 const diagnosis=page.locator('.ci-diagnosis');
 assert.match(await diagnosis.innerText(),/test_status/);
 assert.match(await diagnosis.innerText(),/expected 200/);
 assert.match(await diagnosis.innerText(),/AssertionError <unsafe>/);
 assert.match(await diagnosis.innerText(),/First failure:.*Last pass:/s);
 assert.equal(await diagnosis.locator('unsafe').count(),0);
 assert.equal(requests.filter(p=>p.endsWith('/log')).length,0);
 await page.locator('.ci-pipeline-step[data-step="12"]').focus();await page.keyboard.press('Enter');
 await page.waitForFunction(()=>document.querySelector('[aria-label="Step output"]')?.textContent.includes('expected 200, got 500'));
 assert.equal(requests.filter(p=>p.endsWith('/log')).length,1);
 const link=page.locator('.ig-inspector').getByRole('link',{name:'Open in Woodpecker'}),href=await link.getAttribute('href');
 assert.match(href,/^https:\/\/127\.0\.0\.1:\d+\/repos\/17\/pipeline\/3$/);
 // Intercept the navigation to prove the anchor works without any live provider.
 await context.route(href,route=>route.fulfill({contentType:'text/html',body:'<h1>Local provider destination</h1>'}));
 const opened=context.waitForEvent('page');await link.focus();await page.keyboard.press('Enter');
 const provider=await opened;await provider.getByRole('heading',{name:'Local provider destination'}).waitFor();
 assert.equal(provider.url(),href);
}));

for(const width of [390,1440,3840])test(`command center keeps keyboard charts and internal scrolling usable at ${width}px`,()=>commandCenter(async({page})=>{
 await page.setViewportSize({width,height:1000});await selectApp(page);
 const disclosure=page.locator('[data-ob-charts] details');await disclosure.locator('summary').focus();await page.keyboard.press('Enter');
 assert.equal(await disclosure.evaluate(n=>n.open),true);
 assert.ok(await disclosure.locator('th[scope="col"]').count()>=7);
 assert.match(await disclosure.innerText(),/Failed \/ completed/);
 const charts=page.locator('[data-ob-charts] svg[role="img"]');assert.equal(await charts.count(),2);
 assert.match(await page.locator('[data-ob-charts]').innerText(),/Insufficient data for this trend/);
 for(const chart of await charts.all()){assert.ok(await chart.getAttribute('aria-label'));assert.ok(await chart.locator('desc').textContent());}
 const table=page.getByRole('region',{name:'Project comparison',exact:true});await table.focus();
 assert.equal(await table.evaluate(n=>n===document.activeElement),true);
 assert.equal(await table.evaluate(n=>getComputedStyle(n).overflowX),'auto');
 if(width===390){assert.equal(await table.evaluate(n=>n.scrollWidth>n.clientWidth),true);await table.evaluate(n=>n.scrollLeft=n.scrollWidth);assert.ok(await table.evaluate(n=>n.scrollLeft)>0);}
 await openFailure(page);
 if(width>=1440)assert.equal(await page.locator('.ig-inspector').evaluate(n=>getComputedStyle(n).position),'sticky');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`no document overflow at ${width}`);
}));

test('failed HTTP refresh retains selected project, run, failed step and expanded exact chart data',()=>commandCenter(async({page})=>{
 await selectApp(page);await openFailure(page);
 await page.locator('.ci-pipeline-step[data-step="12"]').click();
 await page.locator('[aria-label="Step output"]').waitFor();
 const disclosure=page.locator('[data-ob-charts] details');await disclosure.locator('summary').click();
 const cards=await page.locator('[data-ob-summary]').innerText(),chart=await disclosure.innerText();
 await page.route('**/api/v1/ci/summary*',route=>route.fulfill({status:503,json:{error:'Fixture outage'}}));
 await page.route('**/api/v1/ci/tests*',route=>route.fulfill({status:503,json:{error:'Fixture outage'}}));
 const failed=evidenceResponses(page,503);
 await page.locator('[data-ob-refresh]').click();await failed;
 await page.waitForFunction(()=>{const status=document.querySelector('[data-ob-status]').textContent;return status.includes('Summary refresh failed')&&status.includes('Project refresh failed')&&!status.includes('Refreshing')&&document.querySelector('[data-ob-tests]').textContent.includes('Test history refresh failed');});
 assert.equal(await page.locator('[data-ob-summary]').innerText(),cards);
 assert.equal(await disclosure.innerText(),chart);assert.equal(await disclosure.evaluate(n=>n.open),true);
 assert.equal(await page.locator('[data-ob-project]').getAttribute('aria-pressed'),'true');
 assert.match(await page.locator('[data-ob-status]').innerText(),/partial/i);
 assert.match(await page.locator('.ig-inspector').innerText(),/Run #3/);
 assert.equal(await page.locator('.ci-pipeline-step[data-step="12"]').getAttribute('aria-pressed'),'true');
 assert.match(await page.locator('[data-ob-tests]').innerText(),/test_status/);
 assert.match(await page.locator('[aria-label="Step output"]').innerText(),/expected 200, got 500/);
 await page.unroute('**/api/v1/ci/summary*');await page.unroute('**/api/v1/ci/tests*');
 const recovered=evidenceResponses(page);
 await page.locator('[data-ob-refresh]').click();await recovered;await selectedReady(page);
 assert.equal(await page.locator('[data-ob-summary]').innerText(),cards);
 assert.equal(await disclosure.innerText(),chart);assert.equal(await disclosure.evaluate(n=>n.open),true);
 assert.match(await page.locator('[data-ob-tests]').innerText(),/test_status/);
 assert.equal(await page.locator('.ci-pipeline-step[data-step="12"]').getAttribute('aria-pressed'),'true');
 assert.equal(await page.locator('[data-ob-project]').getAttribute('aria-pressed'),'true');
}));


test('local application modules load once when the browser asset transport is disconnected',()=>commandCenter(async({page,base,assetReads})=>{
 assert.equal(await page.locator('[data-ob-project]').count(),117);
 assert.equal(assetReads.filter(url=>url===`${base}/app.js`).length,1,'entry module fetched once, without retries');
 // API traffic must still use the browser network, not the asset-only bridge.
 await page.route(`${base}/api/v1/ci/summary*`,route=>route.abort('internetdisconnected'));
 assert.equal(await page.evaluate(async()=>{try{await fetch('/api/v1/ci/summary');return false;}catch{return true;}}),true);
},{invalidateAssetTransport:true}));

test('shared selection: PR scope and a day bucket narrow summary, Tests and Runs through real HTTP',()=>commandCenter(async({page,base})=>{
 const history=await (await fetch(`${base}/api/v1/ci/history`)).json();
 const runs=history.pipelines.filter(r=>r.repository==='org/app');
 for(const r of runs.filter(r=>r.run_number!==3)) {
  const response=await fetch(`${base}/api/v1/ci/reports`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...r,xml:`<testsuite><testcase name="selected-${r.run_number}"><failure/></testcase></testsuite>`})});
  assert.equal(response.status,200);
 }
 await page.reload();await page.waitForFunction(()=>document.querySelectorAll('[data-ob-project]').length===117);
 await page.locator('[data-ob-project]').filter({hasText:'org/app'}).click();
 await page.locator('[data-ob-scope]').selectOption('pr');
 await page.waitForFunction(()=>!document.querySelector('[data-ob-status]').textContent.includes('Refreshing'));
 assert.equal(await page.locator('[data-ob-summary] .ci-ob-card').nth(2).locator('strong').textContent(),'2');
 await page.locator('[data-ci-workspace="tests"]').click();
 assert.match(await page.locator('[data-ci-insights]').innerText(),/3 retained runs · 3 test report attempts/);
 await page.locator('[data-ci-workspace="runs"]').click();await page.locator('[data-tab="failed"]').click();
 assert.equal(await page.locator('#ig-results [data-run]').count(),2,'push failure excluded');
 await page.locator('[data-ci-workspace="health"]').click();
 const day=runs.find(r=>r.run_number===3).created_at.slice(0,10);
 const summaryResponse=page.waitForResponse(r=>{const u=new URL(r.url());return u.pathname==='/api/v1/ci/summary'&&u.searchParams.has('start')&&u.searchParams.has('provider_repo_id');});
 const testsResponse=page.waitForResponse(r=>{const u=new URL(r.url());return u.pathname==='/api/v1/ci/tests'&&u.searchParams.has('start');});
 await page.locator(`[data-health-bucket="day:${day}"]`).click();
 const summary=await (await summaryResponse).json(),tests=await (await testsResponse).json();
 assert.equal(summary.kpis.pipeline_count,1);assert.equal(tests.scope.report_count,1);
 assert.deepEqual(tests.tests.map(t=>t.name),['test_status']);
 await page.locator('[data-health-tree] [data-health-disclosure] > summary').first().click();
 await page.locator('[data-health-tree] [data-health-disclosure] [data-health-disclosure] > summary').first().click();
 await page.locator('[data-health-tree] [data-health-run]').first().waitFor({state:'attached'});
 assert.equal(await page.locator('[data-health-tree] [data-health-run]').count(),1);
 assert.match(await page.locator('[data-health-tree]').textContent(),/Run #3/);
 await page.waitForFunction(()=>document.querySelector('[data-ob-summary] .ci-ob-card:nth-child(3) strong')?.textContent==='1');
 assert.doesNotMatch(await page.locator('[data-ob-tests]').innerText(),/selected-[124]/);
 await page.locator('[data-ci-workspace="tests"]').click();
 assert.match(await page.locator('[data-ci-insights]').innerText(),/1 retained runs · 1 test report attempts/);
 await page.locator('[data-ci-workspace="runs"]').click();
 assert.equal(await page.locator('#ig-results [data-run]').count(),1);
 assert.match(await page.locator('#ig-results').innerText(),/Run #3/);
 await page.locator('[data-ci-workspace="health"]').click();
 await page.locator('[data-health-clear]').click();
 await page.waitForFunction(()=>document.querySelector('[data-ob-summary] .ci-ob-card:nth-child(3) strong')?.textContent==='2');
 await page.locator('[data-ci-workspace="tests"]').click();
 assert.match(await page.locator('[data-ci-insights]').innerText(),/3 retained runs · 3 test report attempts/);
},{selectionContract:true}));

test('shared selection: last-24-runs exact old run reaches summary, Tests and Runs through real HTTP',()=>commandCenter(async({page})=>{
 await page.locator('[data-ob-project]').filter({hasText:'org/app'}).click();
 await hf(page);
 await page.locator('[data-health-mode="runs"]').click();
 const responses=Promise.all(['summary','tests'].map(route=>page.waitForResponse(r=>{
  const u=new URL(r.url());return u.pathname===`/api/v1/ci/${route}`&&u.searchParams.get('run_number')==='3';
 })));
 await page.getByRole('button',{name:/org\/app.*Run #3 ·/}).click();
 for(const response of await responses){
  assert.equal(response.status(),200);
  const query=new URL(response.url()).searchParams;
  assert.equal(query.get('mode'),'runs');assert.equal(query.has('days'),false);
  assert.equal(query.get('provider_repo_id'),'17');assert.equal(query.get('repository'),'org/app');
  const data=await response.json();assert.equal(data.scope.mode,'runs');assert.equal(data.scope.days,null);
  if(data.kpis)assert.equal(data.kpis.pipeline_count,1);
  else {assert.equal(data.scope.report_count,1);assert.deepEqual(data.tests.map(t=>t.name),['test_status']);}
 }
 await page.waitForFunction(()=>document.querySelector('[data-ob-summary] .ci-ob-card:nth-child(3) strong')?.textContent==='1');
 assert.match(await page.locator('[data-ob-selection]').innerText(),/Last 24 runs.*Run #3/);
 assert.doesNotMatch(await page.locator('[data-ob-status]').innerText(),/14-day/);
 await page.locator('[data-ci-workspace="tests"]').click();
 assert.match(await page.locator('[data-ci-insights]').innerText(),/1 retained runs · 1 test report attempts/);
 await page.locator('[data-ci-workspace="runs"]').click();await page.locator('[data-tab="failed"]').click();
 assert.equal(await page.locator('#ig-results [data-run]').count(),1);
 assert.match(await page.locator('#ig-results').innerText(),/Run #3/);
 await page.locator('[data-ci-workspace="health"]').click();
 await page.locator('[data-ob-failures]').check();
 await page.waitForFunction(()=>!document.querySelector('[data-ob-status]').textContent.includes('Refreshing'));
 assert.equal(await page.locator('[data-health-clear]').count(),0);
 await page.locator('[data-ci-workspace="tests"]').click();
 assert.match(await page.locator('[data-ci-insights]').innerText(),/4 retained runs · 1 test report attempts/);
},{oldRun:true}));
