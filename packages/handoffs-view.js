import { escapeHtml as esc } from './herdr-web-components/util.js';
import { renderFlow } from './collab-flow.js';

export function handoffGraphSummary(graph = {}) {
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  const states = {};
  for (const edge of edges) states[edge.state] = (states[edge.state] || 0) + 1;
  return { nodes: Array.isArray(graph.nodes) ? graph.nodes.length : 0,
    edges: edges.length, states, edgeText: edges[0] ? `${edges[0].handoff_id} · ${edges[0].state}` : '' };
}

export function mountHandoffs(container, client) {
  container.innerHTML = `<h1>Handoffs</h1><p class="hd-muted">Private work transfers between harnesses and hosts. Handoff text is untrusted and escaped.</p><div data-handoff-summary></div><section class="cx-flow-box" data-handoff-graph></section><section data-handoff-list></section>`;
  const summary = container.querySelector('[data-handoff-summary]');
  const graph = container.querySelector('[data-handoff-graph]');
  const list = container.querySelector('[data-handoff-list]');
  const draw = data => {
    const stats = handoffGraphSummary(data);
    summary.textContent = `${stats.edges} handoffs · ${stats.nodes} endpoints`;
    const flow = { nodes: (data.nodes || []).map(node => ({ ...node, kind: 'agent', name: node.agent_id || node.session_id || node.role, family: node.harness })),
      edges: (data.edges || []).map(edge => ({ from: edge.from, to: edge.to,
        counts: { [(['queued', 'claimed', 'in_progress'].includes(edge.state) ? 'pending' : edge.state || 'unknown')]: 1 },
        last: edge.updated_at })), spawns: [] };
    renderFlow(graph, flow);
    list.innerHTML = (data.edges || []).map(edge => {
      const from = (data.nodes || []).find(node => node.id === edge.from) || {};
      const to = (data.nodes || []).find(node => node.id === edge.to) || {};
      return `<article class="hd-card"><strong>${esc(from.harness || 'unknown')} → ${esc(to.harness || 'unknown')}</strong><span>${esc(edge.state || 'unknown')}</span><p>${esc(edge.goal || '')}</p><small>${esc(edge.handoff_id || '')}</small></article>`;
    }).join('') || '<p class="hd-muted">No handoffs recorded.</p>';
  };
  client.getHandoffTree().then(draw).catch(error => { summary.textContent = 'Unable to load handoffs'; graph.textContent = error.message; });
  return { refresh: () => client.getHandoffTree().then(draw) };
}
