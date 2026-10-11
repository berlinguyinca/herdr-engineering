// herdr-web-components — humanize raw control-plane events into readable
// "what happened, and why" sentences. Pure + node-testable (no DOM).
//
// The activity feed and per-entity event streams render raw event envelopes
// (event_type + JSON payload). This module turns those into a short headline
// and a one-line explanation, resolving workspace/mission ids to their
// human titles via an injected lookup (ctx.missionTitles) when available.

const KIND = {
  host: 'host',
  mission: 'mission',
  session: 'session',
  artifact: 'artifact',
  service: 'service',
  generic: 'event',
};

// Resolve an id to a readable label: prefer a known human title, else the id.
function label(id, lookup, fallback) {
  if (!id) return fallback || 'the fabric';
  const title = lookup && lookup[id];
  if (title && title !== id) return `“${title}”`;
  return id;
}

function roleOr(payload, fallback) {
  return payload?.agent_role || payload?.role || payload?.agent_kind
    || payload?.kind || fallback || 'agent';
}

// Map a control-plane event envelope to a human description.
//   ctx.missionTitles  - { [mission_id]: title }
// Returns { title, detail, kind, muted }. `muted` marks low-signal events
// (e.g. heartbeats) that the activity feed should de-emphasise.
export function describeEvent(event, ctx = {}) {
  const lookup = ctx.missionTitles || {};
  const p = event.payload || {};
  const name = p.host_name || p.title || p.name || p.model || '';
  // A mission's human title: prefer payload.title when it differs from the id
  // (the plugin now emits the real workspace label), else the mission lookup.
  const missionTitle = (p.title && p.title !== event.entity_id)
    ? `“${p.title}”`
    : label(p.mission_id || event.mission_id || event.entity_id, lookup);
  const mission = missionTitle;
  const host = p.host_id || p.host_name || '';

  switch (event.event_type) {
    case 'HostRegistered':
      return {
        kind: KIND.host,
        title: 'Host online',
        detail: `${p.host_name || event.entity_id} joined the fabric`
          + (p.tailnet_ip ? ` (${p.tailnet_ip})` : ''),
      };
    case 'HostHeartbeat':
      return {
        kind: KIND.host,
        title: 'Host heartbeat',
        detail: `${p.host_name || event.entity_id} reported healthy`,
        muted: true,
      };
    case 'HostUnavailable':
      return {
        kind: KIND.host,
        title: 'Host unavailable',
        detail: `${p.host_name || event.entity_id} stopped responding`,
      };

    case 'MissionStarted':
      return {
        kind: KIND.mission,
        title: 'Mission started',
        detail: `${mission} began — ${p.stage || 'active'}`,
      };
    case 'MissionUpdated':
      return {
        kind: KIND.mission,
        title: 'Mission updated',
        detail: `${mission} → ${p.stage || p.status || 'active'}`,
      };
    case 'MissionEnded':
      return {
        kind: KIND.mission,
        title: 'Mission completed',
        detail: `${mission} wrapped up`,
      };

    case 'SessionStarted':
      return {
        kind: KIND.session,
        title: `${roleOr(p)} started`,
        detail: `${roleOr(p)} began working in ${mission}`
          + (host ? ` on ${host}` : ''),
      };
    case 'SessionUpdated':
      return {
        kind: KIND.session,
        title: `${roleOr(p)} → ${p.status || 'active'}`,
        detail: `${roleOr(p)} is ${p.status || 'working'} in ${mission}`,
      };
    case 'SessionTerminated':
      return {
        kind: KIND.session,
        title: `${roleOr(p)} stopped`,
        detail: `${roleOr(p)} stopped working in ${mission}`,
      };

    case 'ServiceRegistered':
      return {
        kind: KIND.service,
        title: 'Service registered',
        detail: `${p.service_name || p.name || event.entity_id} at ${p.url || ''}`,
      };

    case 'ArtifactRegistered':
      return {
        kind: KIND.artifact,
        title: 'Artifact saved',
        detail: `${p.filename || 'artifact'} stored${mission === 'the fabric' ? '' : ` for ${mission}`}`,
      };

    default:
      return {
        kind: KIND.generic,
        title: event.event_type || 'Event',
        detail: summarizePayload(p),
      };
  }
}

// Compact fallback summary of an arbitrary payload (no raw JSON dump).
function summarizePayload(p) {
  if (!p || typeof p !== 'object') return String(p ?? '');
  const parts = [];
  for (const [k, v] of Object.entries(p)) {
    if (v == null || v === '' || v === false) continue;
    if (typeof v === 'object') continue;
    parts.push(`${k}: ${v}`);
  }
  return parts.join(' · ') || '(no details)';
}

// A short human subtitle for a mission (scope/purpose + sessions).
export function missionSubtitle(mission, sessionsCount = 0) {
  const scope = mission?.purpose || mission?.cwd;
  const bits = [];
  if (scope) bits.push(scope);
  if (sessionsCount) bits.push(`${sessionsCount} session(s)`);
  return bits.join(' · ') || '—';
}

// A compact one-line summary for a session card. Shows what the session is
// doing (terminal title), where it runs, and its branch — hiding empty detail.
export function sessionSummary(session, missionTitles = {}) {
  const role = session?.agent_role || 'agent session';
  const doing = session?.summary ? ` — ${session.summary}` : '';
  const branch = session?.branch ? ` [${session.branch}]` : '';
  const model = session?.model ? ` · ${session.model}` : '';
  const mission = session?.mission_id
    ? ` · in ${label(session.mission_id, missionTitles)}` : '';
  const host = session?.host_id ? ` on ${session.host_id}` : '';
  const activity = (session?.messages || session?.tool_calls)
    ? ` · ${session.messages} msg / ${session.tool_calls} tool calls` : '';
  return `${role}${doing}${branch}${model}${mission}${host}${activity}`;
}
