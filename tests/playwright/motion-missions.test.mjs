// Task 11: motion on Missions, Mission detail, Agents and Agent detail. Unchanged rows keep their
// nodes, new rows enter (not under calm), progress eases, and the agent detail page appends to its
// recorded-event list and updates its head status from the live event bus.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,liveRefresh,sleep,T0} from './helpers/live.mjs';

const calm=(page,mode)=>page.addInitScript(m=>{try{localStorage.setItem('herdr.calm',m);}catch{}},mode);

function projection(n,completed=1){
 return {contract_version:1,mission_key:`pi:M${n}`,host_id:'beast',observed_at:T0,snapshot_at:T0,activity_snapshot_at:T0,connection:'connected',
  mission:{id:`M${n}`,title:`Mission ${n}`,status:'EXECUTING'},
  freshness:{state:'live',age_seconds:1,stale_after_seconds:3600,disconnect_after_seconds:7200},
  activity:{health:'active',currentActivity:{summary:`Working ${n}`},acceptanceCoverage:{completed,total:5}}};
}
function missionsApi(s){
 return p=>{
  if(p==='/api/v1/mission-activity')return {activities:structuredClone(s.projections)};
  if(p==='/api/v1/missions')return {missions:[]};
  return {};
 };
}
// Records which nodes entered with data-entering (it is removed again after the animation, so a
// point-in-time read would race under load) and every text written into the current-tab count.
const ident=el=>el.dataset.missionKey??el.dataset.sessionId??el.dataset.messageSeq??el.dataset.activitySeq??el.getAttribute('data-key')??el.textContent;
const watch=page=>page.evaluate(src=>{
 const ident=eval(src);
 window.__entered=[];window.__counts=[];
 const root=document.getElementById('view')||document.body;
 const note=n=>{if(n.nodeType!==1)return;for(const el of [n,...n.querySelectorAll('[data-entering]')])if(el.hasAttribute('data-entering'))window.__entered.push(ident(el));};
 window.__w?.disconnect();
 window.__w=new MutationObserver(rs=>{for(const r of rs){
  if(r.type==='childList')r.addedNodes.forEach(note);
  if(r.type==='attributes'&&r.attributeName==='data-entering'&&r.target.hasAttribute('data-entering'))window.__entered.push(ident(r.target));
  const t=r.target.nodeType===1?r.target:r.target.parentElement;
  const count=t?.closest?.('[data-tab="current"] span')||(r.type==='childList'?[...r.addedNodes].find(n=>n.nodeType===1&&n.matches?.('[data-tab="current"]'))?.querySelector('span'):null);
  if(count)window.__counts.push(count.textContent);
 }});
 window.__w.observe(root,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['data-entering']});
},ident.toString());
const watched=page=>page.evaluate(()=>({entered:[...new Set(window.__entered)],counts:window.__counts}));
const progressValues=page=>page.evaluate(()=>{
 window.__values=[];
 window.__pv?.disconnect();
 window.__pv=new MutationObserver(rs=>{for(const r of rs)if(r.target.localName==='progress')window.__values.push(r.target.value);});
 window.__pv.observe(document.getElementById('view'),{subtree:true,attributes:true,attributeFilter:['value']});
});

test('missions: unchanged rows keep their nodes, a new row enters, progress eases, counts count up',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s={projections:[projection(1),projection(2,1)]};
  await calm(page,mode);
  await boot(page,base,'/missions',missionsApi(s));
  await until(()=>page.locator('[data-mission-key]').count().then(n=>n===2),'two missions');
  await page.evaluate(()=>{window.__rows=Object.fromEntries([...document.querySelectorAll('[data-mission-key]')].map(r=>[r.dataset.missionKey,r]));});
  await progressValues(page);await watch(page);
  s.projections[1].activity.acceptanceCoverage.completed=4;
  s.projections.push(projection(3));
  await page.clock.runFor(5500);
  await until(()=>page.locator('[data-mission-key="pi:M3"]').count().then(n=>n===1),'third mission');
  const r=await page.evaluate(()=>({
   same1:document.querySelector('[data-mission-key="pi:M1"]')===window.__rows['pi:M1'],
   same2:document.querySelector('[data-mission-key="pi:M2"]')===window.__rows['pi:M2'],
  }));
  assert.equal(r.same1,true,'an unchanged row keeps its node');
  assert.equal(r.same2,true,'a changed row is patched in place');
  await page.clock.runFor(400);await sleep(30);
  const w=await watched(page);
  assert.deepEqual(w.entered,mode==='off'?['pi:M3']:[],'only the new row enters (not under calm)');
  if(mode==='off')assert.ok(w.counts.some(t=>t!=='3'),`the tab count counts up from 2, saw ${w.counts}`);
  else assert.ok(w.counts.every(t=>t==='3'),`calm: the count jumps, saw ${w.counts}`);
  assert.equal(await page.locator('[data-tab="current"] span').textContent(),'3');
  const values=await page.evaluate(()=>window.__values);
  assert.equal(await page.locator('[data-mission-key="pi:M2"] progress').evaluate(el=>el.value),80);
  if(mode==='off')assert.ok(values.some(v=>v>20&&v<80),`progress eases through intermediate values, saw ${values}`);
  else assert.ok(values.every(v=>v===80),`calm: progress jumps, saw ${values}`);
  const transition=await page.locator('[data-mission-key="pi:M1"] [data-freshness]').evaluate(el=>getComputedStyle(el).transitionProperty);
  assert.match(transition,/color/,'freshness badge colour transitions');
 });
});

test('mission detail: a new timeline item slides in and earlier items keep their nodes',async()=>withPage(async(page,base)=>{
 const s={timeline:[{kind:'stage',title:'Planned',ts:'2026-01-01T00:00:01Z'}]};
 await calm(page,'off');
 await boot(page,base,'/missions/m1',p=>{
  if(p==='/api/v1/missions/m1')return {mission_id:'m1',title:'Mission one',status:'running',sessions:[]};
  if(p==='/api/v1/missions/m1/timeline')return {timeline:structuredClone(s.timeline)};
  if(p==='/api/v1/events')return {events:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  return {};
 });
 await page.locator('#mission-timeline li').first().waitFor({state:'attached'});
 await page.evaluate(()=>{window.__first=document.querySelector('#mission-timeline li');});
 await watch(page);
 s.timeline.push({kind:'stage',title:'Executing',ts:'2026-01-01T00:00:08Z'});
 await page.evaluate(()=>window.__emit({event_id:'x1',event_type:'MissionStageChanged'}));
 await page.clock.runFor(6000);
 await until(()=>page.locator('#mission-timeline li').count().then(n=>n===2),'second timeline item');
 const r=await page.evaluate(()=>({same:document.querySelector('#mission-timeline li')===window.__first,entered:[...new Set(window.__entered)].map(t=>t.replace(/\s+/g,' ').trim())}));
 assert.equal(r.same,true,'the earlier item keeps its node');
 assert.equal(r.entered.length,1,`only the new item enters, saw ${r.entered}`);
 assert.match(r.entered[0],/Executing/);
}));

const agent=(id,extra={})=>({session_id:id,display_name:`Agent ${id}`,status:'running',host_id:'beast',cwd:'/w/proj',updated_at:T0,last_activity:T0,...extra});

test('agents: unchanged rows keep their nodes and a new agent enters',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  const s={sessions:[agent('a'),agent('b')]};
  await calm(page,mode);
  await boot(page,base,'/herd',p=>{
   if(p==='/api/v1/sessions')return {sessions:structuredClone(s.sessions)};
   if(p==='/api/v1/missions')return {missions:[]};
   return {};
  });
  await until(()=>page.locator('[data-session-id]').count().then(n=>n===2),'two agents');
  await page.evaluate(()=>{window.__a=document.querySelector('[data-session-id="a"]');});
  await watch(page);
  s.sessions.push(agent('c'));
  await page.clock.runFor(5500);
  await until(()=>page.locator('[data-session-id="c"]').count().then(n=>n===1),'third agent');
  const r=await page.evaluate(()=>({same:document.querySelector('[data-session-id="a"]')===window.__a,entered:[...new Set(window.__entered)]}));
  assert.equal(r.same,true,'an unchanged agent row keeps its node');
  assert.deepEqual(r.entered,mode==='off'?['c']:[],'only the new agent row enters (not under calm)');
 });
});

test('agent detail: the recorded event list appends and the head status follows live events',async()=>withPage(async(page,base)=>{
 const s={session:agent('x',{status:'working'}),events:[{event_id:'e1',sequence:1,event_type:'SessionStarted',source_timestamp:T0,payload:{}}]};
 await calm(page,'off');
 await boot(page,base,'/herd/session/x',p=>{
  if(p==='/api/v1/sessions/x')return structuredClone(s.session);
  if(p==='/api/v1/events')return {events:structuredClone(s.events)};
  if(p==='/api/v1/sessions')return {sessions:[]};
  if(p==='/api/v1/missions')return {missions:[]};
  if(/inspector/.test(p))return {events:[],epoch:'e',has_more:false,summary:{agents:[],association:{}}};
  return {};
 });
 await until(()=>page.locator('#session-stream [data-key]').count().then(n=>n===1),'one recorded event');
 await page.evaluate(()=>{window.__stream=document.getElementById('session-stream');window.__row=window.__stream.firstElementChild;});
 assert.match(await page.locator('.sd-head strong').textContent(),/Last reported working/);
 s.events=[...s.events,{event_id:'e2',sequence:2,event_type:'SessionUpdated',source_timestamp:T0,payload:{status:'waiting'}}];
 s.session.status='waiting';
 await page.evaluate(()=>window.__emit({event_id:'e2',event_type:'SessionUpdated',entity_id:'x'}));
 await page.clock.runFor(6000);
 await until(()=>page.locator('#session-stream [data-key]').count().then(n=>n===2),'second recorded event');
 const r=await page.evaluate(()=>({same:document.getElementById('session-stream')===window.__stream,row:window.__stream.contains(window.__row),children:window.__stream.children.length}));
 assert.deepEqual(r,{same:true,row:true,children:2},'the list grows by one; container and earlier row survive');
 await until(()=>page.locator('.sd-head strong').textContent().then(t=>/Last reported waiting/.test(t)),'head status follows the live event');
}));

test('agent workspace: a streamed message appends with an enter transition; earlier rows keep their nodes',async()=>{
 for(const mode of ['off','on'])await withPage(async(page,base)=>{
  await page.goto(`${base}/__blank`);
  await page.evaluate(async mode=>{
   document.documentElement.dataset.calm=mode;
   document.body.innerHTML='<div id="host"></div>';
   const {mountSessionWorkspace}=await import('/session-workspace.js');
   const events=[{seq:1,rev:1,kind:'message',agent_id:'main',data:{role:'user',text:'Hello'}},{seq:2,rev:2,kind:'tool',agent_id:'main',data:{tool:'bash',status:'ok',output:'x'}}];
   const data={events,rev:2,epoch:'e',has_more:false,summary:{agents:[{agent_id:'main'}],association:{}}};
   const client={getInspector:async()=>structuredClone(data),inspectorStreamUrl:()=>'/stream',listSessions:async()=>({sessions:[]}),sendMessage:async()=>({status:'delivered'})};
   mountSessionWorkspace(document.querySelector('#host'),client,{session_id:'s',status:'working',host_id:'h'},{connect:o=>{window.stream=o;return ()=>{};}});
  },mode);
  await page.waitForSelector('[data-message-seq="1"]');
  await page.waitForSelector('[data-activity-seq="2"]');
  await page.evaluate(()=>{window.__m1=document.querySelector('[data-message-seq="1"]');window.__a2=document.querySelector('[data-activity-seq="2"]');});
  await watch(page);
  await page.evaluate(()=>{window.stream.onEvent({type:'event',event:{seq:3,rev:3,kind:'message',agent_id:'main',data:{role:'assistant',text:'Hi'}}});
   window.stream.onEvent({type:'event',event:{seq:4,rev:4,kind:'tool',agent_id:'main',data:{tool:'edit',status:'ok'}}});});
  await page.waitForSelector('[data-message-seq="3"]');await page.waitForSelector('[data-activity-seq="4"]');
  const r=await page.evaluate(()=>({m1:document.querySelector('[data-message-seq="1"]')===window.__m1,a2:document.querySelector('[data-activity-seq="2"]')===window.__a2,
   entered:[...new Set(window.__entered)].sort()}));
  assert.deepEqual(r,{m1:true,a2:true,entered:mode==='off'?['3','4']:[]});
 });
});
