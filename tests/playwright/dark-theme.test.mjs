// Task 17: the dark theme across every view. For every route the router knows (read from app.js), in
// each state a page can be in (loading skeleton, error, empty, populated; the collab room page and
// the topology page with data included), the dark palette must hold:
//  - every visible text reaches 4.5:1 against the background it is drawn on;
//  - no element paints a light background (a white or near-white fill) other than images.
// Both ways into dark are checked: data-theme="dark" forced on an OS in light mode, and theme auto
// following an OS in dark mode. A control checks the audit flags the light theme's white surfaces.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,sleep,T0} from './helpers/live.mjs';
import {appRoutes} from './helpers/routes.mjs';

const ROUTES=appRoutes();
const MIN_CONTRAST=4.5;

const session=i=>({session_id:`s-${i}`,display_name:`Agent ${i}`,agent_role:`Role ${i}`,host_id:'h-1',status:i%2?'running':'blocked',cwd:'/work/app',updated_at:T0,last_activity:T0,mission_id:'m-1'});
const service=i=>({service_id:`svc-${i}`,service_name:`Service ${i}`,name:`svc-${i}`,host_id:'h-1',status:['healthy','degraded','down'][i%3],kind:'service',updated_at:T0,observed_at:T0,catalog:{scope:'herdr'}});
const host=i=>({host_id:`h-${i}`,host_name:`h-${i}`,status:i%2?'healthy':'degraded',last_seen:T0,updated_at:T0,drift:i%2===0});
const mission=i=>({mission_id:`m-${i}`,title:`Mission ${i}`,status:['EXECUTING','BLOCKED','DONE'][i%3],stage:'review',updated_at:T0,created_at:T0});
const event=i=>({event_id:`e${i}`,sequence:i,event_type:['MissionStageChanged','SessionStatusChanged','ServiceHealthChanged'][i%3],entity_type:'mission',entity_id:'m-1',source_timestamp:T0,payload:{title:'Mission 1',status:'failed'}});
const ago=ms=>new Date(Date.parse(T0)-ms).toISOString();
const run=(n,state)=>({provider_repo_id:7,provider_url:'https://ci.example',repository:'org/app',run_number:n,state,branch:n%2?'main':'dev',created_at:ago(3600000+n*60000),started_at:ago(3500000+n*60000),finished_at:state==='running'||state==='queued'?null:ago(3000000+n*60000),updated_at:ago(1000)});
const RUNS=[run(1,'success'),run(2,'failure'),run(3,'running'),run(4,'queued'),run(5,'success')];
const projection=n=>({contract_version:1,mission_key:`pi:M${n}`,host_id:'h-1',observed_at:T0,snapshot_at:T0,activity_snapshot_at:T0,connection:'connected',
 mission:{id:`M${n}`,title:`Pi mission ${n}`,status:'EXECUTING'},freshness:{state:'live',age_seconds:1,stale_after_seconds:3600,disconnect_after_seconds:7200},
 activity:{health:'active',currentActivity:{summary:`Working ${n}`}}});
const ids=k=>Array.from({length:k},(_,i)=>i+1);

const BASE={mission_id:'m-1',session_id:'s-1',host_id:'h-1',host_name:'h-1',title:'Mission 1',display_name:'Agent 1',updated_at:T0,created_at:T0,stage:'review',status:'ready'};
// One body every view accepts with lists empty (the "empty" state).
const EMPTY={items:[],groups:[],snapshots:[],analyses:[],artifacts:[],versions:[],publications:[],nodes:[],edges:[],...BASE,missions:[],sessions:[],services:[],hosts:[],activities:[],events:[],timeline:[],available:true,reviews:[],repositories:[],issues:[],
 counts:{},pagination:{},components:[],summary:{},reports:[],pipelines:[],samples:[],total:0,completed:0,completion_rate:0,by_stage:{},models:{},
 total_sessions:0,healthy:0,unavailable:0,sessions_detail:[]};
const FULL={...EMPTY,missions:ids(4).map(mission),sessions:ids(4).map(session),services:ids(4).map(service),hosts:ids(3).map(host),
 events:ids(6).map(event),total:12,completed:5,completion_rate:42,by_stage:{review:2,done:5,blocked:1},total_sessions:4,healthy:3,unavailable:1,
 models:{'qwen-27b':{requests:12,tokens:3400}}};

const snap=extra=>({available:true,status:'ready',account:'operator',connection_generation:1,last_success_at:T0,pagination:{page:1,per_page:20,total:2,has_next:false},...extra});
function populated(p){
 switch(p){
  case '/api/v1/mission-activity':return {activities:[projection(1),projection(2)]};
  case '/api/v1/missions/pi:msn-1':return {__status:404,json:{error:'not found'}};
  case '/api/v1/ci':return {available:true,status:'connected',pipelines:{'org/app':RUNS}};
  case '/api/v1/ci/history':return {available:true,status:'ready',repos:[{id:7,provider_url:'https://ci.example',repository:'org/app',default_branch:'main'}],pipelines:RUNS,coverage:{}};
  case '/api/v1/ci/pipeline':return {provider_url:'https://ci.example',pipeline:{...run(3,'running'),commit:'a'.repeat(40),can_retry:true,can_cancel:false,
   tasks:[{id:1,name:'Compile',state:'success'},{id:2,name:'Unit tests',state:'failure'},{id:3,name:'Deploy',state:'blocked'},{id:4,name:'Package',state:'queued'}]}};
  case '/api/v1/ci/log':return {provider_url:'https://ci.example',available:true,log:'expected 200, got 500'};
  case '/api/v1/ci/reports':return {available:true,pipelines:[run(5,'failure'),run(6,'success')],reports:[]};
  case '/api/v1/github':return snap({repositories:[{repository_id:'github.com/lab/a',name_with_owner:'lab/a',open_issue_count:2},{repository_id:'github.com/lab/b',name_with_owner:'lab/b',open_issue_count:0}]});
  case '/api/v1/github/issues':return snap({issues:[{github_id:'1',repository_id:'github.com/lab/a',number:1,title:'Broken build',state:'OPEN',labels:[{name:'bug'}],updated_at:T0},
   {github_id:'2',repository_id:'github.com/lab/a',number:2,title:'Ship it',state:'OPEN',labels:[{name:'autospec'}],updated_at:T0}],counts:{all:2,autospec:1,preparation:0,in_progress:1,blocked:0}});
  case '/api/v1/collab/config':return {client_id:'test',hosted_ui_domain:'test',public_url:'https://example.test',operator:{user_id:'owner',email:'owner@example.test'}};
  case '/api/v1/collab/rooms':return {rooms:[{room_id:'room-one',name:'Review',role:'owner'},{room_id:'room-two',name:'Build',role:'member'}]};
  case '/api/v1/collab/rooms/room-one':return {room_id:'room-one',name:'Review',role:'owner',members:[{user_id:'owner',role:'owner'}]};
  case '/api/v1/collab/rooms/room-one/state':return {changed:true,seq:1,next_cursor:1,
   agents:[{agent_id:'a1',name:'Lead',status:'working',kind:'pi'},{agent_id:'a2',name:'Helper',status:'idle',kind:'codex'}],
   tasks:[{task_id:'t1',title:'Review the patch',status:'running',assignee:'a1'},{task_id:'t2',title:'Fix the test',status:'blocked',assignee:'a2'}],
   events:[{seq:1,kind:'message',author:{type:'user',id:'owner'},text:'Please look at the failing test',created_at:T0},{seq:2,kind:'message',author:{type:'agent',id:'a1'},text:'On it',created_at:T0}],
   flow:{nodes:[],edges:[],tasks:[]}};
 }
 if(p.startsWith('/api/v1/collab/'))return {};
 return FULL;
}

const STATES={
 loading:()=>({__hang:true}),
 error:()=>({__status:500,json:{error:'backend down'}}),
 empty:p=>p==='/api/v1/missions/pi:msn-1'?{__status:404,json:{error:'not found'}}:p.startsWith('/api/v1/collab/')?(p==='/api/v1/collab/rooms'?{rooms:[]}:{}):EMPTY,
 populated,
};

const MODES={
 'data-theme="dark" forced, OS light':async page=>{await page.emulateMedia({colorScheme:'light'});await page.addInitScript(()=>{try{localStorage.setItem('herdr.theme','dark');}catch{}});},
 'theme auto, OS dark':async page=>{await page.emulateMedia({colorScheme:'dark'});},
};

// Runs in the page: every visible element (open shadow roots included) whose own text is not blank
// is checked against the background it sits on (its own and its ancestors' fills, composited over
// the page); every element is checked for a light fill. Images are exempt. Inline SVG (charts,
// graphs) is checked for its text only, by its fill colour.
function audit({min}){
 const parse=s=>{const m=s.match(/rgba?\(([^)]+)\)/);if(!m)return null;const [r,g,b,a=1]=m[1].split(/[\s,/]+/).filter(Boolean).map(Number);return {r,g,b,a};};
 const lum=({r,g,b})=>{const f=v=>{v/=255;return v<=0.03928?v/12.92:((v+0.055)/1.055)**2.4;};return 0.2126*f(r)+0.7152*f(g)+0.0722*f(b);};
 const over=(top,bottom)=>({r:top.r*top.a+bottom.r*(1-top.a),g:top.g*top.a+bottom.g*(1-top.a),b:top.b*top.a+bottom.b*(1-top.a),a:1});
 const up=n=>n.parentElement||(n.getRootNode()?.host??null);
 const IMAGE='img,canvas,video,picture';
 const page=parse(getComputedStyle(document.body).backgroundColor);
 const backdrop=el=>{
  const layers=[];
  for(let n=el;n;n=up(n)){
   const cs=getComputedStyle(n);
   if(/gradient|url\(/.test(cs.backgroundImage))return null;// drawn background: not measurable here
   const c=parse(cs.backgroundColor);
   if(c&&c.a>0){layers.push(c);if(c.a>=1)break;}
  }
  return layers.reduceRight((acc,c)=>over(c,acc),page&&page.a>=1?page:{r:255,g:255,b:255,a:1});
 };
 const visible=el=>{if(!el.getClientRects().length)return false;const cs=getComputedStyle(el);return cs.visibility!=='hidden'&&+cs.opacity>0;};
 const where=el=>{const id=el.id?`#${el.id}`:'';const cls=typeof el.className==='string'&&el.className?`.${el.className.trim().split(/\s+/).slice(0,2).join('.')}`:'';return `${el.tagName.toLowerCase()}${id}${cls}`;};
 const contrast=[],light=[];
 const walk=root=>{for(const el of root.querySelectorAll('*')){
  if(el.shadowRoot)walk(el.shadowRoot);
  if(el.closest(IMAGE)||!visible(el))continue;
  const inSvg=!!el.closest('svg');
  if(inSvg&&!el.matches('text,tspan'))continue;
  const cs=getComputedStyle(el);
  const fill=parse(cs.backgroundColor);
  if(!inSvg&&fill&&fill.a>=0.9&&lum(fill)>0.5)light.push(`${where(el)} ${cs.backgroundColor}`);
  const own=[...el.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim();
  if(!own||el.matches('option,script,style,title')||el.closest(':disabled,[aria-disabled="true"]'))continue;
  const bg=backdrop(el);if(!bg)continue;
  let fg=parse(inSvg?cs.fill:cs.color);if(!fg)continue;
  if(fg.a<1)fg=over(fg,bg);
  const [a,b]=[lum(fg),lum(bg)].sort((x,y)=>y-x);
  const ratio=(a+0.05)/(b+0.05);
  if(ratio<min)contrast.push(`${where(el)} "${own.slice(0,30)}" ${cs.color} on rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)}) = ${ratio.toFixed(2)}`);
 }};
 walk(document);
 return {contrast,light};
}

const settle=async(page,state,hash='')=>{
 if(state==='loading'){await until(()=>page.evaluate(()=>!!document.querySelector('#view [data-skeleton]')),`skeleton on ${hash}`,8000);return;}
 // Loaded: the skeleton gave way. A skeleton left beside a red chip is not a loaded page.
 await until(()=>page.evaluate(()=>document.getElementById('view').children.length>0&&!document.querySelector('#view [data-skeleton]')),`${hash} loaded`,8000);
 await page.clock.runFor(1000);await sleep(150);
};

async function tour(page,base,mode,state){
 await MODES[mode](page);
 await boot(page,base,'/overview',p=>STATES[state](decodeURIComponent(p)));
 const found=[];
 const record=async label=>{const r=await page.evaluate(audit,{min:MIN_CONTRAST});for(const x of r.contrast)found.push(`${label} contrast ${x}`);for(const x of r.light)found.push(`${label} light fill ${x}`);};
 for(const hash of ROUTES){
  await page.evaluate(h=>{location.hash=h;},hash);
  await sleep(50);
  await settle(page,state,hash);
  await record(`${state} ${hash}`);
  // An empty or populated page must really be one: a red chip means a view rejected the stub and
  // the audit just saw its error page.
  if(state==='empty'||state==='populated'){
   const chip=await page.evaluate(()=>{const c=document.querySelector('#view herdr-freshness');return c?.dataset.state==='error'?c.textContent:null;});
   if(chip)found.push(`${state} ${hash} chip is red: ${chip}`);
  }
  if(hash==='/collab'&&state==='populated'){
   await page.locator('[data-room="room-one"]').first().click();
   await sleep(400);await page.clock.runFor(1000);await sleep(200);
   await record(`${state} /collab room`);
  }
  if(hash==='/ci'&&state==='populated'){
   await page.locator('[data-ci-workspace="runs"]').click();
   await page.getByRole('button',{name:'Run #3',exact:true}).click();
   await page.getByRole('button',{name:/Unit tests/}).first().click();
   await sleep(300);await page.clock.runFor(1000);await sleep(200);
   await record(`${state} /ci run inspector`);
  }
 }
 return [...new Set(found)];
}

for(const mode of Object.keys(MODES))for(const state of Object.keys(STATES)){
 test(`dark (${mode}): every route ${state} is readable with no light fills`,async()=>withPage(async(page,base)=>{
  const found=await tour(page,base,mode,state);
  assert.notEqual(await page.evaluate(()=>getComputedStyle(document.body).backgroundColor),'rgb(247, 247, 248)','the dark palette is on');
  if(found.length)console.log(found.join('\n'));
  assert.deepEqual(found,[]);
 }));
}

// Control: the light theme paints white cards, so the audit must report light fills there.
test('control: the light theme has light fills the audit sees',async()=>withPage(async(page,base)=>{
 await page.emulateMedia({colorScheme:'light'});
 await boot(page,base,'/hosts',p=>populated(decodeURIComponent(p)));
 await settle(page,'populated');
 const r=await page.evaluate(audit,{min:MIN_CONTRAST});
 assert.ok(r.light.length>0);
}));

// The light theme keeps its look: the tokens that replaced hard-coded colours resolve to the old values.
test('light theme: tokens that replaced hard-coded colours keep their old values',async()=>withPage(async(page,base)=>{
 await page.emulateMedia({colorScheme:'light'});
 await boot(page,base,'/overview',p=>EMPTY);
 const old={'--ci-pass':'#238636','--ci-fail':'#dc3545','--ci-queued':'#bf8700','--ci-blocked':'#a56ae0','--ci-cell-bg':'#e7ebef','--ci-cell-text':'#263449',
  '--ci-cell-success-bg':'#bfe9ce','--ci-cell-success-text':'#155e32','--ci-cell-failure-bg':'#f5bcc2','--ci-cell-failure-text':'#8f1424',
  '--ci-cell-mixed-bg':'#f8dc9a','--ci-cell-mixed-text':'#775000','--ci-cell-active-bg':'#c4dcfa','--ci-cell-active-text':'#184c91',
  '--ci-cell-neutral-bg':'#dfe0eb','--ci-cell-neutral-text':'#4f4a6b','--ci-cell-stripe-a':'#eef0f4','--ci-cell-stripe-b':'#d8dde6',
  '--ci-cell-incomplete-border':'#697488','--ci-cell-active-dot':'#1263c4','--ci-cell-selected':'#202f48','--focus-ring':'#276ec5',
  '--error-bg':'#fef2f2','--text-on-accent':'#ffffff'};
 const now=await page.evaluate(names=>Object.fromEntries(names.map(n=>[n,getComputedStyle(document.documentElement).getPropertyValue(n).trim()])),Object.keys(old));
 assert.deepEqual(now,old);
}));
