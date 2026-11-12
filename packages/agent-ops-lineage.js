// Work Command Center embed (slice 07): a small task-lineage card under the Overview. It shows, per task, who is accountable, who
// contributed and who reviews, taken straight from the Agent Operations snapshot. Nothing is inferred: review status appears only when a
// review edge exists, and artifacts/tests are not shown at all because the snapshot does not carry their results.
// Gate: both agent_ops_v1 and work_center_v1 must be advertised; with either off the card is not drawn and nothing is fetched.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { workCenterEnabled } from './work-center-flag.js';
import { agentOpsEnabled, nodeName, shortId } from './agent-ops-model.js';
import { loadSnapshot } from './agent-ops-view.js';

const list = (v) => (Array.isArray(v) ? v : []);
export const lineageEnabled = (capabilities) => workCenterEnabled(capabilities) && agentOpsEnabled(capabilities);
export const MAX_TASKS = 6;
export const mapHref = (taskId) => `#/agent-ops?task=${encodeURIComponent(taskId)}`;

/** Snapshot -> rows. Names come from the snapshot's own agents; an unknown id is shown shortened, never guessed. */
export function lineageModel(snapshot, limit = MAX_TASKS) {
  const agents = new Map(list(snapshot?.agents).map((a) => [a.node_id, a]));
  const name = (id) => (id ? (agents.has(id) ? nodeName(agents.get(id)) : shortId(id)) : '');
  const reviews = list(snapshot?.edges).filter((e) => e.kind === 'review');
  const tasks = list(snapshot?.tasks).slice().sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || ''))).slice(0, limit);
  return tasks.map((t) => {
    const review = reviews.filter((e) => e.task_id === t.task_id).map((e) => e.state).at(-1) || null;
    return { id: t.task_id, owner: name(t.accountable_owner), contributors: list(t.contributors).map(name), reviewer: name(t.reviewer), reviewState: review, updatedAt: t.updated_at || null };
  });
}

export function renderLineageCard(rows, { total = rows.length } = {}) {
  const body = rows.length ? `<ul class="ao-lineage-list">${rows.map((r) => `<li class="ao-lineage-row">
    <a href="${esc(mapHref(r.id))}" class="ao-lineage-task">${esc(r.id)}</a>
    <dl class="ao-dl"><dt>Accountable owner</dt><dd>${esc(r.owner || 'not assigned')}</dd><dt>Contributors</dt><dd>${esc(r.contributors.join(', ') || 'none reported')}</dd>
    <dt>Reviewer</dt><dd>${esc(r.reviewer || 'none reported')}${r.reviewState ? ` <span class="ao-chip">review ${esc(r.reviewState)}</span>` : ''}</dd></dl></li>`).join('')}</ul>`
    : '<p class="ao-note">No tasks reported by agents yet.</p>';
  return `<section class="hd-card ao-lineage" data-overview="agent-lineage" aria-labelledby="ao-lineage-h"><h2 id="ao-lineage-h">Task lineage</h2>
    <p class="ao-note">Who is accountable, who contributed and who reviews, as reported by agents. Delegating never moves accountability.</p>${body}
    <p><a href="#/agent-ops">Open the activity map</a>${total > rows.length ? ` · ${total - rows.length} more tasks` : ''}</p></section>`;
}

export function mountAgentOpsLineage(container, client) {
  let disposed = false;
  const paint = async () => {
    const r = await loadSnapshot(client, { signal: AbortSignal.timeout(15000) });
    if (disposed || r.status !== 'ok') { if (!disposed) container.innerHTML = ''; return; }   // off or failing: the Overview stays exactly as it was
    container.innerHTML = renderLineageCard(lineageModel(r.body), { total: list(r.body.tasks).length });
  };
  void paint();
  return { refresh: paint, dispose() { disposed = true; container.innerHTML = ''; } };
}
