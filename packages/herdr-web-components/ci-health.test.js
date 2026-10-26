import test from 'node:test';
import assert from 'node:assert/strict';
import {ciHealthModel,renderCiHeatmap,renderCiTree,ciHealthStyles} from '../ci-health.js';
const now=Date.parse('2026-10-22T12:30:00Z');
const repo=(id=1,extra={})=>({id,repository:`org/app${id}`,default_branch:'main',provider_url:'https://ci.example',forge_url:'https://github.com',...extra});
const run=(n,state='success',extra={})=>({provider_repo_id:1,provider_url:'https://ci.example',repository:'org/app1',run_number:n,branch:'main',state,created_at:'2026-10-22T12:00:00Z',...extra});
const expandedTree=m=>({expanded:new Set(m.selectedRows.flatMap(row=>[row.projectKey,JSON.stringify([row.projectKey,row.pr?`pr:${row.pr.number}`:row.isPr?'pr:unknown':`branch:${row.branch||''}`])]))});
const history=(pipelines=[],extra={})=>({repos:[repo()],pipelines,coverage:{'1':{exhausted:true}},...extra});
test('hours cover exact trailing24h split at UTC boundaries including both partial cells',()=>{
 const m=ciHealthModel(history([run(1,'failure',{created_at:'2026-10-21T12:30:00Z'}),run(2,'success',{created_at:'2026-10-21T12:29:59Z'}),run(3,'success',{created_at:'2026-10-22T12:30:01Z'})]),{mode:'hours'},now);
 assert.equal(m.buckets.length,25);assert.equal(m.buckets[0].start,now-86400000);assert.equal(m.buckets.at(-1).end,now);
 assert.equal(m.projects[0].runs.length,1);assert.equal(m.projects[0].cells[0].counts.failure,1);
 assert.equal(ciHealthModel(history(),{mode:'hours'},Date.parse('2026-10-22T12:00Z')).buckets.length,24);
});
test('calendar modes include UTC today and last-runs ignores date cutoff after filters',()=>{
 for(const mode of ['7','14','30']) {const m=ciHealthModel(history(),{mode},now);assert.equal(m.buckets.length,+mode);assert.equal(m.buckets.at(-1).label,'2026-10-22');}
 const old=Array.from({length:30},(_,i)=>run(i,'success',{created_at:`2025-01-${String(i%28+1).padStart(2,'0')}T12:00:00Z`,branch:i<5?'other':'main'}));
 const m=ciHealthModel(history(old),{mode:'runs'},now);assert.equal(m.projects[0].runs.length,24);assert.equal(m.projects[0].cells.length,24);assert.equal(m.projects[0].cells.at(-1).runs.length,1);
 const sparse=ciHealthModel(history([old[5]]),{mode:'runs'},now);assert.equal(sparse.projects[0].cells[0].runs.length,0);assert.equal(sparse.projects[0].cells.at(-1).runs.length,1);
});
test('selected current UTC day keeps calendar bounds and includes new runs after refresh',()=>{
 const data=history([run(1,'failure',{created_at:'2026-10-22T12:00:00Z'}),run(2,'success',{created_at:'2026-10-22T13:00:00Z'}),run(3,'success',{created_at:'2026-10-23T00:00:00Z'})]);
 for(const mode of ['7','14','30']) {
  const first=ciHealthModel(data,{mode},now),project=first.projects[0],cell=project.cells.at(-1);
  assert.deepEqual(cell.runs.map(r=>r.run_number),[1]);
  assert.equal(cell.start,Date.parse('2026-10-22T00:00:00Z'));
  assert.equal(cell.end,Date.parse('2026-10-23T00:00:00Z'));
  const selection={projectKey:project.key,bucketKey:cell.key};
  const refreshed=ciHealthModel(data,{mode,selection},Date.parse('2026-10-22T14:00:00Z'));
  assert.deepEqual(refreshed.selectedRows.map(r=>r.run_number),[1,2]);
  assert.equal(refreshed.projects[0].cells.at(-1).end,cell.end);
  const midnight=ciHealthModel(data,{mode,selection},Date.parse('2026-10-23T00:00:00Z'));
  assert.deepEqual(midnight.selectedRows.map(r=>r.run_number),[1,2]);
  assert.deepEqual(midnight.projects[0].cells.at(-1).runs.map(r=>r.run_number),[3]);
  const tomorrow=ciHealthModel(data,{mode,selection},Date.parse('2026-10-23T01:00:00Z'));
  assert.deepEqual(tomorrow.selectedRows.map(r=>r.run_number),[1,2]);
 }
});
test('deduplicates provider identities without conflating providers or repository ids',()=>{
 const m=ciHealthModel(history([run(1,'running'),run(1,'success'),run(1,'failure',{provider_url:'https://other'}),run(1,'failure',{provider_repo_id:2,repository:'org/app2'})],{repos:[repo(),repo(1,{provider_url:'https://other'}),repo(2)]}),{scope:'all'},now);
 assert.equal(m.projects.length,3);assert.equal(m.selectedRows.length,3);assert.equal(new Set(m.selectedRows.map(r=>r.runKey)).size,3);assert.equal(m.counts.running,0);
});
test('duplicate repository names keep deterministic provider order',()=>{
 const repos=[repo(2,{repository:'org/shared',provider_url:'https://z.example'}),repo(1,{repository:'org/shared',provider_url:'https://a.example'})];
 const m=ciHealthModel(history([],{repos,coverage:{'https://z.example|2':{exhausted:true},'https://a.example|1':{exhausted:true}}}),{scope:'all'},now);
 assert.deepEqual(m.projects.map(project=>project.identity),['org/shared · https://a.example #1','org/shared · https://z.example #2']);
});
test('only colliding repository labels gain the shortest distinguishing provider host suffix',()=>{
 const repos=[repo(1,{repository:'org/shared',provider_url:'https://ci.alpha.example'}),repo(2,{repository:'org/shared',provider_url:'https://ci.beta.example'}),repo(3,{repository:'org/unique'})];
 const m=ciHealthModel(history([],{repos}),{scope:'all'},now);
 assert.deepEqual(m.projects.map(p=>p.label),['org/shared · alpha.example','org/shared · beta.example','org/unique']);
 assert.deepEqual(m.projects.map(p=>p.key),['["https://ci.alpha.example","1"]','["https://ci.beta.example","2"]','["https://ci.example","3"]']);
 assert.equal(m.projects[0].identity,'org/shared · https://ci.alpha.example #1');
 const html=renderCiHeatmap(m);
 assert.match(html,/>org\/shared · alpha\.example<\/th>/);
 assert.match(html,/title="org\/shared · https:\/\/ci\.alpha\.example #1"/);
 const reversed=ciHealthModel(history([],{repos:[...repos].reverse()}),{scope:'all'},now);
 assert.deepEqual(reversed.projects.map(p=>[p.key,p.label]),m.projects.map(p=>[p.key,p.label]));
 const filtered=ciHealthModel(history([],{repos}),{scope:'all',repository:m.projects[0].key},now);
 assert.equal(filtered.projects[0].label,'org/shared · alpha.example','filtering must not change an established project label');
});
test('same-host provider collisions retain ports, paths and repository ids when needed',()=>{
 const repos=[repo(1,{repository:'org/shared',provider_url:'https://ci.example/team-a/'}),repo(2,{repository:'org/shared',provider_url:'https://ci.example/team-b'}),repo(3,{repository:'org/shared',provider_url:'https://ci.example/team-b'}),repo(4,{repository:'org/shared',provider_url:'https://ci.example:8443'})];
 const m=ciHealthModel(history([],{repos}),{scope:'all'},now);
 assert.equal(new Set(m.projects.map(p=>p.label)).size,4);
 assert.deepEqual(Object.fromEntries(m.projects.map(p=>[p.repo.id,p.label])),{1:'org/shared · ci.example/team-a',2:'org/shared · ci.example/team-b #2',3:'org/shared · ci.example/team-b #3',4:'org/shared · ci.example:8443'});
 assert.equal(m.projects.find(p=>p.repo.id===1).key,'["https://ci.example/team-a","1"]');
});
test('mixed preserves completed failures and canceled/skipped/unknown remain neutral',()=>{
 const m=ciHealthModel(history([run(1,'failure'),run(2,'success'),run(3,'running'),run(4,'cancelled'),run(5,'skipped'),run(6,'unknown')]),{},now);
 const c=m.projects[0].cells.at(-1);assert.equal(c.state,'mixed');assert.equal(c.active,true);assert.equal(c.counts.failure,1);assert.equal(c.counts.canceled,1);
 for(const state of ['canceled','skipped','unknown'])assert.equal(ciHealthModel(history([run(1,state)]),{},now).projects[0].cells.at(-1).state,'neutral');
});
test('coverage distinguishes known empty from unknown, caps, and missing default branch',()=>{
 assert.equal(ciHealthModel(history(),{},now).projects[0].cells[0].state,'empty');
 const m=ciHealthModel(history([],{coverage:{'1':{complete_since:'2026-10-21T00:00:00Z',truncated:true}}}),{mode:'7'},now);
 assert.equal(m.projects[0].cells[0].complete,false);assert.equal(m.projects[0].cells[0].state,'incomplete');assert.equal(m.projects[0].cells.at(-1).complete,true);
 assert.equal(ciHealthModel(history([],{coverage:{}}),{},now).projects[0].complete,false);
 assert.equal(ciHealthModel(history([run(1)],{repos:[repo(1,{default_branch:''})]}),{},now).projects[0].scopeUnavailable,true);
 assert.equal(ciHealthModel(history([],{repos:[repo(),repo(2)]}),{},now).projects.length,2);
});
test('PR scope requires explicit identity or event and validates exact repository forge URLs',()=>{
 const rows=[run(1,'success',{event:'pull_request',branch:'main'}),run(2,'success',{ref:'refs/pull/42/head',branch:'feature'}),run(3,'success',{branch:'pr/43'}),run(4,'success',{event:'pull_request',ref:'feature'}),run(5,'success',{event:'pull_request',ref:'https://github.com/org/app1/pull/44'}),run(6,'success',{event:'pull_request',ref:'https://github.com/org/other/pull/44'})];
 const d=ciHealthModel(history(rows),{},now);assert.deepEqual(d.selectedRows.map(r=>r.run_number),[]);assert.equal(ciHealthModel(history(rows),{scope:'all'},now).selectedRows.find(r=>r.run_number===3).pr,null);
 const m=ciHealthModel(history(rows),{scope:'pr'},now);assert.equal(m.selectedRows.length,5);assert.equal(m.selectedRows.find(r=>r.run_number===2).pr.number,42);assert.equal(m.selectedRows.find(r=>r.run_number===5).pr.number,44);assert.equal(m.selectedRows.find(r=>r.run_number===6).pr,null);
 assert.match(renderCiTree(m,expandedTree(m)),/PR identity unavailable/);assert.doesNotMatch(renderCiTree(m,expandedTree(m)),/PR #43/);
});
test('selection narrows already filtered window and parent latest is separate from failure totals',()=>{
 const data=history([run(1,'failure',{created_at:'2026-10-22T10:00Z'}),run(2,'success',{created_at:'2026-10-22T11:00Z'}),run(3,'failure',{branch:'other'})]);
 const initial=ciHealthModel(data,{},now),p=initial.projects[0];const m=ciHealthModel(data,{selection:{projectKey:p.key,bucketKey:p.cells.at(-1).key}},now);
 assert.equal(m.selectedRows.length,2);const html=renderCiTree(m);assert.match(html,/Latest within selection: success/);assert.match(html,/1\/2 failed/);
 assert.equal(ciHealthModel(data,{branch:'missing',selection:m.selection},now).selectedRows.length,0);
});
test('renderers escape content, expose exact counts and native disclosure hooks with bounded grid',()=>{
 const m=ciHealthModel(history([run(1,'failure',{branch:'<img src=x>',title:'<script>x</script>'})],{repos:[repo(1,{repository:'<svg onload=x>'})]}),{scope:'all'},now);
 const html=renderCiHeatmap(m)+renderCiTree(m,expandedTree(m));assert.doesNotMatch(html,/<script>|<img|<svg/);assert.match(html,/data-health-project=/);assert.match(html,/data-health-bucket=/);assert.match(html,/data-health-run=/);assert.match(html,/<details[^>]*data-health-disclosure=/);assert.match(html,/Exact counts/);assert.match(ciHealthStyles,/overflow.*auto/);assert.match(ciHealthStyles,/sticky/);
});
test('heatmap keeps provider identity accessible without repeating it in every visual row label',()=>{
 const m=ciHealthModel(history([run(1,'failure')],{repos:[repo(1,{repository:'org/app',provider_url:'https://ci.example'})]}),{scope:'all'},now);
 const html=renderCiHeatmap(m);
 const visible=html.match(/<th scope="row" class="ci-health-project"[^>]*>([^<]*)<\/th>/)?.[1];
 assert.equal(visible,'org/app');
 assert.match(html,/aria-label="org\/app · https:\/\/ci\.example #1/);
 assert.match(html,/Exact counts/);
 assert.match(html,/org\/app · https:\/\/ci\.example #1/);
});
test('actual repository forge URL and row forge PR URL establish identity only for exact repository',()=>{
 const data=history([run(1,'success',{event:'pull_request',forge_url:'https://github.com/org/app1/pull/1200'}),run(2,'success',{event:'pull_request',forge_url:'https://github.com/org/app10/pull/1200'}),run(3,'success',{event:'pull_request',forge_url:'https://evil.example/org/app1/pull/1200'})],{repos:[repo(1,{forge_url:'https://github.com/org/app1'})]});
 const m=ciHealthModel(data,{scope:'pr'},now);assert.equal(m.selectedRows[0].pr.number,1200);assert.equal(m.selectedRows[1].pr,null);assert.equal(m.selectedRows[2].pr,null);
});
test('run buttons expose reported duration and PR metadata without inventing roles',()=>{
 const m=ciHealthModel(history([run(1,'failure',{pr_number:7,pull_request:{number:7,title:'Fix <bad>',source_branch:'topic',target_branch:'main'},started_at:'2026-10-22T12:00Z',finished_at:'2026-10-22T12:02Z'})]),{scope:'all'},now);
 const html=renderCiTree(m,expandedTree(m));assert.match(html,/2m/);assert.match(html,/Fix &lt;bad&gt;/);assert.match(html,/topic → main/);
});
test('partial-hour keys and exact-count disclosure stay stable through polling',()=>{
 const first=ciHealthModel(history(),{mode:'hours'},now),next=ciHealthModel(history(),{mode:'hours'},now+5000);
 assert.deepEqual(next.buckets.map(b=>b.key),first.buckets.map(b=>b.key));
 const html=renderCiHeatmap(first);assert.match(html,/data-health-disclosure="exact-counts"/);assert.equal((html.match(/data-health-grid-scroll/g)||[]).length,2);
});
test('missing timestamps keep coverage incomplete while newer contiguous coverage can remain complete',()=>{
 const missing=ciHealthModel(history([run(1,'failure',{created_at:null})]),{},now);assert.equal(missing.projects[0].complete,false);assert.equal(missing.projects[0].unknownTimestamps,true);
 const m=ciHealthModel(history([],{coverage:{'1':{complete_since:'2026-10-22T00:00Z',truncated:true,error:'Older page unavailable'}}}),{},now);
 assert.equal(m.projects[0].cells.at(-1).complete,true);assert.equal(m.projects[0].cells[0].complete,false);assert.equal(m.partial,true);
});
test('last-run cell tooltip exposes actual run metadata and duration',()=>{
 const m=ciHealthModel(history([run(987,'failure',{revision:'abcdef123',branch:'main',started_at:'2026-10-22T12:00Z',finished_at:'2026-10-22T12:02Z'})]),{mode:'runs'},now);
 const html=renderCiHeatmap(m);assert.match(html,/Run #987/);assert.match(html,/abcdef123/);assert.match(html,/main/);assert.match(html,/2m 0s/);
});
test('last-run selections follow provider run identity through insertions and never retarget after eviction',()=>{
 const runs=Array.from({length:24},(_,i)=>run(i+1,'success',{created_at:new Date(now-(24-i)*60000).toISOString()}));
 const initial=ciHealthModel(history(runs),{mode:'runs'},now),project=initial.projects[0];
 const selection={projectKey:project.key,bucketKey:project.cells[1].key};
 assert.equal(project.cells[1].runs[0].run_number,2);
 const newest=run(25,'failure',{created_at:new Date(now).toISOString()});
 const updated=ciHealthModel(history([...runs,newest]),{mode:'runs',selection},now);
 assert.deepEqual(updated.selectedRows.map(row=>row.run_number),[2]);assert.equal(updated.projects[0].cells.length,24);
 const evicted=ciHealthModel(history([...runs,newest,run(26,'success',{created_at:new Date(now).toISOString()})]),{mode:'runs',selection},now);
 assert.deepEqual(evicted.selectedRows,[]);assert.equal(evicted.selection.bucketKey,selection.bucketKey);
});
test('canonical repository annotation filters the matching inventory without false empty counts',()=>{
 const canonical='https://github.com/org/app1';
 const data=history([run(1,'failure'),run(2,'success',{provider_repo_id:2,repository:'org/app2'})],{repos:[repo(1,{repository_id:canonical}),repo(2,{repository_id:'https://github.com/org/app2'})],coverage:{'1':{exhausted:true},'2':{exhausted:true}}});
 const m=ciHealthModel(data,{repository:canonical},now);assert.equal(m.projects.length,1);assert.equal(m.selectedRows.length,1);assert.equal(m.counts.failure,1);assert.equal(m.projects[0].complete,true);
 assert.equal(ciHealthModel(data,{repository:canonical,branch:'other'},now).selectedRows.length,0);
});
test('tree lazily renders expanded descendants and bounds open group runs without truncating totals',()=>{
 const m=ciHealthModel(history(Array.from({length:250},(_,i)=>run(i+1,i%2?'success':'failure'))),{},now);
 const project=m.projects[0].key,group=JSON.stringify([project,'branch:main']);
 const closed=renderCiTree(m);assert.equal((closed.match(/data-health-run=/g)||[]).length,0);assert.match(closed,/125\/250 failed/);
 const projectOpen=renderCiTree(m,{expanded:new Set([project])});assert.equal((projectOpen.match(/data-health-run=/g)||[]).length,0);assert.match(projectOpen,/Branch main/);
 const groupOpen=renderCiTree(m,{expanded:new Set([project,group])});assert.equal((groupOpen.match(/data-health-run=/g)||[]).length,100);assert.match(groupOpen,/data-health-tree-more=/);assert.match(groupOpen,/150 remaining/);assert.match(groupOpen,/125\/250 failed/);
 const more=renderCiTree(m,{expanded:new Set([project,group]),limits:new Map([[group,200]])});assert.equal((more.match(/data-health-run=/g)||[]).length,200);assert.match(more,/50 remaining/);
});
test('coverage diagnostics distinguish provider errors, safety limits and inventory gaps with safe escaping',()=>{
 const m=ciHealthModel(history([],{coverage:{'1':{error:'Rate limited <script>bad</script>',truncated:true}},scope:{inventory_error:'Discovery failed <img src=x>'}}),{},now);
 const html=renderCiHeatmap(m);assert.match(html,/Rate limited &lt;script&gt;bad&lt;\/script&gt;/);assert.match(html,/Provider safety limit reached/);assert.match(html,/Discovery failed &lt;img src=x&gt;/);assert.doesNotMatch(html,/<script>|<img/);assert.match(html,/data-health-disclosure="coverage-issues"/);
});

test('CI-health styles take every colour from theme tokens, so the dark palette applies', async () => {
  const { ciHealthStyles } = await import('../ci-health.js');
  // Strip var(--token, #fallback) fallbacks; whatever hex remains is a hard-coded light colour.
  const bare = ciHealthStyles.replace(/var\(--[\w-]+,[^)]*\)/g, '');
  assert.deepEqual(bare.match(/#[0-9a-f]{3,8}\b|rgba?\(/gi) || [], []);
  assert.match(ciHealthStyles, /\.state-failure\{background:color-mix\(in srgb,var\(--status-failed\)/);
});
