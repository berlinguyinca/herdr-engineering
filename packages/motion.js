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

export function tween(el,from,to,{format=v=>String(Math.round(v)),duration=300}={}){
 const write=text=>{if(el.textContent!==text)el.textContent=text};
 if(isCalm()||from===to||!(duration>0)||typeof requestAnimationFrame!=='function'){write(format(to));return}
 const start=performance.now();
 const step=()=>{
  const p=Math.min(1,(performance.now()-start)/duration);
  write(format(p>=1?to:from+(to-from)*p));
  if(p<1)requestAnimationFrame(step);
 };
 requestAnimationFrame(step);
}

export function flash(el){
 if(isCalm())return;
 el.setAttribute('data-flash','');
 setTimeout(()=>el.removeAttribute('data-flash'),600);
}
