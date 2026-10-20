import test from 'node:test';
import assert from 'node:assert/strict';
import { selectServices, groupServices, serviceState, safeServiceUrl, renderServiceDetails } from '../services-view.js';

const recentReport=new Date().toISOString();
const services = [
  {updated_at:recentReport,service_id:'a',service_name:'Parser API',host_id:'beast',status:'healthy',port:18000,catalog:{scope:'herdr',project:'metabolomics',project_id:'metabolomics',label:'Application',purpose:'Parse spectra',source_port:8080},lifecycle:{state:'current'}},
  {service_id:'b',service_name:'python',host_id:'bender',status:'unavailable',catalog:{scope:'herdr',label:'Development server'},lifecycle:{state:'unavailable'}},
  {service_id:'c',service_name:'Old API',host_id:'beast',status:'unavailable',catalog:{scope:'herdr',project:'metabolomics',project_id:'metabolomics'},lifecycle:{state:'archived',archived:true}},
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
  assert.ok(groups.some(g=>g.label==='No verified project · Development server'));
  assert.equal(groupServices(services,'host').length,2);
});
test('legacy API remains usable without falsely calling unrecognized states healthy', () => {
  assert.equal(serviceState({status:'healthy'}),'unknown');
  assert.equal(serviceState({status:'unavailable'}),'unavailable');
  assert.equal(serviceState({status:'mystery'}),'unknown');
});
test('only credential-free HTTP(S) service URLs become links', () => {
  for(const value of ['javascript:alert(1)','data:text/html,test','//example.com','http://user:secret@example.com','https://example.com/\npath']) assert.equal(safeServiceUrl(value),null);
  assert.equal(safeServiceUrl('http://dev.lan:18000'),'http://dev.lan:18000/');
});
test('detail renders useful provenance and lifecycle without executable markup or URLs', () => {
  const html=renderServiceDetails({...services[0],service_name:'<script>bad</script>',url:'javascript:alert(1)',owner:'python',catalog:{scope:'herdr',...services[0].catalog,project_source:'session.cwd'},lifecycle:{state:'archived',archived:true,reason:'Inactive for 7 days',last_seen_at:'2026-10-10T10:00:00Z'}});
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/href="javascript:/);
  assert.match(html,/Source port/);
  assert.match(html,/8080/);
  assert.match(html,/Inactive for 7 days/);
  assert.match(html,/session.cwd/);
});

test('malformed metadata cannot break grouping or sorting',()=>{
  const malformed=[{updated_at:recentReport,service_id:'bad',service_name:42,host_id:{secret:'not a host'},status:'healthy',catalog:{scope:'herdr',project:{unexpected:true},label:['bad']}}];
  assert.equal(groupServices(selectServices(malformed))[0].label,'No verified project · Unclassified service');
  assert.equal(groupServices(malformed,'host')[0].label,'Unknown host');
});

test('service project identity joins separate repositories without joining equal folder names',()=>{
  const rows=[
    {service_id:'frontend',catalog:{project:'app',project_id:'spectra',repository_id:'github.com/lab/frontend'}},
    {service_id:'backend',project_id:'spectra',repository_id:'github.com/lab/backend',catalog:{project:'app'}},
    {service_id:'independent',catalog:{project:'app',repository_id:'github.com/other/app'}},
    {service_id:'unidentified',catalog:{project:'app',label:'Application'}},
  ];
  const groups=groupServices(rows);
  assert.equal(groups.length,3);
  assert.deepEqual(groups.find(g=>g.label==='spectra').services.map(s=>s.service_id),['frontend','backend']);
  assert.equal(groups.find(g=>g.label==='github.com/other/app').services.length,1);
  assert.ok(groups.some(g=>g.label==='No verified project · Application'));
  assert.equal(groupServices([{catalog:{project:'same',repository_id:'github.com/one/app'}},{catalog:{project:'same',repository_id:'github.com/two/app'}}]).length,2);
});
test('service search includes canonical Git identity and detail links exact owning session',()=>{
  const service={service_id:'web',status:'healthy',updated_at:recentReport,session_id:'pi:owner/a?#',
    project_id:'spectra',repository_id:'github.com/lab/backend',commit_sha:'a'.repeat(40),catalog:{scope:'herdr',project:'folder'}};
  assert.equal(selectServices([service],{search:'github.com/lab/backend spectra '+ 'a'.repeat(12)}).length,1);
  const html=renderServiceDetails(service);
  assert.match(html,/#\/herd\/session\/pi%3Aowner%2Fa%3F%23/);
  assert.match(html,/github.com\/lab\/backend/);
  assert.match(html,/Workspace folder/);
  assert.match(html,/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);
});
