// WC-005: GitHub board <-> Herdr navigation over a stub API. The real app serves the board with no relationship source
// wired (so no Open in Herdr appears); the verified path mounts the real board module with Herdr-recorded refs.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {withPage} from './helpers/fixture.mjs';
import {withFlag} from './helpers/work-center.mjs';
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
 item('P-verified',5,{type:'PULL_REQUEST',number:7,url:'https://github.com/acme/app/pull/7'}),        // acme/app PR 7 -> exactly one mission
 item('P-none',6,{type:'PULL_REQUEST',number:8,url:'https://github.com/acme/app/pull/8'}),            // no mission records it
 item('P-ambiguous',7,{type:'PULL_REQUEST',number:9,url:'https://github.com/acme/app/pull/9'}),      // two missions record it
 item('P-other-repo',8,{type:'PULL_REQUEST',number:7,repository:'acme/other',url:'https://github.com/acme/other/pull/7'}), // same number, other repo
];
const OUTCOMES={outcomes:{
 'pi:MSN-7':{result:'succeeded',prs:[{repository:'acme/app',number:7}]},
 'MSN-9':{result:'succeeded',prs:[{repository:'acme/app',number:9}]},
 'pi:MSN-9':{result:'succeeded',prs:[{repository:'acme/app',number:9}]},
 'pi:MSN-10':{result:'failed',prs:[]},
 // G2: free-text/unrecorded issue hints must never link an issue card (acme/app#1 is I-verified, #2 is I-none)
 'pi:MSN-11':{result:'succeeded',prs:[],title:'Closes #1',last_event:'Fixes acme/app#2',objective:'resolves acme/app#1',issue_number:1,issue:{repository:'acme/app',number:2}},
}};
const REFS=[
 {kind:'mission',id:'pi:MSN-1',type:'issue',repository:'acme/app',number:1},
 {kind:'mission',id:'pi:MSN-2',type:'issue',repository:'acme/app',number:3},
 {kind:'mission',id:'pi:MSN-3',type:'issue',repository:'acme/app',number:3},
];
const rawHandler=(p,url)=>{
 if(p==='/api/v1/mission-outcomes')return OUTCOMES;
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

const handler=withFlag(rawHandler);
test('real app: a PR card recorded by exactly one mission offers Open in Herdr; none, ambiguous, other-repo and issue cards do not; all requests are reads',async()=>withPage(async(page,base)=>{
 const net=await withProjects(page);
 const reads=await boot(page,base,'/github/projects?project=PVT_1',handler);
 await page.locator('[data-ghp-item="P-verified"]').waitFor({timeout:8000});
 assert.equal(await page.locator('[data-ghp-item="P-verified"] [data-herdr-link]').count(),1);
 assert.equal(await page.locator('[data-ghp-item="P-verified"] [data-herdr-link]').getAttribute('href'),'#/missions/pi%3AMSN-7');
 assert.equal(await page.locator('[data-ghp-item="P-verified"] a:not([data-herdr-link])').getAttribute('href'),'https://github.com/acme/app/pull/7');
 for(const id of ['P-none','P-ambiguous','P-other-repo','I-verified','I-none','I-hostile'])assert.equal(await page.locator(`[data-ghp-item="${id}"] [data-herdr-link]`).count(),0,id);
 assert.equal(await page.locator('[data-herdr-partial]').count(),0);
 assert.equal(reads['/api/v1/mission-outcomes'],1,'one read on mount, no polling of the refs');
 assert.deepEqual(net.writes,[]);
 await page.locator('[data-ghp-item="P-verified"] [data-herdr-link]').click();
 await until(async()=>(await page.evaluate(()=>location.hash)).startsWith('#/missions/pi%3AMSN-7'),'mission route');
}));

test('real app: a failed mission-outcomes read shows the partial note and no link',async()=>withPage(async(page,base)=>{
 await withProjects(page);
 await boot(page,base,'/github/projects?project=PVT_1',(p,url)=>p==='/api/v1/mission-outcomes'?{__status:500,json:{error:'boom'}}:handler(p,url));
 await page.locator('[data-ghp-item="P-verified"]').waitFor({timeout:8000});
 assert.equal(await page.locator('[data-herdr-link]').count(),0);
 assert.match(await page.locator('[data-ghp-item="P-verified"]').innerText(),/Herdr data partial/);
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
