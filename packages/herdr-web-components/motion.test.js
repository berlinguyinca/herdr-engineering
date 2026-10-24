import test from 'node:test';
import assert from 'node:assert/strict';
import {isCalm,setCalm,loadCalm,tween,flash} from '../motion.js';

function install({os=false,stored=null,throwing=false}={}){
  const dataset={};const store=new Map(stored?[['herdr.calm',stored]]:[]);
  const raf=[];const timers=[];
  globalThis.document={documentElement:{dataset,removeAttribute(){},setAttribute(){}}};
  globalThis.matchMedia=()=>({matches:os});
  globalThis.localStorage={
    getItem:k=>{if(throwing)throw new Error('denied');return store.get(k)??null},
    setItem:(k,v)=>{if(throwing)throw new Error('denied');store.set(k,v)},
  };
  globalThis.requestAnimationFrame=cb=>{raf.push(cb);return raf.length};
  globalThis.setTimeout=(cb,ms)=>{timers.push({cb,ms});return timers.length};
  return {dataset,store,raf,timers};
}
const el=()=>{const attrs={};return {textContent:'',dataset:attrs,setAttribute(k,v){attrs[k.replace(/^data-/,'')]=v},removeAttribute(k){delete attrs[k.replace(/^data-/,'')]},attrs}};
const realST=globalThis.setTimeout;
test.afterEach(()=>{globalThis.setTimeout=realST});

test('explicit on/off beats the OS preference',()=>{
  const s=install({os:true});
  assert.equal(isCalm(),true);
  s.dataset.calm='off';assert.equal(isCalm(),false);
  s.dataset.calm='on';assert.equal(isCalm(),true);
  const t=install({os:false});assert.equal(isCalm(),false);
  t.dataset.calm='on';assert.equal(isCalm(),true);
});
test('setCalm persists and loadCalm restores',()=>{
  const s=install();
  setCalm('on');assert.equal(s.dataset.calm,'on');assert.equal(s.store.get('herdr.calm'),'on');
  const t=install({stored:'off'});loadCalm();assert.equal(t.dataset.calm,'off');
  setCalm('auto');assert.equal(t.dataset.calm,undefined);
});
test('setCalm and loadCalm survive a throwing localStorage',()=>{
  const s=install({throwing:true});
  assert.doesNotThrow(()=>setCalm('on'));assert.equal(s.dataset.calm,'on');
  assert.doesNotThrow(()=>loadCalm());
});
test('tween under calm writes the final value synchronously with no rAF',()=>{
  const s=install();setCalm('on');const e=el();
  tween(e,0,42,{format:v=>`${Math.round(v)}x`});
  assert.equal(e.textContent,'42x');assert.equal(s.raf.length,0);
});
test('tween animates via rAF and writes only changed text',()=>{
  const s=install();const e=el();let writes=0;let t='';
  Object.defineProperty(e,'textContent',{get:()=>t,set:v=>{writes++;t=v}});
  const now=[0];globalThis.performance={now:()=>now[0]};
  tween(e,0,10,{format:v=>String(Math.round(v)),duration:100});
  now[0]=0;s.raf.shift()(0);const w1=writes;
  s.raf.shift()(0);assert.equal(writes,w1);
  now[0]=100;s.raf.shift()(100);assert.equal(t,'10');
  assert.equal(s.raf.length,0);
});
test('flash sets data-flash for 600ms and no-ops under calm',()=>{
  const s=install();const e=el();flash(e);
  assert.equal(e.attrs.flash,'');assert.equal(s.timers[0].ms,600);
  s.timers[0].cb();assert.equal(e.attrs.flash,undefined);
  const c=install();setCalm('on');const f=el();flash(f);
  assert.equal(f.attrs.flash,undefined);assert.equal(c.timers.length,0);
});
