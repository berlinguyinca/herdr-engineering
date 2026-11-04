import test from 'node:test';
import assert from 'node:assert/strict';
import {ciInsightsModel,renderCiInsights,filterCiObservations,ciRunOutcomeOverview,ciOverviewRows,ciExplorerTargets} from '../ci-insights.js';
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

test('counts-only polling never presents omitted case evidence as passing or absent failures',()=>{
 const html=renderCiInsights(ciInsightsModel({reports:[report(1,10)],scope:{test_details:'omitted'}},{},now));
 assert.match(html,/Test count over time/);
 assert.match(html,/Select a project.*test history/i);
 assert.doesNotMatch(html,/No failed tests|No conflicting outcomes|Failing tests · 0/);
});

test('Tests run metrics use UTC creation days while attempts keep report-time history',()=>{
 const pipelines=[run(1,'failure',{created_at:'2026-10-15T23:59:59Z'}),run(2,'success',{created_at:'2026-10-16T00:00:00Z'}),run(3,'failure',{created_at:null}),run(4,'failure',{created_at:'2026-10-22T12:00:01Z'})];
 const reports=[report(1,1,[tc('failed')],{reported_at:'2026-10-15T13:00:00Z'})];
 const model=ciInsightsModel({pipelines,reports},{days:7},now);
 assert.deepEqual(model.pipelines.map(r=>r.run_number),[2]);
 assert.equal(model.successRate,100);assert.equal(model.missingReports,1);
 assert.equal(model.reports.length,1,'Attempt history keeps its rolling report-time window');
});

test('PR and bucket selection constrain Tests metrics and late report attempts by exact run identity',()=>{
 const pipelines=[run(1,'failure',{event:'pull_request'}),run(2,'success',{event:'push'}),run(3,'failure',{event:'pull_request',created_at:'2026-10-22T00:00:00Z'})];
 const reports=pipelines.map(r=>({...report(r.run_number,1,[tc('failed')]),...r,reported_at:'2026-10-22T11:00:00Z'}));
 const model=ciInsightsModel({pipelines,reports},{days:7,scope:'pr',start:'2026-10-21T00:00:00Z',end:'2026-10-22T00:00:00Z'},now);
 assert.deepEqual(model.pipelines.map(r=>r.run_number),[1]);
 assert.deepEqual(model.reports.map(r=>r.run_number),[1]);
 assert.equal(model.missingReports,0);
});

test('without test reports the overview counts successful and failed runs per repository and says so',()=>{
 const run=(repository,run_number,state)=>({repository,provider_repo_id:repository==='org/a'?1:2,run_number,state,created_at:new Date().toISOString(),branch:'main'});
 const m=ciInsightsModel({pipelines:[run('org/a',1,'success'),run('org/a',2,'failure'),run('org/a',3,'failure'),run('org/b',1,'success'),run('org/b',2,'running')],reports:[]},{});
 const overview=ciRunOutcomeOverview(m);
 assert.deepEqual(overview.map(r=>[r.repository,r.passed,r.failed]),[['org/a',1,2],['org/b',1,0]]);
 const html=renderCiInsights(m,{});
 assert.match(html,/Runs by repository/);assert.match(html,/No test counts were found/);
 assert.match(html,/data-ci-test-repo="1"/);assert.doesNotMatch(html,/data-ci-investigate/);
 assert.match(renderCiInsights(m,{selectedRepo:'1'}),/data-ci-investigate="failing"/);
});

test('counts parsed from logs get bars labelled with their source; run outcomes fill in only for repositories with no counts',()=>{
 const parsed=report(4,12,[],{source:'log_parse',runners:['cargo','node-test'],summary:{total:12,passed:9,failed:3,errored:0,skipped:0}});
 const upload=report(2,5,[],{provider_repo_id:8,repository:'org/up',summary:{total:5,passed:5,failed:0,errored:0,skipped:0}});
 const bare={repository:'org/bare',provider_repo_id:9,branch:'main',run_number:1,state:'failure',created_at:'2026-10-21T12:00:00Z'};
 const m=ciInsightsModel({pipelines:[run(4,'success'),run(2,'success',{provider_repo_id:8,repository:'org/up'}),bare],reports:[parsed,upload]},{},now);
 const rows=ciOverviewRows(m);
 assert.deepEqual(rows.map(r=>[r.repository,r.unit,r.passed,r.failed,r.sources.join('+')]),[['org/app','tests',9,3,'log_parse'],['org/up','tests',5,0,'junit'],['org/bare','runs',0,1,'runs']]);
 const html=renderCiInsights(m,{});
 assert.match(html,/Tests by repository/);assert.match(html,/1 repository is counted from CI logs/);assert.match(html,/1 from uploaded JUnit reports/);
 assert.match(html,/1 show run outcomes only/);
 assert.match(html,/data-ci-source="log_parse"[^>]*title="org\/app: parsed from CI logs \(cargo, node-test\) · run #4"/);
 assert.match(html,/data-ci-source="junit"[^>]*title="org\/up: JUnit report · run #2"/);
 assert.match(html,/data-ci-source="runs"[^>]*title="org\/bare: no test counts yet/);
 assert.match(html,/9 passed<small>from CI logs<\/small>/);assert.match(html,/0 successful runs<small>run outcomes<\/small>/);
});

test('a repository whose big step could not be read says its total is a floor',()=>{
 const m=ciInsightsModel({reports:[report(4,21,[],{source:'log_parse',runners:['unittest'],skipped_steps:[{step:'wcmc',reason:'log_too_large'}]})]},{},now);
 const html=renderCiInsights(m,{});
 assert.match(html,/1 step too large to count/);assert.match(html,/1 step not counted: log too large to read, so the total is a floor/);
});

test('the explorer targets are the latest report of each branch of the chosen repository',()=>{
 const reports=[report(3,5,[],{provider_url:'https://ci.example.org'}),report(4,6,[],{provider_url:'https://ci.example.org',branch:'dev'}),report(5,7,[],{provider_repo_id:8,provider_url:'https://ci.example.org'})];
 const m=ciInsightsModel({reports},{},now);
 const key=ciOverviewRows(m).find(r=>r.repository==='org/app').key;
 assert.deepEqual(ciExplorerTargets(m,key).map(r=>[r.branch,r.run_number]).sort(),[['dev',4],['main',3]]);
 assert.match(renderCiInsights(m,{explorer:'<p id="slot"></p>'}),/id="slot"/);
});
