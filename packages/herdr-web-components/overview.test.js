import test from 'node:test';
import assert from 'node:assert/strict';
import { overviewModel, renderOverviewSection, meaningfulEvents } from '../overview-view.js';

const now = Date.parse('2026-10-20T12:00:00Z');
const live = (key, status = 'EXECUTING', health = 'active') => ({mission_key:key, mission:{id:key,title:'Decode spectra',status},freshness:{state:'live',age_seconds:1},activity:{health,currentActivity:{summary:'Reading spectra'}}});
test('overview merges exact mission namespaces and excludes terminal work from current counts', () => {
  const model = overviewModel({projections:[live('pi:same'),live('pi:failed','FAILED'),live('pi:done','COMPLETE')],missions:[{mission_id:'same',status:'active'},{mission_id:'old',status:'closed'}],sessions:[{session_id:'one',status:'active'},{session_id:'two',status:'stopped'},{session_id:'three',status:'failed'}]},now,now);
  assert.equal(model.missions.current.length,2);
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
  assert.equal(model.missions.lastknown.length,1);
});
test('sections work independently with missing data and keep failures visible beside retained records', () => {
  const data={sessions:[{session_id:'one',title:'Parser',status:'active',updated_at:'2026-10-19T12:00:00Z'}]};
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
  assert.match(html,/Mission reported finished/);assert.match(html,/Mission reported failed/);assert.match(html,/Session reported finished/);
});
test('overview suppresses telemetry noise and bounds untrusted summaries', () => {
  const events=[{event_type:'HostHeartbeat'},{event_type:'mission_activity.snapshot'},{event_type:'ServiceRegistered'},...Array.from({length:20},()=>({event_type:'MissionEnded',entity_type:'mission',entity_id:'private-id',payload:{title:'<script>'+ 'prompt '.repeat(1000)}}))];
  assert.equal(meaningfulEvents(events).length,8);
  const html=renderOverviewSection('activity',{events},{activity:{loaded:true}},now,now);
  assert.doesNotMatch(html,/<script>|private-id|HostHeartbeat|ServiceRegistered/);assert.ok(html.length<6000);
});
test('old host and service statuses remain qualified observations', () => {
  const data={hosts:[{host_id:'beast',status:'healthy',last_seen:'2026-10-19T12:00:00Z'}],services:[{service_id:'service',status:'healthy',lifecycle:{state:'current'}},{service_id:'old',lifecycle:{archived:true}}]};
  const model=overviewModel(data,now,now);
  assert.equal(model.services.current,1);assert.equal(model.services.archived,1);
  assert.match(renderOverviewSection('hosts',data,{hosts:{loaded:true}},now,now),/Last reported healthy/);
  assert.match(renderOverviewSection('services',data,{services:{loaded:true}},now,now),/Reported healthy/);
});
