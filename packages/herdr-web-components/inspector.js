// herdr-web-components — agent inspector renderers (spec 0200).
// Pure, DOM-free string renderers so they are unit-testable with `node --test`.
// Every dynamic value goes through escapeHtml. Nothing here invents data: absent
// capabilities / withheld bodies / missing telemetry are rendered as such.
import { escapeHtml as esc } from './util.js';
import { formatDuration, formatTokens } from './format.js';

export const INSPECTOR_VIEWS = [
  { id: 'conversation', label: 'Conversation' },
  { id: 'reasoning', label: 'Reasoning' },
  { id: 'tools', label: 'Tools' },
  { id: 'files', label: 'Files' },
  { id: 'tests', label: 'Tests' },
  { id: 'usage', label: 'Usage' },
  { id: 'all', label: 'Timeline' },
];

/** Bounded client buffer: never hold more than this many rows per session (server max 1000). */
export const CLIENT_BUFFER = 500;
export const CLIENT_BUFFER_MAX = 1000;
/** Rows rendered per page; the rest sit behind "Show more". */
export const PAGE_SIZE = 100;
/** No event for this long while the session is not ended => telemetry is stale. */
export const STALE_AFTER_S = 120;

const SPLIT_MIN_PX = 900;

/** Hysteresis (prev) stops the layout flapping when the scrollbar changes the width. */
export function pickLayout(pref, widthPx, prev) {
  if (pref === 'split' || pref === 'stacked') return pref;
  const HYST = 40;
  if (prev === 'split') return widthPx >= SPLIT_MIN_PX - HYST ? 'split' : 'stacked';
  if (prev === 'stacked') return widthPx >= SPLIT_MIN_PX + HYST ? 'split' : 'stacked';
  return widthPx >= SPLIT_MIN_PX ? 'split' : 'stacked';
}

/** Merge a streamed/polled entry into a newest-first list (replace by seq, higher rev wins). */
export function mergeEntry(list, entry, cap = CLIENT_BUFFER) {
  const i = list.findIndex((e) => e.seq === entry.seq);
  if (i >= 0) {
    if ((entry.rev || 0) >= (list[i].rev || 0)) list[i] = entry;
    return list;
  }
  list.push(entry);
  list.sort((a, b) => b.seq - a.seq);
  if (list.length > cap) list.length = cap;
  return list;
}

export function telemetryHealth(summary, telemetry) {
  if (telemetry === 'none' || !summary) {
    return { state: 'none', text: 'No inspector telemetry received for this agent.' };
  }
  if (summary.ended) return { state: 'ended', text: 'Agent ended.' };
  const age = summary.last_event_age_s;
  if (age != null && age > STALE_AFTER_S) {
    return { state: 'stale', text: `No telemetry for ${formatDuration(age * 1000)} — the agent may be idle, restarting or disconnected.` };
  }
  return { state: 'live', text: 'Recent inspector data' };
}

function badge(text, cls = '') {
  return `<span class="hd-ins__badge ${cls}">${esc(text)}</span>`;
}

export function renderSummaryBar(data) {
  const s = data.summary;
  const health = telemetryHealth(s, data.telemetry);
  const parts = [badge(health.state === 'live' ? `● ${health.text}` : health.text, `hd-ins__badge--${health.state}`)];
  if (s) {
    if (s.running_tools.length) parts.push(badge(`${s.running_tools.length} tool${s.running_tools.length > 1 ? 's' : ''} running`, 'hd-ins__badge--run'));
    const failed = s.tests.filter((t) => t.status === 'failed').length;
    if (failed) parts.push(badge(`${failed} failing test run${failed > 1 ? 's' : ''}`, 'hd-ins__badge--fail'));
    if (s.usage.context_pct != null) parts.push(badge(`context ${s.usage.context_pct}%`, s.usage.context_pct >= 85 ? 'hd-ins__badge--fail' : ''));
    if (s.usage.input_tokens || s.usage.output_tokens) {
      parts.push(badge(`${formatTokens(s.usage.input_tokens)} in / ${formatTokens(s.usage.output_tokens)} out`));
    }
    const gaps = (data.dropped || 0) + (s.client_dropped || 0);
    if (gaps) parts.push(badge(`${gaps} event${gaps > 1 ? 's' : ''} dropped`, 'hd-ins__badge--warn'));
  }
  return `<div class="hd-ins__bar" role="status">${parts.join('')}</div>`;
}

export function renderAssociations(data) {
  const a = (data.summary && data.summary.association) || {};
  const models = (data.summary && data.summary.models) || [];
  const rows = [];
  if (a.mission_id) rows.push(['Mission', a.mission_id]);
  if (a.handoff_id || a.handoff_from) rows.push(['Handoff', [a.handoff_from && `from ${a.handoff_from}`, a.handoff_id].filter(Boolean).join(' · ')]);
  if (a.parent_session_id) rows.push(['Parent agent', a.parent_session_id]);
  if (a.worktree) rows.push(['Worktree', a.worktree]);
  if (a.branch) rows.push(['Branch', a.branch]);
  if (a.pr) {
    const label = `${a.pr.repo ? `${a.pr.repo}` : ''}#${a.pr.number ?? '?'}`;
    const safe = /^https?:\/\//i.test(a.pr.url || '');
    rows.push(['Pull request', safe ? `<a href="${esc(a.pr.url)}" rel="noopener noreferrer" target="_blank">${esc(label)}</a>` : esc(label), true]);
  }
  if (models.length) {
    rows.push(['Models', models.map((m) => `${m.previous_model ? `${m.previous_model} → ` : ''}${m.model || '?'} (${m.reason})`).join(' ; ')]);
  }
  if (!rows.length) return '';
  return `<dl class="hd-ins__assoc">${rows.map(([k, v, raw]) => `<dt>${esc(k)}</dt><dd>${raw ? v : esc(v)}</dd>`).join('')}</dl>`;
}

export function renderDiff(diff, { maxLines = 200 } = {}) {
  if (diff == null) return '';
  const lines = String(diff).split('\n');
  const shown = lines.slice(0, maxLines);
  const html = shown.map((ln) => {
    let cls = 'ctx';
    if (ln.startsWith('+++') || ln.startsWith('---')) cls = 'meta';
    else if (ln.startsWith('@@')) cls = 'hunk';
    else if (ln.startsWith('+')) cls = 'add';
    else if (ln.startsWith('-')) cls = 'del';
    return `<span class="hd-diff__${cls}">${esc(ln)}</span>`;
  }).join('\n');
  const more = lines.length > maxLines ? `\n<span class="hd-diff__meta">… ${lines.length - maxLines} more lines</span>` : '';
  return `<pre class="hd-diff" tabindex="0">${html}${more}</pre>`;
}

function tailLines(text, n) {
  const lines = String(text).split('\n');
  return lines.length > n ? ['… ' + (lines.length - n) + ' earlier lines', ...lines.slice(-n)].join('\n') : String(text);
}

function withheld(d) {
  return `<div class="hd-ins__withheld">🔒 ${esc(d.withheld_reason || 'withheld')}</div>`;
}

function who(e) {
  const a = e.agent_id && e.agent_id !== 'main'
    ? `<span class="hd-ins__agent" title="${esc(e.parent_agent_id ? `subagent of ${e.parent_agent_id}` : 'agent')}">${esc(e.display_name || e.agent_id)}</span>` : '';
  return a;
}

function meta(e) {
  const t = e.ts ? esc(String(e.ts).replace('T', ' ').slice(0, 19)) : '';
  const flags = [e.redacted && 'redacted', e.truncated && 'truncated', e.sensitive && 'sensitive'].filter(Boolean);
  return `<span class="hd-ins__meta">#${e.seq} · ${t}${flags.length ? ` · ${esc(flags.join(', '))}` : ''}</span>`;
}

export function renderEvent(e, prefs = {}) {
  const d = e.data || {};
  const lines = prefs.tool_output_lines || 20;
  const head = (title, extra = '') => `<div class="hd-ins__head">${who(e)}<span class="hd-ins__title">${title}</span>${extra}${meta(e)}</div>`;
  let body = '';
  let title = esc(e.kind);
  let extra = '';
  switch (e.kind) {
    case 'message':
      title = esc(d.role || 'message');
      body = d.withheld ? withheld(d) : `<div class="hd-ins__text">${esc(d.text ?? '')}</div>`;
      break;
    case 'reasoning':
      title = `reasoning <small>(${esc(d.source === 'provider_raw' ? 'provider-exposed' : 'provider summary')})</small>`;
      body = d.withheld ? withheld(d) : `<div class="hd-ins__text hd-ins__text--reason">${esc(d.text ?? '')}</div>`;
      break;
    case 'tool': {
      const st = d.status || 'running';
      title = `${esc(d.tool || 'tool')} <code>${esc(d.args_preview || '')}</code>`;
      extra = `<span class="hd-ins__st hd-ins__st--${esc(st)}">${esc(st)}${d.exit_code != null && st !== 'running' ? ` (${esc(d.exit_code)})` : ''}${d.duration_ms != null ? ` · ${esc(formatDuration(d.duration_ms))}` : ''}${d.incomplete ? ' · partial telemetry' : ''}</span>`;
      if (prefs.show_tool_output === false) body = '';
      else if (d.withheld) body = withheld(d);
      else if (d.output) body = `<pre class="hd-ins__out">${esc(tailLines(d.output, lines))}</pre>${d.output_more || e.truncated ? '<button class="hd-btn hd-ins__expand" data-seq="' + esc(e.seq) + '">Show full output</button>' : ''}`;
      break;
    }
    case 'file':
      title = `${esc(d.op)} <code>${esc(d.path)}</code>`;
      extra = d.additions != null || d.deletions != null ? `<span class="hd-ins__st"><span class="hd-diff__add">+${esc(d.additions ?? 0)}</span> <span class="hd-diff__del">−${esc(d.deletions ?? 0)}</span></span>` : '';
      body = d.withheld ? withheld(d) : (d.diff ? `${renderDiff(d.diff, {maxLines: prefs.diff_max_lines || 200})}${d.diff_more ? '<button class="hd-btn hd-ins__expand" data-seq="' + esc(e.seq) + '">Show full diff</button>' : ''}` : '');
      break;
    case 'test': {
      const st = d.status || 'running';
      title = `test <code>${esc(d.command || d.run_id)}</code>`;
      extra = `<span class="hd-ins__st hd-ins__st--${st === 'failed' ? 'error' : st === 'passed' ? 'ok' : 'running'}">${esc(st)}${d.passed != null ? ` · ${esc(d.passed)} passed` : ''}${d.failed ? ` · ${esc(d.failed)} failed` : ''}${d.skipped ? ` · ${esc(d.skipped)} skipped` : ''}</span>`;
      body = d.withheld ? withheld(d) : (d.failures || []).map((f) => `<div class="hd-ins__fail"><strong>${esc(f.name)}</strong>${f.file ? ` <code>${esc(f.file)}${f.line ? ':' + esc(f.line) : ''}</code>` : ''}<pre class="hd-ins__out">${esc(f.message)}</pre></div>`).join('');
      break;
    }
    case 'usage':
      title = 'usage';
      body = `<div class="hd-ins__text">${['input_tokens', 'output_tokens', 'reasoning_tokens', 'cache_read_tokens', 'context_tokens'].filter((k) => d[k] != null).map((k) => `${esc(k.replace('_tokens', ''))}: ${esc(formatTokens(d[k]))}`).join(' · ')}${d.model ? ` · ${esc(d.model)}` : ''}</div>`;
      break;
    case 'model':
      title = `model ${esc(d.reason || 'switch')}`;
      body = `<div class="hd-ins__text">${esc(d.previous_model || '?')} → <strong>${esc(d.model || '?')}</strong></div>`;
      break;
    case 'association':
      title = 'association';
      body = `<div class="hd-ins__text">${['mission_id', 'handoff_from', 'handoff_id', 'worktree', 'branch'].filter((k) => d[k]).map((k) => `${esc(k)}: ${esc(d[k])}`).join(' · ')}${d.pr ? ` · PR #${esc(d.pr.number ?? '?')}` : ''}</div>`;
      break;
    case 'lifecycle':
      title = `agent ${esc(d.phase)}`;
      break;
    case 'capabilities':
      title = 'capabilities';
      body = `<div class="hd-ins__text">reasoning: ${esc(d.reasoning)} · streaming tools: ${d.streaming_tools ? 'yes' : 'no'} · diffs: ${d.diffs ? 'yes' : 'no'}</div>`;
      break;
    default:
  }
  return `<article class="hd-ins__row hd-ins__row--${esc(e.kind)}" data-seq="${esc(e.seq)}">${head(title, extra)}${body}</article>`;
}

function reasoningNotice(data, prefs) {
  const c = data.capabilities || {};
  if (c.reasoning === 'none') return '<herdr-empty message="This agent/model does not expose reasoning. Nothing is inferred or reconstructed."></herdr-empty>';
  if (!c.declared || c.reasoning === 'unknown') return '<herdr-empty message="Reasoning capability not reported yet by this agent."></herdr-empty>';
  if (!prefs.show_reasoning) return '<div class="hd-ins__notice">Reasoning is hidden by your preferences. <button class="hd-btn" id="ins-show-reasoning">Show reasoning</button></div>';
  if (data.summary && data.summary.reasoning_withheld) return '<div class="hd-ins__notice">🔒 Reasoning requires the operator role.</div>';
  return '';
}

export function renderFiles(data) {
  const files = (data.summary && data.summary.files) || [];
  if (!files.length) return '<herdr-empty message="No file activity recorded"></herdr-empty>';
  const rows = [...files].reverse().map((f) => `<tr data-seq="${esc(f.last_seq)}">
      <td><code>${esc(f.path)}</code>${f.sensitive ? ' 🔒' : ''}</td><td>${esc(f.ops.join(', '))}</td>
      <td><span class="hd-diff__add">+${esc(f.additions)}</span> <span class="hd-diff__del">−${esc(f.deletions)}</span></td>
      <td>${esc(f.agents.map(id=>data.summary?.agents?.find(a=>a.agent_id===id)?.display_name || id).join(', '))}</td></tr>`).join('');
  return `<table class="hd-ins__table"><thead><tr><th>File</th><th>Ops</th><th>Changes</th><th>By</th></tr></thead><tbody>${rows}</tbody></table>`;
}

export function renderUsage(data) {
  const s = data.summary;
  if (!s) return '<herdr-empty message="No usage reported"></herdr-empty>';
  const u = s.usage;
  const tiles = [['Input', u.input_tokens], ['Output', u.output_tokens], ['Reasoning', u.reasoning_tokens], ['Cache read', u.cache_read_tokens]]
    .map(([k, v]) => `<herdr-metric label="${esc(k)} tokens" value="${esc(formatTokens(v))}"></herdr-metric>`).join('');
  const ctx = u.context_pct != null
    ? `<herdr-metric label="Context" value="${esc(u.context_pct)}% of ${esc(formatTokens(u.latest.context_window))}"></herdr-metric>` : '';
  const agents = s.agents.length > 1
    ? `<h4>Agents</h4><table class="hd-ins__table"><tbody>${s.agents.map((a) => `<tr><td>${esc(a.display_name || a.agent_id)}</td><td>${a.parent_agent_id ? 'helper of ' + esc(s.agents.find(parent=>parent.agent_id===a.parent_agent_id)?.display_name || a.parent_agent_id) : 'primary'}</td><td>${esc(a.tools)} tools</td><td>${esc(a.events)} events</td></tr>`).join('')}</tbody></table>` : '';
  return `<div class="hd-grid">${tiles}${ctx}</div>${agents}`;
}

/** Render one tab's content. Paging is applied by the caller via `limit`. */
export function renderView(view, data, prefs, limit = PAGE_SIZE) {
  const names = new Map((data.summary?.agents || []).map(a=>[a.agent_id,a.display_name]));
  data = {...data,events:data.events.map(event=>({...event,display_name:names.get(event.agent_id)}))};
  if (view === 'usage') return renderUsage(data) + renderRows(data.events.filter((e) => e.kind === 'model' || e.kind === 'usage'), prefs, limit);
  if (view === 'reasoning') {
    const notice = reasoningNotice(data, prefs);
    const rows = prefs.show_reasoning ? renderRows(data.events.filter((e) => e.kind === 'reasoning'), prefs, limit) : '';
    return notice + rows;
  }
  if (view === 'files') return renderFiles(data) + renderRows(data.events.filter((e) => e.kind === 'file' && e.data.diff !== undefined), prefs, limit);
  const kinds = { conversation: ['message'], tools: ['tool'], tests: ['test'] }[view];
  let evs = data.events;
  if (kinds) evs = evs.filter((e) => kinds.includes(e.kind));
  else evs = evs.filter((e) => prefs.show_reasoning || e.kind !== 'reasoning');
  return renderRows(evs, prefs, limit);
}

function renderRows(evs, prefs, limit) {
  if (!evs.length) return '<herdr-empty message="Nothing recorded for this view yet"></herdr-empty>';
  const html = evs.slice(0, limit).map((e) => renderEvent(e, prefs)).join('');
  const more = evs.length > limit ? `<button class="hd-btn" id="ins-more">Show more (${evs.length - limit})</button>` : '';
  return html + more;
}

export function renderPrefsForm(prefs, scope) {
  const sel = (name, opts) => `<label>${esc(name)} <select data-pref="${esc(name)}">${opts.map((o) => `<option${prefs[name] === o ? ' selected' : ''}>${esc(o)}</option>`).join('')}</select></label>`;
  const chk = (name, label) => `<label><input type="checkbox" data-pref="${esc(name)}"${prefs[name] ? ' checked' : ''}> ${esc(label)}</label>`;
  const num = (name, label, min, max) => `<label>${esc(label)} <input type="number" data-pref="${esc(name)}" min="${min}" max="${max}" value="${esc(prefs[name])}"></label>`;
  return `<form class="hd-ins__prefs" data-scope="${esc(scope)}">
    ${sel('layout', ['auto', 'split', 'stacked'])}${sel('density', ['comfortable', 'compact'])}
    ${sel('default_view', ['conversation', 'reasoning', 'tools', 'files', 'tests', 'usage', 'all'])}
    ${chk('show_reasoning', 'Show reasoning (when exposed)')}${chk('show_tool_output', 'Show tool output')}${chk('follow_tail', 'Follow newest')}
    ${num('tool_output_lines', 'Tool output lines', 1, 500)}${num('max_events_per_session', 'Retention: max events', 50, 50000)}${num('retention_hours', 'Retention: hours', 1, 720)}
    <button type="submit" class="hd-btn">Save ${esc(scope)} preferences</button></form>`;
}
