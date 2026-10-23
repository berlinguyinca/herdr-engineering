import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {existsSync,mkdirSync} from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';
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
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`${base}/#/ci`);
  assert.equal(await page.locator('[data-ci-workspace="health"]').getAttribute('aria-selected'),'true');
  assert.equal(await page.locator('[data-ci-workspace="health"]').getAttribute('aria-controls'),'ci-workspace-health');
  await page.locator('[data-ci-workspace="health"]').focus();await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('[data-ci-workspace="runs"]').getAttribute('aria-selected'),'true');
  assert.equal(await page.evaluate(()=>document.activeElement?.dataset.ciWorkspace),'runs');
  await page.locator('[data-ci-workspace="tests"]').click();
  const insights=page.locator('[data-ci-insights]');
  await page.waitForFunction(()=>document.querySelector('[data-ci-insights]')?.textContent.includes('4 test report attempts'));
  await page.locator('[data-ci-repository]').selectOption('17');
  await page.locator('[data-ci-branch]').selectOption('main');
  assert.match(await insights.innerText(),/3 retained runs · 3 test report attempts/);
  assert.match(await insights.innerText(),/33.3%/);assert.match(await insights.innerText(),/2 \/ 3 attempts/);
  assert.match(await insights.innerText(),/First failure/);assert.match(await insights.innerText(),/Last pass/);
  assert.equal(await insights.locator('svg[role="img"]').count(),1);
  await page.locator('[data-ci-period]').selectOption('7');
  assert.match(await insights.innerText(),/3 retained runs/);
  mkdirSync(path.join(root,'artifacts/ci-insights'),{recursive:true});
  await page.screenshot({path:path.join(root,'artifacts/ci-insights/overview.png'),fullPage:true});
  await page.locator('[data-ci-workspace="runs"]').click();
  await page.getByRole('tab',{name:/^Failed/}).click();
  await page.getByRole('button',{name:'Run #3',exact:true}).click();
  const inspector=page.locator('.ig-inspector');
  await inspector.locator('[data-step="12"]').waitFor();
  await page.locator('[data-ci-workspace="tests"]').click();
  assert.equal(await insights.isVisible(),true);
  await page.locator('[data-ci-workspace="health"]').click();
  assert.equal(await inspector.locator('[data-step="12"]').evaluate(e=>getComputedStyle(e).borderLeftColor),'rgb(220, 53, 69)');
  assert.match(await inspector.innerText(),/Test results · 4 tests/);
  const box=await inspector.boundingBox();assert.ok(box.width>900,`Pipeline inspector too narrow: ${box.width}`);
  await inspector.locator('[data-step="12"]').focus();await page.keyboard.press('Enter');
  await page.waitForFunction(()=>document.querySelector('.ig-log')?.textContent.includes('expected 200, got 500'));
  assert.equal(await inspector.locator('[data-step="12"]').getAttribute('aria-pressed'),'true');
  await inspector.locator('[data-collapse-success]').click();
  assert.equal(await inspector.locator('.ci-pipeline-success').count(),1);
  assert.equal(await inspector.locator('[data-step="12"]').isVisible(),true);
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
  assert.equal(await inspector.locator('[data-step="12"]').getAttribute('aria-pressed'),'true');
  assert.match(await inspector.locator('.ig-log').innerText(),/expected 200, got 500/);
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
