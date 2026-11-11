import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdirSync} from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';
const root=path.resolve(import.meta.dirname,'../..');
const evidence=process.env.HERDR_MEMORY_EVIDENCE_DIR||path.join(root,'docs/evidence/memory-explorer');

// Mode: MOCKED herdr-memory adapter behind a real control plane and the real dashboard.
test('Memory Explorer: search, page, open the detail drawer with keyboard, mobile layout',async()=>{
 const server=spawn(process.env.PYTHON||'python3',[path.join(root,'tests/playwright/memory-server.py')],{cwd:root,stdio:['ignore','pipe','pipe']});
 let browser;let stderr='';server.stderr.on('data',d=>stderr+=d);
 try {
  const port=await new Promise((resolve,reject)=>{let out='';const t=setTimeout(()=>reject(new Error(stderr||'timeout')),25000);server.stdout.on('data',d=>{out+=d;const m=out.match(/PORT=(\d+)/);if(m){clearTimeout(t);resolve(m[1]);}});server.once('exit',c=>{clearTimeout(t);reject(new Error(`exit ${c}: ${stderr}`));});});
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${port}/#/memory`);
  await page.getByText('Nothing is listed until you search').waitFor({timeout:15000});
  await page.getByText('healthy').first().waitFor();
  await page.getByText('UNKNOWN').first().waitFor();
  await page.getByLabel('Search',{exact:true}).fill('example');
  await page.keyboard.press('Enter');
  await page.locator('[data-memory="mock-00"]').waitFor();
  assert.equal(await page.locator('.mem-row').count(),20);
  await page.locator('[data-mem-page="next"]').click();
  await page.locator('[data-memory="mock-20"]').waitFor();
  assert.equal(await page.locator('.mem-row').count(),5);
  await page.locator('[data-memory="mock-20"]').focus();await page.keyboard.press('Enter');
  await page.getByText('Linked work').waitFor();
  assert.equal(await page.locator('.mem-links a').count(),3);   // mission, session, handoff: from provenance only
  mkdirSync(evidence,{recursive:true});
  await page.screenshot({path:path.join(evidence,'desktop.png')});
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('[data-mem-drawer]').isHidden(),true);
  await page.setViewportSize({width:390,height:800});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),true);
  await page.locator('[data-memory="mock-20"]').click();await page.getByText('Linked work').waitFor();
  await page.screenshot({path:path.join(evidence,'mobile.png')});
  // Real browser -> existing client -> real API -> selector-sensitive test adapter.
  for (const fixture of [
    {id:'selector-summary',kind:'summary',scope:'project',project:'fixture-project'},
    {id:'selector-decision',kind:'fact',scope:'project',project:'fixture-project'},
    {id:'selector-global',kind:'fact',scope:'global',project:''},
  ]) {
    await page.keyboard.press('Escape');
    await page.getByLabel('Search',{exact:true}).fill('Selector fixture');
    await page.getByLabel('Kind',{exact:true}).fill(fixture.kind);
    await page.getByLabel('Scope',{exact:true}).fill(fixture.scope);
    await page.getByLabel('Project',{exact:true}).fill(fixture.project);
    await page.getByLabel('Search',{exact:true}).press('Enter');
    const row=page.locator(`[data-memory="${fixture.id}"]`);
    await row.waitFor();
    const [request]=await Promise.all([
      page.waitForRequest(r=>new URL(r.url()).pathname===`/api/v1/memory/${fixture.id}`),
      row.click(),
    ]);
    const params=new URL(request.url()).searchParams;
    assert.equal(params.get('kind'),fixture.kind);
    assert.equal(params.get('scope'),fixture.scope);
    if(fixture.project)assert.equal(params.get('project'),fixture.project);
    else assert.equal(params.has('project'),false);
    await page.locator('[data-mem-drawer]').getByText('Linked work',{exact:true}).waitFor();
    assert.equal(await page.locator('[data-mem-drawer] img').count(),0);
  }
  assert.deepEqual(errors,[]);
 } finally { if(browser)await browser.close();server.kill(); }
});
