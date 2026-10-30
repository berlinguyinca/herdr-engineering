// The only place that builds an internal dashboard link (cross-linking design, 2026-10-24).
// A link is built from a known entity kind and an id that is percent-encoded once; nothing here
// ever turns free text into an href, and an unknown kind or a missing id gives null so the caller
// can render plain text instead of a dead link.
const ID_MAX = 300;

const q = (params) => {
  const out = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== '') out.set(k, String(v));
  const s = out.toString();
  return s ? `?${s}` : '';
};
// encodeURIComponent leaves ' ( ) ! * alone; encode them too so an id is safe in any attribute quoting.
const enc = (id) => encodeURIComponent(String(id)).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);

// Detail pages take the id in the path; list pages (CI, reviews, projects) take it as a filter.
export const ENTITY_ROUTES = {
  mission: (id, p) => `#/missions/${enc(id)}${q(p)}`,
  session: (id, p) => `#/herd/session/${enc(id)}${q(p)}`,
  host: (id, p) => `#/hosts/${enc(id)}${q(p)}`,
  service: (id, p) => `#/services/${enc(id)}${q(p)}`,
  ci: (id, p) => `#/ci${q({ search: id, ...p })}`,
  review: (id, p) => `#/reviews${q({ search: id, ...p })}`,
  pr: (id, p) => { const m = /^([\w.-]{1,100})\/([\w.-]{1,100})#([1-9]\d{0,9})$/.exec(id); return m ? `#/pr/${m[1]}/${m[2]}/${m[3]}${q(p)}` : `#/reviews${q({ search: id, ...p })}`; },
  project: (id, p) => `#/github${q({ repository: id, ...p })}`,
  'llm-session': (id, p) => `#/llm/session/${enc(id)}${q(p)}`,
  artifact: (id, p) => `#/artifacts/${enc(id)}${q(p)}`,
  'intel-project': (id, p) => `#/intelligence${q({ project: id, ...p })}`,
};
export const ENTITY_KINDS = Object.keys(ENTITY_ROUTES);

export function entityHref(kind, id, params) {
  if (!Object.hasOwn(ENTITY_ROUTES, kind)) return null;
  if (id === undefined || id === null) return null;
  const text = String(id);
  if (!text || text.length > ID_MAX) return null;
  return ENTITY_ROUTES[kind](text, params);
}

const KIND_NAME = { mission: 'Mission', session: 'Agent', host: 'Host', service: 'Service', ci: 'CI run', review: 'Review', pr: 'Pull request', project: 'Project', 'llm-session': 'LLM usage', artifact: 'Artifact' };
const clip = (v) => (typeof v === 'string' ? v.trim().slice(0, 120) : '');

// A short human label; the raw id comes last and only when nothing better is known.
export function entityLabel(kind, record = {}) {
  const r = record && typeof record === 'object' ? record : {};
  const named = clip(r.label) || clip(r.title) || clip(r.display_name) || clip(r.name);
  if (named) return named;
  if (kind === 'ci' && r.repository && r.run_number != null) return `${clip(r.repository)} · run #${r.run_number}`;
  if (kind === 'pr' && r.repository && r.number != null) return `${clip(r.repository)} #${r.number}`;
  const id = clip(r.id) || clip(r.session_id) || clip(r.mission_id) || clip(r.host_id);
  return id ? `${KIND_NAME[kind] || 'Item'} ${id}` : KIND_NAME[kind] || 'Item';
}

// External forge link for a pull request, built from structured fields only (never from text).
export function forgePrUrl(repository, number) {
  if (typeof repository !== 'string' || !/^[\w.-]{1,100}\/[\w.-]{1,100}$/.test(repository)) return null;
  if (!Number.isInteger(number) || number < 1) return null;
  return `https://github.com/${repository}/pull/${number}`;
}

// Internal "how did agents get here" page for a pull request, from structured fields only. The repository may carry a forge
// host (github.com/org/app) or not (org/app); the page is addressed by owner/name/number.
export function prTrailHref(repository, number) {
  if (typeof repository !== 'string' || !Number.isInteger(number) || number < 1) return null;
  const m = /^(?:[\w.-]+\.[\w.-]+\/)?([\w.-]{1,100})\/([\w.-]{1,100}?)(?:\.git)?$/.exec(repository.trim().replace(/^https?:\/\//, ''));
  const host = /^(?:https?:\/\/)?([\w-]+(?:\.[\w-]+)+)\//.exec(repository.trim());
  return m ? `#/pr/${m[1]}/${m[2]}/${number}${host ? `?host=${encodeURIComponent(host[1].toLowerCase())}` : ''}` : null;
}
