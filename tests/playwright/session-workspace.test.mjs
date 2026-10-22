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
