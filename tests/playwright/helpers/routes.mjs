// Every route the SPA's router knows, read from the ROUTES table in packages/app.js so a new route
// is covered by the suites that loop over them. ':id' becomes a sample id the stub API answers for;
// a Pi mission (its own page shape) is added beside the recorded one.
import fs from 'node:fs';
import path from 'node:path';

const APP=path.resolve(import.meta.dirname,'../../../packages/app.js');
const SAMPLE={missions:'m-1',session:'s-1',services:'svc-1',hosts:'h-1'};

export function appRoutes(){
 const source=fs.readFileSync(APP,'utf8');
 const table=source.match(/const ROUTES = \{([\s\S]*?)\n\};/);
 if(!table)throw new Error('ROUTES table not found in app.js');
 const routes=[...table[1].matchAll(/'([^']+)':/g)].map(m=>m[1].replace(/\/([a-z-]+)\/:id/,(_,seg)=>`/${seg}/${SAMPLE[seg]??'x-1'}`));
 if(routes.includes('/missions/m-1'))routes.splice(routes.indexOf('/missions/m-1')+1,0,'/missions/pi%3Amsn-1');
 return routes;
}
