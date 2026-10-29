// Analytics pages as mounted views (/analytics, /analytics/{missions,models,tokens,hosts}).
// Each page is a pure render of its payload morphed into place, so an identical refresh writes
// nothing and a changed counter writes only its own node (every counter sits in a data-field).
import { morph } from './patch.js';
import { liveRefresher } from './refresh-bus.js';
import { snapshotText, animateChanges } from './motion.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';

const TITLES = { '': 'Analytics', missions: 'Analytics', models: 'Model analytics', tokens: 'Token usage', hosts: 'Host analytics', stages: 'Stage analytics', usage: 'Usage' };

const DASHBOARD_STYLE = `<style>
      .an-dashboard{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:var(--density-gap,16px);align-items:start;width:100%}.an-panel{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:var(--density-panel-padding,16px)}.an-panel h2{font-size:16px;margin:0 0 12px}.an-panel p{font-size:12px;color:var(--text-secondary)}
      .an-value{font-size:28px;font-weight:700;line-height:1.1}.an-value span{font-size:12px;font-weight:400;color:var(--text-secondary)}.an-pairs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.an-pairs div{min-width:0}.an-pairs strong{display:block;font-size:20px}.an-pairs span{font-size:12px;color:var(--text-secondary)}
      .an-links{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(160px,100%),1fr));gap:8px}.an-links a{display:flex;flex-direction:column;gap:4px;min-width:0;padding:12px;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);text-decoration:none}.an-links a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.an-links strong{color:var(--text)}.an-links span{font-size:12px;color:var(--text-secondary)}
      .an-stage{grid-column:span 2}.an-model-list{margin:0;padding:0;list-style:none}.an-model-list li{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-top:1px solid var(--border-subtle);font-size:12px}.an-model-list li:first-child{border-top:0}.an-model-list span{overflow-wrap:anywhere}.an-stage .hd-card{padding:10px;margin-top:8px}
      @media(max-width:700px){.an-stage{grid-column:auto}.an-pairs{grid-template-columns:minmax(0,1fr)}}
    </style>`;

export async function loadAnalytics(client, sub = '') {
  if (sub === 'models') return { models: await client.analytics('models') };
  if (sub === 'tokens') return { tokens: await client.analytics('tokens') };
  if (sub === 'hosts') return { hosts: await client.analytics('hosts') };
  if (sub === 'stages') return { stages: await client.analytics('stages') };
  if (sub === 'usage') return { usage: await client.analytics('usage') };
  const [missions, models, hosts] = await Promise.all([client.analytics('missions'), client.analytics('models'), client.analytics('hosts')]);
  return { missions, models, hosts };
}

const field = (name, value, tag = 'b') => `<${tag} data-field="${name}">${esc(value)}</${tag}>`;

// Stage bar width is the stage's share of all missions, so the bars sum to 100%.
export function stageBars(byStage) {
  const entries = Object.entries(byStage || {}).map(([stage, n]) => [stage, Number(n) || 0]);
  if (!entries.length) return '<herdr-empty message="No mission data"></herdr-empty>';
  const total = entries.reduce((sum, [, n]) => sum + Math.max(0, n), 0);
  return entries.map(([stage, n]) => {
    const share = total > 0 ? Math.round((Math.max(0, n) / total) * 1000) / 10 : 0;
    return `
    <div class="hd-card" data-key="${esc(stage)}" style="display:flex;align-items:center;gap:12px">
      <herdr-status stage="${esc(stage)}"></herdr-status>
      <div class="hd-stage-bar" style="flex:1;background:var(--surface-2);border-radius:4px;height:10px;overflow:hidden" role="img" aria-label="${esc(stage)}: ${share}% of missions">
        <div style="width:${share}%;height:100%;background:var(--accent)"></div>
      </div>
      <span class="hd-card__sub" data-field="stage.${esc(stage)}">${n}</span>
    </div>`;
  }).join('');
}

function dashboard({ missions, models, hosts }) {
  const modelEntries = Object.entries(models.models || {});
  return `${DASHBOARD_STYLE}
    <div class="an-dashboard">
      <section class="an-panel" data-analytics-panel="missions"><h2>Missions</h2><div class="an-value">${field('missions.total', missions.total)} <span>observed</span></div><p>${field('missions.completed', missions.completed, 'span')} completed</p></section>
      <section class="an-panel" data-analytics-panel="completion"><h2>Completion</h2><div class="an-value">${field('missions.completion_rate', `${missions.completion_rate}%`)}</div><p>Across recorded missions</p></section>
      <section class="an-panel" data-analytics-panel="fleet"><h2>Fleet health</h2><div class="an-pairs"><div>${field('hosts.healthy', hosts.healthy, 'strong')}<span>healthy</span></div><div>${field('hosts.unavailable', hosts.unavailable, 'strong')}<span>unavailable</span></div></div><p>${field('hosts.total', hosts.total, 'span')} hosts reporting</p></section>
      <section class="an-panel" data-analytics-panel="models"><h2>Model coverage</h2><div class="an-value">${field('models.count', modelEntries.length)} <span>models</span></div><p>${field('models.total_sessions', models.total_sessions, 'span')} agent sessions</p><ul class="an-model-list">${modelEntries.slice(0, 4).map(([model, usage]) => `<li data-key="${esc(model)}"><span>${esc(model)}</span><strong data-field="model.sessions">${esc(usage.sessions)}</strong></li>`).join('') || '<li><span>No model usage recorded</span></li>'}</ul></section>
      <section class="an-panel" data-analytics-panel="views"><h2>Explore analytics</h2><nav class="an-links" data-analytics-links aria-label="Analytics views"><a href="#/analytics/models"><strong>Models</strong><span>Agent distribution by model</span></a><a href="#/analytics/hosts"><strong>Hosts</strong><span>Reported fleet health</span></a><a href="#/analytics/tokens"><strong>Token usage</strong><span>Requests and tokens by project</span></a><a href="#/analytics/stages"><strong>Stage analytics</strong><span>Per-stage p50/p95 and bottleneck</span></a><a href="#/analytics/usage"><strong>Usage</strong><span>Tokens by model and resources by kind</span></a></nav></section>
      <section class="an-panel an-stage" data-analytics-panel="stages"><h2>Missions by stage</h2>${stageBars(missions.by_stage)}</section>
    </div>`;
}

// The fleet and missions figures that sit under the model usage on the Usage page: four tiles and the way to the detail pages.
export function usageStrip({ missions = {}, models = {}, hosts = {} } = {}) {
  const modelCount = Object.keys(models.models || {}).length;
  const tile = (label, value, note) => `<div class="us-tile"><span class="us-label">${esc(label)}</span><strong>${esc(value)}</strong><span class="us-note">${esc(note)}</span></div>`;
  return `<style>.us-strip{margin:32px 0 0}.us-strip h2{font-size:14px;margin:0 0 12px}.us-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px}
.us-tile{display:flex;flex-direction:column;gap:4px;padding:12px 16px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius)}.us-tile strong{font-size:20px}.us-label,.us-note{font-size:12px;color:var(--text-secondary)}
.us-links{display:flex;flex-wrap:wrap;gap:8px 24px;margin:16px 0 0;font-size:13px}</style>
  <section class="us-strip" data-usage-strip aria-label="Missions and fleet"><h2>Missions and fleet</h2><div class="us-tiles">
    ${tile('Missions', missions.total ?? 0, `${missions.completed ?? 0} completed · ${missions.completion_rate ?? 0}%`)}
    ${tile('Hosts healthy', hosts.healthy ?? 0, `${hosts.unavailable ?? 0} unavailable of ${hosts.total ?? 0}`)}
    ${tile('Models in use', modelCount, `${models.total_sessions ?? 0} agent sessions`)}</div>
    <nav class="us-links" aria-label="More usage detail"><a href="#/analytics/models">Models</a><a href="#/analytics/hosts">Hosts</a><a href="#/analytics/tokens">Token usage</a><a href="#/analytics/stages">Stage timings</a><a href="#/analytics/usage">Resources</a></nav></section>`;
}

function modelsPage({ models: { models, total_sessions } }) {
  return `<p>${field('models.total_sessions', total_sessions, 'span')} total agents</p><div class="hd-grid">` +
    Object.entries(models || {}).map(([model, u]) => `
      <div class="hd-card" data-key="${esc(model)}">
        <div class="hd-card__title">${esc(model)}</div>
        <div class="hd-card__sub">${esc(u.sessions)} agent(s) · ${esc(u.messages)} messages</div>
      </div>`).join('') + '</div>';
}

function tokensPage({ tokens: t }) {
  const fmt = (n) => Number(n || 0).toLocaleString();
  const rows = (bucket) => Object.entries(bucket || {}).map(([k, u]) => `
      <tr data-key="${esc(k)}"><td>${esc(k)}</td><td>${fmt(u.requests)}</td><td>${fmt(u.input_tokens)}</td>
      <td>${fmt(u.output_tokens)}</td><td>${fmt(u.reasoning_tokens)}</td><td>${fmt(u.cache_write_tokens)}</td><td>${fmt(u.failed)}</td></tr>`).join('');
  const table = (title, bucket) => `<h3 style="margin-top:20px">${title}</h3>
    <table class="hd-table"><thead><tr><th></th><th>Requests</th><th>Input</th><th>Output</th>
    <th>Reasoning</th><th>Cache-write</th><th>Failed</th></tr></thead><tbody>${rows(bucket) ||
      '<tr><td colspan="7">No usage recorded yet.</td></tr>'}</tbody></table>`;
  const total = t.total || {};
  return `<div class="hd-grid">
      <herdr-metric label="Requests" value="${fmt(total.requests)}"></herdr-metric>
      <herdr-metric label="Input tokens" value="${fmt(total.input_tokens)}"></herdr-metric>
      <herdr-metric label="Output tokens" value="${fmt(total.output_tokens)}"></herdr-metric>
    </div>` + table('By project', t.by_project) + table('By model', t.by_model) + table('By day', t.by_day);
}

function hostsPage({ hosts: h }) {
  return `<div class="hd-grid">
      <herdr-metric label="Total" value="${esc(h.total)}"></herdr-metric>
      <herdr-metric label="Healthy" value="${esc(h.healthy)}"></herdr-metric>
      <herdr-metric label="Unavailable" value="${esc(h.unavailable)}"></herdr-metric>
    </div>`;
}

// Per-stage p50/p95 durations, the bottleneck stage, and currently open runs.
function stagesPage({ stages }) {
  const entries = Object.entries(stages.stages || {});
  const bottleneck = stages.bottleneck;
  const inProgress = Object.entries(stages.in_progress || {});
  const fmt = (ms) => (typeof ms === 'number' ? `${ms}ms` : '—');
  const rows = entries.map(([stage, s]) => `
      <tr data-key="${esc(stage)}"><td>${esc(stage)}</td><td>${fmt(s.p50_ms)}</td>
      <td>${fmt(s.p95_ms)}</td><td>${esc(s.runs)}</td></tr>`).join('');
  return `<div class="hd-grid">
      <herdr-metric label="Bottleneck" value="${esc(bottleneck || 'None')}"></herdr-metric>
      <herdr-metric label="Stages measured" value="${esc(entries.length)}"></herdr-metric>
    </div>
    <h3 style="margin-top:20px">Per-stage duration</h3>
    <table class="hd-table"><thead><tr><th>Stage</th><th>p50</th><th>p95</th><th>Runs</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4">No closed stage runs recorded yet.</td></tr>'}</tbody></table>
    <h3 style="margin-top:20px">In progress</h3>
    ${inProgress.length ? `<ul class="an-model-list">${inProgress.map(([mission, stagesList]) => `<li data-key="${esc(mission)}"><span>${esc(mission)}</span><strong>${esc(stagesList.join(', '))}</strong></li>`).join('')}</ul>` : '<p class="hd-card__sub">No mission is currently mid-stage.</p>'}`;
}

// Token counts by model and resource totals by kind (mission-scoped usage).
function usagePage({ usage }) {
  const tokens = usage.tokens || {};
  const byModel = Object.entries(tokens.by_model || {});
  const total = tokens.total || {};
  const fmt = (n) => Number(n || 0).toLocaleString();
  const modelRows = byModel.map(([model, t]) => `
      <tr data-key="${esc(model)}"><td>${esc(model)}</td><td>${fmt(t.input_tokens)}</td>
      <td>${fmt(t.output_tokens)}</td><td>${fmt(t.reasoning_tokens)}</td></tr>`).join('');
  const resRows = Object.entries(usage.resources || {}).map(([kind, r]) => `
      <tr data-key="${esc(kind)}"><td>${esc(kind)}</td><td>${fmt(r.count)}</td>
      <td>${fmt(r.total)}</td><td>${esc(r.unit || '—')}</td></tr>`).join('');
  return `<div class="hd-grid">
      <herdr-metric label="Input tokens" value="${fmt(total.input_tokens)}"></herdr-metric>
      <herdr-metric label="Output tokens" value="${fmt(total.output_tokens)}"></herdr-metric>
      <herdr-metric label="Resource kinds" value="${esc(Object.keys(usage.resources || {}).length)}"></herdr-metric>
    </div>
    <h3 style="margin-top:20px">Tokens by model</h3>
    <table class="hd-table"><thead><tr><th>Model</th><th>Input</th><th>Output</th><th>Reasoning</th></tr></thead>
    <tbody>${modelRows || '<tr><td colspan="4">No token usage recorded.</td></tr>'}</tbody></table>
    <h3 style="margin-top:20px">Resources by kind</h3>
    <table class="hd-table"><thead><tr><th>Kind</th><th>Count</th><th>Total</th><th>Unit</th></tr></thead>
    <tbody>${resRows || '<tr><td colspan="4">No resource usage recorded.</td></tr>'}</tbody></table>`;
}

const PAGES = { '': dashboard, missions: dashboard, models: modelsPage, tokens: tokensPage, hosts: hostsPage, stages: stagesPage, usage: usagePage };

const CHANGING = '[data-field], .hd-table td, herdr-metric .hd-metric__value';
const changeKey = (el) => el.closest('herdr-metric')?.getAttribute('label') ?? el;

export function mountAnalytics(container, client, sub = '', { data = null } = {}) {
  const page = PAGES[sub] ? sub : '';
  container.innerHTML = `${page ? '<p class="llm-note" style="font-size:13px"><a href="#/llm">← Usage</a></p>' : ''}<div class="hd-live-head" style="display:flex;flex-wrap:wrap;align-items:baseline;gap:12px"><h1>${TITLES[page]}</h1><herdr-freshness interval="5000"></herdr-freshness></div><div data-analytics-body></div>`;
  const body = container.querySelector('[data-analytics-body]');
  // A refresh that changed a value flashes that cell (a single number counts up); the first paint
  // and identical refreshes do not. morph keeps counter and table-cell nodes, so they pair by node;
  // a <herdr-metric> whose value changed is a new element, so its value pairs by the tile's label.
  let painted = false;
  const apply = (next) => {
    const before = painted ? snapshotText(body, CHANGING, changeKey) : null;
    morph(body, PAGES[page](next));
    if (before) animateChanges(body, CHANGING, before, changeKey);
    painted = true;
  };
  const live = liveRefresher(() => loadAnalytics(client, page), apply, () => container.querySelector('herdr-freshness'));
  if (data) live.applied(data); else live.refresh();
  return { refresh: (hint) => live.refresh(hint), dispose: () => live.dispose() };
}
