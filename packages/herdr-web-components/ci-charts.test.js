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
