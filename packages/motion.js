// Calm state and motion core. Calm = explicit data-calm="on", or OS reduced-motion unless data-calm="off".
const KEY='herdr.calm';

export function isCalm(){
 const mode=document.documentElement.dataset.calm;
 if(mode==='on')return true;
 if(mode==='off')return false;
 return typeof matchMedia==='function'&&matchMedia('(prefers-reduced-motion: reduce)').matches===true;
}

function apply(mode){
 const root=document.documentElement;
 if(mode==='on'||mode==='off')root.dataset.calm=mode;
 else delete root.dataset.calm;
}

export function setCalm(mode){
 if(mode!=='on'&&mode!=='off'&&mode!=='auto')mode='auto';
 apply(mode);
 try{
  if(mode==='auto')localStorage.removeItem?.(KEY);else localStorage.setItem(KEY,mode);
 }catch{}
}

export function loadCalm(){
 let mode=null;
 try{mode=localStorage.getItem(KEY)}catch{}
 apply(mode==='on'||mode==='off'?mode:'auto');
}

const tweens=new WeakMap();

// write: where each formatted step goes (default: the element's text; e.g. v=>{bar.value=v} for <progress>).
export function tween(el,from,to,{format=v=>String(Math.round(v)),duration=300,write=text=>{if(el.textContent!==text)el.textContent=text}}={}){
 const token={};tweens.set(el,token);// a later call on the same element supersedes this one
 if(isCalm()||from===to||!(duration>0)||typeof requestAnimationFrame!=='function'){write(format(to));return}
 const start=performance.now();
 const step=()=>{
  if(tweens.get(el)!==token)return;
  const p=Math.min(1,(performance.now()-start)/duration);
  write(format(p>=1?to:from+(to-from)*p));
  if(p<1)requestAnimationFrame(step);
 };
 requestAnimationFrame(step);
}

// Changed-value motion for views whose update path rewrites text (morph or innerHTML).
// snapshotText() before the write, animateChanges() after it: a value that differs flashes, and a
// single number with the same prefix/suffix (e.g. "10%" -> "20%") counts up from the old value.
// keyOf pairs old and new nodes; the default (node identity) suits morph, a stable attribute
// (an href, a data-key) suits innerHTML swaps. Under calm, the new text simply stays.
const NUM=/^(\D*?)(-?\d+(?:\.\d+)?)(\D*)$/;
const identity=el=>el;

export function snapshotText(root,selector,keyOf=identity){
 const out=new Map();
 if(!root)return out;
 for(const el of root.querySelectorAll(selector)){const k=keyOf(el);if(k!=null&&!out.has(k))out.set(k,el.textContent);}
 return out;
}

export function animateChanges(root,selector,before,keyOf=identity,{target=identity,count=true}={}){
 const changed=[];
 if(!root||!before?.size)return changed;
 for(const el of root.querySelectorAll(selector)){
  const k=keyOf(el);
  if(k==null||!before.has(k))continue;
  const old=before.get(k),now=el.textContent;
  if(old===now)continue;
  changed.push(el);
  if(isCalm())continue;
  const a=count&&old.trim().match(NUM),b=now.trim().match(NUM);
  if(count&&a&&b&&a[1]===b[1]&&a[3]===b[3]&&now===now.trim()){
   const places=(b[2].split('.')[1]||'').length;
   const format=v=>`${b[1]}${(Math.abs(v)<1e-9?0:v).toFixed(places)}${b[3]}`;
   el.textContent=format(+a[2]);// start from the old value in the same task: no jump to the end and back
   tween(el,+a[2],+b[2],{format});
  }
  flash(target(el));
 }
 return changed;
}

// Eases a <progress> (or any element with a numeric .value) from its old value to a new one.
// Call after the markup already holds the new value; under calm it stays there.
export function easeValue(el,from,to){
 if(!el||!Number.isFinite(from)||!Number.isFinite(to)||from===to||isCalm())return;
 el.value=from;
 tween(el,from,to,{format:v=>Math.round(v*10)/10,write:v=>{if(el.value!==v)el.value=v;}});
}

// Marks a node that was just added so it plays the shared enter transition ([data-entering]);
// nothing under calm. The marker clears itself when the animation ends.
export function enter(el){
 if(!el||isCalm())return;
 el.setAttribute('data-entering','');
 const clear=e=>{if(e&&e.target!==el)return;el.removeAttribute('data-entering');};
 el.addEventListener('animationend',clear,{once:true});
 setTimeout(()=>clear(),1000);
}

// Note: under calm, [data-leaving] runs no transition, so no transitionend fires; patch.js removes
// leaving nodes at once under calm and has a fallback timer otherwise.
export function flash(el){
 if(isCalm())return;
 el.setAttribute('data-flash','');
 setTimeout(()=>el.removeAttribute('data-flash'),600);
}
