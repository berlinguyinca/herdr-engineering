import test from 'node:test';
import assert from 'node:assert/strict';
import { selectServices, groupServices, serviceState, safeServiceUrl, renderServiceDetails } from '../services-view.js';

const services = [
  {service_id:'a',service_name:'Parser API',host_id:'beast',status:'healthy',port:18000,catalog:{project:'metabolomics',label:'Application',purpose:'Parse spectra',source_port:8080},lifecycle:{state:'current'}},
  {service_id:'b',service_name:'python',host_id:'bender',status:'unavailable',catalog:{label:'Development server'},lifecycle:{state:'unavailable'}},
  {service_id:'c',service_name:'Old API',host_id:'beast',status:'unavailable',catalog:{project:'metabolomics'},lifecycle:{state:'archived',archived:true}},
];
test('current catalog defaults hide inactive and archived; filters compose across metadata and ports', () => {
  assert.deepEqual(selectServices(services).map(s=>s.service_id),['a']);
  assert.deepEqual(selectServices(services,{status:'all'}).map(s=>s.service_id),['a','b']);
  assert.deepEqual(selectServices(services,{status:'all',archived:true,host:'beast'}).map(s=>s.service_id),['c','a']);
  assert.deepEqual(selectServices(services,{search:'spectra 8080'}).map(s=>s.service_id),['a']);
  assert.deepEqual(selectServices(services,{search:'bender'}),[]);
});
test('grouping uses reported projects and honest type fallback; hosts stay distinguishable', () => {
  const groups=groupServices(services,'project');
  assert.equal(groups.length,2);
  assert.equal(groups.find(g=>g.label==='metabolomics').services.length,2);
  assert.ok(groups.some(g=>g.label==='No project reported · Development server'));
  assert.equal(groupServices(services,'host').length,2);
});
test('legacy API remains usable without falsely calling unrecognized states healthy', () => {
  assert.equal(serviceState({status:'healthy'}),'current');
  assert.equal(serviceState({status:'unavailable'}),'unavailable');
  assert.equal(serviceState({status:'mystery'}),'unknown');
});
test('only credential-free HTTP(S) service URLs become links', () => {
  for(const value of ['javascript:alert(1)','data:text/html,test','//example.com','http://user:secret@example.com','https://example.com/\npath']) assert.equal(safeServiceUrl(value),null);
  assert.equal(safeServiceUrl('http://dev.lan:18000'),'http://dev.lan:18000/');
});
test('detail renders useful provenance and lifecycle without executable markup or URLs', () => {
  const html=renderServiceDetails({...services[0],service_name:'<script>bad</script>',url:'javascript:alert(1)',owner:'python',catalog:{...services[0].catalog,project_source:'session.cwd'},lifecycle:{state:'archived',archived:true,reason:'Inactive for 7 days',last_seen_at:'2026-10-10T10:00:00Z'}});
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/href="javascript:/);
  assert.match(html,/Source port/);
  assert.match(html,/8080/);
  assert.match(html,/Inactive for 7 days/);
  assert.match(html,/session.cwd/);
});

test('malformed metadata cannot break grouping or sorting',()=>{
  const malformed=[{service_id:'bad',service_name:42,host_id:{secret:'not a host'},status:'healthy',catalog:{project:{unexpected:true},label:['bad']}}];
  assert.equal(groupServices(selectServices(malformed))[0].label,'No project reported · Unclassified service');
  assert.equal(groupServices(malformed,'host')[0].label,'Unknown host');
});
