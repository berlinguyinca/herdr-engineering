import test from 'node:test';
import assert from 'node:assert/strict';
import {renderCiPipeline,ciPipelineStyles} from '../ci-pipeline.js';

const task=(id,name,state='success',extra={})=>({id,name,state,workflow:'Build',workflow_id:1,...extra});
test('missing reports and missing dependencies never imply an ordered pipeline',()=>{
  assert.match(renderCiPipeline({}),/No steps reported/);
  const html=renderCiPipeline({tasks:[task(1,'Checkout'),task(2,'Tests','failure')]},{step:'2'});
  assert.match(html,/Dependencies not reported/);
  assert.doesNotMatch(html,/<svg/);
  assert.match(html,/data-step="2" aria-pressed="true"/);
  assert.match(html,/data-state="failure"/);
  assert.match(html,/>Failed</);
});
test('real dependencies render branches and their accessible descriptions',()=>{
  const html=renderCiPipeline({tasks:[task(1,'Checkout','success',{depends_on:[]}),task(2,'Linux','failure',{depends_on:['Checkout']}),task(3,'Mac','running',{depends_on:['Checkout']}),task(4,'Deploy','blocked',{depends_on:['Linux','Mac']})]});
  assert.match(html,/<svg[^>]*aria-hidden="true"/);
  assert.equal((html.match(/class="ci-pipeline-arrow"/g)||[]).length,4);
  assert.match(html,/Depends on: Linux, Mac/);
  assert.match(html,/data-column="1" data-row="1"/);
  assert.match(html,/>Running</);
  assert.match(html,/>Blocked</);
});
test('unresolved, duplicate or cyclic dependencies do not fabricate a graph',()=>{
  for(const tasks of [[task(1,'A','success',{depends_on:['missing']})],[task(1,'A','success',{depends_on:['B']}),task(2,'B','success',{depends_on:['A']})],[task(1,'A'),task(2,'A'),task(3,'B','success',{depends_on:['A']})]]){
    const html=renderCiPipeline({tasks});
    assert.doesNotMatch(html,/<svg/);
    assert.match(html,/Dependency graph is incomplete or ambiguous/);
  }
});
test('workflows, elapsed timings and hostile names remain safe and selectable',()=>{
  const html=renderCiPipeline({workflows:[{id:2,name:'Release',depends_on:['Build']}],tasks:[task(1,'<script>','success',{started_at:'2026-10-22T00:00:00Z',finished_at:'2026-10-22T00:00:30Z'}),task(2,'Ship','queued',{workflow:'Release',workflow_id:2}),task(3,'Skip','skipped',{workflow:'Release',workflow_id:2}),task(4,'Odd','new-provider-state',{workflow:'Release',workflow_id:2})]});
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/30s/);
  assert.match(html,/Workflow depends on: Build/);
  assert.match(html,/>Queued</);
  assert.match(html,/>Skipped</);
  assert.match(html,/Unknown \(new-provider-state\)/);
  assert.match(ciPipelineStyles,/@media/);
  assert.match(ciPipelineStyles,/overflow-wrap:anywhere/);
});


test('malformed and partially reported dependencies remain unknown',()=>{
  for(const tasks of [[task(1,'A','success',{depends_on:[{}]})],[task(1,'A','success',{depends_on:[]}),task(2,'B')]]){
    const html=renderCiPipeline({tasks});
    assert.doesNotMatch(html,/<svg/);
    assert.match(html,/Dependency graph is incomplete or ambiguous/);
  }
});

test('successful stages collapse without hiding the selected step and zoom is bounded',()=>{
 const html=renderCiPipeline({tasks:[task(1,'Checkout'),task(2,'Tests','failure')]},{collapseSuccess:true,zoom:99});
 assert.match(html,/<details[^>]*class="ci-pipeline-success"/);
 assert.match(html,/1 successful steps/);
 assert.match(html,/zoom:1.5/);
 assert.match(renderCiPipeline({tasks:[task(1,'Checkout')]},{collapseSuccess:true,step:'1',zoom:-3}),/zoom:0.75/);
 assert.doesNotMatch(renderCiPipeline({tasks:[task(1,'Checkout')]},{collapseSuccess:true,step:'1'}),/class="ci-pipeline-success"/);
});

test('comparison requires exact reported repository identity and preserves unknown evidence',async()=>{
 const {ciComparisonModel,renderCiComparison}=await import('../ci-pipeline.js');
 const active={provider_url:'https://ci.test',provider_repo_id:17,run_number:2,state:'failure',revision:'abc',tasks:[task(4,'Tests','failure')]};
 assert.equal(ciComparisonModel(active,{...active,provider_repo_id:18}),null);
 assert.equal(ciComparisonModel(active,{...active,provider_url:'https://other.test'}),null);
 assert.equal(ciComparisonModel({run_number:1},{run_number:2}),null);
 const other={...active,run_number:1,state:'success',revision:'def',tasks:undefined};
 const model=ciComparisonModel(active,other);
 assert.equal(model.durationDeltaMs,null);
 assert.equal(model.steps[0].other,'Not reported');
 assert.match(renderCiComparison(model),/Duration not reported/);
 assert.match(renderCiComparison(model),/abc/);
 assert.doesNotMatch(renderCiComparison(model),/data-step|data-ci-action/);
});

test('comparison matches stable workflow names across run-local IDs and bounds step projection',async()=>{
 const {ciComparisonModel}=await import('../ci-pipeline.js');
 const base={provider_url:'https://ci.test/root/',provider_repo_id:17,started_at:'2026-10-22T00:00:00Z',finished_at:'2026-10-22T00:00:30Z'};
 const active={...base,run_number:2,tasks:[task(7,'Tests','failure',{workflow_id:77,started_at:base.started_at,finished_at:base.finished_at})]};
 const other={...base,provider_url:'https://ci.test/root',run_number:1,finished_at:'2026-10-22T00:00:10Z',tasks:[task(8,'Tests','success',{workflow_id:88,started_at:base.started_at,finished_at:'2026-10-22T00:00:10Z'})]};
 const model=ciComparisonModel(active,other);
 assert.equal(model.durationDeltaMs,20000);assert.equal(model.steps.length,1);assert.equal(model.steps[0].other,'success');assert.equal(model.steps[0].durationDeltaMs,20000);
 const bounded=ciComparisonModel({...active,tasks:Array.from({length:250},(_,i)=>task(i,`Step ${i}`))},other);
 assert.equal(bounded.steps.length,200);assert.equal(bounded.truncated,true);
});
