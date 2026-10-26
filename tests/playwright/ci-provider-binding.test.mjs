import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';
const root=path.resolve(import.meta.dirname,'../..');

test('retained provider collisions preserve display, logs, comparison and reject reconfigured actions',async()=>{
 const providerA='https://ci.example/a',providerB='https://ci.example/b';
 let configured=providerA;
 const requests=[],mutations=[];
 // The command center defaults to a dated window; these fixtures test provider identity, not unknown timestamps.
 const created_at=new Date().toISOString();
 const run=(provider,number=12)=>({created_at,provider_url:provider,provider_repo_id:17,repository:'org/app',run_number:number,state:'failure',revision:(provider===providerA?'a':'b').repeat(40),branch:'main'});
 const unidentified={created_at,provider_repo_id:17,repository:'org/app',run_number:99,state:'failure',can_retry:true,can_cancel:true,tasks:[{id:4,name:'Tests',state:'failure'}]};
 const retained=[run(providerA),run(providerA,11),run(providerB),unidentified];
 const report=provider=>({...run(provider),summary:{total:provider===providerA?1:9,passed:0,failed:provider===providerA?1:9},cases:[]});
 const server=createServer(async(req,res)=>{
  const url=new URL(req.url,'http://local');
  const send=(body,status=200)=>{res.writeHead(status,{'content-type':'application/json'});res.end(JSON.stringify(body));};
  if(url.pathname==='/'){
   res.setHeader('content-type','text/html');res.end('<!doctype html><link rel="stylesheet" href="/herdr-design-system/theme.css"><main></main><script type="module">import {HerdrClient} from "/herdr-web-client/client.js";import {mountIntegrations} from "/integrations-view.js";window.view=mountIntegrations(document.querySelector("main"),new HerdrClient(),{tab:"failed"});</script>');return;
  }
  if(url.pathname==='/api/v1/ci'){send({available:true,status:'connected',pipelines:{'org/app':[run(configured)]}});return;}
  if(url.pathname==='/api/v1/ci/reports'){send({reports:[report(providerA),report(providerB)],pipelines:retained});return;}
  if(url.pathname==='/api/v1/ci/history'){send({available:true,status:'ready',repos:[],pipelines:retained});return;}
  if(url.pathname==='/api/v1/ci/performance'){send({schema_version:1,days:14,cache_groups:[],execution_trends:[],alerts:[]});return;}
  if(['/api/v1/ci/pipeline','/api/v1/ci/log','/api/v1/ci/actions'].includes(url.pathname)){
   let body={};if(req.method==='POST'){for await(const chunk of req)body={raw:(body.raw||'')+chunk};body=JSON.parse(body.raw);}
   const expected=req.method==='POST'?body.provider_url:url.searchParams.get('provider_url');
   requests.push({path:url.pathname,expected,body});
   if(expected&&expected!==configured){send({error:'CI provider changed'},400);return;}
   if(url.pathname.endsWith('/pipeline')){send({provider_url:configured,pipeline:{...run(configured),can_retry:true,can_cancel:true,tasks:[{id:4,name:'Tests',state:'failure'}]}});return;}
   if(url.pathname.endsWith('/log')){send({provider_url:configured,available:true,log:`${configured} log`});return;}
   mutations.push({...body,configured});send({ok:true});return;
  }
  try{const file=path.resolve(root,'packages','.'+url.pathname);if(!file.startsWith(root+'/packages/'))throw Error();res.setHeader('content-type',file.endsWith('.css')?'text/css':'text/javascript');res.end(await readFile(file));}catch{res.writeHead(404);res.end();}
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1920,height:1000}}),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  const key=provider=>JSON.stringify([provider,'17','12']);
  const choose=async provider=>{const button=page.locator('[data-run]').filter({hasText:'Run #12'});await button.first().waitFor();await button.evaluateAll((nodes,id)=>nodes.find(node=>node.dataset.run===id).click(),key(provider));};
  await choose(providerA);
  const inspector=page.locator('.ig-inspector');await inspector.locator('[data-step="4"]').waitFor();
  assert.match(await inspector.innerText(),/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);
  assert.match(await inspector.innerText(),/Test results · 1 tests/);assert.doesNotMatch(await inspector.innerText(),/Test results · 9 tests/);
  await inspector.locator('[data-step="4"]').click();await page.waitForFunction(()=>document.querySelector('.ig-log')?.textContent.includes('/a log'));
  const options=await inspector.locator('[data-ci-compare] option').evaluateAll(nodes=>nodes.map(node=>node.value).filter(Boolean));
  assert.deepEqual(options,[JSON.stringify([providerA,'17','11'])]);
  await inspector.locator('[data-ci-compare]').selectOption(options[0]);
  configured=providerB;await page.evaluate(()=>window.view.refresh());
  await page.waitForFunction(()=>document.querySelector('.ig-inspector')?.textContent.includes('Pipeline details could not refresh'));
  assert.match(await inspector.innerText(),/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);
  assert.doesNotMatch(await inspector.innerText(),/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/);
  assert.match(await inspector.locator('.ig-log').innerText(),/\/a log/);
  assert.equal(await inspector.locator('[data-step="4"]').getAttribute('aria-pressed'),'true');
  assert.match(await inspector.locator('.ci-comparison').innerText(),/Active #12 compared with #11/);
  for(const action of ['retry','cancel']){
   await inspector.locator(`[data-ci-action="${action}"]`).click();
   await page.waitForFunction(()=>document.querySelector('.ig-inspector')?.textContent.includes('Woodpecker did not accept the action'));
  }
  assert.deepEqual(mutations,[]);
  for(const request of requests)assert.equal(request.expected,providerA,`${request.path} must keep the selected provider`);
  await choose(providerB);await inspector.locator('[data-step="4"]').waitFor();
  await page.waitForFunction(()=>document.querySelector('.ig-inspector')?.textContent.includes('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'));
  await inspector.locator('[data-step="4"]').click();await page.waitForFunction(()=>document.querySelector('.ig-log')?.textContent.includes('/b log'));
  assert.equal(await inspector.locator('[data-ci-compare] option').count(),1,'Comparison cannot retain A after selecting B');
  await inspector.locator('[data-ci-action="retry"]').click();await page.waitForFunction(()=>document.querySelector('.ig-inspector')?.textContent.includes('Action accepted'));
  assert.equal(mutations.length,1);assert.equal(mutations[0].provider_url,providerB);
  const before=requests.length;
  await page.locator('[data-run]').filter({hasText:'Run #99'}).click();
  assert.match(await inspector.innerText(),/Exact CI provider identity is not reported/);
  assert.equal(await inspector.locator('[data-ci-action]').count(),0);
  await inspector.locator('[data-step="4"]').click();
  assert.equal(requests.length,before,'Unidentified retained runs cannot issue live reads or actions');
  assert.deepEqual(errors,[]);
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
});
