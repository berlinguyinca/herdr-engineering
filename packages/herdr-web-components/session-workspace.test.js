import {test} from 'node:test';
import assert from 'node:assert/strict';
import {selectEvents, linkedAgents, githubLinks, referenceSuggestions, pendingOperation} from '../session-workspace-model.js';

const events = [
  {seq:4,kind:'message',agent_id:'helper',data:{role:'assistant',text:'Fixed'}},
  {seq:3,kind:'tool',agent_id:'helper',data:{tool:'bash',status:'error',output:'boom'}},
  {seq:2,kind:'reasoning',data:{text:'private'}},
  {seq:1,kind:'message',agent_id:'main',data:{role:'user',text:'Please fix this'}},
];
test('conversation reads chronologically and never includes tools or reasoning',()=>{
  assert.deepEqual(selectEvents(events,{pane:'conversation'}).map(e=>e.seq),[1,4]);
  assert.deepEqual(selectEvents(events,{pane:'activity'}).map(e=>e.seq),[3]);
  assert.deepEqual(selectEvents(events,{pane:'conversation',agent:'helper'}).map(e=>e.seq),[4]);
  assert.deepEqual(selectEvents(events,{pane:'activity',errors:true,q:'bash'}).map(e=>e.seq),[3]);
  assert.deepEqual(selectEvents(events,{pane:'activity',type:'file'}),[]);
  assert.deepEqual(selectEvents(events,{pane:'conversation',type:'file',errors:true}).map(e=>e.seq),[1,4]);
});
test('helpers join only through explicit ancestry and retain session navigation',()=>{
  const sessions=[{session_id:'child',parent_session_id:'parent',agent_role:'reviewer'},
    {session_id:'unrelated',cwd:'/same',agent_role:'reviewer'},
    {session_id:'parent',parent_session_id:'root',agent_role:'main'}];
  const agents=linkedAgents({session_id:'parent'},[{agent_id:'helper',parent_agent_id:'main',events:3}],sessions);
  assert.equal(agents.find(a=>a.session_id==='child').relation,'helper');
  assert.equal(agents.find(a=>a.session_id==='root').relation,'parent');
  assert.ok(agents.find(a=>a.agent_id==='helper'));
  assert.ok(!agents.find(a=>a.session_id==='unrelated'));
});
test('GitHub links require explicit safe identity and full commit sha',()=>{
  const links=githubLinks({repository_id:'github.com/owner/repo',commit_sha:'a'.repeat(40)},
    {pr:{repo:'owner/repo',number:9,url:'javascript:alert(1)'},issue:{repo:'owner/repo',number:12}});
  assert.ok(links.some(x=>x.href==='https://github.com/owner/repo/commit/'+'a'.repeat(40)));
  assert.ok(links.some(x=>x.href==='https://github.com/owner/repo/pull/9'));
  assert.ok(links.some(x=>x.href==='https://github.com/owner/repo/issues/12'));
  assert.deepEqual(githubLinks({cwd:'/owner/repo',commit_sha:'abc'},{pr:{url:'javascript:bad'}}),[]);
  assert.deepEqual(githubLinks({repository_id:'github.com/owner/repo/../../evil',commit_sha:'a'.repeat(40)},{}),[]);
});
test('reference completion includes only observed files, linked agents and explicit GitHub links',()=>{
  const refs=referenceSuggestions({session_id:'s'}, {summary:{files:[{path:'src/app.js'}],agents:[{agent_id:'helper',parent_agent_id:'main'}],association:{}}},[]);
  assert.ok(refs.some(x=>x.insertText.includes('src/app.js')));
  assert.ok(refs.some(x=>x.insertText==='@helper'));
  assert.ok(!refs.some(x=>x.insertText.includes('unobserved')));
});

test('Collaboration helpers cross-link only explicitly associated instances',()=>{
  const sessions=[{session_id:'s',host_id:'h',pane_id:'w1:p1'},{session_id:'child-session',host_id:'h',pane_id:'w1:p2'}];
  const collab=[{agent_id:'c-main',name:'coder',instance_id:'s'},
    {agent_id:'c-helper',name:'reviewer',parent_agent_id:'c-main',instance_id:'pane:h:w1:p2'},
    {agent_id:'other',name:'unrelated',instance_id:'other-session'}];
  const agents=linkedAgents(sessions[0],[],sessions,collab);
  assert.equal(agents.find(a=>a.collab_agent_id==='c-helper').session_id,'child-session');
  assert.equal(agents.find(a=>a.collab_agent_id==='c-helper').relation,'helper');
  assert.ok(!agents.find(a=>a.collab_agent_id==='other'));
});

test('ambiguous send retries retain operation identity across new mounts',()=>{
  const records=new Map();const storage={getItem:key=>records.get(key),setItem:(key,value)=>records.set(key,value)};
  let counter=0;const generate=()=>`operation-${++counter}`;
  assert.equal(pendingOperation(storage,'session:s','hello',generate),'operation-1');
  assert.equal(pendingOperation(storage,'session:s','hello',generate),'operation-1');
  assert.equal(pendingOperation(storage,'session:s','different',generate),'operation-2');
  assert.equal(pendingOperation(storage,'session:other','different',generate),'operation-3');
});
