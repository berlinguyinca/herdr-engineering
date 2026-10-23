import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile,access} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';
const packages=path.resolve(import.meta.dirname,'../../packages');

test('native CI charts follow context/search across tabs and preserve focus through refresh',async()=>{
  const server=createServer(async(req,res)=>{
    if(req.url==='/'){res.setHeader('content-type','text/html');res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/herdr-design-system/theme.css"></head><body><main id="app"></main></body></html>');return;}
    const file=path.resolve(packages,'.'+decodeURIComponent(req.url));
    if(!file.startsWith(packages+path.sep)){res.writeHead(404);res.end();return;}
    try{const body=await readFile(file);res.setHeader('content-type',file.endsWith('.css')?'text/css':'text/javascript');res.end(body);}catch{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    const options={headless:true,args:['--no-sandbox']};
    if(process.env.PLAYWRIGHT_CHROMIUM)browser=await chromium.launch({...options,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
    else try{browser=await chromium.launch(options);}catch(error){
      const fallback='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
      try{await access(fallback);}catch{throw error;}
      browser=await chromium.launch({...options,executablePath:fallback});
    }
    const page=await browser.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(async()=>{
      const {mountIntegrations}=await import('/integrations-view.js');
      const row=(run_number,state,branch='main')=>({run_number,state,branch,project_id:'Project Alpha',language:'JavaScript',mission_key:'pi:one',session_id:'session-one',url:`https://ci.example/org/app/${run_number}`,repository:'org/app',repository_id:'github.com/org/app',created_at:new Date().toISOString(),started_at:1792497600,finished_at:1792497660});
      window.chartSnapshot={available:true,status:'partial',stale:true,pipelines:{'org/app':[row(1,'success'),row(2,'failure','feature'),row(3,'running'),{...row(4,'success'),repository_id:'other-forge.example/org/app'}]}};
      window.chartHistory={reports:[],pipelines:[{...row(1,'success'),provider_url:'https://ci.example',url:undefined}],scope:{persistence:'memory_only',pipeline_limit:1000}};
      const client={getCi:async()=>window.chartSnapshot,getCiReports:async()=>window.chartHistory};
      window.chartView=mountIntegrations(document.querySelector('#app'),client,{repository:'github.com/org/app'});
      window.addRun=()=>window.chartSnapshot.pipelines['org/app'].push(row(window.chartSnapshot.pipelines['org/app'].length+1,'success'));
    });
    await page.waitForFunction(()=>document.querySelector('[data-ci-charts-body]')?.textContent.includes('3 matching runs'));
    await page.locator('[data-ci-workspace="tests"]').click();
    assert.equal(await page.locator('[data-ci-charts]').getAttribute('open'),null);
    await page.locator('[data-ci-charts]>summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('[data-ci-charts] svg[role="img"]').count(),5);
    assert.match(await page.locator('[data-ci-charts-body]').innerText(),/Reports are stale/);
    assert.match(await page.locator('[data-ci-charts-body]').innerText(),/Some repositories have incomplete reports/);
    await page.locator('[data-ci-workspace="runs"]').click();
    await page.locator('#ig-tab-finished').click();
    assert.match(await page.locator('[data-ci-charts-body]').innerText(),/3 matching runs/);
    const search=page.locator('[data-ig-search]');
    await search.fill('feature');
    assert.match(await page.locator('[data-ci-charts-body]').innerText(),/1 matching runs/);
    await search.fill('main');
    assert.match(await page.locator('[data-ci-charts-body]').innerText(),/2 matching runs/);
    await page.evaluate(()=>{window.addRun();window.chartView.refresh();});
    await page.waitForFunction(()=>document.querySelector('[data-ci-charts-body]')?.textContent.includes('3 matching runs'));
    assert.equal(await page.evaluate(()=>document.activeElement?.hasAttribute('data-ig-search')),true);
    await page.locator('[data-ci-workspace="tests"]').click();
    const dataSummary=page.locator('[data-ci-chart-data]>summary');
    await dataSummary.focus();await page.keyboard.press('Enter');
    await page.evaluate(()=>{window.addRun();window.chartView.refresh();});
    await page.waitForFunction(()=>document.querySelector('[data-ci-charts-body]')?.textContent.includes('4 matching runs'));
    assert.equal(await page.locator('[data-ci-chart-data]').getAttribute('open'),'');
    assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('[data-ci-chart-data]>summary')),true);
    assert.ok(await page.locator('[data-ci-chart-data] tbody tr').count()>0);
    const projects=page.getByRole('region',{name:'Project execution statistics',exact:true});
    assert.deepEqual(await projects.locator('tbody tr').first().locator('th,td').allTextContents(),['Project Alpha','3','60000','60000','60000']);
    assert.match(await page.locator('[data-ci-charts-body]').innerText(),/Storage is memory-only/);
    // New records and retained observations obey the same branch and period controls.
    await page.locator('[data-ci-workspace="runs"]').click();
    await search.fill('');
    await page.locator('[data-ci-workspace="tests"]').click();
    await page.locator('[data-ci-branch]').selectOption('feature');
    assert.match(await page.locator('.ci-performance').innerText(),/No successful runs with valid positive/);
    await page.locator('[data-ci-branch]').selectOption('main');
    await page.evaluate(()=>{
      window.chartHistory.pipelines.push({run_number:100,state:'success',project_id:'Old project',language:'Rust',repository:'org/app',repository_id:'github.com/org/app',branch:'main',created_at:'2020-01-01T00:00:00Z',started_at:1792497600,finished_at:1792497720});
      window.chartView.refresh();
    });
    await page.waitForFunction(()=>document.querySelector('.ci-performance')?.textContent.includes('4 successful runs'));
    await page.locator('[data-ci-period]').selectOption('7');
    assert.match(await page.locator('.ci-performance').innerText(),/3 successful runs/);
    assert.match(await page.locator('[data-ci-charts-body]').innerText(),/Last 7 days/);
    await page.locator('[data-ci-workspace="runs"]').click();
    await page.locator('#ig-tab-failed').click();
    await page.locator('[data-ci-workspace="tests"]').click();
    assert.match(await page.locator('.ci-performance').innerText(),/3 successful runs/);
    await projects.focus();
    await page.evaluate(()=>{window.addRun();window.chartView.refresh();});
    await page.waitForFunction(()=>document.querySelector('.ci-performance')?.textContent.includes('4 successful runs'));
    assert.equal(await projects.evaluate(node=>node===document.activeElement),true);
    await page.locator('[data-ci-chart-data]>summary').focus();
    await page.keyboard.press('Enter');
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    const chartStyle=await page.locator('.ci-chart-card rect').first().evaluate(node=>getComputedStyle(node).fill);
    assert.match(chartStyle,/rgb/);
    await page.evaluate(()=>window.chartView.dispose());
    assert.deepEqual(errors,[]);
  } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
});
