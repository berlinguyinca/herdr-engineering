import test from 'node:test';
import assert from 'node:assert/strict';
import {ciObservabilityModel,ciProjectKey,renderCiObservability,renderCiTestHistory} from '../ci-observability-view.js';

const metrics={pipeline_count:12,completed_count:5,success_count:4,failure_count:1,active_count:2,pass_rate:.8,duration_seconds:{sample_count:4,p50:10,p95:20},outcomes:{success:4,failure:1,canceled:3,skipped:2,running:2}};
const project=(extra={})=>({provider_url:'https://ci.example',provider_repo_id:'17',repository:'org/app',...metrics,...extra});
const summary={available:true,projects:[project()],kpis:{project_count:1,...metrics},charts:[{day:'2026-10-25',...metrics}],scope:{days:14,scope:'all',partial:true,stale:true,memory_only:true,minimum_percentile_samples:5}};

test('KPI denominator excludes canceled, skipped and active runs; sparse percentiles stay unknown',()=>{
 const model=ciObservabilityModel(summary);
 assert.equal(model.kpis.passRate,.8);assert.equal(model.kpis.completed,5);assert.equal(model.kpis.duration.p50,null);
 const html=renderCiObservability(model);
 assert.match(html,/4 \/ 5 successful or failed runs/);assert.match(html,/Insufficient data/);assert.match(html,/4 measured runs/);
 assert.match(html,/Observed projects/);assert.match(html,/Active runs/);assert.match(html,/Stale/);assert.match(html,/Partial/);assert.match(html,/memory-only/i);
 const measured=ciObservabilityModel({...summary,kpis:{...summary.kpis,duration_seconds:{sample_count:5,p50:10,p95:20}}});
 assert.equal(measured.kpis.duration.p50,10);assert.equal(measured.kpis.duration.p95,20);
 assert.equal(ciObservabilityModel({kpis:{}}).kpis.passRate,null);
});

test('project selection cannot collide across provider, repository ID or repository name',()=>{
 const projects=[project(),project({provider_url:'https://other.example'}),project({provider_repo_id:'18'}),project({repository:'org/renamed'})];
 assert.equal(new Set(projects.map(ciProjectKey)).size,4);
 const model=ciObservabilityModel({...summary,projects},{projectKey:ciProjectKey(projects[2])});
 assert.equal(model.selected.provider_repo_id,'18');
 const html=renderCiObservability(model);
 assert.equal((html.match(/data-ob-project=/g)||[]).length,4);
 assert.match(html,/org\/renamed/);assert.match(html,/https:\/\/other.example/);
});

test('project search, failures-only and sorting retain exact identities and do not alter global KPI denominators',()=>{
 const model=ciObservabilityModel({...summary,projects:[project({repository:'org/slow',failure_count:0,duration_seconds:{sample_count:5,p50:90,p95:100}}),project({repository:'org/fail'})]},{search:'org/',failuresOnly:true,sort:'failure'});
 assert.deepEqual(model.projects.map(p=>p.repository),['org/fail']);assert.equal(model.kpis.completed,5);
 const html=renderCiObservability(ciObservabilityModel({available:false,projects:[],kpis:{},scope:{}}));
 assert.match(html,/unavailable/i);assert.doesNotMatch(html,/NaN|Infinity|0%/);
});

test('test history distinguishes missing evidence from zero tests and uses attempt denominators',()=>{
 assert.match(renderCiTestHistory(null),/Select a project/);
 const missing=renderCiTestHistory({tests:[],scope:{not_reported_run_count:3,evidence_availability:'not_reported'}});
 assert.match(missing,/3 runs.*not reported/);assert.doesNotMatch(missing,/0 tests|0%/);
 const row={id:'case',name:'<broken>',branch:'main',passed:2,failed:1,completed_count:3,failure_count:1,classification:'inconsistent_outcomes',duration_ms:{sample_count:3,p50:99,p95:100},first_failure:'2026-10-20',last_pass:'2026-10-25'};
 const html=renderCiTestHistory({tests:[row],scope:{days:14,minimum_percentile_samples:5}});
 assert.match(html,/Inconsistent outcomes observed/);assert.doesNotMatch(html,/Proven flaky|<broken>/);assert.match(html,/&lt;broken&gt;/);assert.match(html,/1 \/ 3 reported completed test attempts/);assert.match(html,/Insufficient data/);
 assert.match(renderCiTestHistory({tests:[{...row,classification:'proven_flaky'}],scope:{}}),/Proven flaky.*same-revision retry/);
});

// Real DOM/controller coverage; only the remote client boundary is substituted.
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';

test('command center loads selected evidence lazily, rejects stale responses and retains UI state on outages',async()=>{
 const root=path.resolve(import.meta.dirname,'../..');
 const server=createServer(async(req,res)=>{try{if(req.url==='/'){res.setHeader('content-type','text/html');res.end('<main></main>');}else{res.setHeader('content-type','text/javascript');res.end(await readFile(root+req.url));}}catch{res.writeHead(404);res.end();}});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:390,height:844}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.evaluate(async fixture=>{
   const {mountCiObservability}=await import('/packages/ci-observability-view.js');
   window.calls=[];window.fixture=fixture;window.fail=false;
   window.view=mountCiObservability(document.querySelector('main'),{
    getCiSummary:async options=>{calls.push(['summary',options]);if(fail)throw Error('offline');return options.provider_repo_id?{...fixture,projects:fixture.projects.filter(p=>p.provider_repo_id===options.provider_repo_id)}:fixture;},
    getCiTests:async options=>{calls.push(['tests',options]);if(fail)throw Error('offline');if(window.deferTests&&options.provider_repo_id==='17')return new Promise(resolve=>window.releaseTests=resolve);return {tests:[{id:'broken',name:'broken',failure_count:1,completed_count:2,classification:'inconsistent_outcomes',duration_ms:{sample_count:2}}],scope:{not_reported_run_count:2}};}
   },{onContext:context=>window.context=context});
  },{...summary,projects:[project(),project({provider_repo_id:'18'})]});
  await page.locator('[data-ob-project]').first().waitFor();
  assert.deepEqual(await page.evaluate(()=>calls.map(c=>c[0])),['summary']);
  await page.locator('[data-ob-project]').nth(1).click();
  await page.waitForFunction(()=>document.querySelector('[data-ob-tests]').textContent.includes('broken'));
  assert.equal(await page.evaluate(()=>calls.find(c=>c[0]==='tests')[1].provider_repo_id),'18');
  assert.equal(await page.evaluate(()=>context.project.provider_repo_id),'18');
  assert.equal(await page.evaluate(()=>calls.filter(([kind,options])=>kind==='summary'&&!options.provider_repo_id).length),1,'selection reuses the fleet snapshot instead of blocking on an unchanged fleet request');
  await page.locator('[data-ob-clear]').click();
  assert.equal(await page.locator('[data-ob-summary] .ci-ob-card').count(),5,'clear restores the existing fleet KPIs');
  assert.equal(await page.evaluate(()=>calls.filter(([kind,options])=>kind==='summary'&&!options.provider_repo_id).length),1,'clear does not refetch the unchanged fleet scope');
  await page.locator('[data-ob-project]').nth(1).click();
  await page.waitForFunction(()=>document.querySelector('[data-ob-tests]').textContent.includes('broken'));
  const details=page.locator('[data-ob-charts] details');await details.locator('summary').click();
  await page.locator('[data-ob-project]').nth(1).focus();
  await page.evaluate(async()=>{fail=true;await view.refresh();});
  assert.match(await page.locator('[data-ob-status]').textContent(),/last received|retained/i);
  assert.equal(await details.evaluate(n=>n.open),true);
  assert.equal(await page.locator('[data-ob-project]').nth(1).getAttribute('aria-pressed'),'true');
  assert.match(await page.locator('[data-ob-tests]').textContent(),/broken/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.evaluate(()=>{fail=false;window.deferTests=true;});
  await page.locator('[data-ob-project]').first().click();
  await page.waitForFunction(()=>typeof window.releaseTests==='function');
  await page.locator('[data-ob-project]').nth(1).click();
  await page.waitForFunction(()=>document.querySelector('[data-ob-tests]').textContent.includes('broken'));
  await page.evaluate(()=>releaseTests({tests:[{name:'Wrong project evidence'}],scope:{}}));
  assert.doesNotMatch(await page.locator('[data-ob-tests]').textContent(),/Wrong project evidence/);
  await page.evaluate(()=>{fail=true;});
  await page.locator('[data-ob-days]').selectOption('7');
  await page.waitForFunction(()=>calls.filter(c=>c[0]==='summary').at(-1)[1].days===7);
  assert.doesNotMatch(await page.locator('[data-ob-charts]').textContent(),/2026-10-25/,'old-window charts must not be relabeled as new-window evidence');
  await page.evaluate(()=>view.dispose());assert.deepEqual(errors,[]);
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
});

test('CI page shares project context with the existing heatmap and inspector without eager detail or log reads',async()=>{
 const root=path.resolve(import.meta.dirname,'../..');
 const server=createServer(async(req,res)=>{try{if(req.url==='/'){res.setHeader('content-type','text/html');res.end('<link rel="stylesheet" href="/packages/herdr-design-system/theme.css"><main></main>');}else{res.setHeader('content-type',req.url.endsWith('.css')?'text/css':'text/javascript');res.end(await readFile(root+req.url));}}catch{res.writeHead(404);res.end();}});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.evaluate(async fixture=>{
   const {mountIntegrations}=await import('/packages/integrations-view.js');
   const now=new Date().toISOString();window.calls=[];
   const run={provider_url:'https://ci.example',provider_repo_id:18,repository:'org/app',run_number:7,branch:'main',revision:'abc',state:'failure',created_at:now,started_at:now,finished_at:now};
   const repos=[{id:17,repository:'org/app',provider_url:run.provider_url,default_branch:'main'},{id:18,repository:'org/app',provider_url:run.provider_url,default_branch:'main'}];
   window.app=mountIntegrations(document.querySelector('main'),{
    getCiSummary:async options=>{calls.push(['summary',options]);const projects=options.provider_repo_id?fixture.projects.filter(p=>p.provider_repo_id===options.provider_repo_id):fixture.projects;return {...fixture,projects,scope:{...fixture.scope,...options},kpis:{...fixture.kpis,project_count:projects.length}};},
    getCiTests:async options=>{calls.push(['tests',options]);return {tests:[],scope:{not_reported_run_count:1}};},
    getCiHistory:async()=>({available:true,status:'ready',repos,pipelines:[run],coverage:{17:{exhausted:true},18:{exhausted:true}}}),
    getCi:async()=>({available:true,status:'ready',pipelines:{'org/app':[run]}}),
    getCiReports:async()=>({reports:[],pipelines:[run]}),
    getCiPipeline:async(id,number)=>{calls.push(['pipeline',id,number]);return {pipeline:{...run,tasks:[{id:9,name:'Unit tests',state:'failure'}]},diagnosis:{evidence_availability:'unavailable',reason:'No validated report matches this run.',failed_steps:[{id:9,name:'Unit tests',state:'failure'}],failed_tests:[]}};},
    getCiLog:async(id,number,step)=>{calls.push(['log',id,number,step]);return {available:true,log:'Exact provider output',truncated:true};}
   });
  },{...summary,projects:[project(),project({provider_repo_id:'18'})]});
  await page.locator('[data-ob-project]').nth(1).waitFor();
  assert.doesNotMatch(await page.locator('main').innerText(),/\.ig-fix\{/,'styles must not leak into visible page text');
  assert.equal(await page.evaluate(()=>calls.filter(c=>['pipeline','log'].includes(c[0])).length),0);
  await page.locator('[data-ob-project]').nth(1).click();
  assert.deepEqual(errors,[]);assert.equal(await page.locator('th[scope=row].ci-health-project').count(),1,await page.locator('[data-health-map]').textContent());
  assert.equal(await page.locator('[data-health-mode="14"]').getAttribute('aria-pressed'),'true');
  await page.locator('[data-health-tree] [data-health-disclosure] > summary').first().click();
  await page.locator('[data-health-tree] [data-health-disclosure] [data-health-disclosure] > summary').first().click();
  await page.locator('[data-health-run]').click();
  await page.locator('.ci-diagnosis').waitFor();
  assert.match(await page.locator('.ci-diagnosis').textContent(),/Test evidence unavailable/);
  assert.equal(await page.evaluate(()=>calls.filter(c=>c[0]==='log').length),0);
  await page.locator('.ci-diagnosis [data-step="9"]').click();
  await page.waitForFunction(()=>document.querySelector('.ig-log')?.textContent.includes('Exact provider output'));
  assert.equal(await page.evaluate(()=>calls.filter(c=>c[0]==='log').length),1);
  await page.locator('[data-ob-days]').selectOption('7');
  assert.equal(await page.locator('[data-ci-period]').inputValue(),'7','legacy period reflects the shared window');
  assert.equal(await page.locator('[data-health-mode="7"]').getAttribute('aria-pressed'),'true');
  await page.locator('[data-health-branch]').fill('feature');
  await page.waitForFunction(()=>document.querySelector('[data-ob-branch]').value==='feature');
  assert.equal(await page.locator('[data-ci-branch]').inputValue(),'feature','legacy branch reflects heatmap changes');
  for(const width of [390,1440,3840]){await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`no page overflow at ${width}px`);}
  await page.evaluate(()=>app.dispose());assert.deepEqual(errors,[]);
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
});

// Review regressions mount the real page and substitute only provider reads.
async function reviewPage(run) {
 const root=path.resolve(import.meta.dirname,'../..');
 const server=createServer(async(req,res)=>{try{res.setHeader('content-type',req.url==='/'?'text/html':'text/javascript');res.end(req.url==='/'?'<main></main>':await readFile(root+req.url));}catch{res.writeHead(404);res.end();}});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage();page.setDefaultTimeout(5000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.evaluate(async()=>{
   const {mountIntegrations}=await import('/packages/integrations-view.js');
   const identity={provider_url:'https://ci.example',provider_repo_id:'17',repository:'org/current'};
   const now=new Date().toISOString();
   window.runs=[{...identity,run_number:7,state:'failure',branch:'main',revision:'abc',created_at:now,started_at:now,finished_at:now},{...identity,repository:'org/former',run_number:8,state:'failure',branch:'main',revision:'abc',created_at:now,started_at:now,finished_at:now}];
   window.calls=[];
   const projects=runs.map(r=>({...r,success_count:0,failure_count:1,completed_count:1,active_count:0,duration_seconds:{sample_count:0},coverage:{complete:false}}));
   window.fixture={available:true,projects,kpis:{project_count:2,success_count:0,failure_count:2,completed_count:2,active_count:0},charts:[],scope:{days:14,scope:'all',partial:true}};
   window.client={
    getCiSummary:async options=>{calls.push(['summary',options]);return {...fixture,scope:{...fixture.scope,...options}};},
    getCiTests:async options=>{calls.push(['tests',options]);return {tests:[{id:'case',name:'Current failure',failure_count:1,completed_count:1,representative_runs:[runs[0]]}],scope:{days:options.days}};},
    getCiHistory:async()=>({available:true,status:'ready',repos:runs.map(r=>({...r,id:17,default_branch:'main'})),pipelines:runs,coverage:{17:{exhausted:true}}}),
    getCi:async()=>({available:true,status:'ready',pipelines:{history:runs}}),
    getCiReports:async()=>({pipelines:runs,reports:runs.map((r,i)=>({...r,report_id:String(i),reported_at:now,summary:{passed:0,failed:1,errored:0,skipped:0,total:1},tests:[{id:'case'+i,name:i?'Former failure':'Current failure',status:'failed'}]}))}),
    getCiPerformance:async({days})=>({schema_version:1,days,cache_groups:runs.map((r,i)=>({...r,kind:i?'Former cache':'Current cache',sample_count:5,restore_count:5,hit_count:3,hit_rate:.6})),execution_trends:[],alerts:[]}),
    getCiPipeline:async(id,number)=>({pipeline:{...runs.find(r=>r.run_number===number),tasks:[]},diagnosis:{evidence_availability:'unavailable'}})
   };
   window.mount=()=>window.app=mountIntegrations(document.querySelector('main'),client);
  });
  await run(page);assert.deepEqual(errors,[]);
 }finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
}

test('review: exact project identity reaches Runs, health, report and cache charts after a rename',()=>reviewPage(async page=>{
 await page.evaluate(()=>mount());await page.locator('[data-ob-project]').first().waitFor();
 await page.locator('[data-ob-project]').filter({hasText:'org/current'}).click();
 await page.locator('[data-ci-workspace="runs"]').click();await page.locator('[data-tab="failed"]').click();
 assert.equal(await page.locator('#ig-results [data-run]').count(),1);
 assert.match(await page.locator('#ig-results').textContent(),/Run #7/);
 assert.doesNotMatch(await page.locator('[data-ci-charts-body]').textContent(),/org\/former|Former cache/);
 assert.doesNotMatch(await page.locator('[data-ci-insights]').textContent(),/Former failure/);
 assert.deepEqual(await page.locator('th[scope=row].ci-health-project').allTextContents(),['org/current']);
 const value=await page.locator('[data-ci-repository]').inputValue();
 assert.deepEqual(JSON.parse(value),['https://ci.example','17','org/current']);
}));

test('review: Runs repository selection updates command center and health with exact identity in both directions',()=>reviewPage(async page=>{
 await page.evaluate(()=>mount());await page.locator('[data-ob-project]').first().waitFor();
 await page.locator('[data-ci-workspace="runs"]').click();
 const identity=JSON.stringify(['https://ci.example','17','org/former']);
 assert.ok((await page.locator('[data-ci-repository] option').evaluateAll(nodes=>nodes.map(n=>n.value))).includes(identity));
 await page.locator('[data-ci-repository]').selectOption(identity);
 assert.match(await page.locator('[data-ob-selection]').textContent(),/org\/former/);
 assert.deepEqual(await page.locator('th[scope=row].ci-health-project').allTextContents(),['org/former']);
 await page.waitForFunction(()=>calls.some(([key,o])=>key==='tests'&&o.repository==='org/former'&&o.provider_repo_id==='17'&&o.provider_url==='https://ci.example'));
 await page.locator('[data-ci-repository]').selectOption('');
 assert.equal(await page.locator('[data-ob-clear]').isVisible(),false);
 await page.locator('[data-ob-project]').filter({hasText:'org/current'}).click();
 assert.equal(await page.locator('[data-ci-repository]').inputValue(),JSON.stringify(['https://ci.example','17','org/current']));
}));

test('review: representative runs activate and focus the inspector from every workspace, including reopening the same run',()=>reviewPage(async page=>{
 await page.evaluate(()=>mount());await page.locator('[data-ob-project]').first().waitFor();
 await page.locator('[data-ob-project]').filter({hasText:'org/current'}).click();
 for(const workspace of ['tests','health','runs','tests']) {
  await page.locator(`[data-ci-workspace="${workspace}"]`).click();
  await page.locator('[data-ob-run]').click();
  assert.equal(await page.locator('.ig-inspector').isVisible(),true,workspace);
  assert.equal(await page.locator('.ig-inspector').evaluate(n=>n.contains(document.activeElement)),true,`${workspace}: inspector receives focus`);
  assert.match(await page.locator('.ig-inspector').textContent(),/Run #7/);
 }
}));

test('review: saved command-center filters and project survive remount and initialize all shared controls',()=>reviewPage(async page=>{
 await page.evaluate(()=>mount());await page.locator('[data-ob-project]').first().waitFor();
 await page.locator('[data-ob-days]').selectOption('30');await page.locator('[data-ob-scope]').selectOption('pr');
 await page.locator('[data-ob-branch]').fill('topic');await page.locator('[data-ob-search]').fill('org/current');
 await page.locator('[data-ob-sort]').selectOption('duration');await page.locator('[data-ob-failures]').check();
 await page.locator('[data-ob-project]').filter({hasText:'org/current'}).click();
 await page.evaluate(()=>{app.dispose();calls=[];mount();});
 await page.locator('[data-ob-project]').first().waitFor();
 assert.equal(await page.locator('[data-ob-days]').inputValue(),'30');assert.equal(await page.locator('[data-ob-scope]').inputValue(),'pr');
 assert.equal(await page.locator('[data-ob-branch]').inputValue(),'topic');assert.equal(await page.locator('[data-ob-search]').inputValue(),'org/current');
 assert.equal(await page.locator('[data-ob-sort]').inputValue(),'duration');assert.equal(await page.locator('[data-ob-failures]').isChecked(),true);
 assert.equal(await page.locator('[data-health-mode="30"]').getAttribute('aria-pressed'),'true');assert.equal(await page.locator('[data-health-scope]').inputValue(),'pr');
 assert.equal(await page.locator('[data-ci-period]').inputValue(),'30');assert.equal(await page.locator('[data-ci-branch]').inputValue(),'topic');
 assert.match(await page.locator('[data-ob-selection]').textContent(),/org\/current/);
 assert.deepEqual(await page.evaluate(()=>[calls[0][1].days,calls[0][1].scope,calls[0][1].branch]),[30,'pr','topic']);
}));

test('review: mounting preserves existing saved health filters when no command-center preferences exist',()=>reviewPage(async page=>{
 await page.evaluate(()=>{localStorage.setItem('herdr.ci-health.v1:["","",""]',JSON.stringify({mode:'7',scope:'default',branch:'main',search:'org/current',failuresOnly:true}));mount();});
 await page.locator('[data-ob-project]').first().waitFor();
 assert.equal(await page.locator('[data-ob-days]').inputValue(),'7');assert.equal(await page.locator('[data-ob-scope]').inputValue(),'default');
 assert.equal(await page.locator('[data-ob-branch]').inputValue(),'main');assert.equal(await page.locator('[data-ob-search]').inputValue(),'org/current');
 assert.equal(await page.locator('[data-ob-failures]').isChecked(),true);
}));

test('review: mounted unavailable summaries show missing coverage instead of observed-zero cards',()=>reviewPage(async page=>{
 await page.evaluate(()=>{fixture={...fixture,available:false,projects:[],kpis:{project_count:0,success_count:0,failure_count:0,active_count:0},scope:{partial:true}};mount();});
 await page.waitForFunction(()=>calls.length>0);
 assert.match(await page.locator('[data-ob-summary]').textContent(),/unavailable/i);
 assert.match(await page.locator('[data-ob-status]').textContent(),/unavailable.*partial|partial.*unavailable/i);
 assert.equal(await page.locator('[data-ob-summary] .ci-ob-card').count(),0);
 await page.evaluate(()=>{fixture.available=true;app.refresh();});
 await page.locator('[data-ob-summary] .ci-ob-card').first().waitFor();
 assert.match(await page.locator('[data-ob-summary]').textContent(),/Observed projects0/);
 assert.doesNotMatch(await page.locator('[data-ob-summary]').textContent(),/unavailable/i);
}));
