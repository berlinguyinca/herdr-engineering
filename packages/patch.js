// Keyed DOM patcher: changes only what differs, never touches focus, open <details> or selection.
// Items are assumed to be plain JSON-serialisable data (change detection compares JSON.stringify).
const snapshots=new WeakMap();

export function patchText(el,value){
 const next=String(value??'');
 if(el.textContent===next)return false;
 el.textContent=next;return true;
}

export function patchAttr(el,name,value){
 if(value===null||value===undefined||value===false){
  if(!el.hasAttribute(name))return false;
  el.removeAttribute(name);return true;
 }
 const next=value===true?'':String(value);
 if(el.getAttribute(name)===next)return false;
 el.setAttribute(name,next);return true;
}

function calm(){
 const v=document.documentElement.getAttribute('data-calm');
 if(v==='off')return false;
 return v!==null||(typeof matchMedia==='function'&&matchMedia('(prefers-reduced-motion: reduce)').matches);
}

function holdsSelection(el){
 const sel=document.getSelection?.();
 if(!sel||sel.rangeCount===0||sel.isCollapsed)return false;
 return sel.containsNode(el,true)||el.contains(sel.anchorNode)||el.contains(sel.focusNode);
}

function protectedNode(el){
 if(el.contains(document.activeElement)&&document.activeElement!==document.body)return true;
 if(el.matches('details[open]')||el.querySelector('details[open]'))return true;
 return holdsSelection(el);
}

function build(rendered,key){
 let el=rendered;
 if(typeof rendered==='string'){
  const t=document.createElement('template');t.innerHTML=rendered.trim();el=t.content.firstElementChild||document.createElement('div');
 }
 el.setAttribute('data-key',key);return el;
}

// Default update: attributes are patched, then children are replaced wholesale when markup differs.
// This DISCARDS state of unfocused descendants (typed input values, toggled <details>); supply
// opts.update to patch finer-grained. Focused/open/selected rows are skipped before this runs.
function defaultUpdate(el,fresh){
 for(const {name} of [...el.attributes])if(!fresh.hasAttribute(name)&&name!=='data-key'&&!name.startsWith('data-entering'))el.removeAttribute(name);
 for(const {name,value} of [...fresh.attributes])if(name!=='data-key')patchAttr(el,name,value);
 if(el.innerHTML!==fresh.innerHTML)el.innerHTML=fresh.innerHTML;
}

function fallbackMs(){
 const raw=getComputedStyle(document.documentElement).getPropertyValue('--motion-normal').trim();
 const m=raw.match(/^([\d.]+)(ms|s)$/);
 if(!m)return 600;
 return Math.round(parseFloat(m[1])*(m[2]==='s'?1000:1)*3);
}

function leave(el,animate){
 if(!animate||calm()){el.remove();return;}
 if(el.hasAttribute('data-leaving'))return;
 el.setAttribute('data-leaving','');
 const done=e=>{if(e&&e.target!==el)return;if(el.hasAttribute('data-leaving'))el.remove();};
 el.addEventListener('transitionend',done);
 el.addEventListener('animationend',done);
 setTimeout(()=>done(),fallbackMs());
}

export function patchList(container,items,opts){
 const {key,render,update,animate=true}=opts;
 const existing=new Map();
 for(const child of container.children)if(child.hasAttribute('data-key'))existing.set(child.getAttribute('data-key'),child);
 const wanted=[];const seen=new Set();
 for(const item of items){
  const k=String(key(item));if(seen.has(k))continue;seen.add(k);
  const snap=JSON.stringify(item);
  let el=existing.get(k);
  if(!el){
   el=build(render(item),k);snapshots.set(el,snap);
   if(!calm()){
    el.setAttribute('data-entering','');
    const clear=e=>{if(e&&e.target!==el)return;el.removeAttribute('data-entering');};
    el.addEventListener('animationend',clear);el.addEventListener('transitionend',clear);setTimeout(()=>clear(),fallbackMs());
   }
  }else if(el.hasAttribute('data-leaving')){
   el.removeAttribute('data-leaving');// key reappeared: revive instead of duplicating
   if(snapshots.get(el)!==snap&&!protectedNode(el)){if(update)update(el,item);else defaultUpdate(el,build(render(item),k));snapshots.set(el,snap);}
  }else if(snapshots.get(el)!==snap&&!protectedNode(el)){
   if(update)update(el,item);else defaultUpdate(el,build(render(item),k));
   snapshots.set(el,snap);
  }
  wanted.push(el);
 }
 for(const [k,el] of existing)if(!seen.has(k)&&!el.hasAttribute('data-leaving'))leave(el,animate);
 let cursor=container.firstElementChild;
 for(const el of wanted){
  while(cursor&&cursor!==el&&cursor.hasAttribute('data-leaving')&&!wanted.includes(cursor))cursor=cursor.nextElementSibling;
  if(cursor===el)cursor=cursor.nextElementSibling;
  else container.insertBefore(el,cursor);
 }
}
