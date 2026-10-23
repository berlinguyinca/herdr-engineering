import test from 'node:test';
import assert from 'node:assert/strict';
import {ciInsightsModel,renderCiInsights,filterCiObservations} from '../ci-insights.js';
const now=Date.parse('2026-10-22T12:00:00Z');
const run=(run_number,state,extra={})=>({repository:'org/app',provider_repo_id:7,branch:'main',run_number,state,created_at:'2026-10-21T12:00:00Z',started_at:'2026-10-21T12:00:00Z',finished_at:'2026-10-21T12:01:00Z',...extra});
const report=(run_number,total,tests=[],extra={})=>({...run(run_number,'success'),report_id:`r${run_number}`,reported_at:`2026-10-${run_number+18}T12:00:00Z`,summary:{total,passed:total,failed:0,errored:0,skipped:0},tests,...extra});
const tc=(status,extra={})=>({id:'suite.test',suite:'suite',name:'test',status,duration_ms:120,...extra});
test('success rate excludes cancelled/skipped/unknown and measures actual terminal duration',()=>{
 const model=ciInsightsModel({pipelines:[run(1,'success'),run(2,'failure'),run(3,'cancelled'),run(4,'skipped'),run(5,'running'),run(6,'unknown')]},{},now);
 assert.equal(model.successRate,50);assert.equal(model.decided,2);assert.equal(model.failed,1);assert.equal(model.current,2);
 assert.equal(model.medianDurationMs,60000);assert.equal(model.missingReports,4);assert.equal(model.latestTotal,null);
});
test('latest counts and deltas compare only the same provider repository and branch',()=>{
 const reports=[report(1,10),report(2,14),report(3,100,[],{branch:'feature'}),report(4,9,[],{provider_repo_id:8})];
 const model=ciInsightsModel({reports},{branch:'main'},now);
 assert.equal(model.latestTotal,23);assert.equal(model.groups.length,2);
 assert.equal(model.groups.find(g=>g.latest.provider_repo_id===7).delta,4);
 assert.equal(model.groups.find(g=>g.latest.provider_repo_id===8).delta,null);
 assert.equal(ciInsightsModel({reports:[report(1,0)]},{},now).latestTotal,0);
});
test('failure recurrence and first failure/last pass use stable test identity without conflating branches',()=>{
 const reports=[report(1,1,[tc('failed',{message:'first'})]),report(2,1,[tc('passed')]),report(3,1,[tc('failed',{message:'latest',duration_ms:800})]),report(4,1,[tc('passed')],{branch:'feature'})];
 const model=ciInsightsModel({reports},{branch:'main'},now);
 assert.equal(model.failures.length,1);assert.equal(model.failures[0].failures,2);assert.equal(model.failures[0].observations,3);
 assert.equal(model.failures[0].firstFailure,reports[0].reported_at);assert.equal(model.failures[0].lastPass,reports[1].reported_at);
 assert.equal(model.failures[0].test.message,'latest');assert.equal(model.slowTests[0].test.duration_ms,800);
 assert.equal(model.inconsistent.length,0);
});
test('same revision conflicting attempts are evidence of inconsistent outcomes, missing revision is not',()=>{
 const reports=[report(1,1,[tc('failed')],{revision:'abc'}),report(2,1,[tc('passed')],{revision:'abc'}),report(3,1,[tc('failed')])];
 assert.equal(ciInsightsModel({reports},{},now).inconsistent.length,1);
 assert.equal(ciInsightsModel({reports:reports.map(r=>({...r,revision:''}))},{},now).inconsistent.length,0);
});
test('filters apply exact branch/repository and measured time to both reports and runs',()=>{
 const data={reports:[report(1,3),report(2,7,[],{repository:'org/other'})],pipelines:[run(1,'failure'),run(2,'success',{branch:'feature'}),run(3,'success',{created_at:'invalid'})]};
 const filtered=filterCiObservations(data,{repository:'org/app',branch:'main',days:7},now);
 assert.equal(filtered.reports.length,1);assert.equal(filtered.pipelines.length,1);
 assert.equal(filterCiObservations(data,{search:'feature'},now).pipelines.length,1);
 assert.equal(filterCiObservations(data,{repository:'ORG/app'},now).reports.length,0);
});
test('render states distinguish no report from zero, escape failure details, show scope and real trends',()=>{
 const empty=renderCiInsights(ciInsightsModel({}, {}, now));
 assert.match(empty,/No test reports received/);assert.match(empty,/Unavailable/);assert.doesNotMatch(empty,/NaN|Infinity/);
 const model=ciInsightsModel({reports:[report(1,1,[tc('failed',{message:'<script>bad</script>',trace:'<b>trace</b>'})]),report(2,2,[tc('failed')])]},{},now);
 const html=renderCiInsights(model);
 assert.match(html,/Test count over time/);assert.match(html,/First failure/);assert.match(html,/Last pass/);assert.match(html,/Observed history/);
 assert.doesNotMatch(html,/<script>|<b>trace/);
 assert.match(renderCiInsights(ciInsightsModel({reports:[report(1,0)]},{},now)),/>0</);
});
test('latest attempt ordering uses attempt for equal timestamps and deduplicates provider run samples',()=>{
 const a=report(2,2,[tc('failed')],{attempt:1}),b={...report(2,3,[tc('passed')]),attempt:2,report_id:'retry'};
 const m=ciInsightsModel({reports:[b,a],pipelines:[run(1,'running'),run(1,'success')]},{},now);
 assert.equal(m.latestTotal,3);assert.equal(m.totalRuns,1);assert.equal(m.successRate,100);assert.equal(m.failures.length,0);
});

test('run drilldown lists all reported test outcomes and identifies retries',async()=>{
 const {renderCiRunReports}=await import('../ci-insights.js');
 const html=renderCiRunReports([report(1,2,[tc('failed',{message:'<b>bad</b>'}),tc('skipped',{id:'skip',name:'skipped case'})],{attempt:1}),report(1,1,[tc('passed')],{attempt:2})]);
 assert.match(html,/Test results/);assert.match(html,/2 report attempts/);assert.match(html,/passed/);assert.doesNotMatch(html,/<b>bad/);
 assert.match(renderCiRunReports([]),/No test report received/);
});

test('test drilldown bounds rows with searchable keyboard-operable pages',async()=>{
 const {renderCiRunReports}=await import('../ci-insights.js');
 const cases=Array.from({length:120},(_,i)=>tc('passed',{id:`case${i}`,name:`test_${i}`}));
 const reports=[report(1,120,cases)];
 const html=renderCiRunReports(reports);
 assert.match(html,/Page 1 of 3/);assert.equal((html.match(/<tr>/g)||[]).length,51);
 assert.match(renderCiRunReports(reports,{page:2}),/test_119/);
 const filtered=renderCiRunReports(reports,{search:'test_119'});assert.match(filtered,/1 matching tests/);assert.doesNotMatch(filtered,/>test_118</);
});

test('failure tables paginate instead of rendering every received test',()=>{
 const cases=Array.from({length:120},(_,i)=>tc('failed',{id:`case${i}`,name:`failure_${i}`}));
 const model=ciInsightsModel({reports:[report(1,120,cases)]},{},now);
 const html=renderCiInsights(model);
 assert.match(html,/Page 1 of 3/);assert.doesNotMatch(html,/>suite failure_119</);
 assert.match(renderCiInsights(model,{failurePage:2}),/>suite failure_119</);
});

test('server attempt order wins over backdated timestamps and late older runs',async()=>{
 const {renderCiRunReports}=await import('../ci-insights.js');
 const first=report(2,100,[tc('failed')],{attempt:1,reported_at:'2026-10-21T12:00:00Z'});
 const second=report(2,200,[tc('passed')],{attempt:2,reported_at:'2026-10-21T11:00:00Z'});
 const oldRun=report(1,50,[],{reported_at:'2026-10-22T12:00:00Z'});
 const model=ciInsightsModel({reports:[first,second,oldRun]},{},now);
 assert.equal(model.latestTotal,200);assert.equal(model.failures.length,0);assert.equal(model.groups[0].delta,150);
 assert.match(renderCiRunReports([first,second]),/Latest attempt 2/);
});
