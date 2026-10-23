import test from 'node:test';
import assert from 'node:assert/strict';
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
