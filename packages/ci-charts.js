// Small plots of the received CI sample, never an inferred all-time history.
import {escapeHtml as esc} from './herdr-web-components/util.js';
import {formatDuration} from './herdr-web-components/format.js';
import {ciPerformanceModel,renderCiPerformance,renderCiPerformanceData,ciPerformanceStyles} from './ci-performance.js';
import {reportTimestamp} from './record-state.js';

const timestamp=value=>new Date(reportTimestamp(value)).getTime();

export function ciChartModel(entries = [], performance = {}) {
  const rows=Array.isArray(entries) ? entries.filter(e=>e?.record && typeof e.record==='object' && !Array.isArray(e.record) && ['current','failed','finished'].includes(e.tab)) : [];
  const days=new Map(), durations=[];
  let undated=0, unmeasured=0;
  for(const {record:r,tab,id} of rows) {
    const created=timestamp(r.created_at ?? r.created);
    if(Number.isFinite(created)) {
      const day=new Date(created).toISOString().slice(0,10);
      if(!days.has(day))days.set(day,{day,current:0,failed:0,finished:0});
      days.get(day)[tab]++;
    } else undated++;
    if(tab==='current')continue;
    const start=timestamp(r.started_at ?? r.started), end=timestamp(r.finished_at ?? r.finished);
    if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start){unmeasured++;continue;}
    durations.push({id,repository:typeof r.repository==='string'?r.repository:'Repository not reported',runNumber:r.run_number,tab,finishedAt:end,durationMs:end-start});
  }
  const ordered=[...days.values()].sort((a,b)=>a.day.localeCompare(b.day));
  durations.sort((a,b)=>a.finishedAt-b.finishedAt || String(a.id).localeCompare(String(b.id)));
  return {performance:ciPerformanceModel(rows,performance),total:rows.length,undated,older:ordered.slice(0,-14).reduce((sum,d)=>sum+d.current+d.failed+d.finished,0),days:ordered.slice(-14),
    measured:durations.length,unmeasured,durations:durations.slice(-20)};
}
const labels={finished:'Finished',failed:'Failed',current:'Open / unknown'};
const colors={finished:'var(--status-ok)',failed:'var(--status-attention)',current:'var(--accent)'};
const number=value=>Math.round(value*100)/100;
const runLabel=run=>`${run.repository} · Run #${run.runNumber ?? 'unknown'}`;
const utc=value=>new Date(value).toISOString().replace('T',' ').replace(/\.\d{3}Z$/,' UTC');
const table=(headers,rows)=>`<div class="ci-chart-table"><table><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
function outcomePlot(days) {
  if(!days.length)return '<p class="ci-chart-note">No dated runs in this selection.</p>';
  const maximum=Math.max(...days.map(day=>day.current+day.failed+day.finished));
  const width=300/days.length, barWidth=Math.min(28,width*.7);
  const bars=days.map((day,index)=>{
    let y=130;
    return ['finished','failed','current'].map(tab=>{
      const height=day[tab]/maximum*100;y-=height;
      return day[tab] ? `<rect x="${number(42+index*width+(width-barWidth)/2)}" y="${number(y)}" width="${number(barWidth)}" height="${number(height)}" fill="${colors[tab]}"><title>${day.day}: ${day[tab]} ${labels[tab]}</title></rect>` : '';
    }).join('');
  }).join('');
  const ticks=[...new Set([0,Math.floor((days.length-1)/2),days.length-1])].map(i=>`<text x="${number(42+(i+.5)*width)}" y="150" text-anchor="middle">${days[i].day.slice(5)}</text>`).join('');
  return `<svg viewBox="0 0 360 165" role="img" aria-labelledby="ci-outcomes-title ci-outcomes-desc"><title id="ci-outcomes-title">Observed run outcomes by UTC creation day</title><desc id="ci-outcomes-desc">${days.length} reported dates. Stacked counts distinguish finished, failed and open or unknown runs. Exact counts follow in Chart data.</desc><line x1="42" y1="130" x2="342" y2="130"/><text x="34" y="34" text-anchor="end">${maximum}</text><text x="34" y="134" text-anchor="end">0</text>${bars}${ticks}</svg>`;
}
function durationPlot(points) {
  if(!points.length)return '<p class="ci-chart-note">No completed run has valid start and finish times in this selection.</p>';
  const maximum=Math.max(...points.map(p=>p.durationMs));
  const xy=points.map((point,index)=>({x:points.length===1?192:42+index/(points.length-1)*300,y:130-point.durationMs/maximum*100}));
  return `<svg viewBox="0 0 360 165" role="img" aria-labelledby="ci-duration-title ci-duration-desc"><title id="ci-duration-title">Measured finished run durations</title><desc id="ci-duration-desc">${points.length} runs ordered by finish time, oldest to newest. Maximum ${esc(formatDuration(maximum))}. Exact durations and repositories follow in Chart data.</desc><line x1="42" y1="130" x2="342" y2="130"/><text x="34" y="20" text-anchor="start">${esc(formatDuration(maximum))}</text><text x="34" y="134" text-anchor="end">0</text><polyline points="${xy.map(p=>`${number(p.x)},${number(p.y)}`).join(' ')}" fill="none" stroke="var(--accent)" stroke-width="2"/>${points.map((point,index)=>`<circle cx="${number(xy[index].x)}" cy="${number(xy[index].y)}" r="4" fill="${colors[point.tab]}"><title>${esc(runLabel(point))}: ${esc(formatDuration(point.durationMs))}</title></circle>`).join('')}<text x="42" y="150">Older</text><text x="342" y="150" text-anchor="end">Newer</text></svg>`;
}
export const ciChartStyles=ciPerformanceStyles+`<style>
.ci-charts{margin:16px 0;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface)}.ci-charts>summary{padding:12px 16px;cursor:pointer;font-size:13px;font-weight:600}.ci-charts-body{padding:0 16px 14px}.ci-chart-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.ci-chart-card{min-width:0}.ci-chart-card h2{font-size:14px;margin:10px 0 4px}.ci-chart-note{font-size:12px;line-height:1.6;color:var(--text-secondary);margin:6px 0}.ci-chart-card svg{display:block;width:100%;max-height:210px}.ci-chart-card svg text{font-size:11px;fill:var(--text-secondary);font-family:inherit}.ci-chart-card svg line{stroke:var(--border)}.ci-chart-legend{display:flex;flex-wrap:wrap;gap:12px;font-size:11px;color:var(--text-secondary)}.ci-chart-legend span::before{content:'';display:inline-block;width:9px;height:9px;margin-right:5px;background:var(--chart-color);border-radius:2px}.ci-chart-data{font-size:12px;margin-top:12px}.ci-chart-data>summary{cursor:pointer}.ci-chart-data h3{font-size:12px;margin:14px 0 6px}.ci-chart-table{overflow:auto}.ci-chart-table table{border-collapse:collapse;width:100%;font-size:12px}.ci-chart-table th,.ci-chart-table td{padding:6px;text-align:left;border-bottom:1px solid var(--border-subtle);overflow-wrap:anywhere}.ci-chart-table th{color:var(--text-secondary);font-weight:500}
@media(max-width:650px){.ci-chart-grid{grid-template-columns:minmax(0,1fr)}.ci-charts-body{padding:0 12px 12px}}
</style>`;
export function renderCiCharts(model,{stale=false,partial=false,retained=false,days=0,scope={}}={}) {
  return `<p class="ci-chart-note">${model.total} matching runs in ${retained?'retained observed history':'the recent loaded pipeline window'}. All outcomes are included in the trends; search, repository, branch, period, mission and session filters apply. ${Number(days)>0?`Last ${esc(days)} days.`:'All available observations in this window.'}${scope.persistence==='memory_only'?' Storage is memory-only; observations are lost on restart.':''}${scope.pipeline_limit?` Retention: up to ${esc(scope.pipeline_limit)} runs.`:''} This is not a complete history.${stale?' Reports are stale.':''}${partial?' Some repositories have incomplete reports.':''}</p>
    <div class="ci-chart-grid"><section class="ci-chart-card"><h2>Outcomes by UTC day</h2><p class="ci-chart-note">Up to 14 reported dates, grouped by run creation time.</p>${outcomePlot(model.days)}<div class="ci-chart-legend">${Object.entries(labels).map(([key,label])=>`<span style="--chart-color:${colors[key]}">${label}</span>`).join('')}</div><p class="ci-chart-note">Finished includes canceled and skipped runs.${model.undated?` ${model.undated} runs have no valid creation time.`:''}${model.older?` ${model.older} runs on older reported dates are outside this plot.`:''}</p></section>
    <section class="ci-chart-card"><h2>Finished run durations</h2><p class="ci-chart-note">${model.durations.length} of ${model.measured} measured runs, ordered by finish time.</p>${durationPlot(model.durations)}<p class="ci-chart-note">Actual start to finish; queued time is excluded.${model.unmeasured?` ${model.unmeasured} terminal runs lack a valid positive duration.`:''}</p></section></div>
    ${renderCiPerformance(model.performance)}<details class="ci-chart-data" data-ci-chart-data><summary>Chart data</summary><h3>Observed outcomes</h3>${table(['UTC date','Finished','Failed','Open / unknown'],model.days.map(d=>[d.day,d.finished,d.failed,d.current]))}<h3>Measured run durations</h3>${table(['Run','Outcome','Finished (UTC)','Duration'],model.durations.map(p=>[runLabel(p),labels[p.tab],utc(p.finishedAt),formatDuration(p.durationMs)]))}${renderCiPerformanceData(model.performance)}</details>`;
}

// Compact summary statistics are already windowed server-side. Never average
// project percentiles or fill missing dates/samples with invented zero values.
export function ciPercentiles(stats={},minimum=5) {
  const samples=Number.isFinite(stats?.sample_count)?Math.max(0,stats.sample_count):0;
  const value=key=>samples>=Math.max(5,minimum)&&Number.isFinite(stats?.[key])&&stats[key]>=0?stats[key]:null;
  return {samples,p50:value('p50'),p95:value('p95')};
}
const compactRate=row=>row.completed_count>0?row.failure_count/row.completed_count:null;
const exactNumber=value=>Number.isFinite(value)?String(value):'Not reported';
function summaryLine(days,series,label,{percent=false}={}) {
  const values=series.flatMap(s=>days.map(s.value)).filter(Number.isFinite);
  if(!values.length)return '<p class="ci-chart-note">Insufficient data for this trend.</p>';
  const max=percent?1:Math.max(1,...values);
  const x=i=>42+(days.length===1?.5:i/(days.length-1))*300;
  const y=v=>130-v/max*100;
  const paths=series.map(s=>{
    let connected=false;
    const points=days.map((d,i)=>{const v=s.value(d);if(!Number.isFinite(v)){connected=false;return '';}const command=connected?'L':'M';connected=true;return `${command}${number(x(i))} ${number(y(v))}`;}).join(' ');
    return `<path d="${points}" fill="none" stroke="${s.color}" stroke-width="2"${s.dashed?' stroke-dasharray="5 3"':''}/>${days.map((d,i)=>{const v=s.value(d);return Number.isFinite(v)?`<circle cx="${number(x(i))}" cy="${number(y(v))}" r="3" fill="${s.color}"><title>${esc(d.day)} · ${s.label}: ${percent?number(v*100)+'%':exactNumber(v)+' s'}</title></circle>`:'';}).join('')}`;
  }).join('');
  return `<svg viewBox="0 0 360 165" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title><desc>UTC dates, oldest to newest. Gaps mean insufficient observations. Exact values follow in the data table.</desc><line x1="42" y1="130" x2="342" y2="130"/><text x="42" y="20">${percent?'100%':number(max)+' s'}</text>${paths}<text x="42" y="150">${esc(days[0]?.day)}</text><text x="342" y="150" text-anchor="end">${esc(days.at(-1)?.day)}</text></svg>`;
}
export function renderCiSparkline(days=[],label='Project') {
  return summaryLine(days,[{value:compactRate,color:'var(--status-attention)',label:'Failure rate'}],`${label} failure rate`,{percent:true});
}
export function renderCiSummaryCharts(days=[],{minimum=5}={}) {
  if(!days.length)return '<p class="ci-chart-note">No dated observations in this window.</p>';
  const states=[...new Set(days.flatMap(d=>Object.keys(d.outcomes||{})))].sort();
  const maximum=Math.max(1,...days.map(d=>Object.values(d.outcomes||{}).reduce((sum,v)=>sum+(Number.isFinite(v)?v:0),0)));
  const colors=['var(--status-ok)','var(--status-attention)','var(--accent)','var(--text-secondary)'];
  const color=state=>['success','passed'].includes(state)?colors[0]:['failure','failed','error','errored','timed_out','crashed'].includes(state)?colors[1]:['running','started','pending','queued','waiting','blocked'].includes(state)?colors[2]:colors[3];
  const bars=days.map((d,i)=>{let y=130;return states.map(state=>{const count=d.outcomes?.[state]||0,height=count/maximum*100;y-=height;return count?`<rect x="${number(42+i*300/days.length)}" y="${number(y)}" width="${number(Math.min(24,240/days.length))}" height="${number(height)}" fill="${color(state)}"><title>${esc(d.day)} · ${esc(state)}: ${count}</title></rect>`:'';}).join('');}).join('');
  const outcomes=`<svg viewBox="0 0 360 165" role="img" aria-label="Outcomes by UTC day"><title>Outcomes by UTC day</title><desc>Provider outcomes remain distinct. Canceled, skipped and unknown runs never enter pass/fail denominators. Exact values follow.</desc><text x="42" y="20">${maximum} runs</text>${bars}<line x1="42" y1="130" x2="342" y2="130"/><text x="42" y="150">${esc(days[0].day)}</text><text x="342" y="150" text-anchor="end">${esc(days.at(-1).day)}</text></svg>`;
  const failure=renderCiSparkline(days,'Completed runs');
  const duration=summaryLine(days,['p50','p95'].map((key,i)=>({label:key,value:d=>ciPercentiles(d.duration_seconds,minimum)[key],color:i?'var(--text-secondary)':'var(--accent)',dashed:!!i})),'Completed-run duration');
  const exact=table(['UTC date','Provider outcomes','Failed / completed','Failure rate','Duration samples','p50 (seconds)','p95 (seconds)'],days.map(d=>{const p=ciPercentiles(d.duration_seconds,minimum);return [d.day,Object.entries(d.outcomes||{}).map(([key,v])=>`${key}: ${v}`).join(', '),`${d.failure_count} / ${d.completed_count}`,compactRate(d)===null?'Not reported':`${number(compactRate(d)*100)}%`,p.samples,p.p50??'Insufficient data',p.p95??'Insufficient data'];}));
  return `<div class="ci-chart-grid">${[['Outcomes by UTC day',outcomes,'Success · failure · active · other provider outcomes'],['Failure rate',failure,'Failed / successful or failed runs; canceled and skipped excluded.'],['Completed-run duration',duration,`p50 solid · p95 dashed. At least ${Math.max(5,minimum)} measured completed runs per UTC day.`]].map(([title,plot,note])=>`<section class="ci-chart-card"><h3>${title}</h3>${plot}<p class="ci-chart-note">${note}</p></section>`).join('')}</div><details data-ob-disclosure="chart-data" class="ci-chart-data"><summary>Exact chart data</summary>${exact}</details>`;
}
export function renderCiTestPareto(tests=[]) {
  const sorted=[...tests].filter(t=>t.failure_count>0).sort((a,b)=>b.failure_count-a.failure_count).slice(0,10);
  if(!sorted.length)return '<p class="ci-chart-note">No reported test failures in this selection.</p>';
  const max=sorted[0].failure_count;
  return `<section class="ci-chart-card"><h3>Top failing tests</h3><p class="ci-chart-note">Up to 10 tests, ranked by failed or errored reported attempts. Exact counts are in the test table.</p><svg viewBox="0 0 480 ${sorted.length*30+10}" role="img" aria-label="Top failing tests by reported failure count"><title>Top failing tests</title><desc>Horizontal bars ranked by failure count. Labels and exact counts follow in test history.</desc>${sorted.map((t,i)=>`<text x="0" y="${i*30+20}">${esc((t.name||t.id||'Test').slice(0,24))}</text><rect x="180" y="${i*30+6}" width="${t.failure_count/max*250}" height="18" fill="var(--status-attention)"><title>${esc(t.name||t.id)}: ${t.failure_count} failures</title></rect><text x="440" y="${i*30+20}">${t.failure_count}</text>`).join('')}</svg></section>`;
}
