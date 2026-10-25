import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {chromium} from 'playwright';

const root=path.resolve(import.meta.dirname,'../../..');
const packages=path.join(root,'packages');
const types={'.js':'text/javascript','.mjs':'text/javascript','.css':'text/css','.html':'text/html','.json':'application/json','.svg':'image/svg+xml','.md':'text/markdown'};

// Static server over packages/ plus a blank page at /__blank for module-level DOM tests.
function serve(){
 return http.createServer((req,res)=>{
  const url=new URL(req.url,'http://x');
  if(url.pathname==='/__blank'){res.writeHead(200,{'content-type':'text/html'});return res.end('<!doctype html><html><head><meta charset="utf-8"><title>blank</title></head><body></body></html>');}
  const file=path.resolve(packages,'.'+decodeURIComponent(url.pathname==='/'?'/index.html':url.pathname));
  if(!file.startsWith(packages+path.sep)||!fs.existsSync(file)||fs.statSync(file).isDirectory()){res.writeHead(404);return res.end('not found');}
  res.writeHead(200,{'content-type':types[path.extname(file)]||'application/octet-stream'});fs.createReadStream(file).pipe(res);
 });
}

// Browsers the playwright CLI installed into its own cache, newest revision first. Used only when the
// browser matching the installed playwright package is missing (an older/newer browser cached).
export function cachedChromiums(cache=path.join(os.homedir(),'.cache','ms-playwright')){
 let dirs=[];
 try{dirs=fs.readdirSync(cache).filter(d=>/^chromium-\d+$/.test(d));}catch{return [];}
 return dirs.sort((a,b)=>Number(b.split('-')[1])-Number(a.split('-')[1]))
  .flatMap(d=>['chrome-linux64/chrome','chrome-linux/chrome','chrome-mac/Chromium.app/Contents/MacOS/Chromium','chrome-win/chrome.exe'].map(f=>path.join(cache,d,f)))
  .filter(f=>fs.existsSync(f));
}

async function launchChromium(options){
 try{return await chromium.launch(options);}
 catch(error){
  for(const executablePath of cachedChromiums()){try{return await chromium.launch({...options,executablePath});}catch{/* try the next one */}}
  throw error;
 }
}

export async function withPage(run){
 const server=serve();
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 let browser;
 try{
  browser=await launchChromium({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage();
  await run(page,`http://127.0.0.1:${server.address().port}`);
 }finally{await browser?.close();server.close();}
}
