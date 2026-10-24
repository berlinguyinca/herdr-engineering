// Keyed DOM patcher: changes only what differs, never touches focus, open <details> or selection.
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
 return document.documentElement.hasAttribute('data-calm')||(typeof matchMedia==='function'&&matchMedia('(prefers-reduced-motion: reduce)').matches);
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
  const t=document.createElement('template');t.innerHTML=rendered.trim();el=t.content.firstElementChild;
 }
 el.setAttribute('data-key',key);return el;
}

function defaultUpdate(el,fresh){
 for(const {name} of [...el.attributes])if(!fresh.hasAttribute(name)&&name!=='data-key'&&!name.startsWith('data-entering'))el.removeAttribute(name);
 for(const {name,value} of [...fresh.attributes])if(name!=='data-key')patchAttr(el,name,value);
 if(el.innerHTML!==fresh.innerHTML)el.innerHTML=fresh.innerHTML;
}

function leave(el,animate){
 if(!animate||calm()){el.remove();return;}
 if(el.hasAttribute('data-leaving'))return;
 el.setAttribute('data-leaving','');
 const done=()=>el.remove();
 el.addEventListener('transitionend',done,{once:true});
 el.addEventListener('animationend',done,{once:true});
 setTimeout(done,600);
}

export function patchList(container,items,opts){
 const {key,render,update,animate=true}=opts;
 const existing=new Map();
 for(const child of container.children)if(!child.hasAttribute('data-leaving')&&child.hasAttribute('data-key'))existing.set(child.getAttribute('data-key'),child);
 const wanted=[];const seen=new Set();
 for(const item of items){
  const k=String(key(item));if(seen.has(k))continue;seen.add(k);
  const snap=JSON.stringify(item);
  let el=existing.get(k);
  if(!el){
   el=build(render(item),k);snapshots.set(el,snap);
   if(!calm()){
    el.setAttribute('data-entering','');
    const clear=()=>el.removeAttribute('data-entering');
    el.addEventListener('animationend',clear,{once:true});el.addEventListener('transitionend',clear,{once:true});setTimeout(clear,600);
   }
  }else if(snapshots.get(el)!==snap&&!protectedNode(el)){
   if(update)update(el,item);else defaultUpdate(el,build(render(item),k));
   snapshots.set(el,snap);
  }
  wanted.push(el);
 }
 for(const [k,el] of existing)if(!seen.has(k))leave(el,animate);
 let cursor=container.firstElementChild;
 for(const el of wanted){
  while(cursor&&cursor!==el&&cursor.hasAttribute('data-leaving'))cursor=cursor.nextElementSibling;
  if(cursor===el)cursor=cursor.nextElementSibling;
  else container.insertBefore(el,cursor);
 }
}
