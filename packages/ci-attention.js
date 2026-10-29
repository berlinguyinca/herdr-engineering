// What needs a person on the CI page: failures nothing has fixed yet, and runs that look stuck. Built only from the runs the
// server already reports (GET /api/v1/ci); a failure is "unresolved" when the newest finished run of its branch (or pull request)
// failed, so a later green run clears it. The failing step and its log are fetched only when a row is opened.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatDuration } from './herdr-web-components/format.js';

export const WINDOW_MS = 14 * 24 * 3600 * 1000;      // older failures are history, not a to-do list
export const STUCK_RUNNING_MS = 60 * 60 * 1000;      // a gate takes ~30 min (more when the queue is long); past an hour something is wrong
export const STUCK_QUEUED_MS = 30 * 60 * 1000;
const DECISIVE_FAIL = new Set(['failure', 'error', 'killed']);
const DECISIVE = new Set(['success', ...DECISIVE_FAIL]);   // canceled and skipped runs say nothing about the code
const ACTIVE = new Set(['running', 'pending', 'blocked']);
const SHOWN = 5;

const list = (v) => (Array.isArray(v) ? v : []);
const time = (v) => { const t = Date.parse(v); return Number.isFinite(t) ? t : NaN; };
const text = (v) => (typeof v === 'string' ? v : '');
const created = (r) => time(r.created_at);
const finished = (r) => { const f = time(r.finished_at); return Number.isFinite(f) ? f : created(r); };

/** "PR #389" for a pull-request run, else the branch name. */
export function ciRefLabel(run) {
  const pr = /^refs\/pull\/(\d+)\//.exec(text(run.ref));
  if (pr) return `PR #${pr[1]}`;
  return text(run.branch) || text(run.ref).replace(/^refs\/(heads|tags)\//, '') || 'unknown ref';
}

/** Session ids the server linked to this run's commit; entries may be ids or {session_id}. */
export function ciRunSessions(run) {
  return [...new Set(list(run.related_sessions).map((s) => (typeof s === 'string' ? s : text(s?.session_id) || text(s?.id))).filter(Boolean))];
}

/** Unresolved failures and stuck runs from the /api/v1/ci payload. */
export function ciAttentionModel(ci, now = Date.now()) {
  const groups = new Map();
  for (const [repo, runs] of Object.entries(ci?.pipelines || {})) {
    for (const run of list(runs)) {
      if (!run || !Number.isFinite(created(run))) continue;
      const repository = text(run.repository) || repo;
      const key = `${repository}|${/^refs\/pull\//.test(text(run.ref)) ? text(run.ref).replace(/\/merge$|\/head$/, '') : text(run.branch)}`;
      if (!groups.has(key)) groups.set(key, { repository, runs: [] });
      groups.get(key).runs.push({ ...run, repository });
    }
  }
  const failures = []; const running = []; let merged = 0;
  for (const [key, g] of groups) {
    g.runs.sort((a, b) => created(b) - created(a));
    const latest = g.runs.find((r) => DECISIVE.has(r.state));
    const live = g.runs.find((r) => ACTIVE.has(r.state));
    if (live) {
      const since = time(live.started_at); const from = Number.isFinite(since) ? since : created(live);
      const age = now - from;
      const limit = live.state === 'running' ? STUCK_RUNNING_MS : STUCK_QUEUED_MS;
      // A blocked run is waiting for someone (an approval, a dependency), not hung, so it is listed and aged but not called stuck.
      running.push({ key: `${key}#${live.run_number}`, run: live, age, stuck: live.state !== 'blocked' && age > limit, blocked: live.state === 'blocked' });
    }
    if (!latest || !DECISIVE_FAIL.has(latest.state) || now - finished(latest) > WINDOW_MS) continue;
    if (latest.pr_merged) { merged += 1; continue; }
    let consecutive = 0;
    for (const r of g.runs) { if (!DECISIVE.has(r.state)) continue; if (!DECISIVE_FAIL.has(r.state)) break; consecutive += 1; }
    failures.push({ key, run: latest, repository: g.repository, label: ciRefLabel(latest), consecutive, rerunning: Boolean(live && created(live) > created(latest)),
      age: now - finished(latest), sessions: ciRunSessions(latest) });
  }
  failures.sort((a, b) => Number(a.rerunning) - Number(b.rerunning) || a.age - b.age);
  running.sort((a, b) => Number(b.stuck) - Number(a.stuck) || Number(b.blocked) - Number(a.blocked) || b.age - a.age);
  return { failures, running, merged, stuck: running.filter((r) => r.stuck).length };
}

const FAILURE_LINE = /^(not ok |(FAILED|ERROR) |=+ FAILURES =+|\+ exit [1-9]|Error:|AssertionError)/;

/** The lines around failures in a step log (same markers as scripts/ci/wp_log.py), or the last lines when none match. */
export function failureExcerpt(log, { before = 1, after = 14, tail = 20, max = 80 } = {}) {
  const lines = text(log).split('\n');
  const spans = [];
  lines.forEach((line, i) => {
    if (!FAILURE_LINE.test(line)) return;
    const lo = Math.max(0, i - before); const hi = Math.min(lines.length, i + after + 1);
    if (spans.length && lo <= spans[spans.length - 1][1]) spans[spans.length - 1][1] = Math.max(spans[spans.length - 1][1], hi);
    else spans.push([lo, hi]);
  });
  if (!spans.length) return { lines: lines.slice(-tail), matched: false };
  const out = spans.flatMap(([lo, hi], i) => (i ? ['--', ...lines.slice(lo, hi)] : lines.slice(lo, hi)));
  return { lines: out.slice(0, max), matched: true, more: Math.max(0, out.length - max) };
}

const ago = (ms) => (ms < 90000 ? 'just now' : `${formatDuration(ms)} ago`);
const sessionLink = (id) => `<a href="#/herd/session/${esc(encodeURIComponent(id))}">${esc(id.length > 22 ? `${id.slice(0, 21)}…` : id)}</a>`;

function failureRow(f, open) {
  const r = f.run;
  const chips = [f.consecutive > 1 ? `<span class="cia-chip cia-chip--bad">${f.consecutive} failures in a row</span>` : '',
    f.rerunning ? '<span class="cia-chip">re-running</span>' : '',
    f.sessions.length ? `<span class="cia-chip">agent: ${f.sessions.slice(0, 2).map(sessionLink).join(', ')}</span>` : ''].join('');
  return `<details class="cia-row" data-cia-row="${esc(f.key)}"${open ? ' open' : ''}><summary><span class="cia-dot cia-dot--bad" aria-hidden="true"></span>`
    + `<span class="cia-name">${esc(f.repository)}</span><span class="cia-ref">${esc(f.label)}</span><span class="cia-meta">run #${esc(r.run_number)} · ${esc(ago(f.age))}</span>${chips}</summary>`
    + `<div class="cia-body" data-cia-body="${esc(f.key)}"><p class="cia-note">Open to load the failing step.</p></div></details>`;
}

function runningRow(x) {
  const r = x.run;
  return `<li class="cia-run${x.stuck ? ' cia-run--stuck' : ''}" data-cia-running><span class="cia-dot ${x.stuck ? 'cia-dot--bad' : 'cia-dot--live'}" aria-hidden="true"></span>`
    + `<a href="${esc(r.url || '#')}" target="_blank" rel="noopener noreferrer" class="cia-name">${esc(r.repository)}</a><span class="cia-ref">${esc(ciRefLabel(r))}</span>`
    + `<span class="cia-meta">run #${esc(r.run_number)} · ${esc(r.state)} for ${esc(formatDuration(x.age))}</span>${x.stuck ? '<span class="cia-chip cia-chip--bad">looks stuck</span>' : x.blocked ? '<span class="cia-chip">waiting on approval or a dependency</span>' : ''}</li>`;
}

export function renderCiAttention(model, { open = new Set(), showAll = false, runsOpen = null } = {}) {
  const f = model.failures; const shown = showAll ? f : f.slice(0, SHOWN);
  const failBlock = f.length
    ? `${shown.map((x) => failureRow(x, open.has(x.key))).join('')}${f.length > shown.length ? `<button type="button" class="hd-btn hd-btn--secondary hd-btn--sm cia-more" data-cia-more>Show ${f.length - shown.length} more</button>` : ''}`
    : '<p class="cia-note" data-cia-clear>No unresolved failures: the newest finished run of every branch and pull request passed.</p>';
  const runs = showAll ? model.running : model.running.slice(0, SHOWN);
  const runBlock = model.running.length
    ? `<ul class="cia-runs">${runs.map(runningRow).join('')}</ul>${model.running.length > runs.length ? `<button type="button" class="hd-btn hd-btn--secondary hd-btn--sm cia-more" data-cia-more>Show ${model.running.length - runs.length} more</button>` : ''}`
    : '<p class="cia-note">Nothing is running.</p>';
  return `<style>${styles}</style><section class="cia" aria-label="CI needs attention">
<h2>Needs attention <span class="cia-count" data-cia-count>${f.length}</span></h2>
<p class="cia-note">Failures that no later run has fixed${model.merged ? ` · ${model.merged} on merged pull requests are left out` : ''}.</p>${failBlock}
<details class="cia-runsec" data-cia-runsec${(runsOpen ?? model.stuck > 0) ? ' open' : ''}><summary><span class="cia-h">Running now</span> <span class="cia-count">${model.running.length}</span>${model.stuck ? ` <span class="cia-chip cia-chip--bad">${model.stuck} look stuck</span>` : ' <span class="cia-meta">none look stuck</span>'}</summary>${runBlock}</details></section>`;
}

/** The opened row: who and what, the failing step(s), and the lines around the failure. */
export function renderCiDetail(f, detail, logs, errors = {}) {
  const p = detail?.pipeline || {};
  const steps = list(detail?.diagnosis?.failed_steps);
  const who = [text(p.author), text(p.message || p.title).split('\n')[0]].filter(Boolean);
  const head = `<p class="cia-who">${who.map(esc).join(' · ') || 'No commit details reported.'}${text(f.run.revision) ? ` <code>${esc(f.run.revision.slice(0, 8))}</code>` : ''}</p>`;
  const links = `<p class="cia-links"><a href="${esc(f.run.url || '#')}" target="_blank" rel="noopener noreferrer">Open run in Woodpecker ↗</a>${
    f.run.forge_url ? ` · <a href="${esc(f.run.forge_url)}" target="_blank" rel="noopener noreferrer">${esc(f.label)} on the forge ↗</a>` : ''}${
    f.sessions.length ? ` · agent ${f.sessions.map(sessionLink).join(', ')}` : ' · no agent session is linked to this commit'}</p>`;
  if (errors.detail) return `${head}<p class="cia-note" role="status">${esc(errors.detail)}</p>${links}`;
  if (!detail) return `${head}<p class="cia-note" role="status">Loading the failing step…</p>${links}`;
  const body = (steps.length ? steps : []).slice(0, 2).map((s) => {
    const log = logs[s.id]; const ex = log ? failureExcerpt(log.log) : null;
    return `<div class="cia-step"><h3>${esc(s.workflow || 'step')} / ${esc(s.name)} <span class="cia-meta">${esc(s.state)}${s.exit_code != null ? ` · exit ${esc(s.exit_code)}` : ''}</span></h3>${
      errors[s.id] ? `<p class="cia-note" role="status">${esc(errors[s.id])}</p>`
        : !log ? '<p class="cia-note" role="status">Loading the log…</p>'
          : `<pre class="cia-log" tabindex="0">${esc(ex.lines.join('\n'))}</pre><p class="cia-note">${ex.matched ? 'Lines around the failure' : 'No failure marker found; the last lines'}${log.truncated ? ' · the stored log is truncated' : ''}${ex.more ? ` · ${ex.more} more lines not shown` : ''}.</p>`}</div>`;
  }).join('') || '<p class="cia-note">No failed step is reported for this run (it may have been killed before a step started).</p>';
  return `${head}${body}${links}`;
}

const styles = `.cia{margin:12px 0 20px;min-width:0}.cia h2{font-size:15px;margin:16px 0 6px;display:flex;align-items:center;gap:8px}.cia-count{font-size:12px;font-weight:500;padding:0 8px;border:1px solid var(--border);border-radius:99px;color:var(--text-secondary)}
.cia-row>summary::before,.cia-runsec>summary::before{content:'\\25B8';display:inline-block;width:12px;color:var(--text-muted);transition:transform .12s}.cia-row[open]>summary::before,.cia-runsec[open]>summary::before{transform:rotate(90deg)}
.cia-runsec{margin-top:8px}.cia-runsec>summary{display:flex;align-items:center;gap:8px;cursor:pointer;padding:8px 0;list-style:none}.cia-runsec>summary::-webkit-details-marker{display:none}.cia-h{font-size:15px;font-weight:600}
.cia-note{font-size:12px;color:var(--text-secondary);margin:4px 0 8px}.cia-row{border-bottom:1px solid var(--border-subtle)}.cia-row>summary{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;padding:10px 4px;cursor:pointer;list-style:none;font-size:13px}.cia-row>summary::-webkit-details-marker{display:none}.cia-row>summary:hover{background:var(--surface-2)}
.cia-dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--text-muted)}.cia-dot--bad{background:var(--status-failed)}.cia-dot--live{background:var(--status-active)}.cia-name{font-weight:600;overflow-wrap:anywhere}.cia-ref{color:var(--text-secondary)}.cia-meta{font-size:12px;color:var(--text-muted)}
.cia-chip{font-size:11px;padding:0 8px;border:1px solid var(--border);border-radius:99px;color:var(--text-secondary)}.cia-chip--bad{border-color:var(--status-failed);color:var(--status-failed)}
.cia-body{padding:4px 4px 16px 24px}.cia-who{font-size:13px;margin:4px 0 8px;overflow-wrap:anywhere}.cia-links{font-size:12px;margin:8px 0 0}.cia-step h3{font-size:13px;margin:8px 0 4px}
.cia-log{margin:4px 0;padding:12px;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:12px;line-height:1.5;max-height:320px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.cia-runs{list-style:none;margin:0;padding:0}.cia-run{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;padding:8px 4px;border-bottom:1px solid var(--border-subtle);font-size:13px}.cia-more{margin:8px 0}`;

/** Mounts the section into `host`; returns a disposer. Opened rows survive the refresh. */
export function mountCiAttention(host, client, { intervalMs = 20000, now = () => Date.now(), onModel = () => {} } = {}) {
  const open = new Set(); const details = new Map(); const logs = new Map(); const errors = new Map();
  let model = null; let showAll = false; let runsOpen = null; let signature = ''; let disposed = false; let timer = null;
  const controller = new AbortController();

  const detailHtml = (f) => renderCiDetail(f, details.get(f.key), logs.get(f.key) || {}, errors.get(f.key) || {});
  function paint() {
    if (disposed || !model) return;
    const sig = JSON.stringify([model.failures.map((f) => [f.key, f.run.run_number, f.rerunning, f.consecutive]), model.running.map((r) => [r.key, r.stuck]), model.merged, showAll, runsOpen]);
    if (sig !== signature) { signature = sig; host.innerHTML = renderCiAttention(model, { open, showAll, runsOpen }); }
    for (const f of model.failures) {
      const body = host.querySelector(`[data-cia-body="${CSS.escape(f.key)}"]`);
      if (body && open.has(f.key)) body.innerHTML = detailHtml(f);
    }
  }
  async function loadDetail(f) {
    if (details.has(f.key) || errors.get(f.key)?.detail) return;
    try {
      const d = await client.getCiPipeline(f.repository, f.run.run_number, { signal: controller.signal, providerUrl: f.run.provider_url });
      details.set(f.key, d);
    } catch (e) { if (disposed) return; errors.set(f.key, { detail: 'The run details could not be loaded.' }); paint(); return; }
    paint();
    const steps = list(details.get(f.key)?.diagnosis?.failed_steps).slice(0, 2);
    await Promise.all(steps.map(async (s) => {
      try {
        const l = await client.getCiLog(f.repository, f.run.run_number, s.id, { signal: controller.signal, providerUrl: f.run.provider_url });
        logs.set(f.key, { ...(logs.get(f.key) || {}), [s.id]: { log: text(l?.log), truncated: Boolean(l?.truncated) } });
      } catch { errors.set(f.key, { ...(errors.get(f.key) || {}), [s.id]: 'The log could not be loaded.' }); }
      if (!disposed) paint();
    }));
  }
  host.addEventListener('toggle', (event) => {
    const sec = event.target.closest?.('[data-cia-runsec]');
    if (sec) { runsOpen = sec.open; return; }
    const row = event.target.closest?.('[data-cia-row]'); if (!row || !model) return;
    const key = row.dataset.ciaRow;
    if (row.open) { open.add(key); const f = model.failures.find((x) => x.key === key); if (f) { row.querySelector('[data-cia-body]').innerHTML = detailHtml(f); loadDetail(f); } } else open.delete(key);
  }, { capture: true, signal: controller.signal });
  host.addEventListener('click', (event) => { if (event.target.closest?.('[data-cia-more]')) { showAll = true; paint(); } }, { signal: controller.signal });

  async function refresh() {
    if (disposed) return;
    try { model = ciAttentionModel(await client.getCi({ signal: controller.signal }), now()); paint(); onModel(model); }
    catch { if (!model && !disposed) host.innerHTML = '<p class="cia-note" role="status">CI status could not be loaded.</p>'; }
    if (!disposed) timer = setTimeout(refresh, intervalMs);
  }
  refresh();
  return () => { disposed = true; clearTimeout(timer); controller.abort(); };
}
