import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdir,readFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';

const root=path.resolve(import.meta.dirname,'../..');
const packages=path.join(root,'packages');
const evidence=path.join(root,'docs/evidence/tab-supervision');

const fixture={
  room_id:'room-personal',workspace_id:'workspace-personal',jobs:[{
    job_id:'job-release',tab_id:'tab-release',root_agent_id:'agent-root',title:'Release readiness',agents:[
      {agent_id:'agent-root',supervisor_id:null,name:'Primary assistant',role:'supervisor',responsibility:'Integrate verified results',status:'running',health:'healthy',budget:{used_tokens:210,token_limit:1000},checkpoint:{progress:3,evidence_ref:'commit:abc123'}},
      {agent_id:'agent-review',supervisor_id:'agent-root',name:'Independent reviewer',role:'reviewer',responsibility:'Review authorization boundaries',status:'verified',health:'verified',budget:{used_tokens:80,token_limit:250},checkpoint:{progress:2,evidence_ref:'review:approved'}},
    ],
  }],unassigned:[{agent_id:'agent-unknown',name:'Unknown <img src=x onerror=alert(1)>',role:'researcher',provenance:'not_registered_in_supervision'}],
};

async function withTree(run,{viewport={width:1440,height:1000}}={}){
  const server=createServer(async(req,res)=>{
    if(req.url==='/'){
      res.setHeader('content-type','text/html');
      res.end('<!doctype html><html><head><style>:root{--surface:#fff;--surface-2:#f5f7f6;--text:#17201c;--text-secondary:#58645e;--border:#ccd5d0;--border-subtle:#e2e8e5;--accent:#14734b;--radius:8px;--radius-sm:5px}body{margin:20px;font:14px system-ui;background:#f7f8f7;color:var(--text)}#tree{max-width:1200px;margin:auto}</style></head><body><main id="tree"></main></body></html>');
      return;
    }
    const file=path.resolve(packages,'.'+decodeURIComponent(req.url.split('?')[0]));
    if(!file.startsWith(packages+path.sep)){res.writeHead(404);res.end();return;}
    try{res.setHeader('content-type','text/javascript');res.end(await readFile(file));}catch{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.PLAYWRIGHT_CHROMIUM||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
    const page=await browser.newPage({viewport});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(async data=>{
      const {mountSupervisionTree}=await import('/supervision-tree.js');
      window.supervision=mountSupervisionTree(document.querySelector('#tree'),data);
    },fixture);
    await run(page);
    assert.deepEqual(errors,[]);
  }finally{
    await browser?.close();
    await new Promise(resolve=>server.close(resolve));
  }
}

async function withDashboard(run){
  const server=createServer(async(req,res)=>{
    const requestPath=req.url==='/'?'/index.html':decodeURIComponent(req.url.split('?')[0]);
    const file=path.resolve(packages,'.'+requestPath);
    if(!file.startsWith(packages+path.sep)){res.writeHead(404);res.end();return;}
    try{
      const content=await readFile(file);
      res.setHeader('content-type',file.endsWith('.html')?'text/html':file.endsWith('.css')?'text/css':'text/javascript');
      res.end(content);
    }catch{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.PLAYWRIGHT_CHROMIUM||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await run(page,`http://127.0.0.1:${server.address().port}`);
    assert.deepEqual(errors,[]);
  }finally{
    await browser?.close();
    await new Promise(resolve=>server.close(resolve));
  }
}

test('tree renders explicit job ancestry and reviewer evidence',()=>withTree(async page=>{
  assert.equal(await page.locator('[data-job-id="job-release"]').evaluate(node=>node.open),false);
  await page.getByText('Release readiness',{exact:false}).click();
  await page.getByText('Primary assistant',{exact:true}).click();
  await page.getByRole('button',{name:'Select Independent reviewer'}).click();
  const detail=page.getByRole('complementary',{name:'Agent details'});
  assert.match(await detail.innerText(),/Role\s+reviewer/);
  assert.match(await detail.innerText(),/Responsibility\s+Review authorization boundaries/);
  assert.match(await detail.innerText(),/Budget\s+80 \/ 250 tokens/);
  assert.match(await detail.innerText(),/Evidence: review:approved/);
  await mkdir(evidence,{recursive:true});
  await page.screenshot({path:path.join(evidence,'supervision-tree-desktop.png'),fullPage:true});
}));

test('unknown provenance remains separate and hostile names render as text',()=>withTree(async page=>{
  assert.equal(await page.locator('.sv-unknown').evaluate(node=>node.open),false);
  await page.getByText('Unknown or unadopted provenance',{exact:false}).click();
  assert.equal(await page.locator('#tree img').count(),0);
  assert.match(await page.locator('#tree').innerText(),/Unknown <img src=x onerror=alert\(1\)>/);
  assert.equal(await page.locator('[data-agent-id="agent-unknown"]').evaluate(node=>node.closest('[data-job-id]')===null),true);
}));

test('search reveals matching descendants and their explicit ancestors',()=>withTree(async page=>{
  const search=page.getByRole('searchbox',{name:'Search agent tree'});
  await search.fill('authorization boundaries');
  assert.equal(await page.getByText('Primary assistant',{exact:true}).count(),1);
  assert.equal(await page.getByText('Independent reviewer',{exact:true}).count(),1);
  assert.equal(await page.getByText('Unknown <img src=x onerror=alert(1)>',{exact:true}).count(),0);
}));

test('refresh preserves expanded branches, selection and keyboard focus',()=>withTree(async page=>{
  await page.getByText('Release readiness',{exact:false}).click();
  await page.getByText('Primary assistant',{exact:true}).click();
  const inspect=page.getByRole('button',{name:'Select Independent reviewer'});
  await inspect.click();
  await inspect.focus();
  await page.evaluate(data=>window.supervision.update(structuredClone(data)),fixture);
  assert.equal(await page.locator('[data-job-id="job-release"]').evaluate(node=>node.open),true);
  assert.equal(await page.locator('[data-agent-id="agent-root"] details').evaluate(node=>node.open),true);
  assert.match(await page.getByRole('complementary',{name:'Agent details'}).innerText(),/agent-review/);
  assert.equal(await page.getByRole('button',{name:'Select Independent reviewer'}).evaluate(node=>node===document.activeElement),true);
}));

test('mobile tree contains long identities and details without document overflow',()=>withTree(async page=>{
  await page.setViewportSize({width:390,height:844});
  await page.getByText('Release readiness',{exact:false}).click();
  await page.getByText('Primary assistant',{exact:true}).click();
  await page.getByRole('button',{name:'Select Independent reviewer'}).click();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await mkdir(evidence,{recursive:true});
  await page.screenshot({path:path.join(evidence,'supervision-tree-mobile.png'),fullPage:true});
},{viewport:{width:390,height:844}}));

test('catalog excludes guest rooms and preserves each room tree expansion',()=>withTree(async page=>{
  await page.evaluate(async data=>{
    window.supervision.dispose();
    const host=document.querySelector('#tree');host.innerHTML='';
    const {mountSupervisionCatalog}=await import('/supervision-catalog.js');
    const {mountSupervisionTree}=await import('/supervision-tree.js');
    const second={...structuredClone(data),room_id:'room-team',workspace_id:'workspace-team',jobs:[{...structuredClone(data.jobs[0]),job_id:'job-team',tab_id:'tab-team',title:'Team delivery'}],unassigned:[]};
    window.catalogData={'room-personal':data,'room-team':second};
    window.catalogTrees=[];
    window.catalog=mountSupervisionCatalog(host,{request:async requestPath=>{
      if(requestPath==='/api/v1/collab/rooms')return {rooms:[{room_id:'room-personal',name:'Personal',role:'owner'},{room_id:'room-team',name:'Team',role:'contributor'},{room_id:'room-secret',name:'Secret',role:'guest'}]};
      const room=decodeURIComponent(requestPath.split('/').at(-2));return structuredClone(window.catalogData[room]);
    }},{pollMs:10000,treeMount:(...args)=>{const tree=mountSupervisionTree(...args);window.catalogTrees.push(tree);return tree;}});
    await window.catalog.ready;
  },fixture);
  const room=page.getByRole('combobox',{name:'Collaboration room'});
  assert.deepEqual(await room.locator('option').allTextContents(),['Personal','Team']);
  await page.getByText('Release readiness',{exact:false}).click();
  assert.deepEqual(await page.evaluate(()=>[...window.catalogTrees[0].state().expanded]),['job-release']);
  await room.selectOption('room-team');await page.getByText('Team delivery',{exact:false}).waitFor();
  await room.selectOption('room-personal');await page.getByText('Release readiness',{exact:false}).waitFor();
  assert.equal(await page.locator('[data-job-id="job-release"]').evaluate(node=>node.open),true);
}));

test('catalog clears previously authorized tree data after a refresh error',()=>withTree(async page=>{
  await page.evaluate(async data=>{
    window.supervision.dispose();
    const host=document.querySelector('#tree');host.innerHTML='';
    const {mountSupervisionCatalog}=await import('/supervision-catalog.js');
    window.catalogFail=false;
    window.catalog=mountSupervisionCatalog(host,{request:async requestPath=>{
      if(requestPath==='/api/v1/collab/rooms')return {rooms:[{room_id:'room-personal',name:'Personal',role:'owner'}]};
      if(window.catalogFail)throw new Error('authorization expired');
      return structuredClone(data);
    }},{pollMs:10000});
    await window.catalog.ready;
  },fixture);
  await page.getByText('Release readiness',{exact:false}).waitFor();
  await page.evaluate(async()=>{window.catalogFail=true;await window.catalog.refresh();});
  assert.equal(await page.locator('[data-supervision-status]').getAttribute('data-state'),'error');
  assert.equal(await page.locator('[data-supervision-tree] [data-agent-id]').count(),0);
  assert.equal(await page.getByText('Primary assistant',{exact:true}).count(),0);
}));

test('dashboard Agents, Assistants and Collaboration mount the same authorized job tree',()=>withDashboard(async(page,base)=>{
  let stateReads=0;
  await page.route('**/api/v1/**',async route=>{
    const url=new URL(route.request().url());
    const requestPath=url.pathname;
    if(requestPath==='/api/v1/sessions')return route.fulfill({json:{sessions:[]}});
    if(requestPath==='/api/v1/missions')return route.fulfill({json:{missions:[]}});
    if(requestPath==='/api/v1/collab/config')return route.fulfill({json:{client_id:'fixture',hosted_ui_domain:'https://auth.example.test',public_url:'https://collab.example.test',operator:{user_id:'owner',email:'owner@example.test'}}});
    if(requestPath==='/api/v1/collab/rooms')return route.fulfill({json:{rooms:[{room_id:'room-personal',name:'Personal assistant',role:'owner',project_id:'project'}]}});
    if(requestPath==='/api/v1/collab/rooms/room-personal/supervision')return route.fulfill({json:fixture});
    if(requestPath==='/api/v1/collab/rooms/room-personal/state'){
      stateReads+=1;
      if(stateReads>1)await new Promise(resolve=>setTimeout(resolve,250));
      return route.fulfill({json:stateReads===1?{changed:true,seq:1,cursor:0,events:[],agents:[],tasks:[],requests:[],flow:{room_id:'room-personal',nodes:[],edges:[],spawns:[],tasks:[],supervision:fixture}}:{changed:false,seq:1}});
    }
    if(requestPath==='/api/v1/collab/rooms/room-personal')return route.fulfill({json:{room_id:'room-personal',name:'Personal assistant',role:'owner',members:[]}});
    if(requestPath.endsWith('/invites'))return route.fulfill({json:{invites:[]}});
    if(requestPath.endsWith('/profile'))return route.fulfill({json:{allowed_families:[],limits:{}}});
    if(requestPath==='/api/v1/collab/agents')return route.fulfill({json:{agents:[]}});
    if(requestPath==='/api/v1/collab/helpers')return route.fulfill({json:{helpers:[]}});
    if(requestPath.includes('/fleet/'))return route.fulfill({json:{hosts:[],requests:[],helpers:[],allowlist:[],entries:[]}});
    return route.fulfill({json:{}});
  });

  await page.goto(`${base}/#/herd`);
  await page.getByRole('heading',{name:'Agents',exact:true}).waitFor();
  await page.getByText('Release readiness',{exact:false}).click();
  assert.equal(await page.locator('[data-agent-id="agent-root"]').count(),1);
  assert.equal(await page.locator('[data-agent-id="agent-review"]').count(),1);
  await mkdir(evidence,{recursive:true});
  await page.screenshot({path:path.join(evidence,'supervision-dashboard-desktop.png'),fullPage:true});

  await page.evaluate(()=>{location.hash='/assistants';});
  await page.getByRole('heading',{name:'Personal Assistants',exact:true}).waitFor();
  await page.getByText('Release readiness',{exact:false}).click();
  assert.equal(await page.locator('[data-agent-id="agent-root"]').count(),1);
  assert.equal(await page.locator('[data-agent-id="agent-review"]').count(),1);
  await page.setViewportSize({width:390,height:844});
  const assistantHeading=page.getByRole('heading',{name:'Personal Assistants',exact:true});
  await assistantHeading.waitFor();
  await page.evaluate(()=>{scrollTo(0,0);return new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));});
  assert.equal(await assistantHeading.evaluate(node=>{const box=node.getBoundingClientRect();return box.top>=0&&box.top<innerHeight&&box.bottom>0;}),true);
  assert.deepEqual(await page.evaluate(()=>({shells:document.querySelectorAll('herdr-app-shell').length,sidebars:document.querySelector('herdr-app-shell')?.shadowRoot?.querySelectorAll('.hd-shell__sidebar').length})),{shells:1,sidebars:1});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:path.join(evidence,'supervision-dashboard-mobile.png')});

  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(()=>{location.hash='/collab';});
  const roomButton=page.locator('[data-room="room-personal"]');
  await page.waitForTimeout(500);
  assert.equal(await roomButton.count(),1,await page.locator('#view').innerText());
  await roomButton.click();
  const flowTab=page.getByRole('tab',{name:/Flow/});
  await flowTab.waitFor({timeout:5000});await flowTab.click();
  await page.locator('#hcn-supervision [data-job-id="job-release"]').waitFor({timeout:5000});
  await page.locator('#hcn-supervision [data-job-id="job-release"]>summary').click();
  assert.equal(await page.locator('#hcn-supervision [data-agent-id="agent-root"]').count(),1);
  assert.equal(await page.locator('#hcn-supervision [data-agent-id="agent-review"]').count(),1);
}));
