// WC-005: GitHub board <-> Herdr navigation over a stub API. The real app serves the board with no relationship source
// wired (so no Open in Herdr appears); the verified path mounts the real board module with Herdr-recorded refs.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,sleep} from './helpers/live.mjs';

const shots=path.resolve(import.meta.dirname,'../../docs/specs/herdr-work-command-center-wc001/screenshots');
const options=[{id:'o1',name:'Todo'},{id:'o2',name:'Done'}];
const sync={available:true,status:'ready',connection_generation:1,projects_sync:{enabled:true,status:'ready',error:null,projects:1,deferred:0},revision:3};
const board={project_id:'PVT_1',owner:'acme',number:1,title:'Roadmap',url:'https://github.com/orgs/acme/projects/1',closed:false,updated_at:'2026-10-24T00:00:00Z',item_count:5,status_options:options,complete:true,error:null};
const evil='<img src=x onerror="window.__xss=1">';
const item=(id,pos,o={})=>({item_id:id,project_id:'PVT_1',type:'ISSUE',status_option:'o1',status:'Todo',title:`Card ${id}`,number:pos+1,url:`https://github.com/acme/app/issues/${pos+1}`,repository:'acme/app',state:'OPEN',position:pos,...o});
const ITEMS=[
 item('I-verified',0),                                   // acme/app#1 -> exactly one mission
 item('I-none',1),                                       // acme/app#2 -> nothing recorded
 item('I-ambiguous',2),                                  // acme/app#3 -> two missions
 item('I-other-repo',3,{number:1,repository:'acme/other',url:'https://github.com/acme/other/issues/1'}), // same number, other repo
 item('I-hostile',4,{title:evil,url:'javascript:alert(1)'}),
];
const REFS=[
 {kind:'mission',id:'pi:MSN-1',type:'issue',repository:'acme/app',number:1},
 {kind:'mission',id:'pi:MSN-2',type:'issue',repository:'acme/app',number:3},
 {kind:'mission',id:'pi:MSN-3',type:'issue',repository:'acme/app',number:3},
];
const handler=(p,url)=>{
 if(p==='/api/v1/github/projects')return url.searchParams.get('project')?{...sync,project:board,items:ITEMS}:{...sync,projects:[board]};
 return {services:[],hosts:[],activities:[],events:[],items:[],reviews:[],available:true,components:[],summary:{},status:'ok',repositories:[],pipelines:[],missions:[]};
};
const withProjects=async(page,hash)=>{
 const writes=[],github=[];
 page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/v1/github')){github.push(`${r.method()} ${u.pathname}`);}if(!['GET','HEAD','OPTIONS'].includes(r.method())&&u.pathname.startsWith('/api/v1/'))writes.push(`${r.method()} ${u.pathname}`);});
 return {writes,github};
};
const mountWithRefs=(page,refs)=>page.evaluate(async refs=>{
 const {mountGithubProjects}=await import('/github-projects-view.js');
 const get=async({project,signal}={})=>({body:await (await fetch(`/api/v1/github/projects${project?`?project=${project}`:''}`,{signal})).json()});
 const host=document.createElement('div');host.id='wc005-host';document.body.prepend(host);
 window.__board=mountGithubProjects(host,{getGithubProjects:get},{project:'PVT_1',herdrRefs:refs});
},refs);

test('real app: with no relationship source wired, no card offers Open in Herdr and GitHub links are untouched',async()=>withPage(async(page,base)=>{
 const net=await withProjects(page);
 await boot(page,base,'/github/projects?project=PVT_1',handler);
 await page.locator('[data-ghp-item="I-verified"]').waitFor({timeout:8000});
 assert.equal(await page.locator('[data-herdr-link]').count(),0);
 assert.equal(await page.locator('[data-herdr-partial]').count(),0);
 assert.equal(await page.locator('[data-ghp-item="I-verified"] a').getAttribute('href'),'https://github.com/acme/app/issues/1');
 assert.deepEqual(net.writes,[]);
}));

test('verified card links to its mission; none, ambiguous and other-repo cards show no link; hostile item stays inert; GitHub requests are read-only',async()=>withPage(async(page,base)=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const net=await withProjects(page);
 await boot(page,base,'/overview',handler);
 await page.locator('#view').waitFor();
 await mountWithRefs(page,REFS);
 await page.locator('#wc005-host [data-ghp-item="I-hostile"]').waitFor({timeout:8000});
 const card=id=>page.locator(`#wc005-host [data-ghp-item="${id}"]`);
 assert.equal(await card('I-verified').locator('[data-herdr-link]').count(),1);
 assert.equal(await card('I-verified').locator('[data-herdr-link]').getAttribute('href'),'#/missions/pi%3AMSN-1');
 assert.equal(await card('I-verified').locator('a:not([data-herdr-link])').getAttribute('href'),'https://github.com/acme/app/issues/1','external link preserved');
 for(const id of ['I-none','I-ambiguous','I-other-repo','I-hostile'])assert.equal(await card(id).locator('[data-herdr-link]').count(),0,id);
 assert.equal(await page.locator('#wc005-host a[href^="javascript:"]').count(),0);
 assert.equal(await page.evaluate(()=>window.__xss),undefined);
 assert.equal(await page.locator('#wc005-host img').count(),0);
 await page.setViewportSize({width:1200,height:900});
 await page.locator('#wc005-host').screenshot({path:path.join(shots,'wc005-board-links-desktop.png')});
 await page.setViewportSize({width:390,height:844});await sleep(100);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
 await page.locator('#wc005-host').screenshot({path:path.join(shots,'wc005-board-links-mobile.png')});
 assert.ok(net.github.length>0&&net.github.every(r=>r.startsWith('GET ')),`only GET to GitHub endpoints: ${net.github}`);
 assert.deepEqual(net.writes,[]);
 await card('I-verified').locator('[data-herdr-link]').click();
 await until(async()=>(await page.evaluate(()=>location.hash)).startsWith('#/missions/pi%3AMSN-1'),'mission route');
 assert.deepEqual(errors,[]);
}));

test('a failed relationship source shows the quiet partial note and no link',async()=>withPage(async(page,base)=>{
 await withProjects(page);
 await boot(page,base,'/overview',handler);
 await page.evaluate(async()=>{
  const {mountGithubProjects}=await import('/github-projects-view.js');
  const get=async({project,signal}={})=>({body:await (await fetch(`/api/v1/github/projects${project?`?project=${project}`:''}`,{signal})).json()});
  const host=document.createElement('div');host.id='wc005-host';document.body.prepend(host);
  mountGithubProjects(host,{getGithubProjects:get},{project:'PVT_1',herdrRefs:[],herdrRefsFailed:true});
 });
 await page.locator('#wc005-host [data-ghp-item="I-none"]').waitFor({timeout:8000});
 assert.equal(await page.locator('#wc005-host [data-herdr-link]').count(),0);
 assert.match(await page.locator('#wc005-host [data-ghp-item="I-none"]').innerText(),/Herdr data partial/);
}));
