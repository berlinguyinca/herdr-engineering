import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';
const root=path.resolve(import.meta.dirname,'../..');

// A real control plane against a TLS Woodpecker double whose steps print go -v output and karma totals: nothing uploads a
// report and no project is configured, yet the Tests tab shows counts, a test list and the log lines around a test.
async function withLogTests(run) {
 const server=spawn(process.env.PYTHON||'python3',[path.join(root,'tests/playwright/ci-insights-server.py'),'--log-tests'],{cwd:root,stdio:['ignore','pipe','pipe']});
 let browser,stderr='';server.stderr.on('data',d=>stderr+=d);
 try {
  const port=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error(`startup timeout: ${stderr}`)),30000);server.stdout.on('data',d=>{text+=d;const m=text.match(/PORT=(\d+)/);if(m){clearTimeout(timer);resolve(Number(m[1]));}});server.on('exit',()=>reject(Error(stderr)));});
  const options={headless:true,args:['--no-sandbox'],...(process.env.PLAYWRIGHT_CHROMIUM?{executablePath:process.env.PLAYWRIGHT_CHROMIUM}:{})};
  try{browser=await chromium.launch(options);}catch(error){const exe='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';if(!existsSync(exe))throw error;browser=await chromium.launch({...options,executablePath:exe});}
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await run({page,base:`http://127.0.0.1:${port}`});
  assert.deepEqual(errors,[]);
 } finally {await browser?.close();server.kill();}
}

test('log-parsed counts, a per-test list and the log lines around a test appear with no project setup',()=>withLogTests(async({page,base})=>{
 await page.goto(`${base}/#/ci?workspace=tests`);
 const row=page.locator('[data-ci-test-repo]').first();
 await row.waitFor({timeout:30000});
 assert.equal(await row.getAttribute('data-ci-source'),'log_parse');
 assert.match(await row.getAttribute('title'),/parsed from CI logs \(go, karma\)/);
 assert.match(await row.innerText(),/[1-9]\d* failed/);assert.match(await row.innerText(),/from CI logs/);
 assert.match(await page.locator('.ci-test-overview').innerText(),/counted from CI logs/);
 await row.click();
 const explorer=page.locator('[data-ci-explorer]');
 await explorer.locator('[data-cx-test]').first().waitFor();
 assert.match(await explorer.innerText(),/parsed from CI logs \(go, karma\)/);
 // Failures first; the karma step prints only totals, so it adds to the counts but not to the list.
 assert.equal(await explorer.locator('[data-cx-test]').first().getAttribute('data-test-status'),'failed');
 assert.match(await explorer.locator('[data-cx-test]').first().innerText(),/TestBeta/);
 assert.match(await explorer.innerText(),/The list names 4 of 14 counted tests/);
 await explorer.locator('[data-cx-search]').fill('alpha');
 await page.waitForFunction(()=>document.querySelectorAll('[data-cx-test]').length===2);
 await explorer.locator('[data-cx-search]').fill('');
 await explorer.locator('[data-cx-status]').selectOption('failing');
 await page.waitForFunction(()=>document.querySelectorAll('[data-cx-test]').length===2);
 await explorer.locator('[data-cx-test]').first().click();
 const inspector=explorer.locator('[data-cx-inspector]');
 await inspector.locator('.cx-hit').first().waitFor();
 assert.match(await inspector.locator('.cx-log').innerText(),/expected 200, got 500/);
 assert.equal(await explorer.locator('li:has([data-cx-test]) > [data-cx-inspector]').count(),1,'the log unfolds under the test that was clicked');
 await inspector.locator('[data-cx-agents]').click();
 await inspector.locator('[data-cx-session], :text("No active Pi agents")').first().waitFor();
 assert.match(await inspector.locator('.cx-hit').first().innerText(),/TestBeta/);
assert.match(await inspector.innerText(),/parsed from the CI log, step Unit tests/);
 await inspector.locator('[data-cx-history]').click();
 await inspector.locator('.cx-history tbody tr').first().waitFor();
 assert.ok(await inspector.locator('.cx-history tbody tr').count()>=2,'the same test is found in earlier runs');
 await inspector.locator('[data-cx-open-step]').click();
 await page.locator('.ig-inspector').first().waitFor({state:'visible'});
 assert.match(await page.locator('.ig-inspector').first().innerText(),/Run #/);
}));
