import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';

const root=path.resolve(import.meta.dirname,'../..');
const python=process.env.PYTHON || (fs.existsSync(path.join(root,'.venv/bin/python'))?path.join(root,'.venv/bin/python'):'python3');
async function withApp(fn){
 const server=spawn(python,[path.join(root,'tests/playwright/server.py')],{env:{...process.env,BURST:'1'},stdio:['ignore','pipe','pipe']});
 let browser;
 try{
  const port=await new Promise((resolve,reject)=>{let text='';const timeout=setTimeout(()=>reject(new Error('Fixture startup timed out')),20000);server.stdout.on('data',data=>{text+=data;const hit=text.match(/PORT=(\d+)/);if(hit){clearTimeout(timeout);resolve(hit[1]);}});server.once('exit',code=>{clearTimeout(timeout);reject(new Error(`Fixture exited ${code}`));});});
  const options={headless:true,args:['--no-sandbox']};
  if(process.env.PLAYWRIGHT_CHROMIUM)options.executablePath=process.env.PLAYWRIGHT_CHROMIUM;
  try{browser=await chromium.launch(options);}catch(error){const fallback='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';if(!fs.existsSync(fallback))throw error;browser=await chromium.launch({...options,executablePath:fallback});}
  const page=await browser.newPage();await fn(page,`http://127.0.0.1:${port}`);
 }finally{await browser?.close();server.kill();}
}
const repository=(name,count)=>({github_id:name,repository_id:`github.com/lab/${name}`,name_with_owner:`lab/${name}`,open_issue_count:count,issues_loaded:2,issues_complete:false,url:`https://github.com/lab/${name}`});
const issue=(number,extra={})=>({github_id:String(number),repository_id:'github.com/lab/busy',number,title:`Issue ${number}`,state:'OPEN',updated_at:'2001-01-01T00:00:00Z',url:`https://github.com/lab/busy/issues/${number}`,labels:['auto-implement'],assignees:[],autospec:{managed:true,state:'candidate',eligibility:'unknown',evidence_source:'github_labels',reasons:[]},...extra});
const snapshot=(extra={})=>({available:true,status:'ready',refreshing:false,stale:false,partial:false,account:'operator',connection_generation:1,last_success_at:new Date().toISOString(),pagination:{page:1,per_page:20,total:2,has_next:false},counts:{all:2,autospec:1,preparation:0,in_progress:0,blocked:1},...extra});

test('GitHub projects show ranked repos, old open issues, measured progress and lazy safe inspection',async()=>withApp(async(page,base)=>{
 let details=0,listReads=0;const errors=[],writes=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!['GET','HEAD','OPTIONS'].includes(r.method()))writes.push(r.method());});
 const rows=[issue(1,{title:'Old but open <img src=x>'}),issue(2,{title:'Blocked work',autospec:{managed:true,state:'blocked',eligibility:'blocked',evidence_source:'github_labels',reasons:['autospec:needs-human']}})];
 await page.route('**/api/v1/github?*',route=>route.fulfill({json:snapshot({status:'syncing',refreshing:true,partial:true,progress:{phase:'issues',issue_repositories_complete:2,issue_repositories_total:8,current_repository:'lab/busy',issues_seen:12},repositories:[repository('small',2),repository('busy',42)]})}));
 await page.route('**/api/v1/github/issues?*',route=>{listReads++;const tab=new URL(route.request().url()).searchParams.get('tab');return route.fulfill({json:snapshot({issues:tab==='blocked'?[rows[1]]:tab==='autospec'?[rows[0]]:rows})});});
 await page.route('**/api/v1/github/issue?*',route=>{details++;const number=Number(new URL(route.request().url()).searchParams.get('number'));return route.fulfill({json:snapshot({checked_at:new Date().toISOString(),issue:{...rows.find(r=>r.number===number),body:'<script>unsafe()</script>\nIssue description'}})});});
 await page.goto(`${base}/#/github`);await page.locator('[data-gh-repository]').first().waitFor();
 assert.match(await page.locator('[data-gh-repository]').first().innerText(),/lab\/busy/);
 assert.equal(details,0);assert.match(await page.locator('[data-gh-health]').innerText(),/2 of 8 repositories/);
 await page.getByRole('button',{name:'Old but open <img src=x>',exact:true}).click();await page.locator('[data-gh-body]').waitFor();
 assert.equal(details,1);assert.equal(await page.locator('[data-gh-inspector] script').count(),0);assert.equal(await page.locator('#gh-issue-panel img').count(),0);
 assert.match(await page.locator('[data-gh-inspector]').innerText(),/readiness has not been verified/);
 const search=page.getByRole('searchbox',{name:'Search open issues'});await search.fill('Old');await search.focus();
 const before=listReads;await page.waitForTimeout(5500);assert.ok(listReads>before);assert.equal(details,1);assert.equal(await search.evaluate(e=>e===document.activeElement),true);
 await page.getByRole('tab',{name:/^Blocked/}).click();await page.getByRole('button',{name:'Blocked work',exact:true}).waitFor();
 assert.equal(await page.getByRole('button',{name:'Old but open <img src=x>',exact:true}).count(),0);
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
}));

test('GitHub pagination uses server pages and keeps focused controls through polling',async()=>withApp(async(page,base)=>{
 let reads=0;
 await page.route('**/api/v1/github?*',route=>{const p=Number(new URL(route.request().url()).searchParams.get('page'));return route.fulfill({json:snapshot({repositories:[repository(p===1?'busy':'other',p===1?42:1)],pagination:{page:p,per_page:20,total:21,has_next:p===1}})});});
 await page.route('**/api/v1/github/issues?*',route=>{reads++;const p=Number(new URL(route.request().url()).searchParams.get('page'));return route.fulfill({json:snapshot({issues:[issue(p===1?1:2)],pagination:{page:p,per_page:20,total:21,has_next:p===1}})});});
 await page.goto(`${base}/#/github`);await page.getByRole('button',{name:'Issue 1',exact:true}).waitFor();
 await page.locator('[data-gh-issue-page="next"]').focus();const before=reads;await page.waitForTimeout(5500);assert.ok(reads>before);
 assert.equal(await page.locator('[data-gh-issue-page="next"]').evaluate(e=>e===document.activeElement),true);
 await page.locator('[data-gh-issue-page="next"]').press('Enter');await page.getByRole('button',{name:'Issue 2',exact:true}).waitFor();
 assert.equal(await page.getByRole('button',{name:'Issue 2',exact:true}).evaluate(e=>e===document.activeElement),true);
 await page.locator('[data-gh-repo-page="next"]').click();await page.locator('[data-gh-repository="github.com/lab/other"]').waitFor();
 assert.equal(await page.locator('[data-gh-repo-page-label]').innerText(),'Page 2 · 21 total');
}));

test('GitHub credential generation changes clear cached private detail and discard late responses',async()=>withApp(async(page,base)=>{
 let generation=1,details=0;const row=issue(1);let pending;
 await page.route('**/api/v1/github?*',route=>route.fulfill({json:snapshot({connection_generation:generation,repositories:[repository('busy',42)]})}));
 await page.route('**/api/v1/github/issues?*',route=>route.fulfill({json:snapshot({connection_generation:generation,issues:[row,issue(2)]})}));
 await page.route('**/api/v1/github/issue?*',route=>{
  details++;const number=Number(new URL(route.request().url()).searchParams.get('number'));
  if(number===2){pending=route;return;}
  return route.fulfill({json:snapshot({connection_generation:generation,issue:{...row,body:`Private body generation ${generation}`},checked_at:new Date().toISOString()})});
 });
 await page.goto(`${base}/#/github`);await page.getByRole('button',{name:'Issue 1',exact:true}).click();await page.getByText('Private body generation 1',{exact:true}).waitFor();
 generation=2;
 await page.waitForFunction(()=>document.querySelector('[data-gh-inspector]')?.hidden===true,null,{timeout:12000});
 assert.equal(await page.locator('[data-gh-inspector]').innerHTML(),'');
 await page.getByRole('button',{name:'Issue 1',exact:true}).click();await page.getByText('Private body generation 2',{exact:true}).waitFor();assert.equal(details,2);
 await page.getByRole('button',{name:'Issue 2',exact:true}).click();await page.waitForTimeout(200);
 await page.getByRole('button',{name:'Issue 1',exact:true}).click();
 await pending.fulfill({json:snapshot({connection_generation:1,issue:{...issue(2),body:'Late private body'}})}).catch(()=>{});
 await page.waitForTimeout(200);assert.equal(await page.getByText('Late private body',{exact:true}).count(),0);assert.equal(await page.getByText('Private body generation 2',{exact:true}).count(),1);
}));

test('GitHub filters use server scope, retain independent failures and cancel reads on navigation',async()=>withApp(async(page,base)=>{
 let repositoryFailure=false,repoReads=0,issueReads=0,heldDetail;
 const queries=[];
 await page.route('**/api/v1/github?*',route=>{repoReads++;return repositoryFailure?route.fulfill({status:503,json:{error:'temporary outage'}}):route.fulfill({json:snapshot({repositories:[repository('busy',42),repository('other',3)]})});});
 await page.route('**/api/v1/github/issues?*',route=>{
  issueReads++;const query=new URL(route.request().url()).searchParams;queries.push(Object.fromEntries(query));
  const other=query.get('repository')==='github.com/lab/other',blocked=query.get('tab')==='blocked';
  const row=issue(other?3:1,{repository_id:other?'github.com/lab/other':'github.com/lab/busy',title:other?'Other repository issue':blocked?'Blocked issue':'Original issue'});
  return route.fulfill({json:snapshot({issues:query.get('search')==='missing'?[]:[row]})});
 });
 await page.route('**/api/v1/github/issue?*',route=>{heldDetail=route;});
 await page.goto(`${base}/#/github`);await page.getByRole('button',{name:'Original issue',exact:true}).waitFor();
 await page.getByRole('searchbox',{name:'Search open issues'}).fill('missing');await page.getByText('No open issues match this view.',{exact:true}).waitFor();
 assert.equal(queries.at(-1).search,'missing');
 await page.getByRole('searchbox',{name:'Search open issues'}).fill('');await page.getByRole('button',{name:'Original issue',exact:true}).waitFor();
 await page.locator('[data-gh-repository="github.com/lab/other"]').click();await page.getByRole('button',{name:'Other repository issue',exact:true}).waitFor();
 assert.equal(queries.at(-1).repository,'github.com/lab/other');
 await page.getByRole('tab',{name:/^Blocked/}).click();await page.waitForFunction(()=>document.querySelector('[data-gh-tab="blocked"]')?.getAttribute('aria-selected')==='true');
 repositoryFailure=true;await page.waitForFunction(()=>document.body.innerText.includes('Repositories could not refresh.'),null,{timeout:12000});
 assert.equal(await page.locator('[data-gh-repository="github.com/lab/other"]').count(),1);
 await page.getByRole('button',{name:'Other repository issue',exact:true}).click();await page.waitForTimeout(150);assert.ok(heldDetail);
 await page.evaluate(()=>{location.hash='/hosts';});await page.getByRole('heading',{name:'Hosts',exact:true}).waitFor();
 await heldDetail.fulfill({json:snapshot({issue:{...issue(3),body:'Late body after navigation'}})}).catch(()=>{});
 const before={repoReads,issueReads};await page.waitForTimeout(5500);assert.deepEqual({repoReads,issueReads},before);
 assert.equal(await page.getByText('Late body after navigation',{exact:true}).count(),0);
 assert.equal(await page.getByRole('heading',{name:'Hosts',exact:true}).count(),1);
}));
