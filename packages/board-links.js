// WC-005: pure resolver from a GitHub project item to a Herdr mission/session. A link is verified ONLY by explicit
// identifiers: repository + issue/pull-request number + item type, matched against refs that Herdr itself recorded
// (e.g. a mission's pull request record). Never by title or free text. Exactly one distinct target verifies; several
// is ambiguous; a missing repository/number or a failed refs source is partial. The href comes only from entityHref.
import { entityHref } from './entity-links.js';

const text = v => typeof v === 'string' ? v.trim() : '';
const TYPE = {ISSUE: 'issue', PULL_REQUEST: 'pr'};
const TARGET_KINDS = new Set(['mission', 'session']);

// 'org/app' or 'github.com/org/app' (case-insensitive, like GitHub); any other forge host never matches.
export function normalizeRepository(value) {
  const m = /^(?:(?:https?:\/\/)?github\.com\/)?([\w.-]{1,100})\/([\w.-]{1,100}?)(?:\.git)?$/i.exec(text(value));
  return m ? `${m[1]}/${m[2]}`.toLowerCase() : null;
}
const positive = n => Number.isInteger(n) && n > 0;

/**
 * @param {object} item project item (repository, number, type)
 * @param {{refs?: object[]|null, refsFailed?: boolean}} source refs: [{kind:'mission'|'session', id, type:'issue'|'pr', repository, number}];
 *   null/absent means no relationship source is loaded (nothing can be verified: none), refsFailed means it could not be read (partial).
 * @returns {{state:'verified'|'none'|'ambiguous'|'partial', target?:{kind:string,id:string,href:string}}}
 */
export function resolveBoardLink(item, {refs = null, refsFailed = false} = {}) {
  const type = TYPE[item?.type];
  if (item?.type === 'REDACTED') return {state: 'partial'};
  if (!type) return {state: 'none'};
  const repository = normalizeRepository(item.repository);
  if (!repository || !positive(item.number)) return {state: 'partial'};
  if (refsFailed) return {state: 'partial'};
  if (!Array.isArray(refs)) return {state: 'none'};
  const found = new Map();
  for (const r of refs) {
    if (!r || typeof r !== 'object' || !TARGET_KINDS.has(r.kind) || r.type !== type || r.number !== item.number) continue;
    if (normalizeRepository(r.repository) !== repository) continue;
    const id = text(r.id);
    if (id) found.set(`${r.kind}\u0000${id}`, {kind: r.kind, id});
  }
  if (found.size > 1) return {state: 'ambiguous'};
  if (found.size === 0) return {state: 'none'};
  const [target] = found.values(), href = entityHref(target.kind, target.id);
  return href ? {state: 'verified', target: {...target, href}} : {state: 'none'};
}
