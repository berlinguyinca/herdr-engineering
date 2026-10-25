// G9: read-only GitHub Projects boards. Grouped by the project's Status field (field order, empty
// columns shown as empty), a flat-list toggle, cards patched in place from since deltas.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';

const root=path.resolve(import.meta.dirname,'../..');
const python=process.env.PYTHON || (fs.existsSync(path.join(root,'.venv/bin/python'))?path.join(root,'.venv/bin/python'):'python3');
// burst '0': no live event burst, so every list read comes from the 5 s poll (tests that count reads).
async function withApp(fn,{burst='1'}={}){
 const server=spawn(python,[path.join(root,'tests/playwright/server.py')],{env:{...process.env,BURST:burst},stdio:['ignore','pipe','pipe']});
 let browser;
 try{
  const port=await new Promise((resolve,reject)=>{let text='';const timeout=setTimeout(()=>reject(new Error('Fixture startup timed out')),20000);server.stdout.on('data',data=>{text+=data;const hit=text.match(/PORT=(\d+)/);if(hit){clearTimeout(timeout);resolve(hit[1]);}});server.once('exit',code=>{clearTimeout(timeout);reject(new Error(`Fixture exited ${code}`));});});
  const options={headless:true,args:['--no-sandbox']};
  if(process.env.PLAYWRIGHT_CHROMIUM)options.executablePath=process.env.PLAYWRIGHT_CHROMIUM;
  try{browser=await chromium.launch(options);}catch(error){const fallback='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';if(!fs.existsSync(fallback))throw error;browser=await chromium.launch({...options,executablePath:fallback});}
  const page=await browser.newPage();await fn(page,`http://127.0.0.1:${port}`);
 }finally{await browser?.close();server.kill();}
}
const options=[{id:'o1',name:'Todo'},{id:'o2',name:'In progress'},{id:'o3',name:'Done'}];
const sync=(extra={})=>({available:true,status:'ready',connection_generation:1,projects_sync:{enabled:true,status:'ready',error:null,projects:2,deferred:0},...extra});
const board=(id,extra={})=>({project_id:id,owner:'acme',number:id==='PVT_1'?1:2,title:id==='PVT_1'?'Roadmap':'Plain',url:'https://github.com/orgs/acme/projects/1',closed:false,updated_at:'2026-10-24T00:00:00Z',item_count:3,status_options:options,complete:true,error:null,...extra});
const card=(id,option,position,extra={})=>({item_id:id,project_id:'PVT_1',type:'ISSUE',status_option:option,status:options.find(o=>o.id===option)?.name??null,title:`Card ${id}`,number:position+1,url:`https://github.com/acme/app/issues/${position+1}`,repository:'acme/app',state:'OPEN',position,...extra});

test('a project board groups cards by Status, shows empty columns, toggles a flat list and patches one card',async()=>withApp(async(page,base)=>{
 let revision=5,items=[card('I1','o1',0),card('I2','o1',1),card('I3','o3',2)],changed=[];const seen=[],writes=[];
 page.on('request',r=>{if(!['GET','HEAD','OPTIONS'].includes(r.method()))writes.push(r.method());});
 await page.route('**/api/v1/github/projects*',route=>{
  const url=new URL(route.request().url()),project=url.searchParams.get('project'),since=url.searchParams.get('since');
  seen.push({project,since});
  if(!project)return route.fulfill({json:{...sync(),revision,projects:[board('PVT_1'),board('PVT_2',{status_options:null,item_count:1})]}});
  if(project==='PVT_2')return route.fulfill({json:{...sync(),revision,project:board('PVT_2',{status_options:null}),items:[card('J1',null,0,{project_id:'PVT_2',title:'Loose card'})]}});
  if(since)return route.fulfill({json:{...sync(),revision,resync:false,project:board('PVT_1'),upserted:changed,removed:[]}});
  return route.fulfill({json:{...sync(),revision,project:board('PVT_1'),items}});
 });
 await page.clock.install();
 await page.goto(`${base}/#/github/projects`);await page.locator('[data-ghp-item="I1"]').waitFor();
 const columns=()=>page.evaluate(()=>[...document.querySelectorAll('[data-ghp-column]')].map(c=>({name:c.querySelector('[data-ghp-column-name]').textContent,cards:[...c.querySelectorAll('[data-ghp-item]:not([data-leaving])')].map(n=>n.dataset.ghpItem),empty:c.querySelector('.ghp-empty')?.textContent||null})));
 assert.deepEqual(await columns(),[{name:'Todo',cards:['I1','I2'],empty:null},{name:'In progress',cards:[],empty:'No items'},{name:'Done',cards:['I3'],empty:null}]);
 assert.equal(await page.locator('[data-ghp-board] button, [data-ghp-board] input').count(),0,'read-only cards');
 // One card moves: the delta carries it alone; the other cards keep their nodes.
 await page.evaluate(()=>{for(const n of document.querySelectorAll('[data-ghp-item]'))n.__mark=n.dataset.ghpItem;});
 revision=6;changed=[card('I2','o2',1,{title:'Card I2 started'})];items=[items[0],changed[0],items[2]];
 await page.clock.runFor(5000);
 await page.locator('[data-ghp-column="o2"] [data-ghp-item="I2"]').waitFor();
 assert.ok(seen.some(s=>s.project==='PVT_1'&&s.since==='5'),'the poll asks since the held revision');
 assert.deepEqual(await columns(),[{name:'Todo',cards:['I1'],empty:null},{name:'In progress',cards:['I2'],empty:null},{name:'Done',cards:['I3'],empty:null}]);
 assert.deepEqual(await page.evaluate(()=>['I1','I3'].map(id=>document.querySelector(`[data-ghp-item="${id}"]`).__mark)),['I1','I3']);
 // Flat list toggle: every item in board order with its status; the board hides.
 await page.getByRole('button',{name:'Flat list'}).click();
 assert.equal(await page.getByRole('button',{name:'Flat list'}).getAttribute('aria-pressed'),'true');
 assert.equal(await page.locator('[data-ghp-board]').isVisible(),false);
 assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('[data-ghp-list] [data-ghp-item]')].map(n=>[n.dataset.ghpItem,n.querySelector('.ghp-status').textContent])),[['I1','Todo'],['I2','In progress'],['I3','Done']]);
 assert.match(await page.evaluate(()=>location.hash),/layout=list/);
 // A project without a Status field is a list only.
 await page.locator('[data-ghp-project]').selectOption('PVT_2');await page.locator('[data-ghp-item="J1"]').waitFor();
 assert.match(await page.locator('[data-ghp-summary]').innerText(),/no Status field/);
 assert.equal(await page.locator('[data-ghp-board]').isVisible(),false);
 assert.match(await page.locator('[data-ghp-list] [data-ghp-item="J1"] .ghp-status').innerText(),/No status/);
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 assert.deepEqual(writes,[]);
}));

test('the page says when project sync is off',async()=>withApp(async(page,base)=>{
 await page.route('**/api/v1/github/projects*',route=>route.fulfill({json:{...sync({projects_sync:{enabled:false,status:'disabled',error:null,projects:0,deferred:0}}),revision:1,projects:[]}}));
 await page.goto(`${base}/#/github/projects`);
 await page.getByText(/Project sync is off/).waitFor();
 assert.match(await page.locator('[data-ghp-sync]').innerText(),/github\.projects\.enabled/);
 assert.equal(await page.locator('[data-ghp-project]').isDisabled(),true);
}));

// Project rows carry fields no revision covers (complete, error): the projects list is read in full
// every 12th read and on the read after projects_sync changed.
test('the projects list is read in full every 12th read and after the sync state moved',async()=>withApp(async(page,base)=>{
 let rowError=null,status='ready';const reads=[];
 await page.route('**/api/v1/github/projects*',route=>{
  const url=new URL(route.request().url()),project=url.searchParams.get('project'),since=url.searchParams.get('since');
  const state=sync({projects_sync:{enabled:true,status,error:null,projects:1,deferred:0}});
  if(project)return route.fulfill({json:{...state,revision:3,project:board('PVT_1'),items:[card('I1','o1',0)]}});
  reads.push(since);
  return route.fulfill({json:since===null?{...state,revision:3,projects:[board('PVT_1',{complete:rowError===null,error:rowError})]}:{...state,revision:3,resync:false,upserted:[],removed:[]}});
 });
 await page.clock.install();
 await page.goto(`${base}/#/github/projects`);await page.locator('[data-ghp-item="I1"]').waitFor();
 const poll=async()=>{const n=reads.length;await page.clock.runFor(5000);for(let i=0;i<200&&reads.length===n;i++)await page.waitForTimeout(25);};
 const until=async(test,label)=>{for(let i=0;i<40&&!test();i++)await poll();assert.ok(test(),label);await page.waitForTimeout(150);};
 const summary=()=>page.locator('[data-ghp-summary]').innerText();
 rowError='The project has more than github.projects.item_limit (2000) items';
 await until(()=>reads.length>=12,'12 reads');
 assert.deepEqual(reads.slice(1,12),Array(11).fill('3'));assert.doesNotMatch(await summary(),/item_limit/);
 await until(()=>reads.length>=13,'13 reads');
 assert.equal(reads[12],null,'every 12th read is full');
 assert.match(await summary(),/item_limit/,'the row error is repaired');
 const before=reads.length;status='partial';rowError=null;
 await until(()=>reads.length>=before+2,'two more reads');
 assert.deepEqual(reads.slice(before,before+2),['3',null],'the delta carrying the new sync state, then a full read');
 assert.doesNotMatch(await summary(),/item_limit/);
},{burst:'0'}));
