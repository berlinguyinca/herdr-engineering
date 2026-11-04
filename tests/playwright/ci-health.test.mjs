import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdirSync} from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';
const hf=async p=>{await p.locator('[data-health-filters]').waitFor({state:'attached',timeout:15000});await p.evaluate(()=>{const d=document.querySelector('[data-health-filters]');if(d)d.open=true;});};  // the Health filter bar is a closed disclosure by default
const root=path.resolve(import.meta.dirname,'../..');

test('CI health integrates real paginated history, PR drilldown, old filtered runs and responsive layout',async()=>{
 const server=spawn(process.env.PYTHON||'python3',[path.join(root,'tests/playwright/ci-health-server.py')],{cwd:root,stdio:['ignore','pipe','pipe']});
 let browser;let stderr='';server.stderr.on('data',d=>stderr+=d);
 try {
  const port=await new Promise((resolve,reject)=>{let output='';const timeout=setTimeout(()=>reject(new Error(stderr||'Health server timeout')),25000);server.stdout.on('data',d=>{output+=d;const m=output.match(/PORT=(\d+)/);if(m){clearTimeout(timeout);resolve(m[1]);}});server.once('exit',code=>{clearTimeout(timeout);reject(new Error(`Server exit${code}: ${stderr}`));});});
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${port}/#/ci?workspace=health`);
  await hf(page);
  await page.locator('[data-health-mode="14"]').waitFor({timeout:15000});
  await hf(page);
  assert.equal(await page.locator('[data-health-mode="14"]').getAttribute('aria-pressed'),'true');
  await page.waitForFunction(()=>document.querySelector('[data-ci-health]')?.textContent.includes('org/app'),null,{timeout:45000});
  assert.equal(await page.locator('.ci-health-grid > tbody > tr').count(),117);
  await hf(page);
  assert.equal(await page.locator('[data-health-scope]').inputValue(),'all');
  // This drilldown scenario targets the default-branch history explicitly.
  await hf(page);
  await page.locator('[data-health-scope]').selectOption('default');
  await hf(page);
  await page.locator('[data-health-mode="runs"]').click();
  await page.waitForFunction(async()=>{const d=await(await fetch('/api/v1/ci/history')).json();return d.coverage?.['17']?.exhausted&&d.pipelines.some(r=>r.run_number===8);},null,{timeout:45000});
  assert.match(await page.locator('[data-ci-health]').innerText(),/24/);
  const filled=page.locator('.ci-health-grid tbody tr',{has:page.locator('th.ci-health-project',{hasText:/^org\/app\b/})}).locator('.ci-health-cell:not(.state-empty)');
  for(let i=0,n=await filled.count();i<n;i++){await filled.nth(i).click();if(await page.getByRole('button',{name:/^Run #8\b/}).count())break;}
  await page.getByRole('button',{name:/^Run #8\b/}).click();
  await page.locator('.ig-inspector .ci-pipeline-step[data-step="12"]').waitFor();
  assert.match(await page.locator('.ig-inspector').innerText(),/Run #8/);
  assert.equal(await page.locator('[data-health-pipeline-slot] .ig-inspector').count(),1);
  await page.locator('.ig-inspector .ci-pipeline-step[data-step="12"]').click();
  await page.waitForFunction(()=>document.querySelector('.ig-log')?.textContent.includes('expected 200, got 500'));
  assert.equal(await page.locator('.ig-inspector .ci-pipeline-step[data-step="12"]').evaluate(e=>getComputedStyle(e).borderLeftColor),'rgb(220, 53, 69)');
  assert.equal(await page.locator('[data-health-slot-run][aria-current="true"]').count(),1);
  await hf(page);
  const search=page.locator('[data-health-search]');await search.fill('org/app');
  await search.focus();await page.waitForTimeout(5300);
  assert.equal(await search.evaluate(e=>e===document.activeElement),true);
  await search.fill('');
  await page.locator('[data-close-inspector]').click();
  for(const mode of ['7','14','30']){
   await hf(page);
   await page.locator(`[data-health-mode="${mode}"]`).click();
   await hf(page);
   assert.equal(await page.locator(`[data-health-mode="${mode}"]`).getAttribute('aria-pressed'),'true');
  }
  await hf(page);
  await page.locator('[data-health-scope]').selectOption('pr');
  await page.waitForFunction(()=>document.querySelector('[data-health-scope]')?.value==='pr'&&document.querySelector('[data-health-map] .ci-health-grid'));
  await hf(page);
  await page.locator('[data-health-mode="30"]').click();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  // Resolve and measure the refreshing heatmap in one browser task. The
  // periodic history refresh replaces this subtree and can detach a Locator's
  // previously resolved node between separate Playwright operations.
  const gridGeometry=await page.evaluate(()=>{
   const map=document.querySelector('[data-health-map]');
   const scroll=map?.querySelector('[data-health-grid-scroll]');
   const cell=map?.querySelector('.ci-health-grid td');
   const columnHeader=map?.querySelector('.ci-health-grid thead th');
   const rowHeader=map?.querySelector('.ci-health-grid tbody th');
   return {overflowX:getComputedStyle(scroll).overflowX,cellWidth:cell.getBoundingClientRect().width,columnHeaderPosition:getComputedStyle(columnHeader).position,rowHeaderPosition:getComputedStyle(rowHeader).position};
  });
  assert.equal(gridGeometry.overflowX,'auto');
  assert.ok(gridGeometry.cellWidth>=36,'Daily columns retain readable minimum widths');
  assert.equal(gridGeometry.columnHeaderPosition,'sticky');
  assert.equal(gridGeometry.rowHeaderPosition,'sticky');
  mkdirSync(path.join(root,'artifacts/ci-health'),{recursive:true});
  await page.screenshot({path:path.join(root,'artifacts/ci-health/desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:path.join(root,'artifacts/ci-health/mobile.png'),fullPage:true});
  const firstCell=page.locator('[data-health-project][data-health-bucket]').first();
  await firstCell.focus();await page.keyboard.press('ArrowRight');
  assert.equal(await firstCell.evaluate(e=>e!==document.activeElement),true);
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('[data-health-clear]').count(),1);
  await page.locator('[data-health-clear]').click();
  await page.reload();
  await hf(page);
  assert.equal(await page.locator('[data-health-mode="30"]').getAttribute('aria-pressed'),'true');
  await hf(page);
  assert.equal(await page.locator('[data-health-scope]').inputValue(),'pr');
  await hf(page);
  await page.locator('[data-health-search]').fill('org/app');await page.reload();
  await hf(page);
  assert.equal(await page.locator('[data-health-search]').inputValue(),'org/app');
  await page.goto(`http://127.0.0.1:${port}/#/ci?repository=github.com/org/app&search=main&workspace=health`);
  await page.waitForFunction(()=>document.querySelectorAll('.ci-health-grid > tbody > tr').length===1);
  await hf(page);
  assert.equal(await page.locator('[data-health-search]').inputValue(),'main');
  assert.match(await page.locator('.ci-health-grid > tbody > tr').innerText(),/org\/app/);
  assert.deepEqual(errors,[]);
 } finally {await browser?.close();server.kill();}
});
