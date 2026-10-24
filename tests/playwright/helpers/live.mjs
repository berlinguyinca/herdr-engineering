// Shared harness for the live-view tests: the real app (index.html) over a stub
// API (page.route), a fake EventSource driven by window.__emit, and page.clock.
import assert from 'node:assert/strict';

export const sleep=ms=>new Promise(r=>setTimeout(r,ms));
export const T0='2026-01-01T00:00:10Z';

export async function until(fn,label,ms=5000){
 const end=Date.now()+ms;
 while(Date.now()<end){if(await fn())return;await sleep(25);}
 assert.fail(`timed out waiting for ${label}`);
}

// handler(path, url) returns JSON (or undefined for {}); reads counts requests per path.
export async function boot(page,base,hash,handler){
 const reads={};
 await page.route('**/api/v1/**',async route=>{
  const url=new URL(route.request().url());const p=url.pathname;
  reads[p]=(reads[p]||0)+1;
  const body=await handler(p,url);
  if(body&&body.__hang)return; // never settles
  if(body&&body.__raw!==undefined)return route.fulfill({status:body.__status||200,body:body.__raw});
  if(body&&body.__status)return route.fulfill({status:body.__status,json:body.json||{}});
  return route.fulfill({json:body??{}});
 });
 await page.addInitScript(()=>{
  const sources=[];
  window.EventSource=class{constructor(url){this.url=String(url);sources.push(this);}close(){}};
  window.__emit=e=>{for(const s of sources)if(s.url.includes('/events/stream'))s.onmessage?.({data:JSON.stringify(e)});};
 });
 await page.clock.install({time:new Date(T0)});
 await page.setViewportSize({width:1200,height:700});
 await page.goto(`${base}/index.html#${hash}`);
 return reads;
}

// Records DOM mutations under #view (ignoring self-ticking clocks) and inserted loaders (spinner or skeleton).
export const observe=page=>page.evaluate(()=>{
 window.__view=document.getElementById('view');window.__muts=[];window.__spinners=0;
 const ticking=n=>{const el=n.nodeType===1?n:n.parentElement;return !!el?.closest('herdr-freshness,herdr-mission-activity');};
 window.__obs?.disconnect();
 window.__obs=new MutationObserver(records=>{for(const r of records){
  for(const n of r.addedNodes)if(n.nodeType===1&&(n.matches('herdr-spinner,[data-skeleton]')||n.querySelector('herdr-spinner,[data-skeleton]')))window.__spinners++;
  if(!ticking(r.target))window.__muts.push(r);
 }});
 window.__obs.observe(window.__view,{childList:true,subtree:true,attributes:true,characterData:true});
});

export const mutationCount=page=>page.evaluate(()=>window.__muts.length);

// Emits one live event and runs the clock past the refresh throttle until the
// view's freshness chip records a new successful refresh.
export async function liveRefresh(page,event={event_id:`e-${Math.random()}`,event_type:'HostHeartbeat'}){
 const before=await page.evaluate(()=>document.querySelector('#view herdr-freshness')?._last??null);
 await page.evaluate(e=>window.__emit(e),event);
 await page.clock.runFor(6000);
 await until(()=>page.evaluate(b=>{const c=document.querySelector('#view herdr-freshness');return !!c&&c._last!=null&&c._last!==b;},before),'live refresh applied');
 await sleep(50);
}
