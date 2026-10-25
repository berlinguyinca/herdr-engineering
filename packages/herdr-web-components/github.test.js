import test from 'node:test';
import assert from 'node:assert/strict';
import {rankGithubRepositories,githubIssueLabel,renderGithubHealth,renderGithubRepository,renderGithubIssue,renderGithubDetail,applyGithubDelta,githubSyncSignature} from '../github-view.js';

test('GitHub repositories rank known issue-only counts first without fabricating missing counts',()=>{
 const rows=[{name_with_owner:'lab/unknown',open_issue_count:null},{name_with_owner:'lab/empty',open_issue_count:0},{name_with_owner:'lab/busy',open_issue_count:40},{name_with_owner:'lab/small',open_issue_count:3}];
 assert.deepEqual(rankGithubRepositories(rows).map(r=>r.name_with_owner),['lab/busy','lab/small','lab/empty','lab/unknown']);
 assert.match(renderGithubRepository(rows[0]),/Count not reported/);
 assert.match(renderGithubRepository({...rows[2],issues_loaded:10,issues_complete:false}),/10 loaded/);
});
test('issue age never hides an open issue or becomes stale telemetry',()=>{
 const issue={repository_id:'github.com/lab/repo',number:7,title:'Old but open',state:'OPEN',updated_at:'2001-01-01T00:00:00Z',labels:[],autospec:{managed:false,state:'unmanaged',eligibility:'unknown'}};
 const html=renderGithubIssue(issue);
 assert.match(html,/Old but open/);assert.doesNotMatch(html,/stale|expired|finished/i);
});
test('AutoSpec labels remain evidence and do not claim runnable readiness',()=>{
 assert.match(githubIssueLabel({autospec:{managed:true,state:'candidate',eligibility:'unknown'}}),/not verified/i);
 assert.match(githubIssueLabel({autospec:{managed:true,state:'blocked',eligibility:'blocked'}}),/blocked/i);
 const html=renderGithubDetail({issue:{repository_id:'github.com/lab/repo',number:1,title:'Plan',autospec:{managed:true,state:'candidate',eligibility:'unknown',reasons:['Spec missing'],evidence_source:'github_labels'},body:'Review the spec.'}});
 assert.match(html,/Spec missing/);assert.match(html,/GitHub labels/);
 assert.doesNotMatch(html,/Start AutoSpec|Run issue|Ready to run/);
});
test('GitHub synchronization shows measured coverage and explicit auth/partial failures',()=>{
 const html=renderGithubHealth({status:'syncing',refreshing:true,partial:true,progress:{phase:'issues',issue_repositories_complete:2,issue_repositories_total:8,current_repository:'lab/repo',issues_seen:30}});
 assert.match(html,/2 of 8/);assert.match(html,/<progress value="2" max="8"/);assert.match(html,/Partial coverage/);
 assert.doesNotMatch(renderGithubHealth({status:'syncing',refreshing:true,progress:{phase:'repositories',repositories_seen:4}}),/value="4"/);
 assert.match(renderGithubHealth({status:'auth_error',error:'Credentials rejected'}),/authentication/i);
});
test('GitHub text and body are escaped and unsafe links never become anchors',()=>{
 const issue={repository_id:'github.com/lab/repo',number:4,title:'<script>bad()</script>',url:'javascript:bad()',labels:['<img src=x>'],body:'<iframe src=x>',autospec:{managed:false}};
 const html=renderGithubIssue(issue)+renderGithubDetail({issue});
 assert.doesNotMatch(html,/<script>|<iframe|<img|href="javascript/);assert.match(html,/&lt;iframe/);
 assert.doesNotMatch(renderGithubDetail({issue:{...issue,url:'https://user:secret@example.test'}}),/Open on GitHub/);
});

// ---- since deltas -------------------------------------------------------------
const row=(n,updated,extra={})=>({github_id:`I${n}`,repository_id:'github.com/lab/r',number:n,title:`Issue ${n}`,updated_at:updated,...extra});
const held={status:'ready',revision:5,issues:[row(3,'2026-01-03'),row(2,'2026-01-02'),row(1,'2026-01-01')],pagination:{page:1,per_page:20,total:3,has_next:false},counts:{all:3}};
test('an empty delta keeps the rows and takes the new sync state and revision',()=>{
 const next=applyGithubDelta('issues',held,{revision:5,resync:false,upserted:[],removed:[],status:'syncing',counts:{all:3}});
 assert.deepEqual(next.issues,held.issues);assert.equal(next.status,'syncing');assert.equal(next.revision,5);
});
test('a delta changing one issue on the page replaces that row and keeps server order',()=>{
 const next=applyGithubDelta('issues',held,{revision:6,resync:false,upserted:[row(1,'2026-01-09',{title:'Edited'})],removed:[],counts:{all:3}});
 assert.deepEqual(next.issues.map(i=>i.number),[1,3,2]);assert.equal(next.issues[0].title,'Edited');assert.equal(next.revision,6);
 assert.equal(next.issues[1],held.issues[0],'unchanged rows are the same objects');
});
test('a delta the page cannot place asks for a full reload',()=>{
 assert.equal(applyGithubDelta('issues',held,{revision:6,resync:false,upserted:[row(9,'2026-01-09')],removed:[]}),null,'a row new to the page');
 assert.equal(applyGithubDelta('issues',held,{revision:6,resync:false,upserted:[],removed:[{repository_id:'github.com/lab/r',key:'I2'}]}),null,'a removal');
 assert.equal(applyGithubDelta('issues',{...held,pagination:{page:2,per_page:3,total:9,has_next:true}},{revision:6,resync:false,upserted:[row(2,'2026-01-09')],removed:[]},{page:2,perPage:3}),null,'a later page');
 const full={...held,pagination:{page:1,per_page:3,total:9,has_next:true}};
 assert.equal(applyGithubDelta('issues',full,{revision:6,resync:false,upserted:[row(3,'2025-01-01')],removed:[]},{perPage:3}),null,'a row that may now belong to the next page');
});
test('resync replaces the list with the current page of the full list',()=>{
 const all=Array.from({length:45},(_,i)=>row(45-i,`2026-02-${String(45-i).padStart(2,'0')}`));
 const next=applyGithubDelta('issues',held,{revision:40,resync:true,issues:all,status:'ready'},{page:2,perPage:20});
 assert.deepEqual(next.issues.map(i=>i.number),all.slice(20,40).map(i=>i.number));
 assert.deepEqual(next.pagination,{page:2,per_page:20,total:45,has_next:true});assert.equal(next.revision,40);
 assert.equal('resync' in next,false);
});
test('repository deltas match by repository id and keep the open-count order',()=>{
 const repo=(name,count)=>({github_id:`R${name}`,repository_id:`github.com/lab/${name}`,name_with_owner:`lab/${name}`,open_issue_count:count});
 const repos={revision:2,repositories:[repo('a',9),repo('b',5)],pagination:{page:1,per_page:20,total:2,has_next:false}};
 const next=applyGithubDelta('repositories',repos,{revision:3,resync:false,upserted:[repo('b',12)],removed:[]});
 assert.deepEqual(next.repositories.map(r=>[r.name_with_owner,r.open_issue_count]),[['lab/b',12],['lab/a',9]]);
 assert.equal(applyGithubDelta('repositories',repos,{revision:3,resync:false,upserted:[],removed:[{repository_id:'github.com/lab/a',key:'github.com/lab/a'}]}),null);
});
test('the sync signature moves with status, error, progress and the chip state, not with the clock',()=>{
 const base={status:'ready',refreshing:false,stale:false,partial:false,error:null,progress:{},last_success_at:'2026-01-01T00:00:00Z'};
 assert.equal(githubSyncSignature(base),githubSyncSignature({...base,last_success_at:'2026-01-01T00:05:00Z'}));
 for(const change of [{status:'syncing'},{refreshing:true},{error:'x'},{progress:{issue_repositories_complete:3}},{stale:true},{partial:true}])
  assert.notEqual(githubSyncSignature(base),githubSyncSignature({...base,...change}),JSON.stringify(change));
});
test('the health line names the owners in scope and the excluded repository count',()=>{
 const html=renderGithubHealth({status:'ready',scope:{owners:['lab','<b>other</b>'],excluded_repositories:3,restricted:true}});
 assert.match(html,/2 owners in scope: lab, &lt;b&gt;other&lt;\/b&gt;/);assert.match(html,/3 repositories excluded/);
 const many=renderGithubHealth({status:'ready',scope:{owners:Array.from({length:12},(_,i)=>`o${i}`),excluded_repositories:0,restricted:true}});
 assert.match(many,/12 owners in scope: o0, o1, o2, o3, o4, o5, o6, o7 and 4 more/);assert.match(many,/0 repositories excluded/);
 assert.doesNotMatch(renderGithubHealth({status:'ready'}),/owners in scope/);
});
test('an issue row is keyed by its GitHub id',()=>{
 assert.match(renderGithubIssue(row(4,'2026-01-01')),/data-key="I4"/);
 assert.match(renderGithubIssue({repository_id:'github.com/lab/r',number:4}),/data-key="github.com\/lab\/r#4"/);
});
