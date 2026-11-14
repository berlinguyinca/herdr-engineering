// Component browser regression: the local clock must expire unchanged reports,
// without waiting for a provider refresh or a lifecycle status update.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile, access} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';

const packages=path.resolve(import.meta.dirname,'../../packages');
async function browser() {
  const options={headless:true,args:['--no-sandbox']};
  if(process.env.PLAYWRIGHT_CHROMIUM)return chromium.launch({...options,executablePath:process.env.PLAYWRIGHT_CHROMIUM});
  try{return await chromium.launch(options);}catch(error){
    const fallback=(process.env.HOME+'/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome');
    try{await access(fallback);}catch{throw error;}
    return chromium.launch({...options,executablePath:fallback});
  }
}

async function withComponents(run) {
  const server=createServer(async(req,res)=>{
    if(req.url==='/'){res.setHeader('content-type','text/html');res.end('<!doctype html><html><body><div id="services"></div><div id="sessions"></div><div id="missions"></div><div id="overview"></div></body></html>');return;}
    const file=path.resolve(packages,'.'+decodeURIComponent(req.url));
    if(!file.startsWith(packages+path.sep)){res.writeHead(404);res.end();return;}
    try{const contents=await readFile(file);res.setHeader('content-type','text/javascript');res.end(contents);}catch{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let instance;
  try{
    instance=await browser();
    const page=await instance.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.clock.install({time:new Date('2026-10-20T12:00:00Z')});
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await run(page);
    assert.deepEqual(errors,[]);
  }finally{if(instance)await instance.close();await new Promise(resolve=>server.close(resolve));}
}

async function mountPaged(page) {
  await page.evaluate(async()=>{
    const {mountServices}=await import('/services-view.js');
    const service=id=>({service_id:id,service_name:id,host_id:'host',status:'healthy',updated_at:new Date(Date.now()).toISOString(),catalog:{scope:'herdr',project_id:'project',label:'Application'}});
    const body=(ids,more=false)=>({services:ids.map(service),as_of:new Date(Date.now()).toISOString(),snapshot_id:'snap',sort:'group-name-id-nfc-codepoint-v1',
      counts:{scope_total:20,current:10,unknown:7,unavailable:2,archived:1,history_total:10,matching_total:10,matching_groups:1},
      facets:{hosts:[{value:'host',count:20},{value:'other',count:0}]},groups:[{key:'project:project',label:'project',total:10,states:{current:10,unknown:0,unavailable:0,archived:0}}],
      page:{limit:2,returned:ids.length,has_more:more,next_cursor:more?'next':null}});
    window.requests=[]; window.body=body; window.fail=false; window.defer=false; window.pending=[];
    window.client={listServiceCatalog:async options=>{
      window.requests.push(options);
      if(window.defer)return new Promise(resolve=>window.pending.push({options,resolve}));
      if(window.fail)throw {status:400,message:'bad cursor'};
      return options.cursor?body(['b','c']):body(['a','b'],true);
    },listServices:()=>{throw new Error('legacy fetch forbidden');}};
    window.view=mountServices(document.querySelector('#services'),window.client,body(['a','b'],true),{scope:'herdr'});
  });
}

test('paged catalog uses full metadata, merges groups and preserves open state and focus',()=>withComponents(async page=>{
  await mountPaged(page);
  assert.match(await page.locator('#sc-counts').innerText(),/10/);
  assert.match(await page.locator('#sc-history-summary').innerText(),/10 history/);
  assert.equal(await page.locator('#sc-host option').count(),3);
  assert.match(await page.locator('#sc-result').innerText(),/10 matching services · 1 groups/);
  await page.locator('.sc-group summary').click();
  await page.locator('#sc-load-more').click();
  assert.equal(await page.locator('.sc-group').count(),1);
  assert.equal(await page.locator('.sc-row').count(),3);
  assert.equal(await page.locator('.sc-group').getAttribute('open'),null);
  assert.match(await page.locator('.sc-group summary').innerText(),/10 services/);
  await page.evaluate(()=>window.view.refresh());
  assert.equal(await page.locator('.sc-group').getAttribute('open'),null);
  assert.equal(await page.locator('.sc-row').count(),3);
  assert.equal(await page.evaluate(()=>window.requests.filter(r=>r.cursor).length),2);
}));

test('filter changes abort old reads, discard late replies and retain data on 400',()=>withComponents(async page=>{
  await mountPaged(page);
  await page.evaluate(()=>{window.defer=true;});
  await page.locator('#sc-host').selectOption('other');
  await page.waitForFunction(()=>window.pending.length===1);
  await page.locator('#sc-host').selectOption('host');
  await page.waitForFunction(()=>window.pending.length===2);
  assert.equal(await page.evaluate(()=>window.pending[0].options.signal.aborted),true);
  await page.evaluate(()=>window.pending[1].resolve(window.body(['new'])));
  await page.waitForSelector('[data-service-id="new"]');
  await page.evaluate(()=>window.pending[0].resolve(window.body(['old'])));
  assert.equal(await page.locator('[data-service-id="old"]').count(),0);
  await page.evaluate(async()=>{window.defer=false;window.fail=true;await window.view.refresh();});
  assert.equal(await page.locator('[data-service-id="new"]').count(),1);
  assert.match(await page.locator('#sc-error').innerText(),/Could not refresh/);
}));

test('local expiry removes unsafe endpoints immediately and detail uses only getService',()=>withComponents(async page=>{
  await mountPaged(page);
  await page.evaluate(()=>{
    window.fail=true;
    // Stop polling from refreshing the original observation before it expires.
  });
  await page.clock.runFor(901000);
  assert.equal(await page.locator('.sc-row').count(),0);
  await page.evaluate(async()=>{
    window.view.dispose();
    const {mountServiceDetails}=await import('/services-view.js');
    const service=window.body(['detail']).services[0];
    window.details=[];
    window.detail=mountServiceDetails(document.querySelector('#services'),{
      getService:async(id,{signal})=>{window.details.push({id,aborted:signal.aborted});return service;},
      listServices:()=>{throw new Error('full listing forbidden');}},service);
    await window.detail.refresh();
  });
  assert.deepEqual(await page.evaluate(()=>window.details),[{id:'detail',aborted:false}]);
}));

test('expired cursors restart a loaded window and 400 cursors never fall back',()=>withComponents(async page=>{
  await mountPaged(page);
  await page.evaluate(()=>{
    let expired=false;
    window.client.listServiceCatalog=async options=>{
      window.requests.push(options);
      if(options.cursor&&!expired){expired=true;throw {status:409};}
      return window.body(options.cursor?['c','d']:['fresh','b'],!options.cursor);
    };
  });
  await page.locator('#sc-load-more').click();
  await page.waitForSelector('[data-service-id="fresh"]');
  assert.equal(await page.locator('[data-service-id="a"]').count(),0);
  assert.deepEqual(await page.evaluate(()=>window.requests.map(o=>o.cursor)),['next',null]);
  await page.evaluate(()=>{window.client.listServiceCatalog=async options=>{window.requests.push(options);throw {status:400};};});
  await page.locator('#sc-load-more').click();
  await page.waitForFunction(()=>document.querySelector('#sc-error').textContent.includes('Could not refresh'));
  assert.equal(await page.locator('[data-service-id="fresh"]').count(),1);
  assert.equal(await page.evaluate(()=>window.requests.length),3);
}));

test('paged status and archive toggles retain secondary scope and group queries',()=>withComponents(async page=>{
  await mountPaged(page);
  await page.locator('#sc-status').selectOption('archived');
  await page.waitForFunction(()=>window.requests.length===1);
  assert.deepEqual(await page.evaluate(()=>({status:window.requests[0].status,archived:window.requests[0].includeArchived,cursor:window.requests[0].cursor})),{status:'archived',archived:true,cursor:null});
  await page.locator('#sc-archived').uncheck();
  await page.waitForFunction(()=>window.requests.length===2);
  assert.equal(await page.evaluate(()=>window.requests[1].status),'current');
  await page.locator('#sc-group').selectOption('host');
  await page.waitForFunction(()=>window.requests.length===3);
  assert.equal(await page.evaluate(()=>window.requests[2].groupBy),'host');
  await page.evaluate(()=>window.view.dispose());
  await page.evaluate(async()=>{
    const {mountServices}=await import('/services-view.js');
    const body=window.body([]); body.counts.current=0;
    window.view=mountServices(document.querySelector('#services'),window.client,body,{scope:'local'});
    await window.view.refresh();
  });
  assert.equal(await page.evaluate(()=>window.requests.at(-1).scope),'unassigned');
  assert.equal(await page.evaluate(()=>window.requests.at(-1).groupBy),'type');
}));

test('load-more completion keeps keyboard focus in a surviving group',()=>withComponents(async page=>{
  await mountPaged(page);
  await page.locator('#sc-load-more').focus();
  await page.locator('#sc-load-more').press('Enter');
  await page.waitForFunction(()=>document.querySelector('#sc-load-more').hidden);
  assert.equal(await page.evaluate(()=>document.querySelector('#services').contains(document.activeElement)&&document.activeElement!==document.querySelector('#sc-load-more')),true);
}));

test('paged rows retain codepoint server order and disposal aborts in-flight requests',()=>withComponents(async page=>{
  await mountPaged(page);
  await page.evaluate(async()=>{
    window.client.listServiceCatalog=async()=>window.body(['Z','a','é']);
    await window.view.refresh();
  });
  assert.deepEqual(await page.locator('.sc-row').evaluateAll(rows=>rows.map(row=>row.dataset.serviceId)),['Z','a','é']);
  await page.evaluate(()=>{
    window.client.listServiceCatalog=async options=>{window.activeSignal=options.signal;return new Promise(resolve=>{window.finish=resolve;});};
    window.view.refresh();
  });
  await page.evaluate(()=>window.view.dispose());
  assert.equal(await page.evaluate(()=>window.activeSignal.aborted),true);
  await page.evaluate(()=>window.finish(window.body(['late'])));
  assert.equal(await page.locator('[data-service-id="late"]').count(),0);
}));

test('detail compatibility prefers getService then request then old list-only clients',()=>withComponents(async page=>{
  const result=await page.evaluate(async()=>{
    const {mountServiceDetails}=await import('/services-view.js');
    const root=document.querySelector('#services');
    const initial={service_id:'a/b?#',service_name:'original',status:'healthy',updated_at:new Date(Date.now()).toISOString(),catalog:{scope:'herdr'}};
    const calls=[];
    const updated={...initial,service_name:'updated'};
    const clients=[
      {getService:async(id,{signal})=>{calls.push(['getService',id,signal instanceof AbortSignal]);return updated;},request:()=>{throw Error('wrong fallback');},listServices:()=>{throw Error('full fetch');}},
      {request:async(url,{signal})=>{calls.push(['request',url,signal instanceof AbortSignal]);return updated;},listServices:()=>{throw Error('full fetch');}},
      {listServices:async({signal})=>{calls.push(['listServices',signal instanceof AbortSignal]);return {services:[{...updated,service_id:'other'},updated]};}},
    ];
    const names=[];
    for(const client of clients){const view=mountServiceDetails(root,client,initial);await view.refresh();names.push(root.querySelector('h1').textContent);view.dispose();}
    return {calls,names};
  });
  assert.deepEqual(result.names,['updated','updated','updated']);
  assert.deepEqual(result.calls,[['getService','a/b?#',true],['request','/api/v1/services/a%2Fb%3F%23',true],['listServices',true]]);
}));

for(const expand of [true,false])test(`${expand?'Expand':'Collapse'} groups applies to groups arriving on later pages`,()=>withComponents(async page=>{
  await mountPaged(page);
  await page.evaluate(async expand=>{
    window.view.dispose();
    const {mountServices}=await import('/services-view.js');
    function groupedBody(id,project,next){
      const result=window.body([id],!!next);
      result.services[0].catalog.project_id=project;
      result.counts.matching_groups=expand?4:2;
      result.groups=[{key:'project:'+project,label:project,total:1,states:{current:1,unknown:0,unavailable:0,archived:0}}];
      result.page.next_cursor=next;
      return result;
    }
    window.client.listServiceCatalog=async()=>groupedBody('b','future',null);
    window.view=mountServices(document.querySelector('#services'),window.client,groupedBody('a','first','next'));
  },expand);
  await page.locator(expand?'#sc-expand':'#sc-collapse').click();
  await page.locator('#sc-load-more').click();
  await page.waitForSelector('[data-group="project:future"]');
  assert.equal(await page.locator('[data-group="project:future"]').evaluate(node=>node.open),expand);
}));

test('refresh preserves three loaded pages and local expiry updates counts before rereading the window',()=>withComponents(async page=>{
  await mountPaged(page);
  await page.evaluate(async()=>{
    window.view.dispose();
    const {mountServices}=await import('/services-view.js');
    const stamp=new Date(Date.now()-899000).toISOString();
    const make=(ids,next)=>{const result=window.body(ids,!!next);result.services.forEach(row=>row.updated_at=stamp);result.page.next_cursor=next;return result;};
    window.requests=[];
    window.client.listServiceCatalog=async options=>{
      window.requests.push(options);
      if(window.fail)throw {status:500};
      return options.cursor==='next2'?make(['e','f'],null):options.cursor?make(['c','d'],'next2'):make(['a','b'],'next');
    };
    window.view=mountServices(document.querySelector('#services'),window.client,make(['a','b'],'next'));
  });
  await page.locator('#sc-load-more').click();
  await page.waitForSelector('[data-service-id="d"]');
  await page.locator('#sc-load-more').click();
  await page.waitForSelector('[data-service-id="f"]');
  await page.evaluate(async()=>{window.requests=[];await window.view.refresh();});
  assert.equal(await page.locator('.sc-row').count(),6);
  assert.deepEqual(await page.evaluate(()=>window.requests.map(options=>options.cursor)),[null,'next','next2']);
  await page.evaluate(()=>{window.requests=[];window.fail=true;});
  await page.clock.runFor(2000);
  assert.equal(await page.locator('.sc-row').count(),0);
  assert.equal(await page.locator('#sc-counts strong').textContent(),'4');
  assert.match(await page.locator('#sc-history-summary').textContent(),/16 history/);
  assert.match(await page.locator('#sc-result').textContent(),/4 matching services/);
  assert.ok(await page.evaluate(()=>window.requests.length>0));
}));

test('periodic refresh rereads the loaded window without a manual refresh',()=>withComponents(async page=>{
  await mountPaged(page);
  await page.locator('#sc-load-more').click();
  await page.waitForSelector('[data-service-id="c"]');
  await page.evaluate(()=>{window.requests=[];});
  await page.clock.runFor(30000);
  await page.waitForFunction(()=>window.requests.length===2);
  assert.deepEqual(await page.evaluate(()=>window.requests.map(options=>options.cursor)),[null,'next']);
  assert.deepEqual(await page.locator('.sc-row').evaluateAll(rows=>rows.map(row=>row.dataset.serviceId)),['a','b','c']);
}));

for(const status of ['current','all'])test(`local expiry conserves exact counts and group totals for ${status}`,()=>withComponents(async page=>{
  await mountPaged(page);
  await page.evaluate(async status=>{
    window.view.dispose();
    const {mountServices}=await import('/services-view.js');
    const result=window.body(['expiring','remaining']);
    result.services[0].updated_at=new Date(Date.now()-899000).toISOString();
    result.counts={scope_total:5,current:3,unknown:1,unavailable:1,archived:0,history_total:2,matching_total:status==='all'?5:3,matching_groups:1};
    result.groups[0]={key:'project:project',label:'project',total:status==='all'?5:3,states:{current:3,unknown:status==='all'?1:0,unavailable:status==='all'?1:0,archived:0}};
    window.client.listServiceCatalog=async()=>{throw new Error('offline');};
    window.view=mountServices(document.querySelector('#services'),window.client,result,{status});
  },status);
  await page.clock.runFor(2000);
  assert.equal(await page.locator('#sc-counts strong').textContent(),'2');
  assert.match(await page.locator('#sc-history-summary').textContent(),/^3 history/);
  await page.locator('#sc-history').evaluate(node=>{node.open=true;});
  assert.equal(await page.locator('[data-filter="unknown"] strong').textContent(),'2');
  assert.equal(await page.locator('[data-filter="unavailable"] strong').textContent(),'1');
  assert.match(await page.locator('#sc-result').textContent(),new RegExp(`${status==='all'?5:2} matching services · 1 groups`));
  assert.match(await page.locator('.sc-group summary').textContent(),new RegExp(`${status==='all'?5:2} services`));
  if(status==='all')assert.match(await page.locator('.sc-group summary').textContent(),/2 unconfirmed/);
  // A second draw must not apply the same expiry delta again.
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  assert.equal(await page.locator('#sc-counts strong').textContent(),'2');
}));

test('shared first-load progress is indeterminate and delayed',()=>withComponents(async page=>{
  await page.evaluate(async()=>{
    const {skeleton}=await import('/motion.js');
    document.querySelector('#services').innerHTML=skeleton('page content',{rows:7,height:64,bare:false});
  });
  assert.equal(await page.locator('[data-skeleton]').getAttribute('aria-busy'),'true');
  await page.clock.runFor(400);
  assert.equal(await page.locator('progress').isVisible(),true);
  assert.equal(await page.locator('progress').getAttribute('value'),null);
}));

test('slow load-more shares progress, blocks duplicate requests and retains rows on error at mobile width',()=>withComponents(async page=>{
  await page.setViewportSize({width:320,height:700});
  await mountPaged(page);
  await page.evaluate(()=>{
    window.client.listServiceCatalog=options=>{
      window.requests.push(options);
      return new Promise((resolve,reject)=>{window.finishLoad=resolve;window.failLoad=reject;});
    };
  });
  assert.match(await page.locator('#sc-result').textContent(),/2 of 10 loaded/);
  await page.locator('#sc-load-more').click();
  await page.clock.runFor(400);
  assert.equal(await page.locator('#services').getAttribute('aria-busy'),'true');
  assert.equal(await page.locator('#sc-results').getAttribute('aria-busy'),'true');
  assert.equal(await page.locator('#sc-loading progress').isVisible(),true);
  assert.equal(await page.locator('#sc-loading progress').getAttribute('value'),null);
  assert.equal(await page.locator('#sc-load-more').getAttribute('aria-disabled'),'true');
  await page.locator('#sc-load-more').evaluate(button=>{button.click();button.click();});
  assert.equal(await page.evaluate(()=>window.requests.length),1);
  assert.equal(await page.locator('.sc-row').count(),2);
  await page.evaluate(()=>window.failLoad(new Error('offline')));
  await page.waitForFunction(()=>document.querySelector('#services').getAttribute('aria-busy')==='false');
  assert.equal(await page.locator('#sc-results').getAttribute('aria-busy'),'false');
  assert.equal(await page.locator('#sc-loading progress').count(),0);
  assert.equal(await page.locator('#sc-load-more').getAttribute('aria-disabled'),'false');
  assert.match(await page.locator('#sc-error').textContent(),/last received catalog/);
  assert.equal(await page.locator('.sc-row').count(),2);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  assert.equal(await page.locator('#sc-load-more').evaluate(button=>button.classList.contains('hd-btn--secondary')),true);
  await page.locator('#sc-load-more').click();
  await page.evaluate(()=>window.finishLoad(window.body(['c','d'])));
  await page.waitForSelector('[data-service-id="d"]');
  assert.match(await page.locator('#sc-result').textContent(),/4 of 10 loaded/);
  assert.equal(await page.locator('#sc-error').textContent(),'');
}));
