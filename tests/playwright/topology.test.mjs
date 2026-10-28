import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,T0} from './helpers/live.mjs';

const root=path.resolve(import.meta.dirname,'../..');
const python=process.env.PYTHON || (fs.existsSync(path.join(root,'.venv/bin/python'))?path.join(root,'.venv/bin/python'):'python3');
async function withApp(fn){
 const server=spawn(python,[path.join(root,'tests/playwright/server.py')],{env:{...process.env,BURST:'1'},stdio:['ignore','pipe','pipe']});
 let browser;
 try{
  const port=await new Promise((resolve,reject)=>{let text='';const timeout=setTimeout(()=>reject(new Error('Fixture startup timed out')),20000);server.stdout.on('data',data=>{text+=data;const hit=text.match(/PORT=(\d+)/);if(hit){clearTimeout(timeout);resolve(hit[1]);}});server.once('exit',code=>{clearTimeout(timeout);reject(new Error(`Fixture exited ${code}`));});});
  const options={headless:true,args:['--no-sandbox']};
  if(process.env.PLAYWRIGHT_CHROMIUM)options.executablePath=process.env.PLAYWRIGHT_CHROMIUM;
  try{browser=await chromium.launch(options);}catch(error){const fallback='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';if(!fs.existsSync(fallback))throw error;browser=await chromium.launch({...options,executablePath:fallback});}
  const page=await browser.newPage();await fn(page,`http://127.0.0.1:${port}`);
 }finally{await browser?.close();server.kill();}
}
async function seed(page,{services=[],sessions=[],hosts,ci}={}){
 const fresh=new Date().toISOString();
 await page.route('**/api/v1/hosts',route=>route.fulfill({json:{hosts:hosts||[{host_id:'beast',host_name:'Beast',status:'healthy',last_seen:fresh}]}}));
 await page.route('**/api/v1/services',route=>route.fulfill({json:{services:services.map(s=>({...s,catalog:{scope:'herdr',...s.catalog}}))}}));
 await page.route('**/api/v1/sessions',route=>route.fulfill({json:{sessions}}));
 await page.route('**/api/v1/missions',route=>route.fulfill({json:{missions:[]}}));
 await page.route('**/api/v1/mission-activity',route=>route.fulfill({json:{activities:[]}}));
 await page.route('**/api/v1/ci',route=>route.fulfill({json:ci||{available:true,status:'connected',last_success_at:fresh,pipelines:{}}}));
}

test('topology hides historical records by default without dropping unknown hosts or bounded rows',async()=>withApp(async(page,base)=>{
 const fresh=new Date().toISOString(),old='2020-01-01T00:00:00Z';
 const services=Array.from({length:14},(_,i)=>({service_id:`service-${i}`,service_name:`Parser ${String(i).padStart(2,'0')}`,host_id:'beast',status:'healthy',updated_at:fresh,url:`http://example.test:${18000+i}`,catalog:{scope:'herdr',category:'development',label:'Development apps',project:'spectra',project_source:'process.cwd'}}));
 services.push({service_id:'stopped',service_name:'Stopped parser',host_id:'beast',status:'stopped',updated_at:fresh,url:'http://example.test:9999'},
  {service_id:'old',service_name:'Unconfirmed parser',host_id:'beast',status:'healthy',updated_at:old,url:'http://example.test:9999'},
  {service_id:'orphan',service_name:'Unmatched host service',host_id:'beast.local',status:'healthy',updated_at:fresh});
 await seed(page,{services,sessions:[{session_id:'finished',host_id:'beast',title:'Finished worker',status:'closed',updated_at:fresh}]});
 await page.goto(`${base}/#/topology`);
 await page.locator('[data-tx-disclosure="host:beast"]>summary').waitFor();
 assert.equal(await page.locator('[data-topology-entry]').count(),0);
 assert.equal(await page.locator('[data-tx-disclosure="unmatched:beast.local"]').count(),1);
 await page.locator('[data-tx-disclosure="host:beast"]>summary').click();
 await page.locator('[data-tx-disclosure="host:beast/service:development"]>summary').click();
 await page.locator('[data-topology-entry]').first().waitFor();
 assert.equal(await page.locator('[data-topology-entry]').count(),6);
 await page.getByRole('button',{name:'Show 6 more · 8 remaining',exact:true}).click();
 assert.equal(await page.locator('[data-topology-entry]').count(),12);
 await page.getByRole('button',{name:'Show 2 more · 2 remaining',exact:true}).click();
 assert.equal(await page.locator('[data-topology-entry]').count(),14);
 assert.equal(await page.getByText('Stopped parser',{exact:true}).count(),0);
 assert.equal(await page.getByText('Unconfirmed parser',{exact:true}).count(),0);
 await page.getByRole('tab',{name:/^Unconfirmed/}).click();
 await page.locator('[data-tx-disclosure="host:beast/service:other"]>summary').click();
 await page.getByText('Unconfirmed parser',{exact:true}).waitFor();
 assert.equal(await page.getByRole('link',{name:'Open endpoint'}).count(),0);
 await page.getByRole('tab',{name:/^History/}).click();
 await page.getByText('Stopped parser',{exact:true}).waitFor();
 await page.getByText('Finished worker',{exact:true}).waitFor();
 assert.equal(await page.getByRole('link',{name:'Open endpoint'}).count(),0);
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
}));

test('topology preserves open groups and search focus across polling, partial failures and SSE bursts',async()=>withApp(async(page,base)=>{
 let reads=0;const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const service={service_id:'parser',service_name:'Spectra parser',host_id:'beast',status:'healthy',updated_at:new Date().toISOString(),catalog:{scope:'herdr',category:'development',label:'Development apps'}};
 await seed(page,{services:[service]});
 await page.route('**/api/v1/services',route=>{reads++;return reads===1?route.fulfill({json:{services:[service]}}):route.fulfill({status:503,json:{error:'temporary service feed failure'}});});
 await page.goto(`${base}/#/topology`);
 await page.locator('[data-tx-disclosure="host:beast"]>summary').click();
 await page.locator('[data-tx-disclosure="host:beast/service:development"]>summary').click();
 const search=page.getByRole('searchbox',{name:'Search topology'});await search.fill('Spectra');await search.focus();
 await page.waitForFunction(()=>document.body.innerText.includes('Services could not refresh.'),null,{timeout:15000});
 assert.ok(reads>=2);assert.equal(await search.inputValue(),'Spectra');assert.equal(await search.evaluate(e=>e===document.activeElement),true);
 assert.equal(await page.locator('[data-tx-disclosure="host:beast"]').evaluate(e=>e.open),true);
 assert.equal(await page.locator('[data-tx-disclosure="host:beast/service:development"]').evaluate(e=>e.open),true);
 await page.getByText('Spectra parser',{exact:true}).waitFor();assert.deepEqual(errors,[]);
 await page.evaluate(()=>{location.hash='/reviews';});await page.getByRole('heading',{name:'Reviews',exact:true}).waitFor();
 const before=reads;await page.waitForTimeout(5500);assert.equal(reads,before,'Topology polling stopped after navigation');
}));

test('topology groups explicit project identities and escapes endpoint and label content',async()=>withApp(async(page,base)=>{
 const stamp=new Date().toISOString();
 await seed(page,{services:[{service_id:'api/#?',service_name:'API <img src=x onerror=alert(1)>',project_id:'spectra',host_id:'beast',status:'healthy',updated_at:stamp,url:'javascript:alert(1)',catalog:{scope:'herdr',category:'development',label:'Development apps'}},{service_id:'inferred',service_name:'Unassigned frontend',host_id:'beast',status:'healthy',updated_at:stamp,catalog:{project:'spectra',project_source:'process.cwd'}}],sessions:[{session_id:'s1',title:'Parser worker',project_id:'spectra',host_id:'beast',status:'active',updated_at:stamp}]});
 await page.goto(`${base}/#/topology`);await page.getByRole('combobox',{name:'Group by'}).selectOption('project');
 await page.locator('[data-tx-disclosure="project:spectra"]>summary').click();
 await page.locator('[data-tx-disclosure="project:spectra/service:development"]>summary').click();
 await page.getByText('Parser worker',{exact:true}).waitFor();
 assert.equal(await page.locator('#tx-results img').count(),0);assert.equal(await page.locator('#tx-results a[href^="javascript:"]').count(),0);
 await page.locator('a[href="#/services/api%2F%23%3F"]').waitFor();
 assert.equal(await page.locator('a[href="#/services/api%2F%23%3F"]').count(),1);
 assert.equal(await page.locator('[data-tx-disclosure="unassigned"]').count(),1);
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
}));

test('topology group pagination preserves keyboard focus through refresh and the final page',async()=>withApp(async(page,base)=>{
 let reads=0;
 const sessions=Array.from({length:25},(_,index)=>({session_id:`s-${index}`,title:`Worker ${index+1}`,project_id:`project-${String(index+1).padStart(2,'0')}`,host_id:'beast',status:'active'}));
 await seed(page);
 await page.route('**/api/v1/sessions',route=>{reads++;return route.fulfill({json:{sessions:sessions.map(s=>({...s,updated_at:new Date().toISOString()}))}});});
 await page.goto(`${base}/#/topology`);
 await page.getByRole('combobox',{name:'Group by'}).selectOption('project');
 const pager=page.locator('[data-tx-more-groups-button]');
 await pager.waitFor();assert.equal(await page.locator('.tx-group').count(),12);
 await pager.click();
 assert.equal(await page.locator('.tx-group').count(),24);
 assert.equal(await pager.evaluate(e=>e===document.activeElement),true);
 const before=reads;await page.waitForTimeout(5500);assert.ok(reads>before);
 assert.equal(await pager.evaluate(e=>e===document.activeElement),true);
 await pager.press('Enter');
 assert.equal(await page.locator('.tx-group').count(),25);
 assert.equal(await pager.count(),0);
 assert.equal(await page.locator('[data-tx-disclosure="project:project-25"]>summary').evaluate(e=>e===document.activeElement),true);
}));

test('topology does not report zero current missions while one mission feed has never loaded',async()=>withApp(async(page,base)=>{
 let unavailable=true;
 await seed(page);
 await page.route('**/api/v1/missions',route=>route.fulfill({json:{missions:[{mission_id:'manual',title:'Manual plan',host_id:'beast',status:'active',updated_at:new Date().toISOString()}]}}));
 await page.route('**/api/v1/mission-activity',route=>unavailable?route.fulfill({status:503,json:{error:'temporarily unavailable'}}):route.fulfill({json:{activities:[]}}));
 await page.goto(`${base}/#/topology`);
 await page.waitForFunction(()=>document.body.innerText.includes('Live missions could not refresh.'),null,{timeout:10000});
 await page.locator('[data-tx-disclosure="host:beast"]>summary').click();
 await page.getByText('Manual plan',{exact:true}).waitFor();
 const metric=page.locator('[data-tx-metrics]>div').filter({hasText:'current missions'}).locator('strong');
 assert.equal(await metric.innerText(),'—');
 assert.match(await page.locator('[data-tx-result]').innerText(),/Partial view/);
 unavailable=false;
 await page.waitForFunction(()=>[...document.querySelectorAll('[data-tx-metrics]>div')].find(e=>e.innerText.includes('current missions'))?.querySelector('strong')?.textContent==='1',null,{timeout:12000});
}));

// Task 14 extras, on the shared fixture (stub API, fake clock).
const projection=(n,health)=>({contract_version:1,mission_key:`pi:M${n}`,host_id:'beast',observed_at:T0,snapshot_at:T0,activity_snapshot_at:T0,connection:'connected',
 mission:{id:`M${n}`,title:`Mission ${n}`,status:'EXECUTING'},freshness:{state:'live',age_seconds:1,stale_after_seconds:3600,disconnect_after_seconds:7200},
 activity:{health,currentActivity:{summary:`Working ${n}`}}});

test('topology: a current row shows a status dot whose colour follows the row (attention differs from ok)',async()=>withPage(async(page,base)=>{
 await boot(page,base,'/topology',p=>({
  '/api/v1/hosts':{hosts:[{host_id:'beast',host_name:'Beast',status:'healthy',last_seen:T0}]},
  '/api/v1/services':{services:[]},'/api/v1/sessions':{sessions:[]},'/api/v1/missions':{missions:[]},
  '/api/v1/mission-activity':{activities:[projection(1,'active'),projection(2,'stalled')]},
  '/api/v1/ci':{available:true,status:'connected',pipelines:{}},
 })[p]||{});
 await page.locator('[data-tx-disclosure="host:beast"]>summary').click();
 await until(()=>page.locator('[data-topology-entry] .tx-dot').count().then(n=>n===2),'two dotted rows');
 const dots=await page.evaluate(()=>[...document.querySelectorAll('[data-topology-entry]')].map(row=>({
  attention:!!row.querySelector('.tx-warning'),colour:getComputedStyle(row.querySelector('.tx-dot')).backgroundColor})));
 const ok=dots.find(d=>!d.attention),attention=dots.find(d=>d.attention);
 assert.ok(ok&&attention,JSON.stringify(dots));
 assert.notEqual(attention.colour,ok.colour,'an attention row is not shown with the healthy dot');
 assert.equal(attention.colour,await page.evaluate(()=>{const p=document.createElement('p');p.style.color='var(--status-attention)';document.body.append(p);const c=getComputedStyle(p).color;p.remove();return c;}));
}));

test('services: "last report" ticks in place without re-rendering rows',async()=>withPage(async(page,base)=>{
 const services=['a','b'].map(id=>({service_id:id,service_name:`Service ${id}`,status:'healthy',host_id:'beast',port:8000,updated_at:T0,catalog:{scope:'herdr'}}));
 await boot(page,base,'/services?status=all',p=>p==='/api/v1/services'?{services}:{});
 await until(()=>page.locator('[data-service-id]').count().then(n=>n===2),'two services');
 await page.evaluate(()=>{
  window.__rows=[...document.querySelectorAll('[data-service-id]')];window.__structural=0;
  new MutationObserver(rs=>{for(const r of rs)if(r.type==='childList'&&[...r.addedNodes,...r.removedNodes].some(n=>n.nodeType===1))window.__structural++;})
   .observe(document.getElementById('sc-results'),{subtree:true,childList:true});
 });
 const seen=page.locator('[data-service-id="a"] [data-service-seen]');
 const first=await seen.textContent();
 await page.clock.runFor(5000);
 await until(()=>seen.textContent().then(t=>t!==first),'age ticks');
 const r=await page.evaluate(()=>({same:[...document.querySelectorAll('[data-service-id]')].every((row,i)=>row===window.__rows[i]),structural:window.__structural}));
 assert.deepEqual(r,{same:true,structural:0});
}));
