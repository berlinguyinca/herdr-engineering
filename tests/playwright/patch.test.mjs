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
