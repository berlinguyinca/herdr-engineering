// A page whose skeleton is shorter or taller than the page that replaces it jumps when data arrives. The all-routes
// test in skeletons.test.mjs clamps to the first screen at one width; these two pages are measured in full, at a
// desktop and a phone width, because their loaded layout wraps differently.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,sleep} from './helpers/live.mjs';

const TOLERANCE_PX=16;
const now='2026-10-29T10:00:00Z';
const assistant=(id,name)=>({assistant_id:id,name,persona:'helpful',status:'ready',instructions:'',model_policy:'auto',enabled:true,created_at:now,updated_at:now});
const answer=path=>{
 if(path==='/api/v1/assistants')return {assistants:[assistant('a1','Mail assistant'),assistant('a2','CI assistant')]};
 if(path==='/api/v1/memory/status')return {healthy:true,contract:'v1',server_version:'1.2',outbox_pending:0,dead_letter:0,integrations:{}};
 return {};
};
const heights=page=>page.evaluate(()=>{
 const view=document.getElementById('view');
 const top=view.getBoundingClientRect().top;
 // the bottom of the lowest visible content: a wrapper that stretches to the viewport would hide a short page
 // (the placeholder's own box counts: it is the space it holds, even where its rows stop short).
 const leaves=[...view.querySelectorAll('*')].filter(e=>(!e.firstElementChild||e.hasAttribute('data-skeleton'))&&e.getBoundingClientRect().height>0);
 return {view:Math.round(Math.max(...leaves.map(e=>e.getBoundingClientRect().bottom))-top),status:Math.round(view.querySelector('.mem-status')?.getBoundingClientRect().height??0)};
});

for(const [hash,key] of [['/personal-assistant','view'],['/memory','status']])
 for(const width of [1280,390])
  test(`${hash} at ${width}px: the skeleton is as tall as the loaded page (${key})`,async()=>withPage(async(page,base)=>{
   let open;const gate=new Promise(r=>{open=r;});
   await boot(page,base,hash,async path=>{await gate;return answer(path);});
   await page.setViewportSize({width,height:900});
   await until(()=>page.evaluate(()=>document.querySelectorAll('#view [data-skeleton]').length>0),`skeleton on ${hash}`,8000);
   await sleep(150);
   const before=await heights(page);
   open();
   await until(()=>page.evaluate(()=>document.querySelectorAll('#view [data-skeleton]').length===0),`${hash}: skeleton replaced`,8000);
   await sleep(300);
   const after=await heights(page);
   assert.ok(Math.abs(after[key]-before[key])<=TOLERANCE_PX,`${hash} at ${width}px: ${key} height ${before[key]} -> ${after[key]}`);
  }));
