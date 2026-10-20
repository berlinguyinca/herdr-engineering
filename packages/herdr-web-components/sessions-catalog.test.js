import test from 'node:test';
import assert from 'node:assert/strict';
import {sessionEntries, selectSessions, sessionReport, compactSessionRow} from '../sessions-view.js';

const now = Date.parse('2026-10-21T12:00:00Z');
test('session tabs follow explicit outcomes, retaining old and unknown reports in Unconfirmed', () => {
  const entries = sessionEntries([
    {session_id:'old',status:'active',updated_at:'2020-01-01'},
    {session_id:'unknown',status:'unrecognized'},
    {session_id:'closed',status:'closed'},
    {session_id:'stopped',status:'stopped'},
    {session_id:'failed',status:'crashed'},
  ]);
  assert.deepEqual(selectSessions(entries,{tab:'unconfirmed'}).map(e=>e.id).sort(),['old','unknown']);
  assert.deepEqual(selectSessions(entries,{tab:'finished'}).map(e=>e.id).sort(),['closed','stopped']);
  assert.equal(selectSessions(entries,{tab:'failed'})[0].id,'failed');
  assert.match(sessionReport(entries.find(e=>e.id==='old').record,now),/Last reported active/);
  assert.doesNotMatch(sessionReport(entries.find(e=>e.id==='old').record,now),/running|live/i);
});
test('search combines project, model, host and role without exposing process commands', () => {
  const entries=sessionEntries([{session_id:'a',updated_at:new Date().toISOString(),cwd:'/work/project/',model:'qwen3',agent_role:'planner',host_id:'beast',status:'active',cmdline:'secretcommand'}]);
  assert.equal(entries[0].project,'project');
  assert.equal(selectSessions(entries,{search:'project qwen beast planner',host:'beast'}).length,1);
  assert.equal(selectSessions(entries,{search:'secretcommand'}).length,0);
  assert.equal(selectSessions(entries,{host:'bender'}).length,0);
});
test('mission context uses exact association and preserves namespace across colliding IDs', () => {
  const missions=[{mission_id:'MSN-1',source:'pi',title:'Pi mission',host_id:'beast'},
    {mission_id:'MSN-1',source:'herdr',title:'HerdR mission',host_id:'bender'}];
  assert.equal(sessionEntries([{session_id:'s',mission_id:'MSN-1',host_id:'beast'}],missions)[0].mission,null);
  assert.equal(sessionEntries([{session_id:'s',mission_id:'MSN-1'}],missions)[0].mission,null);
  const explicit=[...missions,{mission_id:'MSN-2',source:'pi',sessions:['s'],title:'Explicit membership'}];
  assert.equal(sessionEntries([{session_id:'s',mission_id:'MSN-2'}],explicit)[0].mission.key,'pi:MSN-2');
  assert.equal(sessionEntries([{session_id:'s',mission_id:'MSN-3'}],explicit)[0].mission,null);
  assert.equal(sessionEntries([{session_id:'s',mission_key:'pi:MSN-1'}],missions)[0].mission.key,'pi:MSN-1');
});
test('rows escape markup and encode exact IDs without putting commands in the catalog', () => {
  const [entry]=sessionEntries([{session_id:'pi:s/a?#"',agent_role:'<img onerror=x>',summary:'<script>bad</script>',cmdline:'TOKEN=secret',host_id:'host"',status:'active'}]);
  const html=compactSessionRow(entry,now);
  assert.match(html,/#\/herd\/session\/pi%3As%2Fa%3F%23%22/);
  assert.match(html,/&lt;img onerror=x&gt;/);
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|TOKEN=secret/);
});
test('malformed inputs are tolerated, but invalid records cannot create broken detail links', () => {
  assert.deepEqual(sessionEntries(null,null),[]);
  const entries=sessionEntries([null,5,[],{}, {session_id:3}, {session_id:'valid',agent_role:{x:1},cwd:['bad'],status:{state:'active'}}],{});
  assert.equal(entries.length,1);
  assert.equal(entries[0].project,'Project not reported');
  assert.match(sessionReport(entries[0].record,now),/Status unknown/);
  assert.doesNotMatch(compactSessionRow(entries[0],now),/\[object Object\]/);
});
test('report timestamps use last observation and never infer completion from age', () => {
  assert.match(sessionReport({status:'active',updated_at:'2026-10-21T11:59:50Z'},now),/10s ago/);
  assert.match(sessionReport({status:'active',updated_at:'invalid'},now),/report time unknown/);
  assert.match(sessionReport({status:'finished',updated_at:'2026-10-21T12:00:10Z'},now),/0ms ago/);
  assert.match(sessionReport({status:'active',stage:'closed'},now),/Last reported closed/);
  assert.match(sessionReport({stage:'failed'},now),/Last reported failed/);
  assert.match(sessionReport({status:'running',stage:'failed'},now),/Last reported running/);
});
