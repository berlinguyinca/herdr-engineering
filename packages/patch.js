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

// Tree morph for server-rendered markup: makes `el` match `fresh` (an Element, or an HTML string
// for el's children) with the fewest writes. Identical markup performs zero DOM writes. Children
// pair by key (data-key, data-section, data-host-id, id), else by position and tag. A <details>
// keeps its open state; custom elements are leaves (attributes only; replaced when an attribute
// they do not observe changes, since they render once on connect).
const PRESERVED=new Set(['data-key','data-entering','data-leaving']);
const keyOf=n=>n.nodeType!==1?null:(n.getAttribute('data-key')??n.getAttribute('data-section')??n.getAttribute('data-host-id')??(n.id||null));
const isCustom=el=>el.localName.includes('-');

function morphAttrs(el,fresh){
 const skip=name=>name==='open'&&el.localName==='details';
 for(const {name} of [...el.attributes])if(!fresh.hasAttribute(name)&&!PRESERVED.has(name)&&!skip(name))el.removeAttribute(name);
 for(const {name,value} of [...fresh.attributes])if(!skip(name))patchAttr(el,name,value);
}

function morphNode(old,fresh){
 if(old.nodeType!==1){if(old.nodeValue!==fresh.nodeValue)old.nodeValue=fresh.nodeValue;return old;}
 if(isCustom(old)){
  const changed=[...fresh.attributes].filter(a=>old.getAttribute(a.name)!==a.value).map(a=>a.name);
  if(!changed.length)return old;
  const observed=customElements.get(old.localName)?.observedAttributes||[];
  if(changed.every(n=>observed.includes(n))){for(const n of changed)patchAttr(old,n,fresh.getAttribute(n));return old;}
  const next=document.importNode(fresh,true);old.replaceWith(next);return next;
 }
 morphAttrs(old,fresh);
 morphChildren(old,fresh);
 return old;
}

function morphChildren(parent,freshParent){
 const olds=[...parent.childNodes];const used=new Set();
 const keyed=new Map();
 for(const o of olds){const k=keyOf(o);if(k!=null){if(!keyed.has(k))keyed.set(k,[]);keyed.get(k).push(o);}}
 let pos=0;
 const pairs=[...freshParent.childNodes].map(f=>{
  const k=keyOf(f);let match=null;
  if(k!=null){const list=keyed.get(k)||[];const i=list.findIndex(o=>o.localName===f.localName&&!used.has(o));if(i>=0)match=list[i];}
  else{
   while(pos<olds.length&&(used.has(olds[pos])||keyOf(olds[pos])!=null))pos++;
   const o=olds[pos];
   if(o&&o.nodeType===f.nodeType&&(o.nodeType!==1||o.localName===f.localName)){match=o;pos++;}
  }
  if(match)used.add(match);
  return [match,f];
 });
 for(const o of olds)if(!used.has(o))o.remove();
 pairs.forEach(([o,f],i)=>{
  const node=o?morphNode(o,f):document.importNode(f,true);
  const at=parent.childNodes[i]||null;
  if(at!==node)parent.insertBefore(node,at);
 });
}

export function morph(el,fresh){
 if(typeof fresh==='string'){const t=document.createElement('template');t.innerHTML=fresh;morphChildren(el,t.content);return;}
 morphAttrs(el,fresh);morphChildren(el,fresh);
}
