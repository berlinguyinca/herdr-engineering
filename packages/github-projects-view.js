// GitHub Projects v2 boards, read-only. The server syncs the projects of the owners in scope
// (github.organizations) and serves them with an ETag and `since` deltas; this page shows one
// project as a board grouped by its Status field (or as a flat list) and patches cards in place.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {safeServiceUrl} from './services-view.js';
import {skeleton} from './motion.js';
import {morph,patchList,patchText,patchAttr} from './patch.js';
import {freshnessSources} from './freshness.js';

const text=value=>typeof value==='string'?value:'';
const short=(value,max=160)=>text(value).replace(/\s+/g,' ').trim().slice(0,max);
const records=value=>Array.isArray(value)?value.filter(v=>v&&typeof v==='object'&&!Array.isArray(v)):[];
const human=value=>text(value).replaceAll('_',' ').toLowerCase();
const NO_STATUS={id:'',name:'No status'};
// Every FULL_RELOAD_POLLS-th read is full, and so is the read after projects_sync changed: project
// rows carry fields no revision covers (complete, error), so deltas alone could keep them stale.
const FULL_RELOAD_POLLS=12;
const POLL_MS=5000;
const element=html=>{const t=document.createElement('template');t.innerHTML=html.trim();return t.content.firstElementChild;};
const projectOrder=(a,b)=>text(a.owner).localeCompare(text(b.owner))||(Number(a.number)||0)-(Number(b.number)||0);
const itemOrder=(a,b)=>(Number(a.position)||0)-(Number(b.position)||0);

// Columns in the Status field's option order, each with its items in board order; items with no
// Status, or one the field no longer has, go to a trailing "No status" column (shown only when
// it has items). null when the project has no single-select Status field: flat list only.
export function boardColumns(project,items) {
 const options=Array.isArray(project?.status_options)?records(project.status_options):null;
 if(!options)return null;
 const columns=options.map(o=>({id:text(o.id),name:text(o.name)||'Unnamed status',items:[]}));
 const byId=new Map(columns.map(c=>[c.id,c]));
 const loose={...NO_STATUS,items:[]};
 for(const entry of records(items).slice().sort(itemOrder))(byId.get(text(entry.status_option))||loose).items.push(entry);
 return loose.items.length?[...columns,loose]:columns;
}

// Merge a list response into the held body: a full body is taken as is, a resync replaces the
// list, a delta replaces changed rows by id, drops removed ones and keeps the list order.
export function applyProjectDelta(kind,held,body) {
 const {upserted,removed,resync,[kind]:list,...state}=body||{};
 if(resync)return {...state,[kind]:records(list)};
 if(!Array.isArray(upserted)||!Array.isArray(removed))return body;
 if(!held||!Array.isArray(held[kind]))return null;
 const id=kind==='items'?row=>text(row.item_id):row=>text(row.project_id);
 const changed=new Map(records(upserted).map(row=>[id(row),row]));
 const gone=new Set(records(removed).map(row=>text(row.key)));
 const kept=records(held[kind]).filter(row=>!gone.has(id(row))).map(row=>{const next=changed.get(id(row));changed.delete(id(row));return next||row;});
 return {...held,...state,[kind]:[...kept,...changed.values()].sort(kind==='items'?itemOrder:projectOrder)};
}

const link=(url,label)=>{const safe=safeServiceUrl(url);return safe?`<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`:esc(label);};
const itemTitle=entry=>short(entry.title,200)||(entry.type==='REDACTED'?'Item not visible to this token':'Untitled item');
const itemMeta=entry=>[entry.repository?`${short(entry.repository,100)}${Number.isInteger(entry.number)?` #${entry.number}`:''}`:'',entry.type&&entry.type!=='ISSUE'?human(entry.type):'',entry.state&&entry.state!=='OPEN'?human(entry.state):''].filter(Boolean).join(' · ');
export function renderProjectCard(entry={}) {
 const meta=itemMeta(entry);
 return `<article class="ghp-card" data-key="${esc(entry.item_id||'')}" data-ghp-item="${esc(entry.item_id||'')}"><p class="ghp-title">${link(entry.url,itemTitle(entry))}</p>${meta?`<p class="ghp-meta">${esc(meta)}</p>`:''}</article>`;
}
export function renderProjectRow(entry={}) {
 const meta=itemMeta(entry);
 return `<article class="ghp-row" data-key="${esc(entry.item_id||'')}" data-ghp-item="${esc(entry.item_id||'')}"><span class="ghp-status">${esc(short(entry.status,60)||NO_STATUS.name)}</span><p class="ghp-title">${link(entry.url,itemTitle(entry))}</p>${meta?`<p class="ghp-meta">${esc(meta)}</p>`:''}</article>`;
}
export function projectsSyncNote(body) {
 const sync=body?.projects_sync||{};
 if(sync.status==='disabled')return {text:'Project sync is off. Set github.projects.enabled: true in the github section of the fleet file; the token needs the read:project scope.',warn:true,off:true};
 const parts=[sync.status&&sync.status!=='ready'?human(sync.status):'',Number(sync.deferred)>0?`${sync.deferred} projects deferred`:'',short(sync.error,300)].filter(Boolean);
 return {text:parts.join(' · '),warn:parts.length>0,off:false};
}

const styles=`<style>
.ghp-intro,.ghp-note{color:var(--text-secondary);font-size:13px;line-height:1.6}.ghp-intro{max-width:86ch}.ghp-warning{color:var(--status-attention);font-size:12px;line-height:1.6;overflow-wrap:anywhere}
.ghp-controls{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin:16px 0}.ghp-controls label{display:flex;flex-direction:column;gap:6px;font-size:12px;min-width:0;max-width:100%}.ghp-controls select{padding:9px;font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);max-width:100%}
.ghp-board{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(240px,1fr);gap:12px;overflow-x:auto;padding-bottom:8px;align-items:start}.ghp-board[hidden],.ghp-list[hidden]{display:none}
.ghp-column{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);min-width:0}.ghp-column h2{display:flex;justify-content:space-between;gap:8px;font-size:13px;margin:0;padding:12px;border-bottom:1px solid var(--border-subtle)}.ghp-column h2 small{color:var(--text-secondary);font-weight:400}
.ghp-cards{display:flex;flex-direction:column;gap:8px;padding:10px;min-height:40px}.ghp-card,.ghp-row{background:var(--bg);border:1px solid var(--border-subtle);border-radius:var(--radius-sm);padding:10px;overflow-wrap:anywhere}
.ghp-title{margin:0;font-size:13px;line-height:1.5;font-weight:600}.ghp-meta{margin:4px 0 0;font-size:12px;color:var(--text-secondary)}.ghp-empty{color:var(--text-secondary);font-size:12px;margin:0}
.ghp-list{display:flex;flex-direction:column;gap:8px}.ghp-row{display:grid;grid-template-columns:minmax(90px,160px) minmax(0,1fr);gap:4px 12px;align-items:baseline}.ghp-row .ghp-meta{grid-column:2}.ghp-status{font-size:12px;color:var(--text-secondary)}
@media(max-width:700px){.ghp-board{grid-auto-flow:row;grid-auto-columns:auto;overflow-x:visible}.ghp-row{grid-template-columns:minmax(0,1fr)}.ghp-row .ghp-meta{grid-column:1}}
</style>`;

export function mountGithubProjects(container,client,{project='',layout='board'}={}) {
 let disposed=false,projects=null,items=null,projectsError='',itemsError='',selected=text(project),flat=layout==='list',timer=null;
 const held={projects:{query:null,polls:0,full:false},items:{query:null,polls:0,full:false}};
 const syncSignature=body=>JSON.stringify(body?.projects_sync||null);
 const requests=new Map(),listeners=new AbortController();
 const chipEl=()=>container.querySelector('herdr-freshness'),chip=freshnessSources(chipEl);
 container.innerHTML=`${styles}<div class="hd-live-head"><h1>Project boards</h1><herdr-freshness interval="${POLL_MS}"></herdr-freshness></div><p class="ghp-intro">GitHub Projects of the owners in scope, read-only. Cards follow the project's Status field; open an item on GitHub to change it.</p><p class="ghp-warning" data-ghp-sync role="status"></p><p class="ghp-warning" data-ghp-error role="status"></p>
  <div class="ghp-controls"><label>Project<select data-ghp-project aria-label="Project"></select></label><button type="button" class="hd-btn" data-ghp-layout aria-pressed="${flat}">Flat list</button></div><p class="ghp-note" data-ghp-summary role="status"></p>
  <div class="ghp-board" data-ghp-board role="region" aria-label="Project board"></div><div class="ghp-list" data-ghp-list role="region" aria-label="Project items" hidden></div>`;
 const el=selector=>container.querySelector(selector);
 const current=()=>records(projects?.projects).find(p=>p.project_id===selected)||null;
 function placeholder(target,html){
  // patchList ignores unkeyed children, so an empty note or skeleton is swapped by hand.
  if(html===null){for(const child of [...target.children])if(!child.hasAttribute('data-key'))child.remove();return;}
  morph(target,html);
 }
 function patchCards(target,rows,render){
  if(!rows.length)return false;
  placeholder(target,null);
  patchList(target,rows.map(row=>({id:text(row.item_id),html:render(row)})),{key:row=>row.id,render:row=>row.html,update:(node,row)=>morph(node,element(row.html)),inPlace:true});
  return true;
 }
 function drawBoard(project,rows){
  const board=el('[data-ghp-board]'),columns=boardColumns(project,rows)||[];
  const wanted=new Set(columns.map(c=>`col:${c.id}`));
  for(const node of [...board.children])if(!wanted.has(node.dataset.key))node.remove();
  let cursor=board.firstElementChild;
  for(const column of columns){
   const key=`col:${column.id}`;
   let node=[...board.children].find(n=>n.dataset.key===key);
   if(!node){node=element(`<section class="ghp-column" data-key="${esc(key)}" data-ghp-column="${esc(column.id)}"><h2><span data-ghp-column-name></span><small data-ghp-column-count></small></h2><div class="ghp-cards"></div></section>`);}
   if(node!==cursor)board.insertBefore(node,cursor);else cursor=node.nextElementSibling;
   patchText(node.querySelector('[data-ghp-column-name]'),column.name);
   patchText(node.querySelector('[data-ghp-column-count]'),String(column.items.length));
   const cards=node.querySelector('.ghp-cards');
   if(!patchCards(cards,column.items,renderProjectCard))placeholder(cards,'<p class="ghp-empty">No items</p>');
  }
 }
 // A focused card that moves column leaves its old node (patchList); focus follows the item to
 // its new card instead of dropping to the document.
 function followFocus(id){
  if(!id)return;
  const active=document.activeElement,old=active?.closest?.('[data-ghp-item]');
  if(old&&old.isConnected&&!old.hasAttribute('data-leaving'))return;
  const card=[...container.querySelectorAll('[data-ghp-item]:not([data-leaving])')].find(n=>n.dataset.ghpItem===id&&n.offsetParent!==null);
  const target=card?.querySelector('a')||null;
  if(target){target.focus({preventScroll:true});if(old&&old.hasAttribute('data-leaving'))old.remove();}
 }
 function draw(){
  if(disposed)return;
  const focusedItem=document.activeElement?.closest?.('[data-ghp-item]')?.dataset.ghpItem||'';
  render();
  followFocus(focusedItem);
 }
 function render(){
  const note=projectsSyncNote(projects);
  patchText(el('[data-ghp-sync]'),note.text);
  patchText(el('[data-ghp-error]'),[projectsError,itemsError].filter(Boolean).join(' '));
  const list=records(projects?.projects);
  const select=el('[data-ghp-project]');
  morph(select,list.length?list.map(p=>`<option value="${esc(p.project_id)}" ${p.project_id===selected?'selected':''}>${esc(short(p.owner,60))} · ${esc(short(p.title,120)||`#${p.number}`)}${p.closed?' (closed)':''}</option>`).join(''):'<option value="">No projects</option>');
  patchAttr(select,'disabled',!list.length);
  const project=current(),rows=items&&project&&items.project?.project_id===project.project_id?records(items.items):null;
  const boardless=project&&!Array.isArray(project.status_options);
  const showList=flat||boardless;
  patchAttr(el('[data-ghp-layout]'),'aria-pressed',String(showList));
  patchAttr(el('[data-ghp-layout]'),'disabled',!project||boardless);
  patchAttr(el('[data-ghp-board]'),'hidden',showList||!project);
  patchAttr(el('[data-ghp-list]'),'hidden',!showList||!project);
  patchText(el('[data-ghp-summary]'),!projects?'':!project?(note.off?'':'No projects in scope.'):`${project.item_count??0} items${boardless?' · this project has no Status field, so it is shown as a list':''}${project.complete===false&&project.error?` · ${short(project.error,200)}`:''}`);
  if(!projects){morph(el('[data-ghp-board]'),projectsError?'':skeleton('project board',{rows:2,height:64}));return;}
  if(!project){placeholder(el('[data-ghp-board]'),'');placeholder(el('[data-ghp-list]'),'');return;}
  if(rows===null){if(!itemsError)placeholder(showList?el('[data-ghp-list]'):el('[data-ghp-board]'),skeleton('project items',{rows:2,height:48}));return;}
  if(showList){if(!patchCards(el('[data-ghp-list]'),rows.slice().sort(itemOrder),renderProjectRow))placeholder(el('[data-ghp-list]'),'<p class="ghp-empty">This project has no items.</p>');}
  else drawBoard(project,rows);
 }
 // One read with the held revision as `since` when the held body answers the same query; every
 // FULL_RELOAD_POLLS-th read is full. A 304 (or an unplaceable delta's refetch) is handled here.
 async function read(kind,query,signal){
  const mine=held[kind],body=kind==='items'?items:projects;
  const base=mine.query===query&&body&&Number.isInteger(body.revision)?body:null;
  const full=!base||mine.full||(++mine.polls%FULL_RELOAD_POLLS===0);
  const params={project:kind==='items'?selected:undefined,signal};
  const first=await client.getGithubProjects({...params,since:full?undefined:base.revision});
  if(first.notModified&&base)return null;
  if(!full){const merged=applyProjectDelta(kind,base,first.body);if(merged){mine.full=syncSignature(merged)!==syncSignature(base);return merged;}}
  mine.polls=0;mine.full=false;
  return (full?first:await client.getGithubProjects(params)).body;
 }
 async function request(key,load,accept,reject){
  if(disposed||document.hidden||requests.has(key))return;
  const controller=new AbortController();requests.set(key,controller);let quiet=false;
  const deadline=setTimeout(()=>controller.abort(),15000);
  try{const value=await load(controller.signal);if(!disposed&&requests.get(key)===controller)quiet=accept(value)===false;}
  catch(error){if(!disposed&&requests.get(key)===controller)reject(error);}
  finally{clearTimeout(deadline);if(requests.get(key)===controller)requests.delete(key);if(!quiet)draw();}
 }
 function readProjects(){
  request('projects',signal=>read('projects','all',signal),body=>{
   if(body===null){chip.ok('projects');return false;}
   if(!Array.isArray(body?.projects))throw new Error('Invalid projects response');
   projects=body;held.projects.query='all';projectsError='';chip.ok('projects');
   const note=projectsSyncNote(body);chipEl()?.setDetail(note.off?'project sync off':note.text?'projects '+short(note.text,60):'',{warn:note.warn});
   const list=records(body.projects);
   if(!list.some(p=>p.project_id===selected)){selected=(list.find(p=>!p.closed)||list[0])?.project_id||'';items=null;held.items.query=null;remember();if(selected)readItems();}
  },error=>{chip.fail('projects',`Projects: ${error?.message||'request failed'}`);projectsError=`Projects could not refresh.${projects?' Showing the last received projects.':' Retrying automatically.'}`;});
 }
 function readItems(){
  if(!selected)return;
  const query=selected;
  request('items',signal=>read('items',query,signal),body=>{
   if(body===null){chip.ok('items');return false;}
   if(!Array.isArray(body?.items)||body.project?.project_id!==query)throw new Error('Invalid project items response');
   if(query!==selected)return false;
   items=body;held.items.query=query;itemsError='';chip.ok('items');
  },error=>{if(error?.status===404){chip.ok('items');return;}chip.fail('items',`Project items: ${error?.message||'request failed'}`);itemsError=`Project items could not refresh.${items?' Showing the last received items.':' Retrying automatically.'}`;});
 }
 function remember(){
  // The URL names the shown project and layout without re-rendering the route.
  try{const q=new URLSearchParams();if(selected)q.set('project',selected);if(flat)q.set('layout','list');history.replaceState(null,'',`#/github/projects${q.size?`?${q}`:''}`);}catch{}
 }
 function refresh(){readProjects();readItems();}
 el('[data-ghp-project]').addEventListener('change',event=>{
  selected=event.target.value;requests.get('items')?.abort();requests.delete('items');items=null;itemsError='';held.items.query=null;remember();draw();readItems();
 },{signal:listeners.signal});
 el('[data-ghp-layout]').addEventListener('click',()=>{flat=!flat;remember();draw();},{signal:listeners.signal});
 function visibility(){clearInterval(timer);if(document.hidden){for(const c of requests.values())c.abort();requests.clear();return;}refresh();timer=setInterval(refresh,POLL_MS);}
 document.addEventListener('visibilitychange',visibility,{signal:listeners.signal});
 draw();visibility();
 return {refresh,dispose(){disposed=true;listeners.abort();clearInterval(timer);for(const c of requests.values())c.abort();requests.clear();}};
}
