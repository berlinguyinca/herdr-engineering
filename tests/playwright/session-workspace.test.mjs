import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {chromium} from 'playwright';

async function withWorkspace(run) {
  const root=path.resolve(import.meta.dirname,'../../packages');
  const server=createServer(async(req,res)=>{
    if(req.url==='/'){res.setHeader('content-type','text/html');res.end('<!doctype html><link rel="stylesheet" href="/herdr-design-system/theme.css"><link rel="stylesheet" href="/herdr-design-system/base.css"><link rel="stylesheet" href="/herdr-web-components/components.css"><div id="host" style="padding:20px"></div>');return;}
    const file=path.resolve(root,'.'+req.url.split('?')[0]);
    if(!file.startsWith(root+path.sep)){res.writeHead(404);res.end();return;}
    try{res.setHeader('content-type',file.endsWith('.css')?'text/css':'text/javascript');res.end(await readFile(file));}catch{res.writeHead(404);res.end();}
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  let browser;
  try{
    browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath:process.env.PLAYWRIGHT_CHROMIUM || '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'});
    const page=await browser.newPage({viewport:{width:1400,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(async()=>{
      const {mountSessionWorkspace}=await import('/session-workspace.js');
      Object.defineProperty(window.crypto,'randomUUID',{value:undefined,configurable:true});
      const events=[{seq:1,rev:1,kind:'message',agent_id:'main',data:{role:'user',text:'Please **fix** it'}},
        {seq:2,rev:2,kind:'tool',agent_id:'helper',data:{tool:'bash',status:'error',output:'secret command output'}},
        {seq:3,rev:3,kind:'message',agent_id:'helper',data:{role:'assistant',text:'Fixed `app.js`'}}];
      window.data={events,rev:3,epoch:'e',has_more:false,summary:{agents:[{agent_id:'main'},{agent_id:'helper',parent_agent_id:'main'}],files:[{path:'app.js'}],association:{},counts:{message:2}}};
      window.sent=[];window.operations=[];
      window.workspaceClient={
        getInspector:async()=>structuredClone(window.data),inspectorStreamUrl:()=>'/stream',
        getInspectorEvent:async(id,seq)=>seq===2?{seq,rev:seq,kind:'tool',agent_id:'helper',data:{tool:'bash',status:'error',output:Array.from({length:220},(_,i)=>`Output line ${i}`).join('\n')}}:({seq,rev:seq,kind:'message',agent_id:'helper',data:{role:'assistant',text:'Full message ending'}}),
        listSessions:async()=>({sessions:[]}),sendMessage:async(id,text,artifacts,operation)=>{window.sent.push({id,text});window.operations.push(operation);if(window.forceFailure)throw new Error('Connection lost');return {status:'delivered'};}
      };
      window.remount=()=>{window.view?.();window.view=mountSessionWorkspace(document.querySelector('#host'),window.workspaceClient,
        {session_id:'s',status:'working',agent_session:{kind:'id',value:'real'},terminal_id:'term_s',host_id:'h'},
        {connect:options=>{window.stream=options;return ()=>{window.streamClosed=true;};}});};
      window.remount();
    });
    await page.waitForSelector('[data-message-seq="3"]');
    if(process.env.HERDR_WORKSPACE_SCREENSHOT)await page.screenshot({path:process.env.HERDR_WORKSPACE_SCREENSHOT,fullPage:true});
    await run(page);
    assert.deepEqual(errors,[]);
  }finally{if(browser)await browser.close();await new Promise(r=>server.close(r));}
}

test('split workspace separates messages, collapses output, filters and keeps drafts while streaming',()=>withWorkspace(async page=>{
  assert.equal(await page.locator('[data-message-seq="1"] p strong').innerText(),'fix');
  assert.equal(await page.locator('[data-conversation]').innerText().then(t=>t.includes('secret command output')),false);
  assert.equal(await page.locator('[data-activity] details').getAttribute('open'),null);
  await page.locator('[data-activity] summary').click();
  await page.locator('[data-composer] textarea').fill('Keep this draft');
  await page.evaluate(()=>window.stream.onEvent({type:'event',event:{seq:4,rev:4,kind:'message',agent_id:'main',data:{role:'assistant',text:'New response'}}}));
  await page.waitForSelector('[data-message-seq="4"]');
  assert.equal(await page.locator('[data-composer] textarea').inputValue(),'Keep this draft');
  assert.notEqual(await page.locator('[data-activity] details').getAttribute('open'),null);
  await page.locator('[data-agent-filter]').selectOption('helper');
  await page.waitForFunction(()=>document.querySelectorAll('[data-message-seq]').length===1);
  assert.equal(await page.locator('[data-message-seq]').count(),1);
  await page.locator('[data-agent-filter]').selectOption('');
  await page.locator('[data-composer] button[type="submit"]').click();
  await page.waitForFunction(()=>window.sent.length===1);
  assert.deepEqual(await page.evaluate(()=>window.sent),[{id:'s',text:'Keep this draft'}]);
  await page.evaluate(()=>window.view());
  assert.equal(await page.evaluate(()=>window.streamClosed),true);
}));

test('mobile tabs and splitter keyboard remain accessible',()=>withWorkspace(async page=>{
  await page.locator('[data-splitter]').focus();await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('[data-splitter]').getAttribute('aria-valuenow'),'65');
  await page.setViewportSize({width:480,height:850});
  await page.locator('[data-pane-tab="activity"]').click();
  assert.equal(await page.locator('[data-conversation-pane]').isVisible(),false);
  assert.equal(await page.locator('[data-activity-pane]').isVisible(),true);
}));

test('HTTP-compatible sending retains the same operation after ambiguous failure and remount',()=>withWorkspace(async page=>{
  await page.evaluate(()=>{window.forceFailure=true;});
  await page.locator('[data-composer] textarea').fill('Retry safely');
  await page.locator('[data-composer] button[type=submit]').click();
  await page.waitForFunction(()=>document.querySelector('.conversation-status').textContent==='Connection lost');
  await page.evaluate(()=>{window.forceFailure=false;window.remount();});
  await page.waitForFunction(()=>document.querySelector('[data-composer] textarea').value==='Retry safely');
  await page.locator('[data-composer] button[type=submit]').click();
  await page.waitForFunction(()=>window.operations.length===2);
  const operations=await page.evaluate(()=>window.operations);
  assert.ok(operations[0]);assert.equal(operations[0],operations[1]);
}));

test('long messages explicitly expand and remain expanded after refreshed previews',()=>withWorkspace(async page=>{
  await page.evaluate(()=>{window.data.events[2].data={role:'assistant',text:'Preview',text_more:true};window.stream.onEvent({type:'gap'});});
  await page.waitForSelector('[data-expand-message="3"]');
  await page.locator('[data-expand-message="3"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-message-seq="3"]').textContent.includes('Full message ending'));
  await page.locator('[data-refresh]').click();
  await page.waitForTimeout(200);
  assert.match(await page.locator('[data-message-seq="3"]').innerText(),/Full message ending/);
}));

test('reconnect resets history window so a burst cannot leave an unpageable interior gap',()=>withWorkspace(async page=>{
  await page.evaluate(()=>{
    window.data.events=Array.from({length:200},(_,i)=>({seq:501+i,rev:501+i,kind:'message',agent_id:'main',data:{role:'assistant',text:`New ${i}`}}));
    window.data.has_more=true;window.stream.onEvent({type:'gap'});
  });
  await page.waitForSelector('[data-message-seq="700"]');
  assert.equal(await page.locator('[data-message-seq="1"]').count(),0);
  assert.match(await page.locator('[data-recording-status]').innerText(),/Load older/);
}));

test('expanded tool output displays the full retained body beyond preview line limits',()=>withWorkspace(async page=>{
  await page.evaluate(()=>{window.data.events[1].data.output_more=true;window.stream.onEvent({type:'gap'});});
  await page.locator('[data-activity] summary').click();
  await page.locator('[data-activity] .hd-ins__expand').click();
  await page.waitForFunction(()=>document.querySelector('[data-activity] pre')?.textContent.includes('Output line 219'));
  assert.match(await page.locator('[data-activity] pre').innerText(),/Output line 0\n/);
}));

test('conversation has its own window when hundreds of later tools fill activity, including live refresh',()=>withWorkspace(async page=>{
  await page.evaluate(()=>{
    window.reads=[];
    window.historyEvents=[{seq:1,rev:1,kind:'message',agent_id:'main',data:{role:'user',text:'Retained conversation before many tools'}},
      ...Array.from({length:260},(_,i)=>({seq:i+2,rev:i+2,kind:'tool',agent_id:'helper',data:{tool:'bash',output:`tool ${i}`}}))];
    window.workspaceClient.getInspector=async(id,options)=>{
      window.reads.push(options);
      const available=window.historyEvents.filter(e=>(options.view!=='conversation'||e.kind==='message')&&(!options.before_seq||e.seq<options.before_seq)).sort((a,b)=>b.seq-a.seq);
      return {...window.data,events:available.slice(0,options.limit),has_more:available.length>options.limit,oldest_available_seq:1};
    };
    window.remount();
  });
  await page.locator('[data-message-seq="1"]').waitFor({timeout:3000});
  assert.match(await page.locator('[data-conversation]').innerText(),/Retained conversation before many tools/);
  assert.deepEqual((await page.evaluate(()=>window.reads)).map(r=>r.view).sort(),['all','conversation']);
  await page.locator('[data-composer] textarea').fill('Preserve my draft');
  await page.evaluate(()=>{
    const additional=Array.from({length:2100},(_,i)=>({seq:i+262,rev:i+262,kind:'tool',agent_id:'helper',data:{tool:'bash',output:`live ${i}`}}));
    window.historyEvents.push(...additional);
    for(const event of additional)window.stream.onEvent({type:'event',event});
  });
  await page.waitForFunction(()=>window.reads.filter(r=>r.view==='conversation').length>=2);
  assert.match(await page.locator('[data-conversation]').innerText(),/Retained conversation before many tools/);
  assert.equal(await page.locator('[data-composer] textarea').inputValue(),'Preserve my draft');
  assert.ok(await page.locator('[data-activity-seq]').count()<=2000);
  assert.ok((await page.evaluate(()=>window.reads)).every(r=>!r.before_seq));
}));

test('older conversation pages use the message cursor independently of the activity cursor',()=>withWorkspace(async page=>{
  await page.evaluate(()=>{
    window.reads=[];
    const retained=[...Array.from({length:205},(_,i)=>({seq:i+1,rev:i+1,kind:'message',agent_id:'main',data:{role:'assistant',text:`Message ${i+1}`}})),
      ...Array.from({length:250},(_,i)=>({seq:i+206,rev:i+206,kind:'tool',agent_id:'helper',data:{tool:'bash',output:`tool ${i}`}}))];
    window.workspaceClient.getInspector=async(id,options)=>{
      window.reads.push(options);
      const available=retained.filter(e=>(options.view!=='conversation'||e.kind==='message')&&(!options.before_seq||e.seq<options.before_seq)).sort((a,b)=>b.seq-a.seq);
      return {...window.data,events:available.slice(0,options.limit),has_more:available.length>options.limit,oldest_available_seq:1};
    };window.remount();
  });
  await page.locator('[data-message-seq="205"]').waitFor({timeout:3000});
  assert.equal(await page.locator('[data-message-seq="1"]').count(),0);
  await page.locator('[data-load-older]').click();
  await page.locator('[data-message-seq="1"]').waitFor({timeout:3000});
  const older=await page.evaluate(()=>window.reads.filter(r=>r.before_seq));
  assert.equal(older.find(r=>r.view==='conversation').before_seq,6);
  assert.equal(older.find(r=>r.view==='all').before_seq,256);
  assert.equal(await page.locator('[data-message-seq]').count(),205);
}));

test('epoch replacement resets both independent windows while retaining the unsent draft',()=>withWorkspace(async page=>{
  await page.locator('[data-composer] textarea').fill('Still composing');
  await page.evaluate(()=>{
    window.workspaceClient.getInspector=async(id,{view})=>({...window.data,epoch:'replacement',oldest_available_seq:1,has_more:false,
      events:view==='conversation'?[{seq:1,rev:1,kind:'message',agent_id:'main',data:{role:'assistant',text:'New epoch conversation'}}]:[{seq:300,rev:300,kind:'tool',agent_id:'main',data:{tool:'replacement_tool',output:'new'}}]});
  });
  await page.locator('[data-refresh]').click();
  await page.waitForFunction(()=>document.querySelector('[data-conversation]').textContent.includes('New epoch conversation'));
  assert.equal(await page.locator('[data-message-seq="3"]').count(),0);
  assert.equal(await page.locator('[data-activity-seq="2"]').count(),0);
  assert.equal(await page.locator('[data-activity-seq="300"]').count(),1);
  assert.equal(await page.locator('[data-composer] textarea').inputValue(),'Still composing');
}));

for(const scenario of ['same-epoch gap','mixed epochs with failed retry','retention expiry']){
  test(`deferred expansion cannot resurrect stale content after ${scenario}`,()=>withWorkspace(async page=>{
    await page.locator('[data-composer] textarea').fill('Keep my unsent draft');
    await page.evaluate(()=>{window.data.events[2].data.text_more=true;});
    await page.locator('[data-refresh]').click();await page.locator('[data-expand-message="3"]').waitFor();
    await page.evaluate(()=>{window.workspaceClient.getInspectorEvent=()=>new Promise(resolve=>{window.resolveExpansion=resolve;});});
    await page.locator('[data-expand-message="3"]').click();
    if(scenario==='same-epoch gap'){
      await page.evaluate(()=>{
        window.data.events=[{seq:3,rev:3,kind:'message',agent_id:'helper',data:{role:'assistant',text:'Fresh preview after gap'}}];
        window.stream.onEvent({type:'gap'});
      });
      await page.waitForFunction(()=>document.querySelector('[data-conversation]').textContent.includes('Fresh preview after gap'));
    }else if(scenario==='mixed epochs with failed retry'){
      await page.evaluate(()=>{window.workspaceClient.getInspector=async(id,{view})=>({...window.data,epoch:view==='all'?'e':'replacement'});});
      await page.locator('[data-refresh]').click();
      await page.getByText('Recording changed while loading history; retrying.',{exact:true}).waitFor();
      await page.evaluate(()=>{window.workspaceClient.getInspector=async()=>{throw new Error('Retry offline');};});
      await page.locator('[data-refresh]').click();
      await page.getByText('Recorded history could not load: Retry offline',{exact:true}).waitFor();
    }else{
      await page.evaluate(()=>{window.data.oldest_available_seq=4;window.data.events=[{seq:4,rev:4,kind:'message',agent_id:'main',data:{role:'assistant',text:'Retained newer turn'}}];});
      await page.locator('[data-refresh]').click();
      await page.waitForFunction(()=>document.querySelector('[data-conversation]').textContent.includes('Retained newer turn'));
    }
    await page.evaluate(async()=>{
      window.resolveExpansion({seq:3,rev:3,kind:'message',agent_id:'helper',data:{role:'assistant',text:'OBSOLETE EXPANDED CONTENT'}});
      await new Promise(requestAnimationFrame);
    });
    assert.doesNotMatch(await page.locator('[data-conversation]').innerText(),/OBSOLETE EXPANDED CONTENT/);
    assert.equal(await page.locator('[data-composer] textarea').inputValue(),'Keep my unsent draft');
    if(scenario==='same-epoch gap')assert.match(await page.locator('[data-conversation]').innerText(),/Fresh preview after gap/);
    else assert.equal(await page.locator('[data-message-seq="3"]').count(),0);
  }));
}
