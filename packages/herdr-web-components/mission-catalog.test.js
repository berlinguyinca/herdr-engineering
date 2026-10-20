import test from 'node:test';
import assert from 'node:assert/strict';
import { missionEntries, filterMissions, compactMissionRow, measuredProgress } from '../mission-catalog-view.js';
const now=Date.parse('2026-10-20T12:00:00Z');
const projection=(key='pi:MSN-1',overrides={})=>({mission_key:key,activity_snapshot_at:new Date(now-2000).toISOString(),host_id:'beast',mission:{id:'MSN-1',title:'Repair spectra parser',status:'EXECUTING'},freshness:{state:'live',age_seconds:2},activity:{health:'active',currentActivity:{summary:'Running parser tests'}},...overrides});
test('catalog merges only exact namespaced mission identities and retains missing recorded context',()=>{
 const entries=missionEntries([projection()], [{mission_key:'pi:MSN-1',mission_id:'MSN-1',title:'Recorded title',purpose:'Spectrum format decoder'}, {mission_id:'MSN-1',title:'Independent HerdR mission'}],now,now);
 assert.equal(entries.length,2);assert.equal(entries.find(e=>e.key==='pi:MSN-1').title,'Repair spectra parser');
 assert.equal(entries.find(e=>e.key==='MSN-1').group,'recorded');
 assert.equal(filterMissions(entries,{search:'spectrum decoder'}).length,1);
});
test('only current evidence contributes to running or needs-attention groups',()=>{
 const stalled=projection('pi:attention',{activity:{health:'stalled'}});
 const stale=projection('pi:stale',{freshness:{state:'stale',age_seconds:40},activity:{health:'stalled'}});
 const updating=projection('pi:updating',{connection:'updating',freshness:{state:'updating',age_seconds:0}});
 const finished=projection('pi:done',{mission:{id:'done',status:'COMPLETE'}});
 const groups=Object.fromEntries(missionEntries([projection(),stalled,stale,updating,finished],[],now,now).map(e=>[e.key,e.group]));
 assert.deepEqual(groups,{'pi:MSN-1':'running','pi:attention':'attention','pi:stale':'lastknown','pi:updating':'lastknown','pi:done':'finished'});
 assert.equal(missionEntries([projection()],[],now,now+120000)[0].group,'lastknown');
});
test('title search uses full mission and objective but compact rows bound untrusted content and worker noise',()=>{
 const p=projection();p.mission.title='<script>window.bad=true</script> '+ 'Long mission brief '.repeat(100);p.activity.currentObjective='Find TARGET_SPECTRUM in a long instruction';p.activity.workerDetails=Array.from({length:40},(_,i)=>({workerId:'worker-private-'+i,state:'failed',model:'huge-model'}));
 const entries=missionEntries([p],[],now,now);assert.equal(filterMissions(entries,{search:'target_spectrum',host:'beast'}).length,1);
 const html=compactMissionRow(entries[0],now);
 assert.ok(html.length<3000);assert.doesNotMatch(html,/worker-private|<script>|huge-model/);assert.match(html,/40 finished/);assert.match(html,/&lt;script&gt;/);
});
test('unknown progress stays unknown, measured weighted progress rounds without floating-point artifacts',()=>{
 assert.equal(measuredProgress({progress:{approximatePercent:80}}),null);
 const p=measuredProgress({workflowProgress:{basis:'weighted_dag',completed:2.4000000000000004,total:60}});
 assert.equal(p.percent,4);assert.match(p.label,/Workflow/);assert.doesNotMatch(p.label,/000000/);
 assert.equal(measuredProgress({acceptanceCoverage:{completed:1,total:4}}).percent,25);
 assert.equal(measuredProgress({acceptanceCoverage:{completed:5,total:4}}),null);
});
test('waiting duration and source freshness remain visible without exposing objective or diagnostic clocks',()=>{
 const p=projection();p.activity.waitingReason='gateway_busy';p.activity.waitingSince='2026-10-20T11:58:00Z';p.activity.currentObjective='full objective hidden from row';
 const html=compactMissionRow(missionEntries([p],[],now,now)[0],now);
 assert.match(html,/gateway busy/);assert.match(html,/2m 0s/);assert.match(html,/data-report-age/);assert.doesNotMatch(html,/full objective|Heartbeat age|Last progress/);
});

test('failed lifecycle stays in Failed regardless of telemetry freshness',()=>{
 const failed=projection('pi:failed',{mission:{id:'failed',status:'FAILED'}});
 assert.equal(missionEntries([failed],[],now,now)[0].group,'failed');
 assert.equal(missionEntries([failed],[],now,now+120000)[0].group,'failed');
});
test('queued and unknown workers are not mislabeled finished, and stale counts stay qualified',()=>{
 const p=projection();p.activity.workerDetails=[{state:'queued'},{state:'running'},{state:'failed'},{state:'mystery'}];
 let entry=missionEntries([p],[],now,now)[0];
 assert.match(compactMissionRow(entry,now),/1 current · 1 finished · 2 other workers/);
 entry=missionEntries([p],[],now,now+120000)[0];
 assert.match(compactMissionRow(entry,now+120000),/1 active at last report · 1 finished · 2 other/);
});

test('terminal tabs exclude recorded active or unknown missions and honor lifecycle stage',()=>{
 const entries=missionEntries([], [
  {mission_id:'active',status:'active',stage:'implementing'},
  {mission_id:'unknown'},
  {mission_id:'closed',status:'closed'},
  {mission_id:'failed',status:'active',stage:'failed'},
  {mission_id:'done',status:'active',stage:'completed'},
 ],now,now);
 assert.deepEqual(filterMissions(entries,{tab:'unconfirmed'}).map(e=>e.key),['active','unknown']);
 assert.deepEqual(filterMissions(entries,{tab:'failed'}).map(e=>e.key),['failed']);
 assert.deepEqual(filterMissions(entries,{tab:'finished'}).map(e=>e.key),['closed','done']);
});
test('terminal lifecycle remains visible when disconnected and old worker health says active',()=>{
 const entry=missionEntries([projection('pi:closed',{mission:{status:'CLOSED'},freshness:{state:'disconnected',age_seconds:900}})],[],now,now)[0];
 assert.equal(entry.group,'finished');
 assert.match(compactMissionRow(entry,now),/Closed/);
});

test('stage-based terminal outcomes display the terminal stage and freeze old waits',()=>{
 const record=missionEntries([], [{mission_id:'finished-stage',status:'active',stage:'complete'}],now,now)[0];
 assert.match(compactMissionRow(record,now),/Complete · recorded/);
 const p=projection('pi:done',{mission:{status:'CLOSED'},activity_snapshot_at:'2026-10-20T12:00:00Z',snapshot_at:'2026-10-20T12:00:00Z',activity:{waitingReason:'gateway_busy',waitingSince:'2026-10-20T11:59:00Z'}});
 const entry=missionEntries([p],[],now,now+10000)[0];
 assert.match(compactMissionRow(entry,now+10000),/1m 0s<\/span> at last snapshot/);
});
