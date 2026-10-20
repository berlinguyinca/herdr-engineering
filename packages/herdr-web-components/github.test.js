import test from 'node:test';
import assert from 'node:assert/strict';
import {rankGithubRepositories,githubIssueLabel,renderGithubHealth,renderGithubRepository,renderGithubIssue,renderGithubDetail} from '../github-view.js';

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
