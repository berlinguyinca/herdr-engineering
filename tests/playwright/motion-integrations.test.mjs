// Task 12: motion on Services, Topology, CI/Reviews, GitHub and Collab. These pages still rebuild
// their lists with innerHTML (Task 19 moves them onto the patcher), so motion pairs old and new nodes
// by key: a changed row flashes, counts count up, running CI rows pulse, charts draw in once, and
// live chat rows slide in. Calm mode (or OS reduced motion) does none of it.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {chromium} from 'playwright';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,sleep,T0} from './helpers/live.mjs';

const calm=(page,mode)=>page.addInitScript(m=>{try{localStorage.setItem('herdr.calm',m);}catch{}},mode);
const ago=ms=>new Date(Date.parse(T0)-ms).toISOString();

// Records which keyed nodes flashed, which entered, and every text written to the tracked counts.
// The markers clear themselves after the animation, so point-in-time reads would race under load.
const watch=(page,{key,count})=>page.evaluate(({key,count})=>{
 window.__flashed=[];window.__entered=[];window.__counts=[];
 // A node outside the keyed rows (e.g. a count) is not recorded; with no keys, the class name is.
 const id=el=>key.length?key.map(a=>el.closest(`[${a}]`)?.getAttribute(a)).find(v=>v!=null)??null:el.className;
 const root=document.getElementById('view')||document.body;
 window.__w?.disconnect();
 window.__w=new MutationObserver(rs=>{for(const r of rs){
  const t=r.target.nodeType===1?r.target:r.target.parentElement;
  if(r.type==='attributes'&&r.attributeName==='data-flash'&&t.hasAttribute('data-flash')&&id(t)!=null)window.__flashed.push(id(t));
  if(r.type==='attributes'&&r.attributeName==='data-entering'&&t.hasAttribute('data-entering')&&id(t)!=null)window.__entered.push(id(t));
  if(count&&t?.closest?.(count))window.__counts.push(t.closest(count).textContent);
 }});
 window.__w.observe(root,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['data-flash','data-entering']});
},{key,count});
const watched=page=>page.evaluate(()=>({flashed:[...new Set(window.__flashed)],entered:[...new Set(window.__entered)],counts:window.__counts}));

const service=(id,status='healthy')=>({service_id:id,service_name:`Service ${id}`,status,host_id:'beast',port:8000,updated_at:T0,catalog:{scope:'herdr'}});

test('services: a state change flashes that row and the counts count up; calm does neither',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s={services:'abcdefghij'.split('').map(id=>service(id))};
  await calm(page,mode);
  await boot(page,base,'/services?status=all',p=>p==='/api/v1/services'?{services:structuredClone(s.services)}:{});
  await until(()=>page.locator('[data-service-id]').count().then(n=>n===10),'ten services');
  await watch(page,{key:['data-service-id'],count:'[data-filter="current"] strong'});
  s.services[1].status='unhealthy';
  for(const sv of s.services)sv.updated_at=new Date(Date.parse(T0)+30000).toISOString();
  await page.clock.runFor(31000);
  await until(()=>page.locator('[data-filter="current"] strong').textContent().then(t=>t==='9'),'count 9',8000);
  await page.clock.runFor(400);await sleep(30);
  const w=await watched(page);
  if(mode==='off'){
   assert.deepEqual(w.flashed,['b'],'only the row whose state changed flashes');
   assert.ok(w.counts.some(t=>t!=='9'),`the healthy count counts down from 10, saw ${w.counts}`);
  }else{
   assert.deepEqual(w.flashed,[],'calm: no flash');
   assert.ok(w.counts.every(t=>t==='9'),`calm: the count jumps, saw ${w.counts}`);
  }
 });
});

test('topology: metric tiles count up and a row whose status changes flashes; current rows carry a status dot',async()=>withPage(async(page,base)=>{
 const agent=(id,status='working')=>({session_id:id,agent_role:`Agent ${id}`,status,host_id:'beast',updated_at:T0});
 const s={sessions:[agent('s1'),agent('s2')]};
 await calm(page,'off');
 await boot(page,base,'/topology',p=>{
  if(p==='/api/v1/sessions')return {sessions:structuredClone(s.sessions)};
  if(p==='/api/v1/hosts')return {hosts:[{host_id:'beast',host_name:'beast',status:'healthy',last_seen:T0}]};
  if(p==='/api/v1/services')return {services:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  if(p==='/api/v1/mission-activity')return {activities:[]};
  if(p==='/api/v1/ci')return {available:true,pipelines:{}};
  return {};
 });
 await page.locator('[data-tx-disclosure] > summary').first().click();
 await until(()=>page.locator('[data-topology-entry]').count().then(n=>n>=2),'two agent rows');
 assert.ok(await page.locator('[data-topology-entry] .tx-dot').count()>=2,'current rows show the status dot');
 await watch(page,{key:['data-topology-entry'],count:'[data-tx-metrics] div:nth-child(2) strong'});
 s.sessions[0].status='waiting';
 s.sessions.push(agent('s3'));
 await page.clock.runFor(5500);
 await until(()=>page.locator('[data-topology-entry]').count().then(n=>n>=3),'third agent row');
 await page.clock.runFor(400);await sleep(30);
 const w=await watched(page);
 assert.equal(w.flashed.length,1,`only the changed row flashes, saw ${w.flashed}`);
 assert.match(w.flashed[0],/s1/);
 assert.equal(await page.locator('[data-tx-metrics] strong').nth(1).textContent(),'3');
}));

const run=(n,state,extra={})=>({provider_repo_id:7,provider_url:'https://ci.example',repository:'org/app',run_number:n,state,branch:'main',created_at:ago(3600000+n*60000),started_at:ago(3500000+n*60000),finished_at:state==='running'?null:ago(3000000+n*60000),updated_at:ago(1000),...extra});
function ciApi(s){
 return p=>{
  if(p==='/api/v1/ci')return {available:true,status:'connected',pipelines:{'org/app':structuredClone(s.runs)}};
  if(p==='/api/v1/ci/history')return {available:true,repos:[{id:7,provider_url:'https://ci.example',repository:'org/app',default_branch:'main'}],pipelines:structuredClone(s.runs),coverage:{}};
  if(p==='/api/v1/ci/reports')return {available:true,pipelines:[],reports:[]};
  return {};
 };
}

test('CI: running rows pulse and charts draw in on the first paint only; calm stops the pulse',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s={runs:[run(1,'success'),run(2,'failure'),run(3,'running')]};
  await calm(page,mode);
  await boot(page,base,'/ci?tab=current',ciApi(s));
  await page.locator('[data-ci-workspace="runs"]').click();
  const running=page.locator('.ig-row[data-state="running"]');
  await running.first().waitFor();
  const animation=await running.first().locator('.ig-state strong').evaluate(el=>getComputedStyle(el).animationName);
  assert.equal(animation,mode==='off'?'hd-pulse':'none');
  await page.locator('[data-ci-charts-body] svg polyline').first().waitFor({state:'attached'});
  assert.equal(await page.locator('[data-ci-charts-body] svg polyline').first().getAttribute('data-drawn'),'','first paint draws in');
  s.runs.push(run(4,'success'));
  await page.clock.runFor(6000);
  await until(()=>page.locator('[data-ci-charts-body] svg circle').count().then(n=>n===3),'chart redrawn with the new run',8000);
  assert.equal(await page.locator('[data-ci-charts-body] svg [data-drawn]').count(),0,'a later chart paint does not draw in again');
 });
});

test('CI health: a cell whose outcome changes flashes; others do not',async()=>withPage(async(page,base)=>{
 const s={runs:[run(1,'success'),run(2,'success',{created_at:ago(7200000+120000)})]};
 await calm(page,'off');
 await boot(page,base,'/ci',ciApi(s));
 const cells=page.locator('[data-health-map] [data-health-project][data-health-bucket]');
 await until(()=>page.locator('[data-health-map] .ci-health-cell.state-success').count().then(n=>n>=2),'two successful cells',10000);
 await watch(page,{key:['data-health-bucket']});
 const before=await cells.evaluateAll(list=>list.map(c=>[c.dataset.healthBucket,c.textContent]));
 s.runs[0].state='failure';s.runs[0].updated_at=T0;
 await page.clock.runFor(6000);
 await until(()=>page.locator('[data-health-map] .ci-health-cell.state-failure').count().then(n=>n===1),'failure cell',10000);
 const w=await watched(page);
 const after=await cells.evaluateAll(list=>list.map(c=>[c.dataset.healthBucket,c.textContent]));
 const changed=after.filter(([k,t])=>before.find(([b])=>b===k)?.[1]!==t).map(([k])=>k);
 assert.equal(changed.length,1);
 assert.deepEqual(w.flashed,changed,'only the changed cell flashes');
}));

const repo=(name,count)=>({github_id:name,repository_id:`github.com/lab/${name}`,name_with_owner:`lab/${name}`,open_issue_count:count,issues_loaded:2,issues_complete:true});
const issue=(n,extra={})=>({github_id:String(n),repository_id:'github.com/lab/busy',number:n,title:`Issue ${n}`,state:'OPEN',updated_at:'2026-01-01T00:00:00Z',labels:[],assignees:[],...extra});
const snap=(extra={})=>({available:true,status:'ready',refreshing:false,stale:false,partial:false,account:'operator',connection_generation:1,last_success_at:T0,pagination:{page:1,per_page:20,total:2,has_next:false},...extra});

test('GitHub: a changed issue row flashes and tab counts count up; calm does neither',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s={issues:[issue(1),issue(2)],counts:{all:12,autospec:1,preparation:0,in_progress:0,blocked:1}};
  await calm(page,mode);
  await boot(page,base,'/github',p=>{
   if(p==='/api/v1/github')return snap({repositories:[repo('busy',2)]});
   if(p==='/api/v1/github/issues')return snap({issues:structuredClone(s.issues),counts:structuredClone(s.counts)});
   return {};
  });
  await until(()=>page.locator('[data-gh-issue-row]').count().then(n=>n===2),'two issues');
  await watch(page,{key:['data-gh-issue-row'],count:'[data-gh-tab-count="all"]'});
  s.issues[1]={...s.issues[1],title:'Issue 2 renamed',updated_at:'2026-01-01T00:00:09Z'};
  s.counts={...s.counts,all:20};
  await page.clock.runFor(5500);
  await until(()=>page.locator('[data-gh-tab-count="all"]').textContent().then(t=>t==='20'),'count 20',8000);
  await page.clock.runFor(400);await sleep(30);
  const w=await watched(page);
  if(mode==='off'){
   assert.equal(w.flashed.length,1,`only the changed issue flashes, saw ${w.flashed}`);
   assert.match(w.flashed[0],/2\b/);
   assert.ok(w.counts.some(t=>t!=='12'&&t!=='20'),`the count counts up through intermediate values, saw ${w.counts}`);
  }else{
   assert.deepEqual(w.flashed,[]);
   // The view rewrites the same text on every draw; calm never writes a value in between.
   assert.ok(w.counts.every(t=>t==='12'||t==='20'),`calm: the count jumps, saw ${w.counts}`);
  }
 });
});

async function withCollab(fn,mode){
 const T='2026-10-23T10:00:00+00:00';
 const m={events:[{sequence:1,event_id:'e1',event_type:'message',created_at:T,room_id:'room-one',sender:{type:'user',id:'bob',owner_user_id:'bob'},text:'first'}]};
 const server=createServer(async(req,res)=>{
  if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end('<link rel="stylesheet" href="/motion.css"><link rel="stylesheet" href="/collab.css"><div id="host"></div>');return;}
  try{const file=new URL(`../../packages/${req.url.split('?')[0].slice(1)}`,import.meta.url);res.setHeader('Content-Type',req.url.endsWith('.css')?'text/css':'application/javascript');res.end(await readFile(file));}
  catch{res.writeHead(404);res.end();}
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 let browser;
 try{
  const fallback='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
  browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.PLAYWRIGHT_CHROMIUM||(existsSync(fallback)?fallback:undefined)});
  const page=await browser.newPage({viewport:{width:1100,height:900}});
  await page.route('**/api/v1/collab/**',async route=>{
   const url=new URL(route.request().url());const path=url.pathname.replace('/api/v1/collab','');
   let body={};
   if(path==='/config')body={client_id:'test',hosted_ui_domain:'test',public_url:'https://example.test',operator:{user_id:'owner',email:'owner@example.test'}};
   else if(path==='/rooms')body={rooms:[{room_id:'room-one',name:'Review',role:'owner'}]};
   else if(path==='/admin/users')body={users:[]};
   else if(path==='/rooms/room-one')body={room_id:'room-one',name:'Review',role:'owner',members:[]};
   else if(path==='/rooms/room-one/state'){
    const cursor=Number(url.searchParams.get('cursor')||0),events=m.events.filter(e=>e.sequence>cursor);
    if(!events.length)await new Promise(r=>setTimeout(r,150));
    const last=events.length?events.at(-1).sequence:cursor;
    body={changed:true,seq:last,next_cursor:last,agents:[],tasks:[],events,flow:{nodes:[],edges:[],tasks:[]}};
   }
   await route.fulfill({json:body});
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.evaluate(async mode=>{document.documentElement.dataset.calm=mode;const {mountCollab}=await import('/collab-view.js');window.collab=mountCollab(document.querySelector('#host'));},mode);
  await page.locator('[data-room="room-one"]').click();
  await fn(page,m);
  await page.evaluate(()=>window.collab.dispose());
 }finally{await browser?.close();await new Promise(r=>server.close(r));}
}

test('collab: live chat rows slide in, history does not; calm does neither',async()=>{
 for(const mode of ['off','on'])await withCollab(async(page,m)=>{
  await page.locator('#hcn-log .cx-m').first().waitFor();
  assert.equal(await page.locator('#hcn-log [data-entering]').count(),0,'history appears without motion');
  await watch(page,{key:[]});
  m.events.push({sequence:2,event_id:'e2',event_type:'message',created_at:'2026-10-23T10:01:00+00:00',room_id:'room-one',sender:{type:'user',id:'bob',owner_user_id:'bob'},text:'second'});
  await until(()=>page.locator('#hcn-log .cx-m').count().then(n=>n===2),'second message');
  await sleep(50);
  const w=await watched(page);
  assert.equal(w.entered.length,mode==='off'?1:0,`live row enters only without calm, saw ${w.entered}`);
 },mode);
});
