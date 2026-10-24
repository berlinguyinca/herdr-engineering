import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
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

export async function withPage(run){
 const server=serve();
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 let browser;
 try{
  const options={headless:true,args:['--no-sandbox']};
  if(process.env.PLAYWRIGHT_CHROMIUM)options.executablePath=process.env.PLAYWRIGHT_CHROMIUM;
  try{browser=await chromium.launch(options);}catch(error){const fallback='/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';if(!fs.existsSync(fallback))throw error;browser=await chromium.launch({...options,executablePath:fallback});}
  const page=await browser.newPage();
  await run(page,`http://127.0.0.1:${server.address().port}`);
 }finally{await browser?.close();server.close();}
}
