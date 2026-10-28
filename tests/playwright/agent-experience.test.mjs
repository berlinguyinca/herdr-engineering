import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';

async function withAgent(run){
  const root=path.resolve(import.meta.dirname,'../../packages');
  const server=createServer(async(req,res)=>{
    if(req.url==='/'){res.setHeader('content-type','text/html');res.end('<!doctype html><div id="host"></div><div id="catalog"></div>');return;}
    const file=path.resolve(root,'.'+req.url.split('?')[0]);
    if(!file.startsWith(root+path.sep)){res.writeHead(404);res.end();return;}
    try{res.setHeader('content-type',file.endsWith('.css')?'text/css':'text/javascript');res.end(await readFile(file));}catch{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));let browser;
  try{
    browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.PLAYWRIGHT_CHROMIUM || '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'});
    const page=await browser.newPage({viewport:{width:1400,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);await page.clock.install();
    await page.evaluate(async()=>{
      const {mountSessionWorkspace}=await import('/session-workspace.js');
      window.agent={session_id:'agent:one',display_name:'Amber Finch',agent_handle:'amber-finch',name_version:1,status:'active',updated_at:new Date().toISOString(),capabilities:{rename_agent:true,regenerate_summary:true,fix_summary_item:true}};
      window.summary={state:'ready',overview:'Fixed **recording**.',where_left_off:'Review the final diff.',next_steps:['Run the full tests'],decisions:[],discussed:[],completed:['Captured messages'],blockers:['Lint fails <b>here</b>'],generated_at:'2026-10-22T12:00:00Z',model:'small-qwen',coverage:{status:'partial',message_count:3,reasons:['earlier messages expired']}};
      window.summaryReads=0;window.regenerations=0;window.renames=[];window.fixes=[];
      window.client={
        getInspector:async()=>({epoch:'one',events:[{seq:1,rev:1,kind:'message',agent_id:'main',data:{role:'assistant',text:'Ready'}},{seq:2,rev:2,kind:'tool',agent_id:'helper',data:{tool:'read_json',output:'{"<script>":{"answer":42}}'}}],summary:{agents:[{agent_id:'main',display_name:'Amber Finch'},{agent_id:'helper',display_name:'Slate Owl',agent_handle:'slate-owl',parent_agent_id:'main'}],counts:{message:1}}}),
        inspectorStreamUrl:()=>'/stream',listSessions:async()=>({sessions:[window.agent]}),listMissions:async()=>({missions:[]}),
        getAgentSummary:async()=>{window.summaryReads++;if(window.summaryFailure)throw new Error('Inference offline');return structuredClone(window.summary);},
        fixSummaryItem:async(id,list,index,text)=>{window.fixes.push({id,list,index,text});if(list==='blockers')throw Object.assign(new Error('The agent that owns this summary is not running.'),{status:409,body:{fallback:'helper_unavailable'}});return {ok:true,status:'delivered'};},
        regenerateAgentSummary:async()=>{window.regenerations++;return {...window.summary,state:'pending'};},
        renameAgent:async(id,name,version)=>{window.renames.push({id,name,version});if(window.renameConflict)throw Object.assign(new Error('Name already reserved'),{status:409});return {display_name:name,handle:'new-name',version:2};},
      };
      window.remount=()=>{window.dispose?.();window.dispose=mountSessionWorkspace(document.querySelector('#host'),window.client,window.agent,{connect:()=>()=>{}});};window.remount();
    });
    await page.clock.runFor(200);assert.deepEqual(errors,[]);await page.locator('[data-message-seq="1"]').waitFor();await run(page);assert.deepEqual(errors,[]);
  }finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
}

test('Agents use readable names, summary provenance and secure JSON activity',()=>withAgent(async page=>{
  assert.equal(await page.locator('[data-agent-name]').innerText(),'Amber Finch');
  assert.equal(await page.locator('[data-message-seq="1"] header strong').innerText(),'Amber Finch');
  assert.match(await page.locator('[data-agent-summary]').innerText(),/Where we left off[\s\S]*Review the final diff/);
  assert.match(await page.locator('[data-agent-summary]').innerText(),/Partial recording · 3 messages/);
  await page.getByRole('button',{name:'Slate Owl ← Amber Finch'}).waitFor();
  await page.locator('[data-activity-seq="2"] > summary').click();
  assert.equal(await page.locator('.conversation-json script').count(),0);
  assert.match(await page.locator('.conversation-json').innerText(),/<script>/);
  await page.evaluate(async()=>{const {mountSessionsView}=await import('/sessions-view.js');window.catalog=mountSessionsView(document.querySelector('#catalog'),window.client);});
  await page.getByRole('heading',{name:'Agents',exact:true}).waitFor();
  assert.match(await page.locator('[data-session-link]').innerText(),/Amber Finch/);
  await page.getByRole('searchbox',{name:'Search agents'}).fill('amber-finch');
  assert.equal(await page.locator('[data-session-link]').count(),1);
}));

test('cached summaries retain old content and coverage across pending/failure; polling stops hidden and disposed',()=>withAgent(async page=>{
  await page.evaluate(()=>{window.summary={state:'pending',generated_at:null,coverage:{status:'recorded_window',message_count:99}};});
  await page.clock.fastForward(10000);
  await page.getByText('Summary update pending.',{exact:true}).waitFor();
  assert.match(await page.locator('[data-agent-summary]').innerText(),/Fixed recording/);
  assert.match(await page.locator('[data-agent-summary]').innerText(),/Partial recording · 3 messages/);
  await page.evaluate(()=>{window.summaryFailure=true;});await page.clock.fastForward(10000);
  await page.getByText(/Summary unavailable: Inference offline/).waitFor();
  assert.match(await page.locator('[data-agent-summary]').innerText(),/Review the final diff/);
  const count=await page.evaluate(()=>window.summaryReads);
  await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,value:true}));await page.clock.fastForward(30000);
  assert.equal(await page.evaluate(()=>window.summaryReads),count);
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});window.dispose();});await page.clock.fastForward(20000);
  assert.equal(await page.evaluate(()=>window.summaryReads),count);
  assert.equal(await page.evaluate(()=>window.regenerations),0);
}));

test('workspace fullscreen preserves draft and nested editor Escape focus priority',()=>withAgent(async page=>{
  const input=page.getByRole('combobox',{name:'Message'});await input.fill('Keep the draft');
  await page.getByRole('button',{name:'Fullscreen workspace',exact:true}).click();
  assert.equal(await page.locator('.sw--fullscreen').count(),1);
  await page.getByRole('button',{name:'Expand editor',exact:true}).click();
  await input.fill('@Slate');await input.press('Escape');
  assert.equal(await page.locator('.conversation-composer--expanded').count(),1);
  await input.press('Escape');
  assert.equal(await page.locator('.conversation-composer--expanded').count(),0);
  assert.equal(await page.locator('.sw--fullscreen').count(),1);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.sw--fullscreen').count(),0);
  assert.equal(await input.inputValue(),'@Slate');
  assert.equal(await page.getByRole('button',{name:'Fullscreen workspace',exact:true}).evaluate(el=>el===document.activeElement),true);
}));

test('operator rename conflicts preserve input and viewer controls remain hidden',()=>withAgent(async page=>{
  await page.getByRole('button',{name:'Rename agent',exact:true}).click();
  await page.getByRole('textbox',{name:'Agent name',exact:true}).fill('Reserved Name');
  await page.evaluate(()=>{window.renameConflict=true;});await page.getByRole('button',{name:'Save name',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'Name already reserved'}).waitFor();
  assert.equal(await page.getByRole('textbox',{name:'Agent name',exact:true}).inputValue(),'Reserved Name');
  assert.equal(await page.locator('[data-agent-name]').innerText(),'Amber Finch');
  await page.evaluate(()=>{window.renameConflict=false;});await page.getByRole('textbox',{name:'Agent name',exact:true}).fill('Silver Heron');
  await page.getByRole('button',{name:'Save name',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-agent-name]').textContent==='Silver Heron');
  assert.deepEqual(await page.evaluate(()=>window.renames.at(-1)),{id:'agent:one',name:'Silver Heron',version:1});
  await page.evaluate(()=>{window.agent.capabilities={rename_agent:false,regenerate_summary:false};window.remount();});
  assert.equal(await page.getByRole('button',{name:'Rename agent',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'Regenerate summary',exact:true}).count(),0);
}));

test('an ambiguous saved name explains its stable-ID fallback and recovers through rename',()=>withAgent(async page=>{
  await page.evaluate(()=>{
    window.agent.display_name=window.agent.session_id;
    window.agent.agent_handle=null;
    window.agent.name_error='Saved agent name conflicts with another identity; using the session ID until renamed.';
    window.remount();
  });
  assert.equal(await page.locator('[data-agent-name]').innerText(),'agent:one');
  assert.match(await page.locator('[data-agent-name-error]').innerText(),/conflicts.*session ID/);
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(()=>{
    window.agent.session_id='agent:'+'x'.repeat(200);
    window.agent.display_name=window.agent.session_id;
    window.remount();
  });
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
  await page.getByRole('button',{name:'Rename agent',exact:true}).click();
  await page.getByRole('textbox',{name:'Agent name',exact:true}).fill('Silver Heron');
  await page.getByRole('button',{name:'Save name',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-agent-name]').textContent==='Silver Heron');
  assert.equal(await page.locator('[data-agent-name-error]').count(),0);
  assert.deepEqual(await page.evaluate(()=>window.renames.at(-1)),{id:'agent:'+'x'.repeat(200),name:'Silver Heron',version:1});
}));

test('Summary blockers and next steps can be handed back to the owning agent once',()=>withAgent(async page=>{
  await page.locator('[data-agent-summary] details > summary').click();
  const fixes=page.getByRole('button',{name:'Let the agent fix this',exact:true});
  assert.equal(await fixes.count(),2);
  assert.match(await page.locator('[data-agent-summary]').innerText(),/Lint fails <b>here<\/b>/);
  await page.locator('[data-fix-summary][data-list="next_steps"]').click();
  await page.getByRole('button',{name:'Sent to agent',exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'Sent to agent',exact:true}).isDisabled(),true);
  assert.equal(await page.locator('[data-agent-summary] details').evaluate(node=>node.open),true);
  assert.deepEqual(await page.evaluate(()=>window.fixes),[{id:'agent:one',list:'next_steps',index:0,text:'Run the full tests'}]);
  await page.locator('[data-fix-summary][data-list="blockers"]').click();
  await page.getByRole('alert').filter({hasText:'not running'}).waitFor();
  assert.match(await page.getByRole('alert').filter({hasText:'not running'}).innerText(),/Start the agent again/);
  assert.equal(await page.locator('[data-fix-summary][data-list="blockers"]').isDisabled(),false);
  await page.clock.fastForward(10000);await page.clock.runFor(100);
  assert.equal(await page.getByRole('button',{name:'Sent to agent',exact:true}).isDisabled(),true);
  await page.locator('[data-fix-summary][data-list="next_steps"]').click({force:true});
  assert.equal(await page.evaluate(()=>window.fixes.filter(f=>f.list==='next_steps').length),1);
  await page.evaluate(()=>{window.agent.capabilities={fix_summary_item:false};window.remount();});
  await page.clock.runFor(200);
  assert.equal(await page.locator('[data-fix-summary]').count(),0);
}));
