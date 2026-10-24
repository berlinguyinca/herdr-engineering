import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {withPage} from './helpers/fixture.mjs';

const packages=path.resolve(import.meta.dirname,'../../packages');

async function open(page,base){
 await page.clock.install({time:new Date('2026-01-01T00:00:00Z')});
 await page.goto(`${base}/__blank`);
 await page.addStyleTag({url:`${base}/herdr-design-system/theme.css`});
 await page.addStyleTag({url:`${base}/motion.css`});
 await page.evaluate(async()=>{
  await import('/freshness.js');
  const m=await import('/motion.js');window.setCalm=m.setCalm;
 });
}

test('freshness chip goes amber past 2x interval, red after a failed touch',async()=>withPage(async(page,base)=>{
 await open(page,base);
 await page.evaluate(()=>{
  document.body.innerHTML='<herdr-freshness id="f" interval="5000"></herdr-freshness>';
  window.f=document.getElementById('f');f.touch(true);
 });
 const st=()=>page.evaluate(()=>({state:f.dataset.state,text:f.textContent}));
 assert.deepEqual(await st(),{state:'fresh',text:'live · updated 0s ago'});
 await page.clock.fastForward(4000);
 await page.clock.runFor(1000);
 assert.equal((await st()).state,'fresh');
 assert.match((await st()).text,/updated 5s ago/);
 await page.clock.runFor(6000);
 const stale=await st();
 assert.equal(stale.state,'stale');assert.equal(stale.text,'stale · updated 11s ago','a stale chip never reads "live"');
 const amber=await page.evaluate(()=>getComputedStyle(f).color);
 await page.evaluate(()=>f.touch(false,'x'));
 const err=await st();
 assert.equal(err.state,'error');assert.match(err.text,/x/);
 assert.notEqual(await page.evaluate(()=>getComputedStyle(f).color),amber);
 await page.evaluate(()=>f.touch(true));
 assert.equal((await st()).state,'fresh');
}));

test('a chip that was never updated waits instead of claiming live',async()=>withPage(async(page,base)=>{
 await open(page,base);
 await page.evaluate(()=>{document.body.innerHTML='<herdr-freshness id="f" interval="5000"></herdr-freshness>';window.f=document.getElementById('f');});
 const st=()=>page.evaluate(()=>({state:f.dataset.state,text:f.textContent}));
 assert.deepEqual(await st(),{state:'fresh',text:'waiting for first update'});
 await page.evaluate(()=>f.touch(false,'down'));
 assert.deepEqual(await st(),{state:'error',text:'error · down · not yet updated'});
 await page.evaluate(()=>f.touch(true));
 assert.deepEqual(await st(),{state:'fresh',text:'live · updated 0s ago'});
 await page.evaluate(()=>f.setDetail('partial',{warn:true}));
 assert.deepEqual(await st(),{state:'stale',text:'stale · updated 0s ago · partial'},'a warned chip never reads "live"');
}));

test('freshness does not rewrite the DOM when text is unchanged',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const writes=await page.evaluate(async()=>{
  document.body.innerHTML='<herdr-freshness id="f" interval="60000"></herdr-freshness>';
  const f=document.getElementById('f');f.touch(true);
  let n=0;new MutationObserver(r=>{n+=r.length}).observe(f,{childList:true,characterData:true,subtree:true,attributes:true});
  f.touch(true);f._render();f._render();
  await Promise.resolve();
  return n;
 });
 assert.equal(writes,0);
}));

test('data-theme="dark" changes --bg; auto follows prefers-color-scheme; light overrides it',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const bg=()=>page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--bg').trim());
 await page.emulateMedia({colorScheme:'light'});
 const light=await bg();assert.ok(light);
 await page.evaluate(()=>{document.documentElement.dataset.theme='dark'});
 const dark=await bg();assert.notEqual(dark,light);
 await page.evaluate(()=>{delete document.documentElement.dataset.theme});
 await page.emulateMedia({colorScheme:'dark'});
 assert.equal(await bg(),dark);
 await page.evaluate(()=>{document.documentElement.dataset.theme='light'});
 assert.equal(await bg(),light);
}));

test('setCalm("on") zeroes transition duration on a [data-flash] element',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const dur=()=>page.evaluate(()=>getComputedStyle(document.getElementById('x')).transitionDuration);
 await page.evaluate(()=>{document.body.innerHTML='<div id="x" data-flash>v</div>'});
 assert.notEqual(await dur(),'0s');
 await page.evaluate(()=>setCalm('on'));
 assert.ok(parseFloat(await dur())<0.001);
 assert.equal(await page.evaluate(()=>getComputedStyle(document.getElementById('x')).animationName),'none');
}));

test('inline script applies stored theme and calm before any module loads',async()=>withPage(async(page,base)=>{
 await page.addInitScript(()=>{localStorage.setItem('herdr.theme','dark');localStorage.setItem('herdr.calm','on')});
 await page.route('**/api/**',r=>r.fulfill({status:200,contentType:'application/json',body:'{}'}));
 let release;const gate=new Promise(r=>{release=r});
 await page.route(/\/(motion|theme-controls|freshness|app)\.js$/,async route=>{await gate;await route.continue();});
 // deferred module scripts block DOMContentLoaded, so resolve on commit and poll for the inline script's effect
 await page.goto(`${base}/`,{waitUntil:'commit'});
 await page.waitForFunction(()=>document.documentElement.dataset.theme==='dark');
 // modules are held back, so only the inline script can have set these
 const early=await page.evaluate(()=>({t:document.documentElement.dataset.theme,c:document.documentElement.dataset.calm,controls:!!document.querySelector('.hd-controls')}));
 assert.deepEqual(early,{t:'dark',c:'on',controls:false});
 release();
 await page.waitForSelector('.hd-controls');
 const r=await page.evaluate(()=>[...document.querySelectorAll('.hd-controls select')].map(s=>s.value));
 assert.deepEqual(r,['dark','on']);
 await page.selectOption('.hd-controls select[data-control="calm"]','off');
 assert.equal(await page.evaluate(()=>[document.documentElement.dataset.calm,localStorage.getItem('herdr.calm')].join()),'off,off');
 await page.selectOption('.hd-controls select[data-control="theme"]','auto');
 assert.equal(await page.evaluate(()=>[document.documentElement.dataset.theme??'none',localStorage.getItem('herdr.theme')].join()),'none,');
}));

test('explicit calm=off beats OS reduced motion; auto and on honour it',async()=>withPage(async(page,base)=>{
 await open(page,base);
 await page.emulateMedia({reducedMotion:'reduce'});
 await page.evaluate(()=>{document.body.innerHTML='<div id="x" data-flash>v</div>'});
 const dur=()=>page.evaluate(()=>getComputedStyle(document.getElementById('x')).transitionDuration);
 const anim=()=>page.evaluate(()=>getComputedStyle(document.getElementById('x')).animationName);
 await page.evaluate(()=>setCalm('auto'));
 assert.ok(parseFloat(await dur())<0.001);assert.equal(await anim(),'none');
 await page.evaluate(()=>setCalm('off'));
 assert.equal(await dur(),'0.6s');assert.equal(await anim(),'hd-flash');
 await page.evaluate(()=>setCalm('on'));
 assert.ok(parseFloat(await dur())<0.001);
}));

test('every var(--token) used in packages is defined',()=>{
 const files=[];
 (function walk(d){for(const e of fs.readdirSync(d,{withFileTypes:true})){
  const p=path.join(d,e.name);
  if(e.isDirectory()){if(e.name!=='node_modules')walk(p);}
  else if(/\.(css|js|html)$/.test(e.name)&&!/\.test\.js$/.test(e.name))files.push(p);
 }})(packages);
 const themeCss=fs.readFileSync(path.join(packages,'herdr-design-system/theme.css'),'utf8');
 const themeDefs=new Set([...themeCss.matchAll(/(--[\w-]+)\s*:/g)].map(m=>m[1]));
 const localDefs=new Set();const used=new Map();
 for(const f of files){
  const s=fs.readFileSync(f,'utf8');
  for(const m of s.matchAll(/(--[\w-]+)\s*:/g))localDefs.add(m[1]);
  for(const m of s.matchAll(/setProperty\(\s*['"`](--[\w-]+)/g))localDefs.add(m[1]);
  for(const m of s.matchAll(/var\(\s*(--[\w-]+)(?![\w-]*\$\{)/g)){
   if(s[m.index+m[0].length]==='$')continue;
   (used.get(m[1])??used.set(m[1],[]).get(m[1])).push(path.relative(packages,f));
  }
 }
 // Tokens a view sets on its own elements are legitimate; the shared ones must live in theme.css.
 const sharedUsed=[...used].filter(([n])=>!localDefs.has(n)||themeDefs.has(n));
 const missing=sharedUsed.filter(([n])=>!themeDefs.has(n)).map(([n,f])=>`${n} (${[...new Set(f)].join(', ')})`);
 assert.deepEqual(missing,[]);
});
