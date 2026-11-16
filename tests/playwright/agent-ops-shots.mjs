// Captures the web Agent Operations map from the REAL room-seeded server (tests/playwright/agent-ops-room-server.py).
//   node tests/playwright/agent-ops-shots.mjs <outdir>
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright';
import {cachedChromiums} from './helpers/fixture.mjs';

const REPO=path.resolve(import.meta.dirname,'..','..');
const PY=process.env.PYTHON||'/private/tmp/claude-501/aoenv/bin/python';
const out=path.resolve(process.argv[2]||'shots');fs.mkdirSync(out,{recursive:true});

const proc=spawn(PY,[path.join(REPO,'tests','playwright','agent-ops-room-server.py')],{stdio:['ignore','pipe','inherit']});
const port=await new Promise((res,rej)=>{let b='';setTimeout(()=>rej(new Error('start timeout')),30000);proc.stdout.on('data',d=>{b+=d;const m=b.match(/PORT=(\d+)/);if(m)res(+m[1]);});});
const base=`http://127.0.0.1:${port}`;
let browser;
try{browser=await chromium.launch({headless:true,args:['--no-sandbox']});}catch(e){
 for(const executablePath of cachedChromiums()){try{browser=await chromium.launch({headless:true,args:['--no-sandbox'],executablePath});break;}catch{}}
 if(!browser)throw e;
}
const errors=[];
async function open(scheme,w,h,{reduced=false,hash='#/agent-ops'}={}){
 const ctx=await browser.newContext({viewport:{width:w,height:h},colorScheme:scheme,reducedMotion:reduced?'reduce':'no-preference'});
 const page=await ctx.newPage();
 page.on('pageerror',e=>errors.push(`${scheme} ${w}: ${e.message}`));
 page.on('console',m=>{if(m.type()==='error')errors.push(`${scheme} ${w} console: ${m.text()}`);});
 await page.goto(`${base}/index.html${hash}`);
 await page.locator('.ao-node').first().waitFor({timeout:15000});
 await page.waitForTimeout(1800);
 // The server allows 2 live streams per caller (the single operator); a just-closed page's stream lingers until the next write.
 // A page that lost that race shows the "paused" banner: wait for the old stream to drop and reload, never screenshot a banner.
 for(let i=0;i<8&&await page.locator('[data-ao-banner]:visible').count();i++){
  await page.waitForTimeout(4000);await page.reload();await page.locator('.ao-node').first().waitFor({timeout:15000});await page.waitForTimeout(2000);
 }
 if(await page.locator('[data-ao-banner]:visible').count())errors.push(`${scheme} ${w}: banner still visible at capture`);
 return {ctx,page};
}
const shot=async(page,name,full=true)=>{await page.screenshot({path:path.join(out,name),fullPage:full});console.log('wrote',name);};

for(const scheme of ['light','dark']){
 for(const [w,h] of [[1440,900],[820,1100],[390,844]]){
  const {ctx,page}=await open(scheme,w,h);
  await shot(page,`map-${scheme}-${w}.png`);
  await ctx.close();
 }
}
// replay frame (light 1440): scrub to the middle
{const {ctx,page}=await open('light',1440,900);
 const max=await page.locator('[data-ao-scrub]').getAttribute('max');
 await page.locator('[data-ao-scrub]').fill(String(Math.floor(Number(max)*0.55)));
 await page.waitForTimeout(1500);await shot(page,'map-replay-light-1440.png');await ctx.close();}
// reduced motion
{const {ctx,page}=await open('light',1440,900,{reduced:true});await shot(page,'map-reduced-motion-light-1440.png');await ctx.close();}
// room collaboration view: Communication mode, room filter on
{const {ctx,page}=await open('light',1440,900);
 await page.locator('[data-ao-mode="communication"]').click();await page.waitForTimeout(800);
 const sel=page.locator('[data-ao-filter="room"]');
 if(await sel.count()){const opts=await sel.locator('option').evaluateAll(o=>o.map(x=>x.value).filter(Boolean));if(opts.length)await sel.selectOption(opts[0]);}
 await page.waitForTimeout(800);await shot(page,'room-collaboration-light-1440.png');
 await ctx.close();}
// ownership inspector (task + ownership edge) and handoff inspector (stalled + failed)
for(const scheme of ['light','dark']){
 const {ctx,page}=await open(scheme,1440,900);
 await page.locator('[data-ao-mode="ownership"]').click();await page.waitForTimeout(600);
 await page.locator('[data-ao-task="T-428"]').first().click();await page.waitForTimeout(600);
 await shot(page,`inspector-ownership-task-${scheme}-1440.png`);
 await page.locator('[data-ao-edge^="ownership|"]').first().dispatchEvent('click');await page.waitForTimeout(600);
 await shot(page,`inspector-ownership-edge-${scheme}-1440.png`);
 await page.locator('[data-ao-mode="handoffs"]').click();await page.waitForTimeout(600);
 const handoff=async(state,name)=>{const e=page.locator('[data-ao-edge^="handoff|"]');const n=await e.count();
  for(let i=0;i<n;i++){await e.nth(i).dispatchEvent('click');await page.waitForTimeout(300);
   if(new RegExp(state,'i').test(await page.locator('.ao-inspector').innerText())){await shot(page,name);return;}}
  console.log('no handoff edge in state',state);};
 await handoff('stalled',`inspector-handoff-stalled-${scheme}-1440.png`);
 await handoff('failed',`inspector-handoff-failed-${scheme}-1440.png`);
 await handoff('completed',`inspector-handoff-completed-${scheme}-1440.png`);
 await ctx.close();
}
// agent inspector (amber ring node)
{const {ctx,page}=await open('light',1440,900);await page.locator('[data-ao-node="s-codex"]').click();await page.waitForTimeout(500);await shot(page,'inspector-agent-codex-light-1440.png');await ctx.close();}
await browser.close();proc.kill('SIGTERM');
console.log('page/console errors:',JSON.stringify(errors));
