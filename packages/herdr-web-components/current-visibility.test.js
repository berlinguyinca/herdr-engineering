import test from 'node:test';
import assert from 'node:assert/strict';
import {reportFreshness, recordVisibility, recordOutcome, REPORT_FRESH_MS} from '../record-state.js';
import {serviceState, selectServices, renderServiceDetails} from '../services-view.js';
import {sessionEntries, selectSessions} from '../sessions-view.js';
import {missionEntries, filterMissions} from '../mission-catalog-view.js';
import {overviewModel, renderOverviewSection} from '../overview-view.js';

const now=Date.parse('2026-10-20T12:00:00Z');
const stamp=offset=>new Date(now+offset).toISOString();
const live={mission_key:'pi:live',mission:{status:'EXECUTING'},activity_snapshot_at:stamp(-1000),freshness:{state:'live',age_seconds:1},activity:{health:'stalled'}};

test('report visibility uses source time, rejects missing, malformed, and future timestamps',()=>{
  assert.equal(reportFreshness(stamp(-1000),now),'current');
  assert.equal(reportFreshness(stamp(5000),now),'current');
  assert.equal(reportFreshness(now/1000-1,now),'current');
  assert.equal(reportFreshness(now-1000,now),'current');
  assert.equal(reportFreshness(stamp(-REPORT_FRESH_MS),now),'current');
  assert.equal(reportFreshness(stamp(-REPORT_FRESH_MS-1),now),'stale');
  for(const value of [null,undefined,'','not a date','2026-10-20', '2026-10-20T12:00:00',stamp(5001),Infinity,{},true]) assert.equal(reportFreshness(value,now),'unknown',String(value));
});
test('unconfirmed status is presentation only and terminal lifecycle always wins',()=>{
  const old={status:'active',updated_at:stamp(-REPORT_FRESH_MS-1)};
  assert.equal(recordVisibility(old,now),'unconfirmed');
  assert.equal(recordOutcome(old),'current');
  assert.equal(recordVisibility({...old,updated_at:stamp(0)},now),'current');
  assert.equal(recordVisibility({...old,status:'mystery',updated_at:stamp(0)},now),'unconfirmed');
  assert.equal(recordVisibility({...old,status:'failed'},now),'failed');
  assert.equal(recordVisibility({...old,stage:'closed'},now),'finished');
});
test('services expire without a status update and remain inspectable through filters',()=>{
  const rows=[{service_id:'live',status:'healthy',updated_at:stamp(0),lifecycle:{state:'current',last_seen_at:stamp(0)}},
    {service_id:'missing',status:'healthy'}, {service_id:'stopped',status:'stopped'},
    {service_id:'archived',status:'stopped',lifecycle:{archived:true}}];
  rows.forEach(row=>row.catalog={scope:'herdr'});
  assert.deepEqual(selectServices(rows,{now}).map(s=>s.service_id),['live']);
  assert.equal(serviceState(rows[0],now+REPORT_FRESH_MS+1),'unknown');
  assert.equal(serviceState({status:'stopped',updated_at:stamp(0),lifecycle:{state:'current',last_seen_at:stamp(0)}},now),'unavailable');
  assert.equal(selectServices(rows,{status:'unknown',now}).length,1);
  assert.equal(selectServices(rows,{status:'unavailable',now}).length,1);
  assert.equal(selectServices(rows,{status:'archived',archived:true,now}).length,1);
  assert.equal(selectServices(rows,{status:'all',archived:true,now}).length,4);
});
test('session defaults exclude old and missing reports, while Unconfirmed remains searchable',()=>{
  const rows=[{session_id:'fresh',status:'active',updated_at:stamp(0)}, {session_id:'old',status:'active',updated_at:stamp(-REPORT_FRESH_MS-1)}, {session_id:'missing',status:'active'}, {session_id:'done',status:'closed'}];
  const entries=sessionEntries(rows,[],now);
  assert.deepEqual(selectSessions(entries).map(e=>e.id),['fresh']);
  assert.deepEqual(selectSessions(entries,{tab:'unconfirmed',search:'old'}).map(e=>e.id),['old']);
  assert.equal(selectSessions(entries,{tab:'finished'}).length,1);
  assert.equal(selectSessions(sessionEntries(rows,[],now+REPORT_FRESH_MS+1)).length,0);
});
test('missions require actual fresh activity evidence and keep real stalls visible',()=>{
  const missing={...live,mission_key:'pi:missing',activity_snapshot_at:undefined};
  const stale={...live,mission_key:'pi:stale',activity_snapshot_at:stamp(-120000)};
  const future={...live,mission_key:'pi:future',activity_snapshot_at:stamp(6000)};
  const done={...stale,mission_key:'pi:done',mission:{status:'COMPLETE'}};
  const entries=missionEntries([live,missing,stale,future,done],[{mission_id:'recorded',status:'active',updated_at:stamp(0)}],now,now);
  assert.deepEqual(filterMissions(entries,{tab:'current'}).map(e=>e.key),['pi:live','recorded']);
  assert.equal(entries.find(e=>e.key==='pi:live').group,'attention');
  assert.equal(filterMissions(entries,{tab:'unconfirmed'}).length,3);
  assert.equal(filterMissions(entries,{tab:'finished'}).length,1);
  assert.equal(missionEntries([live],[],now,now+31000)[0].tab,'unconfirmed');
});
test('overview current totals match catalogs and retain clear history links',()=>{
  const data={projections:[live],sessions:[{session_id:'old',status:'active',updated_at:stamp(-REPORT_FRESH_MS-1)}],services:[{service_id:'old',catalog:{scope:'herdr'},status:'healthy',updated_at:stamp(-REPORT_FRESH_MS-1)}]};
  const model=overviewModel(data,now,now);
  assert.equal(model.sessions.current.length,0);
  assert.equal(model.sessions.unconfirmed.length,1);
  assert.equal(model.services.current,0);
  assert.equal(model.missions.current.length,1);
  assert.equal(overviewModel(data,now,now+31000).missions.current.length,0);
  const html=renderOverviewSection('sessions',data,{sessions:{loaded:true}},now,now);
  assert.match(html,/#\/herd\?tab=unconfirmed/);
  assert.doesNotMatch(html,/#\/herd\/session\/old/);
});

test('native working and busy sessions stay Current while telemetry is fresh',()=>{
  for(const status of ['working','busy','idle']) {
    assert.equal(recordVisibility({status,updated_at:stamp(0)},now),'current');
    assert.equal(recordVisibility({status,updated_at:stamp(-REPORT_FRESH_MS-1)},now),'unconfirmed');
  }
});

test('historical dynamic endpoints remain context without advertising a reusable live port',()=>{
  const service={service_id:'web',status:'healthy',url:'http://dev.lan:18012',updated_at:new Date().toISOString()};
  assert.match(renderServiceDetails(service),/href="http:\/\/dev.lan:18012\/"/);
  for(const record of [{...service,status:'stopped'},{...service,updated_at:'2020-01-01T00:00:00Z'},{...service,lifecycle:{archived:true}}]) {
    const html=renderServiceDetails(record);
    assert.doesNotMatch(html,/href="http:\/\/dev.lan:18012/);
    assert.match(html,/Recorded endpoint \(not current\): http:\/\/dev.lan:18012/);
  }
});

test('main service catalog requires confirmed HerdR origin and keeps host listeners separate',()=>{
  const base={status:'healthy',updated_at:stamp(0)};
  const rows=[{...base,service_id:'owned',catalog:{scope:'herdr'}},
    {...base,service_id:'system',catalog:{scope:'system'}},
    {...base,service_id:'unassigned',catalog:{scope:'unassigned'}},
    {...base,service_id:'legacy',session_id:'guessed',project:'herdr',repository:'github.com/example/herdr'},
    {...base,service_id:'invalid',catalog:{scope:'HERDR'}}];
  assert.deepEqual(selectServices(rows,{now}).map(s=>s.service_id),['owned']);
  assert.deepEqual(selectServices(rows,{scope:'system',now}).map(s=>s.service_id),['invalid','legacy','system','unassigned']);
  assert.equal(overviewModel({services:rows},now,now).services.current,1);
  assert.match(renderOverviewSection('services',{services:rows},{services:{loaded:true}},now,now),/#\/system-services/);
});

test('native open mission phases remain Current only with recent reports',()=>{
  for(const phase of ['specification','implementation','review','repair','validation','pr']) {
    for(const lifecycle of [{status:phase},{stage:phase}]) {
      const record={mission_id:`mission-${phase}`,updated_at:stamp(0),...lifecycle};
      assert.equal(recordVisibility(record,now),'current',phase);
      const fresh=missionEntries([],[record],now,now)[0];
      assert.equal(fresh.tab,'current');
      assert.equal(fresh.group,'recorded');
      const old=missionEntries([],[record],now,now+REPORT_FRESH_MS+1)[0];
      assert.equal(old.tab,'unconfirmed');
      assert.equal(recordOutcome(record),'current');
    }
  }
  assert.equal(recordVisibility({status:'napping',updated_at:stamp(0)},now),'unconfirmed');
});

test('fresh legacy heartbeats cannot revive an expired or missing HerdR listener observation',()=>{
  const observation={state:'observed',observed_at:stamp(-90001),stale_after_seconds:90};
  const base={service_id:'owned',status:'healthy',updated_at:stamp(0),lifecycle:{state:'current',last_seen_at:stamp(0)},catalog:{scope:'herdr',metadata_observation:observation}};
  assert.equal(serviceState(base,now),'unknown');
  assert.equal(serviceState({...base,catalog:{...base.catalog,metadata_observation:{...observation,observed_at:stamp(-89000)}}},now),'current');
  assert.equal(serviceState({...base,catalog:{...base.catalog,metadata_observation:{...observation,state:'missing',observed_at:stamp(0)}}},now),'unknown');
  assert.equal(serviceState({...base,catalog:{scope:'herdr'}},now),'current');
  for(const scope of ['system','unassigned']) {
    assert.equal(serviceState({...base,catalog:{scope,metadata_observation:observation}},now),'current');
    assert.equal(serviceState({...base,catalog:{scope},metadata_observation:{...observation,state:'missing',observed_at:stamp(0)}},now),'unknown');
  }
  assert.equal(base.status,'healthy');
  assert.equal(serviceState({...base,status:'stopped'},now),'unavailable');
});
test('local-only observed listeners never advertise an exposed or actionable endpoint',()=>{
  const base={service_id:'local',status:'healthy',updated_at:new Date().toISOString(),port:18012,endpoint_scope:'local_only',url:'http://dev.lan:18012',catalog:{scope:'herdr'}};
  const html=renderServiceDetails(base);
  assert.match(html,/Local only/);
  assert.match(html,/Observed port/);
  assert.doesNotMatch(html,/Exposed|Open endpoint|href="http:/);
});

test('unconfirmed listener ownership is neither observed nor absent and disables endpoints in every scope',()=>{
  for(const scope of ['herdr','system','unassigned']) {
    const service={service_id:'ambiguous',status:'healthy',updated_at:new Date().toISOString(),url:'http://dev.lan:18012',catalog:{scope,
      metadata_observation:{state:'unconfirmed',observed_at:new Date().toISOString(),stale_after_seconds:90}}};
    assert.equal(serviceState(service),'unknown');
    const html=renderServiceDetails(service);
    assert.match(html,/<dt>Listener observation<\/dt><dd>Unconfirmed<\/dd>/);
    assert.match(html,/Listener ownership could not be confirmed/);
    assert.doesNotMatch(html,/<dd>Observed<\/dd>|Missing from latest observation|Listener was absent|href="http:/);
    assert.equal(service.status,'healthy');
  }
});
