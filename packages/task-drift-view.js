// The task drift comparison view (H06): what a task set out to do beside what its record says it did — the original objective, the observed actions,
// the first violation, the scope diff, the model and quantization, the uncertainty. Rendering only: no fetch, fs, clock, timer, control, handler or
// CSS file, and above all no similarity, tokenization or alignment figure — the plan forbids inventing one, so none is computed or printed here.
// Drift is H02's decideDrift, called only when the report carries the numbers to compare and never re-decided here; relevance comes only from what the
// report states about an action, so an action it never classes says so instead of being counted relevant by default.
import { decideDrift } from './diagnostics-model.js';
import { escapeHtml } from './herdr-web-components/util.js';

export const UNKNOWN = 'unknown', NONE = 'none reported';   // "unknown": a field the report does not carry (never '-', never 0, never a blank that reads as "nothing happened"); "none reported": it listed the field and listed nothing, which is a stated fact, not an absence
const plain = (v) => typeof v === 'object' && v !== null && !Array.isArray(v), text = (v) => (typeof v === 'string' ? v : ''), finite = (v) => typeof v === 'number' && Number.isFinite(v);
const list = (v) => (Array.isArray(v) ? v.map((s) => text(s).trim()).filter(Boolean) : null);   // null: the report did not list it; []: it listed none
const stamp = (v) => (finite(v) ? new Date(v).toISOString().slice(0, 16).replace('T', ' ') : text(v).trim() || UNKNOWN);   // the record's own instant, never a clock reading
const idOf = (a) => text(a.id) || text(a.actionId) || UNKNOWN, field = (label, value) => `<div class="diag-field"><b>${escapeHtml(text(value) || UNKNOWN)}</b><span>${escapeHtml(label)}</span></div>`;

/** The report's own classification of its actions, keyed by action id — relevance is read from what it states, never from what an action did. */
export function relevanceLists(report = {}) { const source = plain(report.relevance) ? report.relevance : {}, map = new Map();
  for (const [word, ids] of [['relevant', source.relevant], ['unrelated', source.unrelated]]) for (const id of list(ids) ?? []) map.set(id, word);
  return map; }
/** How the record itself classes one action: its own flag, else the list of the report it appears in, else "unknown" — never defaulted to relevant. */
export function relevanceOf(action, lists = new Map()) { const a = plain(action) ? action : {};
  if (typeof a.relevant === 'boolean') return a.relevant ? 'relevant' : 'unrelated';
  if (a.relevance === true || a.relevance === 'relevant') return 'relevant'; if (a.relevance === false || a.relevance === 'unrelated') return 'unrelated';
  return lists.get(idOf(a)) || UNKNOWN; }

// Relevant and unrelated are drawn apart by class and by border, and an action the report never classes keeps a dashed border of its own: the
// difference a reader sees is the difference the report stated, and the third drawing is honestly the absence of a statement, not a silent default.
const action = (item, lists) => { const a = plain(item) ? item : {}, kind = relevanceOf(a, lists);
  return `<li class="drift-action drift-${kind === UNKNOWN ? 'unclassed' : kind}" data-drift-action data-relevance="${escapeHtml(kind)}" data-action-id="${escapeHtml(idOf(a))}">`
    + `<b>${escapeHtml(idOf(a))}</b><time>${escapeHtml(stamp(a.at ?? a.timestamp))}</time><span>${escapeHtml(text(a.summary) || text(a.label) || text(a.description) || UNKNOWN)}</span>`
    + `<em>${kind === UNKNOWN ? 'relevance unknown' : `relevance: ${kind}`}</em></li>`; };
// H02 decides, here or nowhere: no numbers in the report means no verdict is manufactured, and a verdict the report states itself is echoed, not re-derived.
const drift = (report) => { const src = [report, plain(report.drift) ? report.drift : null, plain(report.comparison) ? report.comparison : null]   // where the report states the numbers to compare, if anywhere
    .find((s) => plain(s) && ['expected', 'actual', 'tolerance'].some((k) => s[k] !== undefined));
  if (src) { const d = decideDrift(src); return { state: d.drifted ? 'drift' : 'no drift', reason: d.reason }; }
  if (typeof report.drift === 'boolean') return { state: report.drift ? 'drift' : 'no drift', reason: text(report.driftReason) || UNKNOWN };
  return { state: UNKNOWN, reason: 'the report carries no expected, actual or tolerance, so drift is unknown' }; };
// The violation the report carries, or the absence of one: an action's position in the list is never read as the first thing that went wrong.
const violation = (report) => { const v = plain(report.firstViolation) ? report.firstViolation : plain(report.first_violation) ? report.first_violation : null;
  return v ? `${field('first violation', idOf(v))}${field('first violation at', stamp(v.at ?? v.timestamp))}`
    : '<p class="drift-note" data-drift-violation="unknown">first violation unknown — the report carries no first-violation record, so no observed action is pointed at instead</p>'; };
const paths = (label, items) => `<div class="drift-scope" data-drift-scope="${escapeHtml(label)}"><b>${escapeHtml(label)}</b>`   // every scope path is untrusted text
  + (items === null ? `<span class="drift-note">${UNKNOWN}</span>` : items.length ? `<ul class="drift-paths" role="list">${items.map((p) => `<li><code>${escapeHtml(p)}</code></li>`).join('')}</ul>` : `<span class="drift-note">${NONE}</span>`) + '</div>';
const uncertainty = (report) => { const u = plain(report.uncertainty) ? report.uncertainty : { text: text(report.uncertainty) },   // its own words only: no level or hedge is supplied here
  level = text(u.level).trim(), body = text(u.text).trim() || text(u.note).trim() || text(u.reason).trim();
  return level || body ? `${field('uncertainty', level)}<p class="drift-note" data-drift-uncertainty="stated">${escapeHtml(body || 'the report states an uncertainty level and no further text')}</p>`
    : '<p class="drift-note" data-drift-uncertainty="unknown">uncertainty unknown — the report states nothing about its own uncertainty</p>'; };
/** Read-only drift comparison in the plan's order: objective, observed actions, H02's verdict with its reason and the first violation, scope added/removed, model + quantization, uncertainty. */
export function renderDriftComparison(report) {
  const r = plain(report) ? report : {}, lists = relevanceLists(r), actions = Array.isArray(r.actions) ? r.actions : [], m = plain(r.model) ? r.model : {}, scope = plain(r.scope) ? r.scope : {};
  const objective = text(r.objective?.original) || text(r.objective) || text(r.originalObjective) || UNKNOWN, d = drift(r);
  return '<style>.drift-comparison{display:grid;gap:6px;font-size:13px}.drift-comparison h3{margin:8px 0 0;font-size:14px}.drift-objective,.drift-note{margin:0;color:var(--text-secondary);overflow-wrap:anywhere}.drift-actions,.drift-paths{list-style:none;margin:4px 0;padding:0;display:grid;gap:4px}'
    + '.drift-action{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;border-left:4px solid var(--border);padding-left:8px}.drift-relevant{border-left-color:var(--status-passed)}.drift-unrelated{border-left-color:var(--status-failed)}.drift-unclassed{border-left-style:dashed}'
    + '.drift-action time,.drift-action em{font-size:11px;color:var(--text-muted)}.drift-action em{font-style:normal}.drift-scope{margin:4px 0}.drift-scope b{font-size:13px}.drift-paths code{overflow-wrap:anywhere}.drift-fields{display:flex;flex-wrap:wrap;gap:8px}'
    + '.diag-field{border:1px solid var(--border-subtle);border-radius:4px;padding:0 8px}.diag-field b{font-size:13px}.diag-field span{font-size:11px;color:var(--text-secondary)}</style>'
    + `<section class="drift-comparison" data-drift-comparison><h3>Original objective</h3><p class="drift-objective" data-drift-objective>${escapeHtml(objective)}</p>`
    + `<h3>Observed actions</h3><ul class="drift-actions" role="list" aria-label="Observed actions">${actions.map((a) => action(a, lists)).join('') || `<li class="drift-note" data-drift-empty>${Array.isArray(r.actions) ? NONE : UNKNOWN}</li>`}</ul>`
    + `<h3>Drift and first violation</h3><div class="drift-fields">${field('drift', d.state)}</div><p class="drift-note" data-drift-reason>${escapeHtml(d.reason)}</p>${violation(r)}`
    + `<h3>Scope diff</h3>${paths('added', list(scope.added ?? r.addedPaths))}${paths('removed', list(scope.removed ?? r.removedPaths))}`
    + `<h3>Model and quantization</h3><div class="drift-fields">${field('model', m.name ?? r.modelName ?? (text(r.model) ? r.model : ''))}${field('quantization', m.quantization ?? r.quantization)}</div><h3>Uncertainty</h3>${uncertainty(r)}</section>`; }
