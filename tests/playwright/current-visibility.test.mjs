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
    const fallback='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
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

test('current catalogs expire unchanged reports locally and keep history reachable',()=>withComponents(async page=>{
    await page.evaluate(async()=>{
      const [{mountServices},{mountSessionsView},{mountMissionCatalog},{mountOverview}]=await Promise.all([
        import('/services-view.js'),import('/sessions-view.js'),import('/mission-catalog-view.js'),import('/overview-view.js')]);
      const stamp=offset=>new Date(Date.now()+offset).toISOString();
      const services=[{service_id:'service-live',service_name:'Live API',status:'healthy',updated_at:stamp(-899000)},
        {service_id:'service-stopped',service_name:'Stopped API',status:'stopped',updated_at:stamp(0)},
        {service_id:'service-archived',service_name:'Archived API',status:'stopped',lifecycle:{archived:true}}].map(service=>({...service,catalog:{scope:'herdr'}}));
      const sessions=[{session_id:'session-live',status:'working',updated_at:stamp(-899000)},
        {session_id:'session-finished',status:'closed',updated_at:stamp(0)}];
      const missions=[{mission_id:'mission-recorded',title:'Fresh recorded plan',status:'active',updated_at:stamp(-899000)}];
      const activities=[{mission_key:'pi:mission-live',mission:{id:'mission-live',title:'Stalled work',status:'EXECUTING'},
        activity_snapshot_at:stamp(-29000),freshness:{state:'live',age_seconds:29},activity:{health:'stalled'}}];
      const client={listServices:async()=>({services}),listSessions:async()=>({sessions}),listMissions:async()=>({missions}),
        listMissionActivity:async()=>({activities}),listHosts:async()=>({hosts:[]}),activity:async()=>({events:[]}),
        health:async()=>({status:'ok'}),getCi:async()=>({available:false}),listReviews:async()=>({reviews:[]})};
      window.views=[mountServices(document.querySelector('#services'),client,services),mountSessionsView(document.querySelector('#sessions'),client),
        mountMissionCatalog(document.querySelector('#missions'),client),mountOverview(document.querySelector('#overview'),client)];
    });
    await page.waitForSelector('#sessions [data-session-id="session-live"]');
    await page.waitForSelector('#missions [data-mission-key="pi:mission-live"]');
    assert.equal(await page.locator('#services .sc-row').count(),1);
    assert.equal(await page.locator('#sc-history').getAttribute('open'),null);
    assert.match(await page.locator('#mc-tab-current').innerText(),/2/);
    assert.match(await page.locator('#overview [data-overview="sessions"]').innerText(),/1\s+Recently reported agents/);
    await page.clock.runFor(2000);
    assert.equal(await page.locator('#services .sc-row').count(),0);
    assert.equal(await page.locator('#sessions [data-session-id]').count(),0);
    assert.equal(await page.locator('#missions [data-mission-key]').count(),0);
    assert.match(await page.locator('#mc-tab-current').innerText(),/0/);
    assert.match(await page.locator('#mc-tab-unconfirmed').innerText(),/2/);
    assert.match(await page.locator('#overview [data-overview="sessions"]').innerText(),/0\s+Recently reported agents/);
    await page.locator('#ss-tab-unconfirmed').click();
    assert.equal(await page.locator('#sessions [data-session-id="session-live"]').count(),1);
    await page.locator('#ss-tab-finished').click();
    assert.equal(await page.locator('#sessions [data-session-id="session-finished"]').count(),1);
    await page.locator('#mc-tab-unconfirmed').click();
    assert.equal(await page.locator('#missions [data-mission-key]').count(),2);
    await page.locator('#mc-search').fill('Fresh recorded');
    assert.equal(await page.locator('#missions [data-mission-key]').count(),1);
    await page.locator('#sc-history-summary').click();
    await page.locator('#sc-history-counts [data-filter="unknown"]').click();
    assert.equal(await page.locator('#services [data-service-id="service-live"]').count(),1);
    await page.locator('#sc-status').selectOption('archived');
    assert.equal(await page.locator('#services [data-service-id="service-archived"]').count(),1);
    await page.evaluate(()=>window.views.forEach(view=>view.dispose()));
}));

test('automatic expiry preserves focused services and gives disappearing rows a stable fallback',()=>withComponents(async page=>{
  await page.evaluate(async()=>{
    const [{mountServices},{mountSessionsView},{mountMissionCatalog}]=await Promise.all([
      import('/services-view.js'),import('/sessions-view.js'),import('/mission-catalog-view.js')]);
    const stamp=offset=>new Date(Date.now()+offset).toISOString();
    const services=[{service_id:'expiring',service_name:'Expiring API',status:'healthy',updated_at:stamp(-899000)},
      {service_id:'remaining',service_name:'Remaining API',url:'http://dev.lan:18012',status:'healthy',updated_at:stamp(0)}].map(service=>({...service,catalog:{scope:'herdr'}}));
    const sessions=[{session_id:'expiring-session',status:'working',updated_at:stamp(-895000)}];
    const activities=[{mission_key:'pi:expiring',mission:{title:'Expiring mission',status:'EXECUTING'},
      activity_snapshot_at:stamp(-27000),freshness:{state:'live',age_seconds:27},activity:{health:'stalled'}}];
    const client={listServices:async()=>({services}),listSessions:async()=>({sessions}),listMissions:async()=>({missions:[]}),listMissionActivity:async()=>({activities})};
    window.views=[mountServices(document.querySelector('#services'),client,services),mountSessionsView(document.querySelector('#sessions'),client),mountMissionCatalog(document.querySelector('#missions'),client)];
  });
  await page.waitForSelector('#sessions [data-session-id]');
  await page.waitForSelector('#missions [data-mission-key]');
  await page.locator('[data-service-id="remaining"] a[target="_blank"]').focus();
  await page.clock.runFor(2000);
  assert.equal(await page.locator('[data-service-id="expiring"]').count(),0);
  assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('href')),'http://dev.lan:18012/');
  await page.locator('[data-mission-key="pi:expiring"] .mc-title').focus();
  await page.clock.runFor(2000);
  assert.equal(await page.locator('[data-mission-key="pi:expiring"]').count(),0);
  assert.equal(await page.evaluate(()=>document.activeElement?.id),'mc-tab-unconfirmed');
  await page.locator('[data-session-id="expiring-session"] [data-session-link]').focus();
  await page.clock.runFor(2000);
  assert.equal(await page.locator('[data-session-id="expiring-session"]').count(),0);
  assert.equal(await page.evaluate(()=>document.activeElement?.id),'ss-search');
  // Refreshes in other components must not move focus from the session search.
  await page.evaluate(()=>window.views[0].refresh());
  assert.equal(await page.evaluate(()=>document.activeElement?.id),'ss-search');
  await page.locator('[data-service-id="remaining"] .sc-name').focus();
  await page.evaluate(()=>window.views[0].refresh());
  assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('href')),'#/services/remaining');
  await page.clock.fastForward(900001);
  assert.equal(await page.locator('#services [data-service-id]').count(),0);
  assert.equal(await page.evaluate(()=>document.activeElement?.id),'sc-search');
  await page.evaluate(()=>window.views.forEach(view=>view.dispose()));
}));

test('service detail expires endpoints locally while retaining focus and scope context',()=>withComponents(async page=>{
  await page.evaluate(async()=>{
    const {mountServiceDetails}=await import('/services-view.js');
    const service={service_id:'expiring',service_name:'Owned API',status:'healthy',url:'http://dev.lan:18012',
      updated_at:new Date(Date.now()-899000).toISOString(),catalog:{scope:'herdr',scope_reason:'Verified session ancestry'}};
    const client={listServices:async()=>({services:[service]})};
    window.renewService=()=>{service.updated_at=new Date(Date.now()).toISOString();};
    window.detail=mountServiceDetails(document.querySelector('#services'),client,service);
  });
  await page.locator('#services a[target="_blank"]').focus();
  assert.match(await page.locator('#services').innerText(),/Verified session ancestry/);
  await page.clock.runFor(2000);
  assert.equal(await page.locator('#services a[target="_blank"]').count(),0);
  assert.match(await page.locator('#services').innerText(),/Recorded endpoint \(not current\)/);
  assert.equal(await page.evaluate(()=>document.activeElement?.hasAttribute('data-service-back')),true);
  await page.evaluate(()=>window.detail.refresh());
  assert.equal(await page.locator('#services a[target="_blank"]').count(),0);
  assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('href')),'#/services');
  await page.evaluate(async()=>{window.renewService();await window.detail.refresh();});
  assert.equal(await page.locator('#services a[target="_blank"]').count(),1);
  assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('href')),'#/services');
  await page.evaluate(()=>window.detail.dispose());
}));

test('service ownership separates catalogs and keeps secondary current and history filters useful',()=>withComponents(async page=>{
  await page.evaluate(async()=>{
    const {mountServices}=await import('/services-view.js');
    const stamp=new Date(Date.now()).toISOString();
    const services=[{service_id:'owned',service_name:'Owned app',status:'healthy',updated_at:stamp,catalog:{scope:'herdr'}},
      {service_id:'system',service_name:'System daemon',status:'healthy',updated_at:stamp,catalog:{scope:'system'}},
      {service_id:'unassigned',service_name:'Unassigned listener',status:'healthy',updated_at:stamp},
      {service_id:'stopped',service_name:'Stopped system daemon',status:'stopped',updated_at:stamp,catalog:{scope:'system'}}];
    const client={listServices:async()=>({services})};
    window.serviceView=mountServices(document.querySelector('#services'),client,services);
    window.openSystem=()=>{window.serviceView.dispose();window.serviceView=mountServices(document.querySelector('#services'),client,services,{scope:'system'});};
  });
  assert.equal(await page.locator('#services [data-service-id]').count(),1);
  assert.equal(await page.locator('#services [data-service-id="owned"]').count(),1);
  assert.match(await page.locator('#sc-history-summary').innerText(),/^0 /);
  assert.equal(await page.locator('#services a[href="#/system-services"]').count(),1);
  await page.evaluate(()=>window.openSystem());
  assert.equal(await page.locator('#services [data-service-id]').count(),1);
  assert.equal(await page.locator('#services [data-service-id="system"]').count(),1);
  assert.equal(await page.locator('#services [data-service-id="owned"]').count(),0);
  assert.match(await page.locator('#sc-history-summary').innerText(),/^1 /);
  await page.locator('#sc-status').selectOption('unavailable');
  assert.equal(await page.locator('#services [data-service-id="stopped"]').count(),1);
  await page.evaluate(()=>window.serviceView.dispose());
}));

test('refresh keeps explicitly collapsed groups closed when a child still owns focus',()=>withComponents(async page=>{
  await page.evaluate(async()=>{
    const [{mountServices},{mountMissionCatalog}]=await Promise.all([import('/services-view.js'),import('/mission-catalog-view.js')]);
    const services=[{service_id:'owned',service_name:'Owned app',status:'healthy',updated_at:new Date(Date.now()).toISOString(),catalog:{scope:'herdr'}}];
    let revision=0;
    const client={listServices:async()=>({services}),listMissions:async()=>({missions:[{mission_id:'closed-record',status:'closed',updated_at:String(++revision)}]}),listMissionActivity:async()=>({activities:[{
      mission_key:'pi:live',mission:{status:'EXECUTING',title:'Live mission'},activity_snapshot_at:new Date(Date.now()).toISOString(),
      freshness:{state:'live',age_seconds:0},activity:{health:'active',currentActivity:{summary:'Activity '+(++revision)}}}]})};
    window.serviceView=mountServices(document.querySelector('#services'),client,services);
    window.missionView=mountMissionCatalog(document.querySelector('#missions'),client);
  });
  await page.waitForSelector('.mc-title');
  await page.locator('.sc-name').focus();
  await page.evaluate(()=>{document.querySelector('.sc-group').open=false;return window.serviceView.refresh();});
  assert.equal(await page.locator('.sc-group').getAttribute('open'),null);
  assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('.sc-group>summary')),true);
  await page.locator('.mc-title').focus();
  await page.evaluate(()=>{document.querySelector('[data-group="running"]').open=false;return window.missionView.refreshRecorded();});
  assert.equal(await page.locator('[data-group="running"]').getAttribute('open'),null);
  assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('[data-group="running"]>summary')),true);
  await page.evaluate(()=>{window.serviceView.dispose();window.missionView.dispose();});
}));

test('listener observation expiry hides stale apps despite legacy heartbeats and local-only ports stay qualified',()=>withComponents(async page=>{
  await page.evaluate(async()=>{
    const {mountServices,mountServiceDetails}=await import('/services-view.js');
    const service={service_id:'observed-app',service_name:'Observed app',status:'healthy',port:18012,url:'http://dev.lan:18012',
      updated_at:new Date(Date.now()).toISOString(),catalog:{scope:'herdr',metadata_observation:{state:'observed',observed_at:new Date(Date.now()-89000).toISOString(),stale_after_seconds:90}}};
    const client={listServices:async()=>{service.updated_at=new Date(Date.now()).toISOString();return {services:[service]};}};
    window.observedService=service;
    window.observedCatalog=mountServices(document.querySelector('#services'),client,[service]);
    window.observedDetail=mountServiceDetails(document.querySelector('#overview'),client,service);
  });
  assert.equal(await page.locator('#services [data-service-id]').count(),1);
  await page.locator('#overview a[target="_blank"]').focus();
  await page.clock.runFor(2000);
  assert.equal(await page.locator('#services [data-service-id]').count(),0);
  assert.equal(await page.locator('#overview a[target="_blank"]').count(),0);
  assert.match(await page.locator('#overview').innerText(),/No recent listener observation/);
  assert.equal(await page.evaluate(()=>document.activeElement?.hasAttribute('data-service-back')),true);
  await page.evaluate(async()=>{await window.observedCatalog.refresh();await window.observedDetail.refresh();});
  assert.equal(await page.locator('#services [data-service-id]').count(),0);
  assert.equal(await page.locator('#overview a[target="_blank"]').count(),0);
  await page.locator('#sc-status').selectOption('unknown');
  assert.equal(await page.locator('#services [data-service-id]').count(),1);
  await page.evaluate(async()=>{
    window.observedService.endpoint_scope='local_only';
    window.observedService.catalog.metadata_observation.observed_at=new Date(Date.now()).toISOString();
    await window.observedCatalog.refresh();await window.observedDetail.refresh();
  });
  await page.locator('#sc-status').selectOption('current');
  assert.equal(await page.locator('#services [data-service-id]').count(),1);
  assert.match(await page.locator('#services .sc-location').innerText(),/Observed port :18012/);
  assert.match(await page.locator('#services .sc-location').innerText(),/Local only/);
  assert.doesNotMatch(await page.locator('#services .sc-location').innerText(),/Exposed/);
  assert.equal(await page.locator('#services a[target="_blank"], #overview a[target="_blank"]').count(),0);
  await page.evaluate(()=>{window.observedCatalog.dispose();window.observedDetail.dispose();});
}));
