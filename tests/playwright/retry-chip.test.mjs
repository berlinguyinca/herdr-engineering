// A failed page retries in place on the next event (rerenderView). A retry that is still in flight when
// the user navigates away belongs to the old page: when it fails late, the new page's chip must stay
// as it is.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';
import {boot,until,sleep,T0} from './helpers/live.mjs';

const service={service_id:'svc-1',service_name:'Web',name:'web',host_id:'h-1',status:'healthy',kind:'service',updated_at:T0,observed_at:T0,catalog:{scope:'herdr'}};

test('a stale retry that fails after navigation does not turn the new page red',async()=>withPage(async(page,base)=>{
 let hostReads=0,release;const held=new Promise(r=>{release=r;});
 await boot(page,base,'/hosts/h-1',async p=>{
  if(p.startsWith('/api/v1/hosts/')){
   hostReads++;
   if(hostReads>1)await held;// the retry hangs until the test releases it
   return {__status:500,json:{error:'host backend down'}};
  }
  if(p==='/api/v1/services')return {services:[service]};
  return {};
 });
 const chip=()=>page.evaluate(()=>{const c=document.querySelector('#view herdr-freshness');return c?{state:c.dataset.state,text:c.textContent}:null;});
 await until(async()=>(await chip())?.state==='error','host page red');
 const firstReads=hostReads;
 await page.evaluate(()=>window.__emit({event_id:'r1',event_type:'HostHeartbeat'}));
 await page.clock.runFor(6000);
 await until(async()=>hostReads>firstReads,'retry in flight');
 await page.evaluate(()=>{location.hash='/services';});
 await until(()=>page.locator('#view [data-service-id]').count().then(n=>n>0),'services page');
 await until(async()=>(await chip())?.state==='fresh','services chip fresh');
 release();
 await sleep(300);await page.clock.runFor(500);await sleep(100);
 const after=await chip();
 assert.notEqual(after.state,'error',`services chip after the stale retry failed: ${after.text}`);
 assert.doesNotMatch(after.text,/host backend down/);
}));
