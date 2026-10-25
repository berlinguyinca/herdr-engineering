// Resolver for inline entity references (session:<id>, mission:<id>, host:<id>, ci:<repo>#<run>, pr:<repo>#<n>)
// in agent chat and summaries. Dashboard only. A reference links only when the entity is in the index now,
// and its target is built by entity-links.js, never from the text.
import { entityHref, entityLabel } from './entity-links.js';

const TTL_MS = 60000;
const norm = (s) => { try { return decodeURIComponent(s); } catch { return s; } };

export function createEntityIndex(client, { now = () => Date.now() } = {}) {
  let sessions = new Map(), missions = new Map(), hosts = new Set(), loadedAt = 0, inflight = null;
  async function load() {
    if (inflight) return inflight;
    if (now() - loadedAt < TTL_MS && loadedAt) return false;
    inflight = (async () => {
      const [s, m, h] = await Promise.all([
        client.listSessions?.().catch(() => null), client.listMissions?.().catch(() => null), client.listHosts?.().catch(() => null)]);
      if (s?.sessions) sessions = new Map(s.sessions.map((x) => [x.session_id, x]));
      if (m?.missions) missions = new Map(m.missions.flatMap((x) => [[x.mission_key || x.mission_id, x], [x.mission_id, x]]));
      if (h?.hosts) hosts = new Set(h.hosts.map((x) => x.host_id));
      loadedAt = now();
      return true;
    })().finally(() => { inflight = null; });
    return inflight;
  }
  const resolver = {
    resolve(kind, raw) {
      const id = norm(raw);
      if (kind === 'session' && sessions.has(id)) return { href: entityHref('session', id), label: entityLabel('session', sessions.get(id)) };
      if (kind === 'mission' && missions.has(id)) return { href: entityHref('mission', missions.get(id).mission_key || id), label: entityLabel('mission', missions.get(id)) };
      if (kind === 'host' && hosts.has(id)) return { href: entityHref('host', id), label: `Host ${id}` };
      // CI runs and pull requests are written repo#number; the dashboard links to the filtered list.
      const m = /^([\w.-]{1,100}\/[\w.-]{1,100})#(\d{1,9})$/.exec(id);
      if (m && kind === 'ci') return { href: entityHref('ci', m[2], { repository: m[1] }), label: `${m[1]} · run #${m[2]}` };
      if (m && kind === 'pr') return { href: entityHref('pr', `${m[1]}#${m[2]}`), label: `${m[1]} #${m[2]}` };
      return null;
    },
  };
  return { load, resolver };
}
