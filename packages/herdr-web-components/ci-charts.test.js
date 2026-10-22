import test from 'node:test';
import assert from 'node:assert/strict';
import {ciChartModel,renderCiCharts} from '../ci-charts.js';
import {integrationEntries,selectIntegrations} from '../integrations-view.js';
const entry=(tab,record={})=>({id:'run',tab,record:{repository:'org/app',run_number:1,created_at:'2026-10-20T23:30:00Z',...record}});

test('observed outcomes group by UTC creation day without relabeling canceled as success',()=>{
  const model=ciChartModel([entry('finished',{state:'canceled'}),entry('failed'),entry('current'),entry('finished',{created_at:'2026-10-21T01:30:00+02:00'}),entry('failed',{created_at:'invalid'})]);
  assert.deepEqual(model.days,[{day:'2026-10-20',current:1,failed:1,finished:2}]);
  assert.equal(model.undated,1);
  assert.equal(model.total,5);
  assert.doesNotMatch(renderCiCharts(model),/success rate|passed/i);
});
test('duration points require actual terminal start and finish timestamps',()=>{
  const valid={started_at:1792540800,finished_at:1792540830};
  const rows=[entry('finished',valid),entry('failed',{started_at:1792540800000,finished_at:1792540860000}),
    entry('current',valid),entry('finished',{finished_at:1792540830}),entry('failed',{started_at:1792540800}),
    entry('finished',{started_at:'bad',finished_at:1792540830}),entry('finished',{started_at:1792540900,finished_at:1792540800}),
    entry('finished',{started_at:1792540800,finished_at:1792540800})];
  const model=ciChartModel(rows);
  assert.deepEqual(model.durations.map(p=>p.durationMs),[30000,60000]);
  assert.equal(model.unmeasured,5);
});
test('charts bound observed days and duration points while identifying omitted records',()=>{
  const rows=Array.from({length:25},(_,i)=>entry('finished',{created_at:new Date(Date.UTC(2026,9,i+1)).toISOString(),started_at:1792540800+i*100,finished_at:1792540830+i*100}));
  const model=ciChartModel(rows);
  assert.equal(model.days.length,14);
  assert.equal(model.older,11);
  assert.equal(model.durations.length,20);
  assert.equal(model.measured,25);
  const html=renderCiCharts(model,{stale:true,partial:true});
  assert.match(html,/recent loaded pipeline window/i);
  assert.match(html,/Reports are stale/);
  assert.match(html,/Some repositories have incomplete reports/);
  assert.match(html,/20 of 25 measured/);
  assert.match(html,/11 runs on older/);
});
test('empty or malformed data produces honest empty charts and escaped labels',()=>{
  const model=ciChartModel([null,{}, {tab:'finished',record:[]},entry('finished',{repository:'<script>alert(1)</script>',started_at:1792540800,finished_at:1792540830})]);
  assert.equal(model.total,1);
  const html=renderCiCharts(model);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/&lt;script&gt;/);
  const empty=renderCiCharts(ciChartModel([]));
  assert.match(empty,/No dated runs/);
  assert.match(empty,/No completed run has valid/);
  assert.doesNotMatch(empty,/NaN|Infinity/);
});

test('chart selection spans outcomes but respects canonical repository and mission context exactly',()=>{
  const entries=integrationEntries({pipelines:{'org/app':[
    {repository_id:'github.com/org/app',run_number:1,state:'success',branch:'main',mission_key:'pi:one'},
    {repository_id:'github.com/org/app',run_number:2,state:'running',branch:'main',mission_key:'pi:one'},
    {repository_id:'codeberg.org/org/app',run_number:3,state:'failure',branch:'main',mission_key:'pi:one'},
    {repository_id:'github.com/org/app',run_number:4,state:'success',branch:'other',mission_key:'pi:one'},
    {repository_id:'github.com/org/app',run_number:5,state:'failure',branch:'main',mission_key:'pi:two'},
  ]}});
  const selected=selectIntegrations(entries,{tab:'all',repository:'github.com/org/app',search:'main',mission:'pi:one'});
  assert.deepEqual(selected.map(e=>e.record.run_number),[1,2]);
  assert.equal(ciChartModel(selected).total,2);
  assert.equal(selectIntegrations(entries,{tab:'all',repository:'github.com/ORG/app'}).length,0);
});

test('out-of-range numeric timestamps cannot crash the charts or create duration points',()=>{
  const model=ciChartModel([entry('finished',{created_at:1e30,started_at:1e30,finished_at:1e31})]);
  assert.equal(model.undated,1);
  assert.equal(model.unmeasured,1);
  assert.equal(model.durations.length,0);
  assert.doesNotMatch(renderCiCharts(model),/NaN|Infinity/);
});

test('retained chart scope is explicit when the backend supplies observed history',()=>{
 const html=renderCiCharts(ciChartModel([]),{retained:true});
 assert.match(html,/retained observed history/);assert.doesNotMatch(html,/recent loaded pipeline window/);
});

const performanceEntry=(run,durationMs,record={})=>entry('finished',{provider_url:'https://ci.example',provider_repo_id:1,run_number:run,project_id:'alpha',language:'JavaScript',state:'success',started_at:1792540800000,finished_at:1792540800000+durationMs,...record});

test('performance uses only successful positive execution times and nearest-rank p95',()=>{
  const rows=[performanceEntry(1,1000),performanceEntry(2,3000),performanceEntry(3,10000),performanceEntry(4,100,{state:'failure'}),performanceEntry(5,100,{state:'canceled'}),performanceEntry(6,100,{state:'skipped'}),performanceEntry(7,0),performanceEntry(8,-1),performanceEntry(9,100,{started_at:null}),performanceEntry(10,100,{state:'running'}),performanceEntry(11,100,{started_at:'bad'}),performanceEntry(12,100,{started_at:1e30})];
  const m=ciChartModel(rows).performance;
  assert.ok(m,'performance summary is available');
  assert.equal(m.measured,3);assert.equal(m.invalid,5);assert.equal(m.excluded,4);
  assert.deepEqual(m.projects.map(g=>[g.label,g.count,g.meanMs,g.medianMs,g.p95Ms]),[['alpha',3,14000/3,3000,10000]]);
  assert.deepEqual(m.languages.map(g=>[g.label,g.count]),[['JavaScript',3]]);
  const twenty=ciChartModel(Array.from({length:20},(_,i)=>performanceEntry(i+1,(i+1)*1000))).performance;
  assert.equal(twenty.projects[0].p95Ms,19000);
  assert.equal(twenty.projects[0].medianMs,10500);
});

test('performance deduplicates canonical provider/repository/run identity and keeps unknown groupings explicit',()=>{
  const one=performanceEntry(1,1000), duplicate=performanceEntry(1,1000,{language:null}),otherProvider=performanceEntry(1,3000,{provider_url:'https://other.example'}),otherRepo=performanceEntry(1,5000,{provider_repo_id:2}),unknown=performanceEntry(2,7000,{project_id:null,language:' '});
  const m=ciChartModel([one,duplicate,otherProvider,otherRepo,unknown]).performance;
  assert.ok(m);assert.equal(m.measured,4);assert.equal(m.duplicates,1);
  assert.equal(m.projects.find(g=>g.label==='alpha').meanMs,3000);
  assert.deepEqual(m.languages.map(g=>[g.label,g.count]).sort(),[['JavaScript',3],['Unknown',1]]);
  assert.equal(m.projects.find(g=>g.label==='Unknown').count,1);
  const even=ciChartModel([performanceEntry(1,1000),performanceEntry(2,3000)]).performance;
  assert.equal(even.projects[0].medianMs,2000);
});

test('performance histogram includes exact boundaries once and an unbounded overflow bucket',()=>{
  const m=ciChartModel([1,59999,60000,299999,300000,599999,600000,1799999,1800000,3599999,3600000,7200000].map((duration,i)=>performanceEntry(i+1,duration))).performance;
  assert.ok(m);
  assert.deepEqual(m.bins.map(b=>b.count),[2,2,2,2,2,2]);
  assert.deepEqual(m.projects[0].bins,[2,2,2,2,2,2]);
  assert.equal(m.bins.reduce((sum,b)=>sum+b.count,0),12);
});

test('performance rendering escapes metadata, reports bounded visual coverage and exposes every exact group',()=>{
  const rows=Array.from({length:12},(_,i)=>performanceEntry(i+1,1001+i,{project_id:`Project ${i}`,language:i?'Rust':'<img src=x onerror=alert(1)>'}));
  const html=renderCiCharts(ciChartModel(rows),{retained:true,stale:true,partial:true,days:7});
  assert.match(html,/primary repository language/);assert.match(html,/Mean \(ms\)/);assert.match(html,/Median \(ms\)/);assert.match(html,/p95 \(ms\)/);
  assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img/);
  assert.match(html,/10 of 12 projects/);assert.match(html,/Project 11/);assert.match(html,/1001/);
  assert.match(html,/setup, build, test and cache/);assert.match(html,/Last 7 days/);
  assert.match(html,/nearest.rank/i);assert.match(renderCiCharts(ciChartModel([])),/No successful runs with valid positive start-to-finish times/);
});

test('performance deduplicates a recent run URL with retained provider identity',()=>{
  const m=ciChartModel([performanceEntry(1,60000,{provider_url:undefined,url:'https://ci.example/repos/1/pipeline/1'}),performanceEntry(1,60000),performanceEntry(1,120000,{provider_url:'https://another-ci.example'})]).performance;
  assert.equal(m.measured,2);assert.equal(m.duplicates,1);assert.equal(m.projects[0].meanMs,90000);
});

test('successful performance respects all shared filters across outcome tabs',()=>{
  const now=Date.now(),recent=new Date(now-86400000).toISOString(),old=new Date(now-30*86400000).toISOString();
  const base={state:'success',provider_repo_id:7,project_id:'alpha',language:'Go',mission_key:'pi:one',session_id:'session:one',branch:'main',created_at:recent,started_at:now-60000,finished_at:now};
  const entries=integrationEntries({pipelines:{'org/app':[
    {...base,run_number:1}, {...base,run_number:2,created_at:old}, {...base,run_number:3,branch:'other'}, {...base,run_number:4,mission_key:'pi:other'}, {...base,run_number:5,session_id:'session:other'}, {...base,run_number:6,provider_repo_id:8}, {...base,run_number:7,state:'failed'},
  ]}});
  const selected=selectIntegrations(entries,{tab:'all',repository:'7',branch:'main',days:7,mission:'pi:one',session:'session:one',search:'alpha'});
  assert.deepEqual(selected.map(e=>e.record.run_number),[1,7]);
  assert.equal(ciChartModel(selected).performance.measured,1);
});

test('each project and language has an accessible histogram alongside its average',()=>{
  const html=renderCiCharts(ciChartModel([performanceEntry(1,60000)]));
  assert.match(html,/aria-label="Project alpha duration histogram"/);
  assert.match(html,/aria-label="Current primary repository language JavaScript duration histogram"/);
});

test('CI row selection distinguishes provider origins inferred from run URLs',()=>{
  const entries=integrationEntries({pipelines:{'org/app':[
    {provider_repo_id:1,run_number:1,url:'https://ci.one/org/app/1',title:'First provider'},
    {provider_repo_id:1,run_number:1,url:'https://ci.two/org/app/1',title:'Second provider'},
  ]}});
  assert.notEqual(entries[0].id,entries[1].id);
  assert.equal(entries.find(e=>e.id===entries[1].id).record.title,'Second provider');
  const retained=integrationEntries({pipelines:{'org/app':[{provider_url:'https://ci.two',provider_repo_id:1,run_number:1}]}});
  assert.equal(entries[1].id,retained[0].id);
});

test('provider base paths deduplicate retained runs without merging separate same-origin deployments',()=>{
  const rows=[
    performanceEntry(1,60000,{provider_url:'https://CI.EXAMPLE:443/woodpecker/'}),
    performanceEntry(1,60000,{provider_url:undefined,url:'https://ci.example/woodpecker/repos/1/pipeline/1?tab=logs'}),
    performanceEntry(1,120000,{provider_url:undefined,url:'https://ci.example/other-ci/repos/1/pipeline/1/'}),
    performanceEntry(1,180000,{provider_url:undefined,url:'https://ci.example/repos/1/pipeline/1'}),
  ];
  const m=ciChartModel(rows).performance;
  assert.equal(m.measured,3);assert.equal(m.duplicates,1);assert.equal(m.projects[0].meanMs,120000);
  const entries=integrationEntries({pipelines:{'org/app':rows.map(e=>e.record)}});
  assert.equal(entries[0].id,entries[1].id);
  assert.notEqual(entries[1].id,entries[2].id);
  assert.notEqual(entries[2].id,entries[3].id);
});
