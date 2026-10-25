// The hostile-text canary shared by the page-level gate (ui-gates-xss.test.mjs) and the render-level gate (ui-gates-render-xss.test.mjs).
// EVERY string the server sends is tainted unless it is an identifier or a value the page branches on (an id, a fence, a digest, a
// state, a kind, a timestamp, a handle used to look something up): changing those breaks the page rather than testing it. The
// earlier version listed the display fields by hand, and a field nobody listed (a bounded task's target_name) was never tested.
export const KEEP = /(^|_)(id|ids|key|fence|digest|sha|token|state|status|kind|type|role|action|actions|mode|family|visibility|source|scope|version|revision|cursor|offset|code|stage|tone|level|severity|schema|handle|instance|device|credential|ref|fp|cmd|channel|transport|direction|format|mime|encoding|lang|tz|unit|tier|phase|outcome|result|verdict|decision|trust|origin|namespace|prefix|suffix|field|fields|path_kind|edge|edge_type|node_type|anchor|target|subject|expected|binding|principal|session|surface|pane|tab|workspace|from|to|by|domain|issuer|audience|public_url)$|_at$|^(next|prev|since|until|ts|time|ttl|seq|sequence|ok|id|n|kind|type|state|status|role)$/;
// Identifier-looking keys that ARE shown to people and that the server restricts, so only a proxy can test them.
export const FORCE = new Set(['workspace_id', 'host', 'instance_id', 'display_name', 'email', 'reported_by', 'agent_name', 'delegate_label', 'owner_label', 'role_label', 'last_activity', 'target_name', 'attestor']);
export const URLS = new Set(['url', 'href', 'link_url', 'pr_url', 'html_url', 'web_url']);
export const tag = (k) => String(k).replace(/[^a-z0-9]/gi, '').slice(0, 24);
// The payload breaks out of both contexts: a double quote first (out of an attribute value, leaving an event handler and a marker
// attribute behind), then markup (out of text). Escaped, it is just characters.
export const payload = (k) => `" onfocus=window.__xss=1 data-cx-attr x="'><img src=x onerror=window.__xss=1><b id=cx-canary-${tag(k)}>`;

export function taint(v, key, used) {
  if (typeof v === 'string') {
    if (URLS.has(key)) { used.add(key); return 'javascript:window.__xss=9'; }
    if (v.length && key && (FORCE.has(key) || !KEEP.test(key)) && !/^[0-9a-f-]{8,}$/i.test(v) && !/^\d{4}-\d\d-\d\dT/.test(v)) { used.add(key); return `${payload(key)} ${v}`; }
    return v;
  }
  if (Array.isArray(v)) return v.map((x) => taint(x, key, used));
  if (v && typeof v === 'object') { const o = {}; for (const [k, x] of Object.entries(v)) o[k] = taint(x, k, used); return o; }
  return v;
}


// Server fields that the page-level gate taints and that no page source reads (see ui-gates-render-xss.test.mjs). The value is the
// pattern a page source would contain if it started to read the field.
export const UNREAD = {
  bindings: '\\.bindings\\b|\\bbindings\\s*:', categories: '\\.categories\\b|\\bcategories\\s*:', category: '\\.category\\b|\\bcategory\\s*:',
  changed_files: 'changed_files', diff_summary: 'diff_summary', means: '\\.means\\b|\\bmeans\\s*:', order: '\\.order\\b|\\border\\s*:',
  review_quorum: 'review_quorum', self_reports: 'self_reports', touched_paths: 'touched_paths',
  // `head` is read only as a truthy flag (asOf.head, as_of.head: "this is the live view"); a provenance object's `.value` is never read
  // (writer and source are): both are found inside an esc() call, which is how every renderer prints a field (a bare `${x.value}` is the DOM's own input value and the unescaped bug the other gates catch)
  head: '(?<!asOf|as_of)\\.head\\b', value: 'esc\\([^)]*\\.value\\b',
};
