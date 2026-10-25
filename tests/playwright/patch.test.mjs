import {test} from 'node:test';
import assert from 'node:assert/strict';
import {withPage} from './helpers/fixture.mjs';

async function open(page,base){
 await page.goto(`${base}/__blank`);
 await page.evaluate(async()=>{
  const m=await import('/patch.js');Object.assign(window,m);
  document.body.innerHTML='<div id="c"></div>';
  window.c=document.getElementById('c');
  window.opts={key:i=>i.id,render:i=>`<div data-key="${i.id}">${i.text}<input></div>`,update:(el,i)=>{el.firstChild.nodeValue=i.text;},animate:false};
 });
}

test('patchList inserts, updates in place, reorders and removes by key',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  const out={};
  patchList(c,[{id:'a',text:'A'},{id:'b',text:'B'}],opts);
  const a=c.children[0],b=c.children[1];
  out.entering=[...c.children].map(e=>e.hasAttribute('data-entering'));
  patchList(c,[{id:'a',text:'A2'},{id:'b',text:'B'}],opts);
  out.sameNode=c.children[0]===a;out.updated=a.firstChild.nodeValue;
  patchList(c,[{id:'b',text:'B'},{id:'a',text:'A2'}],opts);
  out.reordered=c.children[0]===b&&c.children[1]===a;
  patchList(c,[{id:'a',text:'A2'}],opts);
  out.removed=c.children.length===1&&c.children[0]===a;
  return out;
 });
 assert.deepEqual(r.entering,[true,true]);assert.equal(r.sameNode,true);assert.equal(r.updated,'A2');assert.equal(r.reordered,true);assert.equal(r.removed,true);
}));

test('patchList animated removal waits for transitionend',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  const o={...opts,animate:true};const out={};
  patchList(c,[{id:'a',text:'A'},{id:'b',text:'B'}],o);
  const b=c.children[1];
  patchList(c,[{id:'a',text:'A'}],o);
  out.leaving=b.hasAttribute('data-leaving')&&b.parentNode===c;
  b.dispatchEvent(new Event('transitionend',{bubbles:true}));
  out.gone=b.parentNode===null;
  return out;
 });
 assert.equal(r.leaving,true);assert.equal(r.gone,true);
}));

test('patchList removes immediately in calm mode',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const gone=await page.evaluate(()=>{
  document.documentElement.setAttribute('data-calm','');
  const o={...opts,animate:true};
  patchList(c,[{id:'a',text:'A'},{id:'b',text:'B'}],o);const b=c.children[1];
  patchList(c,[{id:'a',text:'A'}],o);
  return b.parentNode===null;
 });
 assert.equal(gone,true);
}));

test('identical data causes zero DOM mutations',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const count=await page.evaluate(async()=>{
  const items=[{id:'a',text:'A'},{id:'b',text:'B'}];
  patchList(c,items,opts);
  const seen=[];const mo=new MutationObserver(r=>seen.push(...r));mo.observe(c,{subtree:true,childList:true,attributes:true,characterData:true});
  patchList(c,items.map(i=>({...i})),opts);patchList(c,items,opts);
  await new Promise(r=>setTimeout(r,50));seen.push(...mo.takeRecords());mo.disconnect();
  return seen.length;
 });
 assert.equal(count,0);
}));

test('focused input and open details survive an update of that item',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  const o={key:i=>i.id,render:i=>`<div data-key="${i.id}"><span>${i.text}</span><input><details><summary>s</summary>x</details></div>`,update:(el,i)=>{el.querySelector('span').textContent=i.text;}};
  patchList(c,[{id:'a',text:'A'},{id:'b',text:'B'}],o);
  const input=c.children[0].querySelector('input');input.focus();input.value='typed';
  c.children[1].querySelector('details').open=true;
  patchList(c,[{id:'a',text:'A2'},{id:'b',text:'B2'}],o);
  return {focus:document.activeElement===input,value:input.value,aText:c.children[0].querySelector('span').textContent,
   open:c.children[1].querySelector('details').open,bText:c.children[1].querySelector('span').textContent};
 });
 assert.equal(r.focus,true);assert.equal(r.value,'typed');assert.equal(r.aText,'A');assert.equal(r.open,true);assert.equal(r.bText,'B');
}));

test('patchText and patchAttr report whether they wrote',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(async()=>{
  const el=document.createElement('p');el.textContent='x';el.setAttribute('title','t');c.append(el);
  const seen=[];const mo=new MutationObserver(m=>seen.push(...m));mo.observe(el,{subtree:true,childList:true,attributes:true,characterData:true});
  const same=[patchText(el,'x'),patchAttr(el,'title','t')];
  await new Promise(r=>setTimeout(r,20));const idle=seen.length+mo.takeRecords().length;
  const wrote=[patchText(el,'y'),patchAttr(el,'title','u')];
  return {same,idle,wrote,text:el.textContent,title:el.getAttribute('title')};
 });
 assert.deepEqual(r.same,[false,false]);assert.equal(r.idle,0);assert.deepEqual(r.wrote,[true,true]);assert.equal(r.text,'y');assert.equal(r.title,'u');
}));

test('default update (no update callback): changes write, identical is silent, focused row untouched',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(async()=>{
  const o={key:i=>i.id,render:i=>`<div data-key="${i.id}"><span>${i.text}</span><input></div>`,animate:false};
  patchList(c,[{id:'a',text:'A'},{id:'b',text:'B'}],o);
  const a=c.children[0],b=c.children[1];
  const seen=[];const mo=new MutationObserver(m=>seen.push(...m));mo.observe(c,{subtree:true,childList:true,attributes:true,characterData:true});
  patchList(c,[{id:'a',text:'A'},{id:'b',text:'B'}],o);
  await new Promise(r=>setTimeout(r,20));const idle=seen.length+mo.takeRecords().length;mo.disconnect();
  b.querySelector('input').focus();
  patchList(c,[{id:'a',text:'A2'},{id:'b',text:'B2'}],o);
  return {idle,same:c.children[0]===a&&c.children[1]===b,a:a.querySelector('span').textContent,b:b.querySelector('span').textContent};
 });
 assert.equal(r.idle,0);assert.equal(r.same,true);assert.equal(r.a,'A2');assert.equal(r.b,'B');
}));

test('a row holding the text selection is not rewritten',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  const o={key:i=>i.id,render:i=>`<div data-key="${i.id}"><span>${i.text}</span></div>`,animate:false};
  patchList(c,[{id:'a',text:'A'},{id:'b',text:'B'}],o);
  const range=document.createRange();range.selectNodeContents(c.children[0].firstChild);
  const sel=getSelection();sel.removeAllRanges();sel.addRange(range);
  patchList(c,[{id:'a',text:'A2'},{id:'b',text:'B2'}],o);
  return [c.children[0].textContent,c.children[1].textContent];
 });
 assert.deepEqual(r,['A','B2']);
}));

test('render without a root element does not throw; leaving key reappearing is revived',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  const out={};
  try{patchList(c,[{id:'x'},{id:'y'}],{key:i=>i.id,render:i=>i.id==='x'?'':`<div>${i.id}</div>`,animate:false});out.threw=false;}catch(e){out.threw=String(e);}
  out.count=c.children.length;
  c.innerHTML='';
  const o={key:i=>i.id,render:i=>`<div>${i.id}</div>`,animate:true};
  patchList(c,[{id:'a'}],o);const a=c.children[0];
  patchList(c,[],o);const leaving=a.hasAttribute('data-leaving');
  patchList(c,[{id:'a'}],o);
  a.dispatchEvent(new Event('transitionend'));
  out.revived=leaving&&c.children.length===1&&c.children[0]===a&&!a.hasAttribute('data-leaving')&&a.parentNode===c;
  const child=document.createElement('i');a.append(child);
  patchList(c,[],o);child.dispatchEvent(new Event('transitionend',{bubbles:true}));
  out.bubbleIgnored=a.parentNode===c;
  return out;
 });
 assert.equal(r.threw,false);assert.equal(r.count,2);assert.equal(r.revived,true);assert.equal(r.bubbleIgnored,true);
}));

test('morph: identical markup writes nothing; changes patch in place, keyed children keep nodes, open details stay open',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  customElements.define('x-once',class extends HTMLElement{connectedCallback(){this.textContent='v='+this.getAttribute('value');}});
  const html=(a,b,v)=>`<p data-section="a">${a}</p><details data-section="d"><summary>S ${b}</summary>body</details><x-once value="${v}"></x-once><p data-section="b">B</p>`;
  morph(c,html(1,1,1));
  const [pa,det,once,pb]=c.children;det.open=true;
  const muts=[];const obs=new MutationObserver(rs=>muts.push(...rs));obs.observe(c,{subtree:true,childList:true,attributes:true,characterData:true});
  morph(c,html(1,1,1));const identical=obs.takeRecords().length+muts.length;
  morph(c,html(2,2,1));const changed=obs.takeRecords();
  morph(c,`<p data-section="b">B</p><p data-section="a">2</p><details data-section="d"><summary>S 2</summary>body</details><x-once value="9"></x-once>`);
  obs.disconnect();
  return {identical,changedTargets:changed.map(m=>m.target.nodeType===3?m.target.parentNode.localName:m.target.localName),
   keep:c.children[0]===pb&&c.children[1]===pa&&c.children[2]===det,open:det.open,
   once:c.querySelector('x-once')!==once&&c.querySelector('x-once').textContent==='v=9'};
 });
 assert.equal(r.identical,0);
 assert.deepEqual(r.changedTargets.sort(),['p','summary']);
 assert.deepEqual({keep:r.keep,open:r.open,once:r.once},{keep:true,open:true,once:true});
}));

test('morph never moves or replaces a focused node or one holding the selection',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  morph(c,'<p data-key="a">A</p><input data-key="b"><div data-key="d"><button>go</button></div>');
  const input=c.querySelector('input');input.focus();
  morph(c,'<input data-key="b"><p data-key="a">A</p><div data-key="d"><button>go</button></div>');
  const out={inputKept:c.querySelector('input')===input,inputFocused:document.activeElement===input};
  const btn=c.querySelector('button');btn.focus();
  morph(c,'<input data-key="b"><p data-key="a">A</p><div data-key="d"><a href="#">go</a></div>');
  out.buttonKept=btn.isConnected&&document.activeElement===btn;
  btn.blur();
  morph(c,'<p>hello world</p>');
  const p=c.querySelector('p');const range=document.createRange();range.selectNodeContents(p);getSelection().removeAllRanges();getSelection().addRange(range);
  morph(c,'<span>hello world</span>');
  out.selectionKept=p.isConnected&&!getSelection().isCollapsed&&getSelection().toString()==='hello world';
  getSelection().removeAllRanges();
  morph(c,'<span>hello world</span>');
  out.replacedLater=!p.isConnected&&c.firstElementChild.localName==='span';
  return out;
 });
 assert.deepEqual(r,{inputKept:true,inputFocused:true,buttonKept:true,selectionKept:true,replacedLater:true});
}));

test('morph syncs form-control properties, except on a focused control',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  morph(c,'<input id="t" value="a"><input id="k" type="checkbox"><select id="s"><option value="1">1</option><option value="2">2</option></select><textarea id="x">one</textarea>');
  const [t,k,s,x]=['t','k','s','x'].map(id=>document.getElementById(id));
  t.value='typed';k.checked=true;s.value='2';x.value='edited';
  morph(c,'<input id="t" value="b"><input id="k" type="checkbox"><select id="s"><option value="1" selected>1</option><option value="2">2</option></select><textarea id="x">two</textarea>');
  const out={t:t.value,k:k.checked,s:s.value,x:x.value};
  t.focus();t.value='mine';
  morph(c,'<input id="t" value="c"><input id="k" type="checkbox" checked><select id="s"><option value="1" selected>1</option><option value="2">2</option></select><textarea id="x">two</textarea>');
  out.focused=t.value;out.k2=k.checked;
  return out;
 });
 assert.deepEqual(r,{t:'b',k:false,s:'1',x:'two',focused:'mine',k2:true});
}));

test('morph detects attributes removed from custom elements without dropping self-stamped ones',async()=>withPage(async(page,base)=>{
 await open(page,base);
 const r=await page.evaluate(()=>{
  customElements.define('x-obs',class extends HTMLElement{static observedAttributes=['mode'];connectedCallback(){this.setAttribute('role','status');}});
  customElements.define('x-render',class extends HTMLElement{connectedCallback(){this.textContent=this.getAttribute('hint')||'none';}});
  morph(c,'<x-obs mode="a"></x-obs><x-render hint="h"></x-render>');
  const obs=c.querySelector('x-obs'),ren=c.querySelector('x-render');
  morph(c,'<x-obs></x-obs><x-render></x-render>');
  const after=c.querySelector('x-render');
  return {obsKept:c.querySelector('x-obs')===obs,mode:obs.hasAttribute('mode'),role:obs.getAttribute('role'),replaced:after!==ren,text:after.textContent};
 });
 assert.deepEqual(r,{obsKept:true,mode:false,role:'status',replaced:true,text:'none'});
}));
