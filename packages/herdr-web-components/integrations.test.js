import test from 'node:test';
import assert from 'node:assert/strict';
import {integrationEntries,integrationOutcome,integrationTimestamp,selectIntegrations,integrationHealth,integrationRow,renderPipeline,activePiSessions,pipelineDetails} from '../integrations-view.js';
import {renderOverviewSection} from '../overview-view.js';

test('CI outcomes preserve unknown and pending work without inventing a stall',()=>{
  for(const state of ['pending','running','blocked','unknown','waiting'])assert.equal(integrationOutcome({state}),'current');
  for(const state of ['failure','error','killed'])assert.equal(integrationOutcome({state}),'failed');
  for(const state of ['success','canceled','skipped'])assert.equal(integrationOutcome({state}),'finished');
  const row=integrationRow(integrationEntries({pipelines:{'owner/repo':[{state:'pending',run_number:1,created_at:1}]}})[0]);
  assert.match(row,/Waiting/);assert.doesNotMatch(row,/stalled|%/);
});
test('review status stays authoritative over upstream decision codes',()=>{
  const entries=integrationEntries({reviews:[{review_id:'a',status:'awaiting_review'},{review_id:'b',status:'changes_requested',decision:'annotated'},{review_id:'c',status:'approved'},{review_id:'d',status:'failed'}]},'reviews');
  assert.deepEqual(entries.map(e=>e.tab),['current','changes','finished','failed']);
  assert.equal(integrationOutcome({status:'changes_requested',decision:'denied'},'reviews'),'changes');
  assert.equal(integrationOutcome({status:'closed',decision:'annotated'},'reviews'),'finished');
});
test('filters require exact repository and context while text search spans branch and revision',()=>{
  const entries=integrationEntries({pipelines:{'a/web':[{run_number:1,branch:'feature/ui',revision:'abcdef',project_id:'spectra'}],'b/web':[{run_number:1,branch:'main',revision:'fedcba',project_id:'spectra'}]}});
  assert.equal(selectIntegrations(entries,{search:'spectra feature abc'}).length,1);
  assert.equal(selectIntegrations(entries,{repository:'a/web'}).length,1);
  assert.equal(selectIntegrations(entries,{repository:'web'}).length,0);
  const reviews=integrationEntries({reviews:[{review_id:'one',mission_id:'pi:m1',session_id:'s1'},{review_id:'two',mission_id:'m1',session_id:'s2'}]},'reviews');
  assert.equal(selectIntegrations(reviews,{mission:'pi:m1'}).length,1);
  assert.equal(selectIntegrations(reviews,{session:'s1'}).length,1);
});
test('provider errors and genuine discovery counts remain distinct from pipeline progress',()=>{
  const html=integrationHealth({status:'auth_error',error:'Credential rejected',stale:true,checked_at:'2026-10-20T12:00:00Z'});
  assert.match(html,/authentication failed/);assert.match(html,/Showing last received/);assert.match(html,/Last successful refresh time not reported/);
  const syncing=integrationHealth({status:'syncing',refreshing:true,progress:{current:'owner/repo',completed:2,total:10}});
  assert.match(syncing,/Checking owner\/repo/);assert.match(syncing,/<progress value="2" max="10"/);
  assert.doesNotMatch(integrationHealth({status:'syncing',refreshing:true}),/<progress/);
  assert.match(integrationHealth({status:'connected',project_mapping_error:'Project mapping is invalid.'}),/Project mapping is invalid.*Using repository groups/);
});
test('untrusted reviews and logs are text and unsafe upstream URLs are rejected',()=>{
  const record={review_id:'x',title:'<script>alert(1)</script>',url:'javascript:alert(1)',plan:'<iframe src=x>',feedback:'<img src=x>',revision:'deadbeef'};
  const html=integrationRow(integrationEntries({reviews:[record]},'reviews')[0],'reviews');
  assert.doesNotMatch(html,/<script>|<iframe|<img|href="javascript/);assert.match(html,/&lt;iframe/);
  for(const url of ['https://user:secret@example.com','javascript:alert(1)','https://example.com/\nfoo']) {
    assert.doesNotMatch(integrationRow({id:'x',record:{url},tab:'current'},'reviews'),/Open in Plannotator/);
  }
  const detail=renderPipeline({run_number:4,repository:'o/r',state:'running',tasks:[{id:10,name:'compile',state:'success'},{id:11,name:'test',state:'running'}]},{step:'11',log:{log:'<script>bad</script>',available:true}});
  assert.match(detail,/1 of 2 steps finished/);assert.match(detail,/&lt;script&gt;bad/);assert.doesNotMatch(detail,/data-ci-action/);
  assert.match(renderPipeline({can_retry:true,run_number:1}),/data-ci-action="retry"/);
});
test('timestamps accept upstream seconds, milliseconds and ISO while refusing zero',()=>{
  assert.equal(integrationTimestamp(1720000000),1720000000000);
  assert.equal(integrationTimestamp(1720000000000),1720000000000);
  assert.equal(integrationTimestamp('2026-10-20T12:00:00Z'),Date.parse('2026-10-20T12:00:00Z'));
  assert.equal(integrationTimestamp(0),0);
});
test('overview shows integration availability and separate outcomes without false healthy zeroes',()=>{
  const html=renderOverviewSection('ci',{ci:{available:false,status:'auth_error'}},{ci:{loaded:true}});
  assert.match(html,/authentication needs attention/);assert.doesNotMatch(html,/<strong>0<\/strong>/);
  const reviews=renderOverviewSection('reviews',{reviews:{reviews:[{review_id:'r',status:'changes_requested'}]}},{reviews:{loaded:true}});
  assert.match(reviews,/1<\/strong><span>Changes requested/);assert.match(reviews,/#\/reviews\?tab=changes/);
});

test('completed review links do not pretend an exited Plannotator process remains live',()=>{
  const record={review_id:'r',status:'approved',review_url:'https://host.example:1337',plan:'Stored plan',feedback:'Approved'};
  const html=integrationRow(integrationEntries({reviews:[record]},'reviews')[0],'reviews');
  assert.doesNotMatch(html,/Open in Plannotator/);assert.match(html,/Stored plan/);assert.match(html,/Recorded review/);
});

test('CI context links require confirmed related records and preserve exact mission namespaces',()=>{
  const entries=integrationEntries({pipelines:{'o/r':[{run_number:1,related_missions:[{mission_id:'m1',mission_key:'pi:m1'}],related_sessions:[{session_id:'s1'}]}]}});
  assert.equal(selectIntegrations(entries,{mission:'pi:m1'}).length,1);
  assert.equal(selectIntegrations(entries,{mission:'m1'}).length,0);
  assert.equal(selectIntegrations(entries,{session:'s1'}).length,1);
  assert.equal(selectIntegrations(entries,{session:'s2'}).length,0);
});

test('real provider detail fields preserve the visible run identity and full revision',()=>{
  const detail=pipelineDetails({number:42,status:'failure',commit:'abc123',repository:'7',tasks:[]},{repository:'lab/repo',run_number:42,state:'running'});
  assert.equal(detail.repository,'lab/repo');assert.equal(detail.run_number,42);
  assert.equal(detail.revision,'abc123');assert.equal(detail.state,'failure');
  assert.match(renderPipeline(detail),/Run #42/);
});
test('debug target selector excludes closed, failed, unknown and other agent sessions',()=>{
  const sessions=[{session_id:'yes',agent_role:'pi',status:'active',host_id:'beast',terminal_id:'term_1',herdr_session:'default',agent_session:{agent:'pi'}},{session_id:'closed',agent_role:'pi',status:'active',stage:'complete'},
    {session_id:'unverified',agent_role:'pi',status:'active'},{session_id:'other',agent_role:'codex',status:'active'},{session_id:'unknown',agent_role:'pi',status:'unknown'},{session_id:'failed',agent_role:'pi',status:'failed'}];
  assert.deepEqual(activePiSessions(sessions).map(s=>s.session_id),['yes']);
  assert.match(renderPipeline({state:'failure'}),/Debug with Pi/);
  assert.doesNotMatch(renderPipeline({state:'running'}),/Debug with Pi/);
});

test('CI scope filters use provider identity and exact branch and bounded dates',()=>{
 const entries=integrationEntries({pipelines:{'org/app':[
 {provider_repo_id:7,run_number:1,branch:'main',created_at:new Date().toISOString()},
 {provider_repo_id:7,run_number:2,branch:'main-extra',created_at:new Date().toISOString()},
 {provider_repo_id:8,run_number:3,branch:'main',created_at:new Date().toISOString()},
 {provider_repo_id:7,run_number:4,branch:'main',created_at:'2020-01-01T00:00:00Z'},
 ]}});
 assert.deepEqual(selectIntegrations(entries,{tab:'all',repository:'7',branch:'main',days:7}).map(e=>e.record.run_number),[1]);
});

test('CI inspector entry identity distinguishes provider instances',()=>{
 const entries=integrationEntries({pipelines:{history:[
  {provider_url:'https://ci.one',provider_repo_id:17,run_number:42},
  {provider_url:'https://ci.two',provider_repo_id:17,run_number:42},
 ]}});
 assert.notEqual(entries[0].id,entries[1].id);
});

test('CI search narrows colliding repository and run IDs by provider URL',()=>{
  const run={provider_repo_id:7,repository:'org/repo',run_number:12,state:'failed'};
  const entries=integrationEntries({pipelines:{history:[{...run,provider_url:'https://ci-a.example'},{...run,provider_url:'https://ci-b.example'}]}});
  const selected=selectIntegrations(entries,{tab:'failed',repository:'7',search:'https://ci-b.example 12'});
  assert.equal(selected.length,1);
  assert.equal(selected[0].record.provider_url,'https://ci-b.example');
});

test('CI provider selector compares canonical URLs exactly and preserves deployment paths',()=>{
  const run={provider_repo_id:7,repository:'org/repo',run_number:12,state:'failed'};
  const entries=integrationEntries({pipelines:{history:[{...run,provider_url:'https://CI.example/'},{...run,provider_url:'https://ci.example/secondary/'}]}});
  const options={tab:'failed',repository:'7',search:'12'};
  assert.deepEqual(selectIntegrations(entries,{...options,provider:'https://ci.example'}).map(entry=>entry.record.provider_url),['https://CI.example/']);
  assert.deepEqual(selectIntegrations(entries,{...options,provider:'https://CI.example/secondary'}).map(entry=>entry.record.provider_url),['https://ci.example/secondary/']);
  assert.equal(selectIntegrations(entries,{...options,provider:'https://ci.example/missing'}).length,0);
});

test('active run reports never cross provider collisions and unidentified reports remain ambiguous',async()=>{
 const {ciSelectedRunReports}=await import('../integrations-view.js');
 const active={provider_url:'https://ci.example/a',provider_repo_id:17,run_number:12};
 const other={...active,provider_url:'https://ci.example/b'};
 const unscoped={provider_repo_id:17,run_number:12};
 assert.deepEqual(ciSelectedRunReports([active,other,unscoped],[active,other],active),[active]);
 assert.deepEqual(ciSelectedRunReports([unscoped],[active],active),[unscoped]);
 assert.deepEqual(ciSelectedRunReports([unscoped],[active],unscoped),[]);
 assert.deepEqual(ciSelectedRunReports([unscoped],[active,unscoped],active),[]);
});

test('a failed run offers Fix with Pi next to Send to Pi, disabled until an agent is chosen, and marks agents on its branch',()=>{
  const sessions=[{session_id:'a',display_name:'Amber',branch:'feat/x',host_id:'beast'},{session_id:'b',display_name:'Birch',branch:'main'}];
  const failed={state:'failure',branch:'feat/x'};
  const none=renderPipeline(failed,{debugSessions:sessions,debugSessionId:''});
  assert.match(none,/<button type="button" class="hd-btn" data-debug-fix disabled>Fix with Pi<\/button>/);
  assert.match(none,/never merges/);
  assert.match(none,/Amber · beast · on this branch/);
  assert.doesNotMatch(none,/Birch · on this branch/);
  assert.match(renderPipeline(failed,{debugSessions:sessions,debugSessionId:'a'}),/data-debug-fix >Fix with Pi/);
  assert.doesNotMatch(renderPipeline({state:'running'}),/Fix with Pi/);
});

test('a selected CI run shows its related items above the steps',()=>{
  const related=renderPipeline({run_number:12,repository:'org/app',state:'success',tasks:[]},{relatedHtml:'<section class="rel">RELATED-HERE</section>'});
  assert.match(related,/RELATED-HERE/);
  assert.ok(related.indexOf('RELATED-HERE')<related.indexOf('Pipeline')||!related.includes('Pipeline'));
  assert.doesNotMatch(renderPipeline({run_number:12,tasks:[]}),/RELATED-HERE|class="rel"/);
});

test('inspector includes API diagnosis even when provider supplies no steps',()=>{
 const html=renderPipeline({run_number:7,state:'failure',event:'push',revision:'abc',created_at:'2026-10-25T10:00:00Z',diagnosis:{evidence_availability:'unavailable',reason:'No validated report matches this exact run.',failed_steps:[],failed_tests:[]}});
 assert.match(html,/Failure diagnosis/);assert.match(html,/No validated report/);assert.match(html,/push/);assert.match(html,/2026-10-25T10:00:00Z/);
});
