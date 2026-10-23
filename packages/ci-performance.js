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
export function ciPerformanceModel(entries=[],observability={}) {
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
  return {...cacheModel(observability),measured:samples.length,invalid,excluded,duplicates,projects:grouped(samples,'project'),languages:grouped(samples,'language'),bins:histogram(samples.map(s=>s.durationMs)).map((count,i)=>({label:binLabels[i],count}))};
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
  return `<section class="ci-performance" aria-label="Successful CI execution performance"><h2>Successful run performance</h2><p class="ci-chart-note">${m.measured} successful runs with valid positive execution times. Start-to-finish includes setup, build, test and cache work; queued time is excluded. ${m.excluded} non-successful runs excluded; ${m.invalid} successful runs lack valid positive times.${m.duplicates?` ${m.duplicates} duplicate observations removed.`:''} Current primary repository language comes from repository metadata, not individual steps. Unknown project or language stays Unknown.</p>${m.measured?`<div class="ci-chart-grid"><section class="ci-chart-card"><h2>Successful run duration histogram</h2>${histogramPlot(m.bins)}</section><section class="ci-chart-card"><h2>Average execution time by project</h2>${meanPlot(m.projects,'Mean execution time by project')}</section><section class="ci-chart-card"><h2>Current primary repository language</h2><p class="ci-chart-note">Average successful execution time by language.</p>${meanPlot(m.languages,'Mean execution time by language')}</section></div>`:'<p class="ci-chart-note">No successful runs with valid positive start-to-finish times in this selection. Missing measurements do not mean zero duration.</p>'}</section>${renderCachePerformance(m)}`;
}
const table=(label,headers,rows)=>`<div class="ci-chart-table" tabindex="0" role="region" aria-label="${esc(label)}"><table><caption>${esc(label)}</caption><thead><tr>${headers.map(h=>`<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map((v,i)=>i===0?`<th scope="row">${esc(v)}</th>`:`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
export function renderCiPerformanceData(m) {
  return `<h3>Successful execution statistics</h3><p class="ci-chart-note">Mean and median use all measured successful runs. p95 uses the nearest-rank method (ceil(0.95 × count)); small samples are not reliable tail estimates. Exact values below are milliseconds. Histogram intervals include their lower bound and exclude their upper bound.</p>${table('Successful duration histogram',['Execution time','Runs'],m.bins.map(b=>[b.label,b.count]))}${[['Project',m.projects],['Current primary repository language',m.languages]].map(([label,groups])=>table(`${label} execution statistics`,[label,'Runs','Mean (ms)','Median (ms)','p95 (ms)'],groups.map(g=>[g.label,g.count,g.meanMs,g.medianMs,g.p95Ms]))+table(`${label} duration histogram`,[label,...binLabels],groups.map(g=>[g.label,...g.bins]))).join('')}${renderCacheData(m)}`;
}
export const ciPerformanceStyles=`<style>.ci-cache-performance{border-top:1px solid var(--border);margin-top:16px;padding-top:12px;min-width:0}.ci-cache-performance h2{font-size:16px}.ci-cache-performance h3{font-size:14px}.ci-cache-performance h4{font-size:12px}.ci-cache-card{min-width:0;border:1px solid var(--border);border-radius:var(--radius);padding:12px;overflow-wrap:anywhere;font-size:12px}.ci-cache-card svg{display:block;width:100%;max-height:180px}.ci-cache-card svg text{fill:var(--text-secondary);font:11px sans-serif}.ci-cache-card svg line{stroke:var(--border)}.ci-cache-controls{padding:0 16px;flex-wrap:wrap}.ci-cache-controls p{flex:1;min-width:0}.ci-cache-performance .ci-chart-table{max-width:100%;min-width:0}.ci-performance{border-top:1px solid var(--border);margin-top:16px;padding-top:12px}.ci-performance>h2{font-size:16px}.ci-performance-bars{list-style:none;padding:0;margin:12px 0}.ci-performance-bars li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 12px;margin:12px 0;font-size:12px}.ci-performance-bars li>span:first-child{overflow-wrap:anywhere}.ci-performance-bars strong{font-weight:500}.ci-performance-bars small{font-weight:400;color:var(--text-secondary)}.ci-performance-bars .ci-performance-histogram{grid-column:1/-1;width:100%;max-width:360px;max-height:74px}.ci-performance-track{grid-column:1/-1;display:block;height:7px;background:var(--border-subtle);border-radius:3px;overflow:hidden}.ci-performance-track i{display:block;height:100%;background:var(--accent)}.ci-chart-table caption{text-align:left;font-weight:600;padding:14px 6px 6px}.ci-chart-table:focus-visible{outline:2px solid var(--accent);outline-offset:2px}</style>`;

const objects=value=>Array.isArray(value)?value.filter(row=>row&&typeof row==='object'&&!Array.isArray(row)):[];
const finite=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0;
const exact=value=>finite(value)?value:'Not reported';
const percentile=(stats,key)=>stats?.sample_count>=5&&finite(stats[key])?stats[key]:null;
const groupLabel=g=>`${text(g.project_id)||text(g.repository)||'Unknown project'} · ${text(g.kind)||text(g.phase)||'Unknown phase'}`;
const windowLabel=w=>`${text(w?.start)||'Unknown start'} to ${text(w?.end)||'Unknown end'}`;
function cacheModel({data=null,filters={},loading=false,error=''}={}) {
  const contextUnsupported=!!(filters.mission||filters.session);
  const terms=text(filters.search).toLowerCase().split(/\s+/).filter(Boolean);
  const matches=row=>!contextUnsupported&&(!filters.repository||[row.repository,row.repository_id,String(row.provider_repo_id??'')].includes(filters.repository))&&(!filters.branch||row.branch===filters.branch)&&terms.every(term=>[row.project_id,row.repository,row.repository_id,row.branch,row.revision,row.kind,row.phase].filter(Boolean).join(' ').toLowerCase().includes(term));
  return {cacheGroups:objects(data?.cache_groups).filter(matches),trends:objects(data?.execution_trends).filter(matches),alerts:objects(data?.alerts).filter(matches),observability:{data,loading,error,contextUnsupported}};
}
function percentilePlot(label,points) {
  const values=points.flatMap(p=>['p50','p95'].map(key=>percentile(p.stats,key))).filter(finite);
  if(!values.length)return '<p class="ci-chart-note">Insufficient data: at least 5 measured samples per operation or window are required for p50/p95.</p>';
  const max=Math.max(1,...values);
  return `<svg viewBox="0 0 360 160" role="img" aria-label="${esc(label)}"><desc>p50 is solid; p95 is dashed. Values are seconds. Missing or insufficient measurements are omitted. Exact values follow in Chart data.</desc><text x="40" y="15">${esc(max)} s</text><line x1="40" y1="125" x2="330" y2="125"/>${['p50','p95'].map((key,k)=>{
    const dots=points.map((point,i)=>({value:percentile(point.stats,key),x:80+i*200})).filter(p=>finite(p.value));
    return `${dots.length===2?`<polyline points="${dots.map(p=>`${p.x},${125-p.value/max*95}`).join(' ')}" fill="none" stroke="${k?'var(--status-attention)':'var(--accent)'}" stroke-width="2" ${k?'stroke-dasharray="5 4"':''}/>`:''}${dots.map(p=>`<circle cx="${p.x}" cy="${125-p.value/max*95}" r="${k?5:3}" fill="${k?'var(--status-attention)':'var(--accent)'}"><title>${esc(key)}: ${esc(p.value)} seconds</title></circle>`).join('')}`;
  }).join('')}${points.map((p,i)=>`<text x="${80+i*200}" y="147" text-anchor="middle">${esc(p.label)}</text>`).join('')}</svg><p class="ci-chart-note">p50: solid · p95: dashed · seconds</p>`;
}
const alertRules={cache_budget:'Cache maintenance p95 reaches 90% of the 90-second budget',low_hit_rate:'Cache hit rate below 50% with at least 10 restore operations',execution_regression:'Execution p95 at least 2× the preceding window with at least 5 samples in both windows'};
function alertData(alerts) {
  return table('Regression alerts',['Project / cache or phase','Rule','Metric','Current value','Baseline / threshold','Samples','Baseline samples','Observation window'],alerts.map(a=>[groupLabel(a),alertRules[a.rule]||'Reported regression',a.metric,exact(a.current_value),exact(a.baseline_value),exact(a.sample_count),exact(a.baseline_sample_count),windowLabel(a.window)]));
}
function renderCachePerformance(m) {
  const {data,loading,error,contextUnsupported}=m.observability;
  const status=loading?'Loading cache health…':error?`${error}${data?' Showing last received cache observations; these may be stale.':''}`:!data?'Cache health is unavailable.':'';
  return `<section class="ci-cache-performance" aria-label="Cache health and regression trends"><h2>Cache health and regression trends</h2>${status?`<p class="ci-chart-note" role="status">${esc(status)}</p>`:''}${contextUnsupported?'<p class="ci-chart-note">Cache and execution aggregates cannot be scoped to a mission or session with this API. Aggregates and alerts are hidden; run charts above retain the selected context.</p>':data?`<p class="ci-chart-note">${esc(data.days)}-day observation window: ${esc(windowLabel(data.window))}. Cache v1 uses ingestion time; branch is unknown and branch filters exclude these records. Missing measurements are not zero. Percentiles need at least 5 measured samples. Partial coverage: retained observations only; this is not a complete history.${data.scope?.stale?' Reports are stale.':''}${data.scope?.persistence==='memory_only'?' Storage is memory-only; observations are lost on restart.':''}${finite(data.scope?.record_limit)?` Retention: up to ${data.scope.record_limit} telemetry records.`:''}</p><div class="ci-chart-grid">${m.cacheGroups.slice(0,10).map(g=>`<section class="ci-cache-card"><h3>${esc(groupLabel(g))}</h3><p>Cache hit rate: <strong>${finite(g.hit_rate)?`${Number((g.hit_rate*100).toFixed(2))}%`:'No restore operations'}</strong> · ${esc(exact(g.hit_count))} hits / ${esc(exact(g.restore_count))} restores</p><p class="ci-chart-note">${esc(exact(g.sample_count))} operations. Hit rate includes restore outcomes only; saves do not inflate the denominator.</p><h4>Restore and save p50/p95</h4>${percentilePlot(`${groupLabel(g)} restore and save percentiles`,[{label:'Restore',stats:g.restore},{label:'Save',stats:g.save}])}<p class="ci-chart-note">Selected-window comparison, not a historical cache trend.${['restore','save'].filter(op=>!finite(percentile(g[op],'p95'))).map(op=>` ${op}: Insufficient data (${exact(g[op]?.sample_count)} measured samples).`).join('')}</p></section>`).join('')}</div>${m.cacheGroups.length?`<p class="ci-chart-note">${Math.min(10,m.cacheGroups.length)} of ${m.cacheGroups.length} cache groups; ${Math.max(0,m.cacheGroups.length-10)} groups omitted from visuals. All groups are in Chart data.</p>`:'<p class="ci-chart-note">No cache observations match this selection.</p>'}<div class="ci-chart-grid">${m.trends.slice(0,10).map(g=>`<section class="ci-chart-card"><h3>${esc(groupLabel(g))}</h3><p class="ci-chart-note">${g.measurement_source==='provider_execution'?'Successful provider execution; start to finish, queue excluded':'Wrapped phase timing; distinct from provider execution'}. Current versus preceding window.</p>${percentilePlot(`${groupLabel(g)} execution trend`,[{label:'Preceding window',stats:g.baseline},{label:'Current window',stats:g}])}</section>`).join('')}</div>${m.trends.length?`<p class="ci-chart-note">${Math.min(10,m.trends.length)} of ${m.trends.length} execution trend groups shown. All groups are in Chart data.</p>`:''}<h3>Informational regression alerts</h3><p class="ci-chart-note">Visible observations only; no notifications or pipeline changes. Time metrics are seconds; hit rate is a fraction.</p>${m.alerts.length?alertData(m.alerts):'<p class="ci-chart-note">No regression alerts reported for this selection; missing or insufficient samples cannot establish health.</p>'}`:''}</section>`;
}
function renderCacheData(m) {
  if(!m.observability.data||m.observability.contextUnsupported)return '';
  return `<h3>Cache and execution observation data</h3><p class="ci-chart-note">Exact values are seconds; hit rate is a fraction. Insufficient data means fewer than 5 measured samples, not zero. Window: ${esc(windowLabel(m.observability.data.window))}.</p>${table('Cache hit rates',['Project / cache','Repository','Branch','Operations','Restore operations','Hits','Hit rate'],m.cacheGroups.map(g=>[groupLabel(g),g.repository,g.branch||'Unknown',exact(g.sample_count),exact(g.restore_count),exact(g.hit_count),exact(g.hit_rate)]))}${table('Cache operation statistics',['Project / cache','Operation','Samples','p50 (seconds)','p95 (seconds)','Mean (seconds)'],m.cacheGroups.flatMap(g=>['restore','save'].map(op=>[groupLabel(g),op,exact(g[op]?.sample_count),percentile(g[op],'p50')??'Insufficient data',percentile(g[op],'p95')??'Insufficient data',exact(g[op]?.mean)])))}${table('Execution trend statistics',['Project / phase','Source','Window','Samples','p50 (seconds)','p95 (seconds)','Mean (seconds)'],m.trends.flatMap(g=>[['Preceding',g.baseline],['Current',g]].map(([label,stats])=>[groupLabel(g),g.measurement_source,label,exact(stats?.sample_count),percentile(stats,'p50')??'Insufficient data',percentile(stats,'p95')??'Insufficient data',exact(stats?.mean)])))}`;
}
