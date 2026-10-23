// Execution-time statistics for successful observed runs, not compile-only time.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
import {reportTimestamp} from './record-state.js';

const text=value=>typeof value==='string'?value.trim():'';
const timestamp=value=>new Date(reportTimestamp(value)).getTime();
const limits=[60000,300000,600000,1800000,3600000,Infinity];
const binLabels=['<1 min','1–<5 min','5–<10 min','10–<30 min','30–<60 min','≥60 min'];
export function ciRunKey(r) {
  let provider=text(r.provider_url).replace(/\/+$/,'');
  if(provider)try{const url=new URL(provider);provider=url.origin+url.pathname.replace(/\/+$/,'');}catch{/* Keep an explicit non-URL provider identity as reported. */}
  else if(text(r.url))try{
    const url=new URL(r.url),route=url.pathname.match(/^(.*?)\/repos\/[^/]+\/pipeline\/[^/]+\/?$/);
    // Woodpecker run URLs retain the deployment prefix; other URL shapes only identify an origin.
    provider=url.origin+(route?route[1].replace(/\/+$/,''):'');
  }catch{/* Unreported provider remains explicit in the key. */}
  return JSON.stringify([provider,String(r.provider_repo_id??r.repository_id??r.repository??''),String(r.run_number??'')]);
}
const histogram=values=>limits.map((limit,i)=>values.filter(value=>value<limit&&(i===0||value>=limits[i-1])).length);
function grouped(samples,field) {
  const groups=new Map();
  for(const sample of samples){const label=sample[field];if(!groups.has(label))groups.set(label,[]);groups.get(label).push(sample.durationMs);}
  return [...groups].map(([label,values])=>{
    values.sort((a,b)=>a-b);const count=values.length,mid=Math.floor(count/2);
    return {label,count,meanMs:values.reduce((sum,value)=>sum+value,0)/count,medianMs:count%2?values[mid]:(values[mid-1]+values[mid])/2,p95Ms:values[Math.ceil(count*.95)-1],bins:histogram(values)};
  }).sort((a,b)=>b.meanMs-a.meanMs||a.label.localeCompare(b.label));
}
export function ciPerformanceModel(entries=[]) {
  const unique=new Map();let duplicates=0;
  // Entries are newest-first; retain the freshest observation of each run.
  for(const entry of entries){const key=ciRunKey(entry.record);if(unique.has(key)){duplicates++;continue;}unique.set(key,entry.record);}
  const samples=[];let invalid=0,excluded=0;
  for(const r of unique.values()){
    if(!['success','passed'].includes(text(r.state||r.status).toLowerCase())){excluded++;continue;}
    const start=timestamp(r.started_at??r.started),end=timestamp(r.finished_at??r.finished);
    if(!Number.isFinite(start)||!Number.isFinite(end)||start<=0||end<=start){invalid++;continue;}
    samples.push({durationMs:end-start,project:text(r.project_id)||'Unknown',language:text(r.language)||'Unknown'});
  }
  return {measured:samples.length,invalid,excluded,duplicates,projects:grouped(samples,'project'),languages:grouped(samples,'language'),bins:histogram(samples.map(s=>s.durationMs)).map((count,i)=>({label:binLabels[i],count}))};
}
function histogramPlot(bins) {
  const max=Math.max(1,...bins.map(b=>b.count));
  return `<svg viewBox="0 0 480 180" role="img" aria-label="Successful run duration histogram"><desc>Counts in non-overlapping duration intervals. Exact counts follow in Chart data.</desc><line x1="32" y1="140" x2="472" y2="140"/><text x="24" y="34" text-anchor="end">${max}</text><text x="24" y="144" text-anchor="end">0</text>${bins.map((b,i)=>`<rect x="${42+i*72}" y="${140-b.count/max*108}" width="50" height="${b.count/max*108}" fill="var(--accent)"><title>${esc(b.label)}: ${b.count} runs</title></rect><text x="${67+i*72}" y="160" text-anchor="middle">${esc(b.label)}</text>`).join('')}</svg>`;
}
function groupHistogram(group,label) {
  const max=Math.max(1,...group.bins);
  return `<svg class="ci-performance-histogram" viewBox="0 0 360 74" role="img" aria-label="${esc(label+' '+group.label+' duration histogram')}"><desc>Counts by execution time; bars scale within this group. Exact counts follow in Chart data.</desc>${group.bins.map((count,i)=>`<rect x="${8+i*60}" y="${42-count/max*30}" width="44" height="${count/max*30}" fill="var(--accent)"><title>${esc(binLabels[i])}: ${count} runs</title></rect><text x="${30+i*60}" y="10" text-anchor="middle">${count}</text><text x="${30+i*60}" y="62" text-anchor="middle">${esc(binLabels[i])}</text>`).join('')}</svg>`;
}
function meanPlot(groups,label) {
  const shown=groups.slice(0,10),max=Math.max(1,...shown.map(g=>g.meanMs));
  return `<ol class="ci-performance-bars" aria-label="${esc(label)}">${shown.map(g=>`<li><span>${esc(g.label)}</span><strong>${esc(formatDuration(g.meanMs))} <small>· ${g.count} runs</small></strong><span class="ci-performance-track" aria-hidden="true"><i style="width:${g.meanMs/max*100}%"></i></span>${groupHistogram(g,label==='Mean execution time by project'?'Project':'Current primary repository language')}</li>`).join('')}</ol><p class="ci-chart-note">${shown.length} of ${groups.length} ${label==='Mean execution time by project'?'projects':'languages'}, ordered by mean duration. All groups and histogram counts are in Chart data.</p>`;
}
export function renderCiPerformance(m) {
  return `<section class="ci-performance" aria-label="Successful CI execution performance"><h2>Successful run performance</h2><p class="ci-chart-note">${m.measured} successful runs with valid positive execution times. Start-to-finish includes setup, build, test and cache work; queued time is excluded. ${m.excluded} non-successful runs excluded; ${m.invalid} successful runs lack valid positive times.${m.duplicates?` ${m.duplicates} duplicate observations removed.`:''} Current primary repository language comes from repository metadata, not individual steps. Unknown project or language stays Unknown.</p>${m.measured?`<div class="ci-chart-grid"><section class="ci-chart-card"><h2>Successful run duration histogram</h2>${histogramPlot(m.bins)}</section><section class="ci-chart-card"><h2>Average execution time by project</h2>${meanPlot(m.projects,'Mean execution time by project')}</section><section class="ci-chart-card"><h2>Current primary repository language</h2><p class="ci-chart-note">Average successful execution time by language.</p>${meanPlot(m.languages,'Mean execution time by language')}</section></div>`:'<p class="ci-chart-note">No successful runs with valid positive start-to-finish times in this selection. Missing measurements do not mean zero duration.</p>'}</section>`;
}
const table=(label,headers,rows)=>`<div class="ci-chart-table" tabindex="0" role="region" aria-label="${esc(label)}"><table><caption>${esc(label)}</caption><thead><tr>${headers.map(h=>`<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map((v,i)=>i===0?`<th scope="row">${esc(v)}</th>`:`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
export function renderCiPerformanceData(m) {
  return `<h3>Successful execution statistics</h3><p class="ci-chart-note">Mean and median use all measured successful runs. p95 uses the nearest-rank method (ceil(0.95 × count)); small samples are not reliable tail estimates. Exact values below are milliseconds. Histogram intervals include their lower bound and exclude their upper bound.</p>${table('Successful duration histogram',['Execution time','Runs'],m.bins.map(b=>[b.label,b.count]))}${[['Project',m.projects],['Current primary repository language',m.languages]].map(([label,groups])=>table(`${label} execution statistics`,[label,'Runs','Mean (ms)','Median (ms)','p95 (ms)'],groups.map(g=>[g.label,g.count,g.meanMs,g.medianMs,g.p95Ms]))+table(`${label} duration histogram`,[label,...binLabels],groups.map(g=>[g.label,...g.bins]))).join('')}`;
}
export const ciPerformanceStyles=`<style>.ci-performance{border-top:1px solid var(--border);margin-top:16px;padding-top:12px}.ci-performance>h2{font-size:16px}.ci-performance-bars{list-style:none;padding:0;margin:12px 0}.ci-performance-bars li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 12px;margin:12px 0;font-size:12px}.ci-performance-bars li>span:first-child{overflow-wrap:anywhere}.ci-performance-bars strong{font-weight:500}.ci-performance-bars small{font-weight:400;color:var(--text-secondary)}.ci-performance-bars .ci-performance-histogram{grid-column:1/-1;width:100%;max-width:360px;max-height:74px}.ci-performance-track{grid-column:1/-1;display:block;height:7px;background:var(--border-subtle);border-radius:3px;overflow:hidden}.ci-performance-track i{display:block;height:100%;background:var(--accent)}.ci-chart-table caption{text-align:left;font-weight:600;padding:14px 6px 6px}.ci-chart-table:focus-visible{outline:2px solid var(--accent);outline-offset:2px}</style>`;
