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
 await page.setViewportSize({width:3840,height:2160});
 const wide=await page.evaluate(()=>{const repo=document.querySelector('.gh-repositories').getBoundingClientRect(),list=document.querySelector('.gh-issue-list').getBoundingClientRect(),inspector=document.querySelector('[data-gh-inspector]').getBoundingClientRect();return {repo:repo.width,list:list.width,inspector:inspector.width,overflow:document.documentElement.scrollWidth>innerWidth};});
 assert.ok(wide.repo>=280&&wide.repo<=360,`repository rail should stay scannable, got ${wide.repo}`);
 assert.ok(wide.list>900&&wide.inspector>1200,'populated issue list and inspector should share the wide canvas');assert.equal(wide.overflow,false);
 for(const viewport of [{width:2560,height:1440},{width:1920,height:1080},{width:768,height:1024}]){await page.setViewportSize(viewport);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`GitHub should not overflow at ${viewport.width}px`);}
 await page.getByRole('button',{name:'Back to issues',exact:true}).click();
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

// Removing the pane bounds or selection restoration must break these interaction checks.
test('GitHub wide panes scroll independently and preserve focus, selection and scroll through refresh',async()=>withApp(async(page,base)=>{
 let reads=0;
 const rows=Array.from({length:20},(_,i)=>issue(i+1,{title:`Workspace issue ${i+1}`,labels:['auto-implement','ctx:large','reasoning:high','lang:javascript']}));
 await page.route('**/api/v1/github?*',route=>route.fulfill({json:snapshot({repositories:Array.from({length:20},(_,i)=>repository(`repository-${i}`,100-i))})}));
 await page.route('**/api/v1/github/issues?*',route=>{reads++;return route.fulfill({json:snapshot({issues:rows})});});
 await page.route('**/api/v1/github/issue?*',route=>route.fulfill({json:snapshot({checked_at:new Date().toISOString(),issue:{...rows[0],body:'Long description line\n'.repeat(500)}})}));
 await page.setViewportSize({width:1920,height:1080});await page.goto(`${base}/#/github`);
 await page.getByRole('button',{name:'Workspace issue 1',exact:true}).click();await page.locator('[data-gh-body]').waitFor();
 for(const viewport of [{width:3840,height:2160},{width:2560,height:1440},{width:1920,height:1080}]){
  await page.setViewportSize(viewport);
  const bounds=await page.evaluate(()=>['.gh-repositories','.gh-issue-list','.gh-inspector'].map(selector=>{const e=document.querySelector(selector),r=e.getBoundingClientRect();return {selector,top:r.top,bottom:r.bottom,height:r.height};}));
  for(const pane of bounds)assert.ok(pane.top>=0&&pane.bottom<=viewport.height,`${pane.selector} must fit ${viewport.width}px viewport: ${JSON.stringify(pane)}`);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  for(const selector of ['.gh-repositories','.gh-main'])assert.equal(await page.locator(selector).evaluate(e=>getComputedStyle(e).position),'sticky');
 }
 await page.evaluate(()=>{document.querySelector('.gh-repo-list').scrollTop=120;document.querySelector('.gh-issue-list').scrollTop=140;document.querySelector('.gh-inspector').scrollTop=160;});
 const positions=()=>page.evaluate(()=>({repo:document.querySelector('.gh-repo-list').scrollTop,issues:document.querySelector('.gh-issue-list').scrollTop,detail:document.querySelector('.gh-inspector').scrollTop,window:scrollY}));
 const before=await positions();assert.ok(before.repo>0&&before.issues>0&&before.detail>0,`all three panes must scroll: ${JSON.stringify(before)}`);
 await page.locator('.gh-issue-list').hover();await page.mouse.wheel(0,240);await page.waitForTimeout(150);
 const after=await positions();assert.ok(after.issues>before.issues);assert.equal(after.repo,before.repo);assert.equal(after.detail,before.detail);assert.equal(after.window,before.window);
 await page.locator('[data-gh-body]').focus();const retained=await positions(),beforeReads=reads;
 await page.waitForTimeout(5500);assert.ok(reads>beforeReads);assert.deepEqual(await positions(),retained);
 assert.equal(await page.locator('[data-gh-body]').evaluate(e=>e===document.activeElement),true);
 assert.equal(await page.locator('[data-gh-issue="github.com/lab/busy#1"]').getAttribute('aria-pressed'),'true');
 await page.getByRole('button',{name:'Refresh issue',exact:true}).evaluate(e=>e.focus({preventScroll:true}));
 await page.getByRole('button',{name:'Refresh issue',exact:true}).press('Enter');await page.waitForTimeout(150);
 // Refreshing detail retains the inspector's position even though its contents are replaced.
 const refreshPosition=await positions();assert.deepEqual(refreshPosition,retained);
 assert.equal(await page.getByRole('button',{name:'Refresh issue',exact:true}).evaluate(e=>e===document.activeElement),true);
}));

test('GitHub narrow detail focuses Back and restores document list scroll',async()=>withApp(async(page,base)=>{
 const rows=Array.from({length:20},(_,i)=>issue(i+1));
 await page.route('**/api/v1/github?*',route=>route.fulfill({json:snapshot({repositories:[repository('busy',20)]})}));
 await page.route('**/api/v1/github/issues?*',route=>route.fulfill({json:snapshot({issues:rows})}));
 await page.route('**/api/v1/github/issue?*',route=>{const number=Number(new URL(route.request().url()).searchParams.get('number'));return route.fulfill({json:snapshot({issue:{...rows[number-1],body:'Mobile description\n'.repeat(150)}})});});
 for(const viewport of [{width:768,height:1024},{width:390,height:844}]){
  await page.setViewportSize(viewport);await page.goto(`${base}/#/github`);await page.getByRole('button',{name:'Issue 12',exact:true}).waitFor();
  const item=page.getByRole('button',{name:'Issue 12',exact:true});await item.scrollIntoViewIfNeeded();const listScroll=await page.evaluate(()=>scrollY);assert.ok(listScroll>0);
  await item.click();await page.locator('[data-gh-body]').waitFor();
  const back=page.getByRole('button',{name:'Back to issues',exact:true});assert.equal(await back.count(),1);
  assert.equal(await back.evaluate(e=>e===document.activeElement),true);
  assert.equal(await page.locator('.gh-issue-list').isVisible(),false);
  await page.evaluate(()=>scrollBy(0,450));
  const backBounds=await back.boundingBox();assert.ok(backBounds.y>=0&&backBounds.y<60,'Back remains available while reading detail');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await back.click();assert.equal(await item.evaluate(e=>e===document.activeElement),true);
  assert.ok(Math.abs(await page.evaluate(()=>scrollY)-listScroll)<2,'closing detail restores the list document position');
  assert.equal(await page.locator('.gh-issue-list').evaluate(e=>getComputedStyle(e).overflowY),'hidden');
 }
}));

test('GitHub desktop selection enters narrow detail with focus and returns to a visible selected row',async()=>withApp(async(page,base)=>{
 const rows=Array.from({length:20},(_,i)=>issue(i+1));
 await page.route('**/api/v1/github?*',route=>route.fulfill({json:snapshot({repositories:[repository('busy',20)]})}));
 await page.route('**/api/v1/github/issues?*',route=>route.fulfill({json:snapshot({issues:rows})}));
 await page.route('**/api/v1/github/issue?*',route=>{const number=Number(new URL(route.request().url()).searchParams.get('number'));return route.fulfill({json:snapshot({issue:{...rows[number-1],body:'Resized description\n'.repeat(180)}})});});
 await page.setViewportSize({width:1920,height:1080});await page.goto(`${base}/#/github`);
 for(const number of [1,12]){
  await page.setViewportSize({width:1920,height:1080});
  const item=page.getByRole('button',{name:`Issue ${number}`,exact:true});await item.click();await page.locator('[data-gh-body]').waitFor();
  await page.setViewportSize({width:390,height:844});
  await page.waitForFunction(()=>document.activeElement?.hasAttribute('data-gh-close'),null,{timeout:1500});
  assert.equal(await page.locator('.gh-issue-list').isVisible(),false);
  await page.evaluate(()=>scrollBy(0,1000));
  await page.getByRole('button',{name:'Back to issues',exact:true}).click();
  assert.equal(await item.evaluate(e=>e===document.activeElement),true);
  const row=await item.boundingBox();assert.ok(row.y>=0&&row.y+row.height<=844,`selected issue ${number} must return within the viewport: ${JSON.stringify(row)}`);
 }
}));

// G8: the page polls with If-None-Match and since=<revision>. A 304 writes nothing; a delta with one
// changed issue patches that one row; a resync replaces the list; the scope line names the owners.
test('GitHub polls conditionally and applies since deltas row by row',async()=>withApp(async(page,base)=>{
 let revision=10,mode='delta',rows=[issue(3,{updated_at:'2001-01-03T00:00:00Z'}),issue(2,{updated_at:'2001-01-02T00:00:00Z'}),issue(1,{updated_at:'2001-01-01T00:00:00Z'})],changed=[];
 const seen={repos:[],issues:[]};
 const scope={owners:['lab','partner'],excluded_repositories:4,restricted:true};
 const reply=(route,kind,full,delta)=>{
  const url=new URL(route.request().url()),since=url.searchParams.get('since'),inm=route.request().headers()['if-none-match']||null;
  const tag=`W/"${revision}-${kind}-${mode}"`;seen[kind].push({since,inm});
  if(inm===tag)return route.fulfill({status:304,headers:{etag:tag,'cache-control':'no-store'}});
  const body=since===null?full():mode==='resync'?{...full(),resync:true,pagination:undefined}:delta();
  return route.fulfill({status:200,headers:{etag:tag,'cache-control':'no-store'},json:{...snapshot({scope,last_success_at:'2001-01-01T00:00:00Z'}),...body,revision}});
 };
 await page.route('**/api/v1/github?*',route=>reply(route,'repos',()=>({repositories:[repository('busy',3)]}),()=>({resync:false,upserted:[],removed:[]})));
 const counts=()=>({all:rows.length,autospec:rows.length,preparation:0,in_progress:0,blocked:0});
 await page.route('**/api/v1/github/issues?*',route=>reply(route,'issues',()=>({issues:rows,counts:counts()}),()=>({resync:false,upserted:changed,removed:[],counts:counts()})));
 await page.clock.install();
 await page.goto(`${base}/#/github`);await page.getByRole('button',{name:'Issue 1',exact:true}).waitFor();
 const health=await page.locator('[data-gh-health]').innerText();
 assert.match(health,/2 owners in scope: lab, partner/);assert.match(health,/4 repositories excluded/);
 // Mark the row nodes and record every DOM write under the view, except the self-ticking ages and chip.
 await page.evaluate(()=>{
  for(const row of document.querySelectorAll('[data-gh-issue-row]'))row.__mark=row.dataset.ghIssueRow;
  window.__muts=[];new MutationObserver(list=>{for(const m of list){const el=m.target.nodeType===1?m.target:m.target.parentElement;if(el?.closest('[data-gh-time],herdr-freshness'))continue;window.__muts.push({row:el?.closest('[data-gh-issue-row]')?.dataset.ghIssueRow||null,type:m.type,attr:m.attributeName,tag:el?.tagName});}}).observe(document.querySelector('#view'),{subtree:true,childList:true,attributes:true,characterData:true});
 });
 const poll=async kind=>{const n=seen[kind].length;await page.clock.runFor(5000);await page.waitForFunction(()=>true);for(let i=0;i<200&&seen[kind].length===n;i++)await page.waitForTimeout(25);await page.waitForTimeout(150);};
 const muts=()=>page.evaluate(()=>{const m=window.__muts;window.__muts=[];return m;});
 // 1. Unchanged: the first poll asks since the held revision (a new URL, so no tag yet) and gets an
 // empty delta; the next sends that answer's ETag and gets 304. Neither writes to the DOM.
 await muts();await poll('issues');
 assert.deepEqual(seen.issues.at(-1),{since:'10',inm:null});
 await poll('issues');
 assert.deepEqual(seen.issues.at(-1),{since:'10',inm:'W/"10-issues-delta"'});
 assert.deepEqual(await muts(),[],'an empty delta and a 304 perform no DOM writes');
 // 2. One issue edited: the delta carries it alone; only its row changes, every row keeps its node.
 revision=11;rows=[issue(2,{title:'Issue 2 edited',updated_at:'2001-01-05T00:00:00Z'}),rows[0],rows[2]];changed=[rows[0]];
 await poll('issues');
 assert.equal(seen.issues.at(-1).since,'10');
 await page.getByRole('button',{name:'Issue 2 edited',exact:true}).waitFor();
 const rowsAfter=await page.evaluate(()=>[...document.querySelectorAll('[data-gh-issue-row]')].map(r=>({key:r.dataset.ghIssueRow,mark:r.__mark||null})));
 assert.deepEqual(rowsAfter.map(r=>r.key),['github.com/lab/busy#2','github.com/lab/busy#3','github.com/lab/busy#1'],'server order: the edited issue moves up');
 for(const r of rowsAfter)assert.equal(r.mark,r.key,`${r.key} kept its node`);
 const written=new Set((await muts()).map(m=>m.row).filter(Boolean));
 assert.deepEqual([...written],['github.com/lab/busy#2'],'only the edited row is written');
 // 3. The next poll asks from the new revision.
 changed=[];await poll('issues');assert.equal(seen.issues.at(-1).since,'11');
 // 4. Resync: the server cannot answer since=11 any more; the list is replaced.
 mode='resync';revision=40;rows=[issue(8,{updated_at:'2001-02-02T00:00:00Z'}),issue(7,{updated_at:'2001-02-01T00:00:00Z'})];
 await poll('issues');
 await page.getByRole('button',{name:'Issue 8',exact:true}).waitFor();
 assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('[data-gh-issue-row]:not([data-leaving])')].map(r=>r.dataset.ghIssueRow)),['github.com/lab/busy#8','github.com/lab/busy#7']);
 mode='delta';await poll('issues');assert.equal(seen.issues.at(-1).since,'40');
}));
