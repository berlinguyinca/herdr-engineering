import test from 'node:test';
import assert from 'node:assert/strict';
import {topologyEntries,selectTopology,groupTopology,topologyRow} from '../topology-view.js';
const now=Date.parse('2026-10-20T12:00:00Z');
const fresh='2026-10-20T11:59:00Z',old='2026-10-19T12:00:00Z';
const host={host_id:'beast',host_name:'Beast',status:'healthy',last_seen:fresh};
const session=(id,status='active',updated_at=fresh)=>({session_id:id,host_id:'beast',agent_role:'pi',status,updated_at});
const service=(id,status='healthy',updated_at=fresh)=>({service_id:id,host_id:'beast',service_name:id,status,updated_at,catalog:{scope:'herdr'}});

test('default topology contains only current evidence and keeps all history reachable',()=>{
 const entries=topologyEntries({sessions:[session('active'),session('old','active',old),session('closed','stopped'),session('unknown','unknown')],services:[service('web'),service('dead','unavailable'),service('stale','healthy',old)]},{},now);
 assert.deepEqual(selectTopology(entries).map(e=>e.id).sort(),['service:web','session:active']);
 assert.equal(selectTopology(entries,{status:'unconfirmed'}).length,3);
 assert.equal(selectTopology(entries,{status:'history'}).length,2);
 assert.equal(selectTopology(entries,{status:'all'}).length,7);
});
test('observations with unknown or absent host identities stay inspectable without guessed aliases',()=>{
 const entries=topologyEntries({services:[service('web'),{...service('orphan'),host_id:'beast.local'},{...service('absent'),host_id:null}]},{},now);
 const groups=groupTopology(entries,[host]);
 assert.equal(groups.reduce((n,g)=>n+g.entries.length,0),3);
 assert.equal(groups.find(g=>g.host?.host_id==='beast').entries.length,1);
 assert.ok(groups.some(g=>g.unconfirmedHost && g.entries.some(e=>e.id==='service:orphan')));
 assert.ok(groups.some(g=>g.unconfirmedHost && g.entries.some(e=>e.id==='service:absent')));
});
test('project edges require explicit identity and never join matching working-directory basenames',()=>{
 const entries=topologyEntries({services:[{...service('api'),catalog:{scope:'herdr',project:'spectra',project_source:'process.cwd'}},{...service('frontend'),project_id:'shared-spectra'}],sessions:[{...session('worker'),repository_id:'github.com/lab/spectra'},{...session('other'),project_id:'shared-spectra'}]},{},now);
 const groups=groupTopology(entries,[host],{group:'project'});
 assert.equal(groups.find(g=>g.key==='project:shared-spectra').entries.length,2);
 assert.equal(groups.find(g=>g.key==='repository:github.com/lab/spectra').entries.length,1);
 assert.equal(groups.find(g=>g.key==='unassigned').entries[0].id,'service:api');
});
test('CI uses real lifecycle labels, reliable snapshot freshness and exact related host evidence only',()=>{
 const data={ci:{available:true,status:'connected',last_success_at:fresh,pipelines:{'lab/api':[{run_number:1,state:'running',project_id:'p',provider_repo_id:'7'},{run_number:2,state:'failure'},{run_number:3,state:'success'}]}}};
 const entries=topologyEntries(data,{},now);
 assert.equal(selectTopology(entries).length,1);
 assert.equal(selectTopology(entries,{status:'history'}).length,2);
 assert.match(topologyRow(entries.find(e=>e.outcome==='failed'),now),/failure/);
 assert.doesNotMatch(topologyRow(entries.find(e=>e.outcome==='failed'),now),/>Unknown</);
 data.ci.stale=true;
 assert.equal(selectTopology(topologyEntries(data,{},now)).length,0);
 assert.equal(selectTopology(topologyEntries(data,{},now),{status:'unconfirmed'}).length,1);
});
test('topology rows escape labels, encode identities and reject unsafe endpoint URLs',()=>{
 const entry=topologyEntries({services:[{...service('one/#?'),service_name:'<img src=x>',url:'javascript:alert(1)',catalog:{scope:'herdr',project:'home',project_source:'process.cwd'}}]},{},now)[0];
 const html=topologyRow(entry,now);
 assert.doesNotMatch(html,/<img|javascript:|href=".*one\/#\?/);
 assert.match(html,/#\/services\/one%2F%23%3F/);
 assert.match(html,/Workspace: home/);assert.match(html,/process.cwd/);
});
test('fresh mission attention stays visible and stale mission telemetry is unconfirmed',()=>{
 const projection=(key,age)=>({mission_key:key,host_id:'beast',activity_snapshot_at:new Date(now-age*1000).toISOString(),mission:{id:key,title:key,status:'EXECUTING'},freshness:{state:'live',age_seconds:age},activity:{health:'stalled'}});
 const entries=topologyEntries({projections:[projection('pi:fresh',1),projection('pi:stale',999999)]},{projections:now},now);
 assert.equal(selectTopology(entries).length,1);
 assert.equal(selectTopology(entries,{status:'unconfirmed'}).length,1);
 assert.match(topologyRow(entries[0],now),/#\/missions\/pi%3A/);
});

test('CI repository identity shares its default group and explicit mapping bridges separate repositories',()=>{
 const ci={available:true,last_success_at:fresh,repos:[{repository_id:'github.com/lab/api',project_id:'spectra'},{repository_id:'github.com/lab/web',project_id:'spectra'}],pipelines:{'lab/api':[{run_number:1,state:'running',repository_id:'github.com/lab/api',project_id:'spectra'}]}};
 const entries=topologyEntries({ci,sessions:[{...session('one'),repository_id:'github.com/lab/web'}]},{},now);
 assert.equal(groupTopology(entries,[],{group:'project'})[0].entries.length,2);
 ci.repos=[];ci.pipelines['lab/api'][0].project_id='github.com/lab/api';
 const defaults=topologyEntries({ci,sessions:[{...session('two'),repository_id:'github.com/lab/api'}]},{},now);
 assert.equal(groupTopology(defaults,[],{group:'project'})[0].entries.length,2);
});

test('main topology excludes system and unassigned listeners even when healthy',()=>{
 const entries=topologyEntries({services:[service('owned'),{...service('system'),catalog:{scope:'system'}},{...service('unassigned'),catalog:{scope:'unassigned'}},{...service('missing'),catalog:{}}]},{},now);
 assert.deepEqual(entries.map(e=>e.id),['service:owned']);
});

test('local-only observed listeners never advertise a remote endpoint or exposed port',()=>{
 const row=topologyEntries({services:[{...service('local'),port:8000,url:'http://host.test:8000',catalog:{scope:'herdr',endpoint_scope:'local_only'}}]},{},now)[0];
 const html=topologyRow(row,now);
 assert.match(html,/Observed :8000/);assert.match(html,/Local only/);
 assert.doesNotMatch(html,/Exposed|Open endpoint|href="http/);
});

test('explicit local-only scope wins over conflicting catalog or root scope',()=>{
 for(const [rootScope,catalogScope] of [['local_only','forwarded'],['forwarded','local_only']]){
  const row=topologyEntries({services:[{...service('local'),endpoint_scope:rootScope,port:8000,url:'http://host.test:8000',catalog:{scope:'herdr',endpoint_scope:catalogScope}}]},{},now)[0];
  const html=topologyRow(row,now);
  assert.match(html,/Local only/);assert.doesNotMatch(html,/Exposed|Open endpoint|href="http/);
 }
});
