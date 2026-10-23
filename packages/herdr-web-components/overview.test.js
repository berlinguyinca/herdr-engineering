import test from 'node:test';
import assert from 'node:assert/strict';
import * as overview from '../overview-view.js';
import { overviewModel, renderOverviewSection, meaningfulEvents } from '../overview-view.js';

const now = Date.parse('2026-10-20T12:00:00Z');
const live = (key, status = 'EXECUTING', health = 'active') => ({mission_key:key,activity_snapshot_at:new Date(now-1000).toISOString(), mission:{id:key,title:'Decode spectra',status},freshness:{state:'live',age_seconds:1},activity:{health,currentActivity:{summary:'Reading spectra'}}});
test('overview merges exact mission namespaces and excludes terminal work from current counts', () => {
  const model = overviewModel({projections:[live('pi:same'),live('pi:failed','FAILED'),live('pi:done','COMPLETE')],missions:[{mission_id:'same',status:'active'},{mission_id:'old',status:'closed'}],sessions:[{session_id:'one',status:'active',updated_at:new Date(now).toISOString()},{session_id:'two',status:'stopped'},{session_id:'three',status:'failed'}]},now,now);
  assert.equal(model.missions.current.length,1);
  assert.equal(model.missions.running.length,1);
  assert.equal(model.missions.failed.length,1);
  assert.equal(model.missions.finished.length,2);
  assert.equal(model.sessions.current.length,1);
  assert.equal(model.sessions.finished.length,1);
  assert.equal(model.sessions.failed.length,1);
});
test('stale mission reports do not count as running or current attention', () => {
  const model = overviewModel({projections:[live('pi:stalled','EXECUTING','stalled')]},now,now+180000);
  assert.equal(model.missions.attention.length,0);
  assert.equal(model.missions.running.length,0);
  assert.equal(model.missions.unconfirmed.length,1);
});
test('sections work independently with missing data and keep failures visible beside retained records', () => {
  const data={sessions:[{session_id:'one',title:'Parser',status:'active',updated_at:'2026-10-20T11:59:00Z'}]};
  const html=renderOverviewSection('sessions',data,{sessions:{loaded:true,error:true}},now,now);
  assert.match(html,/Parser/);assert.match(html,/Could not refresh/);assert.match(html,/Last reported active/);
  assert.doesNotMatch(html,/Reported healthy|Currently running/);
  assert.match(renderOverviewSection('missions',data,{},now,now),/Loading/);
  assert.match(html,/#\/herd\?tab=finished/);
  assert.match(html,/#\/herd\/session\/one/);
});
test('initial live API failure leaves current metrics unavailable and qualifies partial totals', () => {
  const html=renderOverviewSection('missions',{missions:[{mission_id:'one',status:'active'}]},{missions:{loaded:true},projections:{error:true}},now,now);
  assert.match(html,/<strong>—<\/strong><span>Running with live reports/);
  assert.match(html,/Partial mission totals/);
  assert.doesNotMatch(html,/<strong>0<\/strong><span>Need attention/);
});
test('event ages use the source timestamp from the control-plane contract', () => {
  const html=renderOverviewSection('activity',{events:[{event_type:'MissionEnded',source_timestamp:'2026-10-20T11:59:00Z'}]},{activity:{loaded:true}},now,now);
  assert.match(html,/data-overview-stamp="2026-10-20T11:59:00Z">1m 0s/);
  assert.doesNotMatch(html,/time unknown/);
});
test('actual repository terminal reports are visible without treating ordinary refreshes as changes', () => {
  const events=[{event_type:'MissionUpdated',payload:{status:'active'}},{event_type:'SessionUpdated',payload:{status:'active'}},
    {event_type:'MissionUpdated',payload:{status:'active',stage:'complete'}},{event_type:'MissionStageChanged',payload:{to:'failed'}},{event_type:'SessionUpdated',payload:{status:'stopped'}}];
  assert.equal(meaningfulEvents(events).length,3);
  const html=renderOverviewSection('activity',{events},{activity:{loaded:true}},now,now);
  assert.match(html,/Mission reported finished/);assert.match(html,/Mission reported failed/);assert.match(html,/Agent reported finished/);
});
test('overview suppresses telemetry noise and bounds untrusted summaries', () => {
  const events=[{event_type:'HostHeartbeat'},{event_type:'mission_activity.snapshot'},{event_type:'ServiceRegistered'},...Array.from({length:20},()=>({event_type:'MissionEnded',entity_type:'mission',entity_id:'private-id',payload:{title:'<script>'+ 'prompt '.repeat(1000)}}))];
  assert.equal(meaningfulEvents(events).length,8);
  const html=renderOverviewSection('activity',{events},{activity:{loaded:true}},now,now);
  assert.doesNotMatch(html,/<script>|private-id|HostHeartbeat|ServiceRegistered/);assert.ok(html.length<6000);
});
test('old host and service statuses remain qualified observations', () => {
  const data={hosts:[{host_id:'beast',status:'healthy',last_seen:'2026-10-19T12:00:00Z'}],services:[{service_id:'service',status:'healthy',lifecycle:{state:'current'}},{service_id:'old',catalog:{scope:'herdr'},lifecycle:{archived:true}}]};
  const model=overviewModel(data,now,now);
  assert.equal(model.services.current,0);assert.equal(model.services.archived,1);
  assert.match(renderOverviewSection('hosts',data,{hosts:{loaded:true}},now,now),/Last reported healthy/);
  assert.match(renderOverviewSection('services',data,{services:{loaded:true}},now,now),/Reported healthy/);
});

test('integration lifecycle changes appear without heartbeat or synchronization noise',()=>{
  const types=['PlannotatorReviewCompleted','PlannotatorReviewFailed','CIPipelineFinished','CIPipelineFailed','PlannotatorReviewStarted','PlannotatorReviewHeartbeat','CISyncCompleted'];
  const events=types.map(event_type=>({event_type}));
  assert.equal(meaningfulEvents(events).length,4);
  const html=renderOverviewSection('activity',{events},{activity:{loaded:true}},now,now);
  assert.match(html,/Review completed/);assert.match(html,/Review failed/);
  assert.match(html,/CI run finished/);assert.match(html,/CI run failed/);
  assert.doesNotMatch(html,/heartbeat|sync|Started/);
});

test('fleet strip counts reporting hosts and surfaces version drift', async () => {
  const { renderOverviewSection } = await import('../overview-view.js');
  const now = Date.now();
  const data = {
    hosts: [{ host_id: 'a', host_name: 'bender', last_seen: new Date(now - 60e3).toISOString() },
            { host_id: 'b', host_name: 'mac.lan', last_seen: new Date(now - 5 * 864e5).toISOString() }],
    fleet: { hosts: [{ host: 'bender' }], components: [], summary: { drifted: 3, missing: 1 } },
  };
  const html = renderOverviewSection('fleet', data, { hosts: { loaded: true }, fleet: { loaded: true } }, now, now);
  assert.match(html, /<strong>1 \/ 2<\/strong>/);
  assert.match(html, /<strong>3<\/strong><span>Versions that differ/);
  assert.match(html, /No recent report from mac\.lan/);
});

test('cleanTitle strips pasted-prompt markers', async () => {
  const { cleanTitle } = await import('../herdr-web-components/util.js');
  assert.equal(cleanTitle('- "Phase 0 forensic audit is mandatory"'), 'Phase 0 forensic audit is mandatory');
  assert.equal(cleanTitle('# Pi Engineering — Fault-Tolerant'), 'Pi Engineering — Fault-Tolerant');
  assert.equal(cleanTitle('Plain title'), 'Plain title');
});

test('snoozed attention missions leave the count and rows, and can be brought back', () => {
  const projections = [live('pi:stuck', 'EXECUTING', 'stalled')];
  const states = { projections: { loaded: true }, missions: { loaded: true } };
  const base = renderOverviewSection('missions', { projections, missions: [] }, states, now, now);
  assert.match(base, /<strong>1<\/strong><span>Need attention/);
  assert.match(base, /data-snooze="pi:stuck"/);
  const snoozed = renderOverviewSection('missions', { projections, missions: [], snoozed: new Map([['pi:stuck', now + 1000]]) }, states, now, now);
  assert.match(snoozed, /<strong>0<\/strong><span>Need attention/);
  assert.match(snoozed, /1 snoozed for 24 hours/);
  assert.doesNotMatch(snoozed, /data-snooze="pi:stuck"/);
  const expired = renderOverviewSection('missions', { projections, missions: [], snoozed: new Map([['pi:stuck', now - 1]]) }, states, now, now);
  assert.match(expired, /data-snooze="pi:stuck"/);
});


test('attention orders failures before review actions and unconfirmed evidence, deduplicating exact identities', () => {
  const run = {provider_url:'https://ci.example',provider_repo_id:7,repository:'org/repo',run_number:12,state:'failed',updated_at:'2026-10-20T11:59:00Z'};
  const data = {projections:[live('pi:blocked','EXECUTING','blocked')],ci:{pipelines:{recent:[run,run,{...run,provider_repo_id:8}]}},reviews:{reviews:[{review_id:'r/1',title:'Decision',state:'changes_requested',updated_at:'2026-10-20T12:00:00Z'}]},hosts:[{host_id:'host/1',last_seen:'2026-10-19T12:00:00Z'}]};
  const result=overview.overviewAttention(data,{},now,now);
  assert.equal(result.items.length,5);
  assert.equal(result.items[0].severity,'critical');
  assert.equal(result.items[2].href,'#/missions/pi%3Ablocked');
  assert.equal(result.items[3].href,'#/reviews?tab=changes&search=r%2F1');
  assert.equal(result.items[4].href,'#/hosts/host%2F1');
  assert.notEqual(result.items[0].key,result.items[1].key);
});

test('attention uses recency within severity and actionability and retains failed sources as partial evidence', () => {
  const data={ci:{stale:true,pipelines:{recent:[{provider_repo_id:1,run_number:1,state:'failed',updated_at:'2026-10-20T10:00:00Z'},{provider_repo_id:1,run_number:2,state:'failed',updated_at:'2026-10-20T11:00:00Z'}]}}};
  const result=overview.overviewAttention(data,{ci:{loaded:true,error:true}},now,now);
  assert.match(result.items[0].detail,/Run #2/);
  assert.equal(result.complete,false);
  assert.ok(result.unavailable.includes('ci'));
  const html=renderOverviewSection('attention',{}, {ci:{error:true}},now,now);
  assert.match(html,/Attention/);assert.match(html,/Partial coverage/);
  assert.doesNotMatch(html,/No attention needed|0 items/);
});

test('overview preferences discard unknown and fixed cards and restore omitted cards safely', () => {
  assert.deepEqual(overview.sanitizeOverviewPreferences({order:['ci','ci','attention','bogus','health'],hidden:['ci','attention','health','bogus']}),{
    order:['ci','missions','fleet','sessions','reviews','services','hosts','activity'],hidden:['ci'],
  });
  for(const value of [null,42,'bad',[],{order:'ci',hidden:{ci:true}}]) {
    assert.deepEqual(overview.sanitizeOverviewPreferences(value),{order:['missions','fleet','sessions','ci','reviews','services','hosts','activity'],hidden:[]});
  }
});

test('complete attention includes fleet drift and distinguishes an empty complete snapshot from unknown coverage',()=>{
  const states=Object.fromEntries(['projections','missions','sessions','services','hosts','ci','reviews','health','fleet'].map(key=>[key,{loaded:true}]));
  const data={fleet:{summary:{drifted:2}},health:{status:'ok'}};
  const result=overview.overviewAttention(data,states,now,now);
  assert.equal(result.complete,true);
  assert.equal(result.items[0].href,'#/hosts?drift=1');
  assert.match(result.items[0].detail,/2/);
  assert.match(renderOverviewSection('attention',{},states,now,now),/No attention items in the received reports/);
  assert.match(renderOverviewSection('attention',{}, {},now,now),/— observed items/);
});

test('attention drops superseded failures when the same exact CI run has a newer success',()=>{
  const run={provider_url:'https://ci.example',provider_repo_id:7,repository:'org/repo',run_number:12};
  const result=overview.overviewAttention({ci:{pipelines:{history:[{...run,state:'failed',updated_at:'2026-10-20T11:00:00Z'},{...run,state:'success',updated_at:'2026-10-20T11:30:00Z'}]}}},{},now,now);
  assert.equal(result.items.length,0);
});

test('attention qualifies provider partial coverage even after successful source loading',()=>{
  const states=Object.fromEntries(['projections','missions','sessions','services','hosts','ci','reviews','health','fleet'].map(key=>[key,{loaded:true}]));
  assert.equal(overview.overviewAttention({ci:{available:true,status:'partial',pipelines:{}}},states,now,now).complete,false);
});

test('recorded failed missions rank newest first rather than alphabetical title',()=>{
  const result=overview.overviewAttention({missions:[
    {mission_id:'a-older',title:'Alpha older failure',status:'failed',updated_at:'2026-10-20T10:00:00Z'},
    {mission_id:'z-newer',title:'Zulu newer failure',status:'failed',updated_at:'2026-10-20T11:00:00Z'},
  ]},{},now,now);
  assert.deepEqual(result.items.map(item=>item.title),['Zulu newer failure','Alpha older failure']);
  assert.equal(result.items[0].updated,Date.parse('2026-10-20T11:00:00Z'));
});

test('CI attention destinations retain provider identity when repository IDs and run numbers collide',()=>{
  const run={provider_repo_id:7,repository:'org/repo',run_number:12,state:'failed'};
  const result=overview.overviewAttention({ci:{pipelines:{history:[{...run,provider_url:'https://ci-a.example'},{...run,provider_url:'https://ci-b.example'}]}}},{},now,now);
  assert.equal(result.items.length,2);
  const destinations=result.items.map(item=>new URL(item.href.slice(1),'https://dashboard.example'));
  assert.deepEqual(destinations.map(url=>url.searchParams.get('provider')),['https://ci-a.example','https://ci-b.example']);
  assert.deepEqual(destinations.map(url=>url.searchParams.get('search')),['12','12']);
  for(const url of destinations)assert.equal(url.searchParams.get('repository'),'7');
});
