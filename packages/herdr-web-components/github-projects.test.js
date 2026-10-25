import test from 'node:test';
import assert from 'node:assert/strict';
import {boardColumns,applyProjectDelta,renderProjectCard,renderProjectRow,projectsSyncNote} from '../github-projects-view.js';

const options=[{id:'o1',name:'Todo'},{id:'o2',name:'In progress'},{id:'o3',name:'Done'}];
const item=(id,status_option,extra={})=>({item_id:id,status_option,status:options.find(o=>o.id===status_option)?.name??null,title:`Item ${id}`,type:'ISSUE',position:Number(id.slice(1)),...extra});

test('the board has one column per Status option in field order, plus No status, and keeps empty columns',()=>{
 const cols=boardColumns({status_options:options},[item('i2','o3'),item('i1','o1'),item('i3',null),item('i4','gone')]);
 assert.deepEqual(cols.map(c=>[c.id,c.name,c.items.map(i=>i.item_id)]),[['o1','Todo',['i1']],['o2','In progress',[]],['o3','Done',['i2']],['','No status',['i3','i4']]]);
});
test('a board without items without status has no No status column; a project without a Status field has none',()=>{
 assert.deepEqual(boardColumns({status_options:options},[item('i1','o1')]).map(c=>c.id),['o1','o2','o3']);
 assert.equal(boardColumns({status_options:null},[item('i1','o1')]),null);
});
test('a delta replaces and removes items by id and keeps board order; resync replaces the list',()=>{
 const held={revision:3,items:[item('i1','o1'),item('i2','o1'),item('i3','o2')]};
 const next=applyProjectDelta('items',held,{revision:4,resync:false,upserted:[item('i2','o3')],removed:[{project_id:'P',key:'i3'}],project:{project_id:'P'}});
 assert.deepEqual(next.items.map(i=>[i.item_id,i.status_option]),[['i1','o1'],['i2','o3']]);
 assert.equal(next.revision,4);assert.equal(next.items[0],held.items[0]);assert.equal('upserted' in next,false);
 const added=applyProjectDelta('items',held,{revision:5,resync:false,upserted:[item('i0','o1')],removed:[]});
 assert.deepEqual(added.items.map(i=>i.item_id),['i0','i1','i2','i3']);
 const resync=applyProjectDelta('items',held,{revision:9,resync:true,items:[item('i7','o1')]});
 assert.deepEqual(resync.items.map(i=>i.item_id),['i7']);assert.equal(resync.revision,9);assert.equal('resync' in resync,false);
 const projects=applyProjectDelta('projects',{projects:[{project_id:'A',owner:'acme',number:2},{project_id:'B',owner:'acme',number:1}]},{revision:2,resync:false,upserted:[{project_id:'C',owner:'acme',number:3,title:'New'}],removed:[{project_id:'A',key:'A'}]});
 assert.deepEqual(projects.projects.map(p=>p.project_id),['B','C']);
});
test('cards and rows are read-only, escaped and keyed by item id',()=>{
 const card=renderProjectCard(item('i1','o1',{title:'<img src=x>',url:'https://github.com/acme/app/issues/7',repository:'acme/app',number:7}));
 assert.match(card,/data-key="i1"/);assert.match(card,/&lt;img src=x&gt;/);assert.match(card,/acme\/app #7/);
 assert.doesNotMatch(card,/<button|<input|<select/);
 assert.match(renderProjectRow(item('i2','o3')),/data-key="i2"[\s\S]*Done/);
 assert.match(renderProjectCard(item('i3',null,{title:null,type:'REDACTED'})),/Item not visible to this token/);
 assert.doesNotMatch(renderProjectCard(item('i4','o1',{url:'javascript:alert(1)'})),/javascript:/);
});
test('the sync note says when Projects sync is off or partial',()=>{
 assert.match(projectsSyncNote({projects_sync:{status:'disabled'}}).text,/github\.projects\.enabled/);
 assert.equal(projectsSyncNote({projects_sync:{status:'ready'}}).warn,false);
 const partial=projectsSyncNote({projects_sync:{status:'partial',deferred:2,error:'budget'}});
 assert.equal(partial.warn,true);assert.match(partial.text,/2 projects deferred/);
});
