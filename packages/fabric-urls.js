// Browser side of fabric URL resolution (spec herdr-fabric-url-resolution-20261028).
// Detection stays in conversation-markdown.js; this module only answers "what is the canonical URL for this
// exact string?" from a cache that the server (POST /api/v1/fabric/resolve-urls) fills. Rendering is synchronous,
// so a URL not yet resolved renders as written and is upgraded on the next paint. Stored text is never rewritten.
const TTL_MS = 30000;
const MAX_BATCH = 200;
const URL_TAIL = '(?::\\d{1,5})?[^\\s<>`*"\\\\)\\]]*';
const LOOPBACK = 'localhost|127(?:\\.\\d{1,3}){3}|0\\.0\\.0\\.0|\\[::1?\\]|\\[::ffff:[^\\]]+\\]|[A-Za-z0-9.-]+\\.lan';
const buildCandidate = (names) => new RegExp(
  `https?:\\/\\/(?:${[LOOPBACK, ...names.map((n) => `(?:[A-Za-z0-9-]+\\.)*${n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`)].join('|')})${URL_TAIL}`, 'gi');
let CANDIDATE = buildCandidate([]);

// The fabric's own names come from the gateway (public fleet name, legacy aliases), so a URL like
// https://svc.local.metabolomics.us:3188/x is recognised and canonicalised, not only *.lan.
export function setFabricNames(names) {
  const clean = [...new Set((names || []).filter((n) => typeof n === 'string' && /^[A-Za-z0-9.-]{1,253}$/.test(n)))];
  CANDIDATE = buildCandidate(clean.map((n) => n.toLowerCase().replace(/^\.+|\.+$/g, '')));
}

// `openEnd`: the text is still streaming, so a URL touching its end may be incomplete; it is left out until the stream settles.
export function candidateUrls(text, { openEnd = false } = {}) {
  const source = String(text ?? '');
  const seen = new Set();
  for (const m of source.matchAll(new RegExp(CANDIDATE.source, 'gi'))) {
    if (openEnd && m.index + m[0].length === source.length) continue;
    seen.add(m[0].replace(/[.,;:!?'"}>]+$/, ''));
  }
  return [...seen].slice(0, MAX_BATCH);
}

export function createFabricUrlResolver(client, { sourceMachine = null, now = () => Date.now(), onChange = () => {} } = {}) {
  const cache = new Map(); // url -> {info, at}
  let pending = new Set(), timer = null, inflight = false;
  if (typeof client.getFabricNames === 'function') {
    Promise.resolve().then(() => client.getFabricNames()).then((out) => {
      if (out?.available && out.names?.length) { setFabricNames(out.names); onChange(); }
    }).catch(() => {});
  }
  const fresh = (e) => e && now() - e.at < TTL_MS;
  async function flush() {
    if (inflight || !pending.size) return;
    const batch = [...pending].slice(0, MAX_BATCH);
    pending = new Set([...pending].slice(MAX_BATCH));
    inflight = true;
    try {
      const out = await client.resolveFabricUrls(batch, typeof sourceMachine === 'function' ? sourceMachine() : sourceMachine);
      let changed = false;
      for (const r of out?.resolutions || []) {
        if (!r || typeof r.original !== 'string') continue;
        const prev = cache.get(r.original)?.info;
        cache.set(r.original, { info: r, at: now() });
        if (!prev || prev.status !== r.status || prev.canonical !== r.canonical) changed = true;
      }
      if (changed) onChange();
    } catch {
      for (const u of batch) cache.set(u, { info: { original: u, canonical: null, status: 'unavailable', reason: '' }, at: now() });
    } finally { inflight = false; if (pending.size) schedule(); }
  }
  function schedule() { if (!timer) timer = setTimeout(() => { timer = null; flush(); }, 25); }
  return {
    resolver: {
      resolve(url) {
        const e = cache.get(url);
        if (!fresh(e)) { if (!pending.has(url)) { pending.add(url); schedule(); } }
        return e ? e.info : null; // stale answers are shown while a refresh is in flight
      },
    },
    prefetch(...texts) { return this.prefetchWith({}, ...texts); },
    prefetchWith(options, ...texts) {
      for (const t of texts) for (const u of candidateUrls(t, options)) if (!fresh(cache.get(u))) pending.add(u);
      if (pending.size) schedule();
    },
    flush,
    clear() { cache.clear(); pending.clear(); },
  };
}

// Delegated handler for the "copy original / copy fabric URL" buttons rendered by conversation-markdown.js.
export function installFabricLinkActions(root, { clipboard = globalThis.navigator?.clipboard } = {}) {
  const onClick = async (event) => {
    const button = event.target?.closest?.('[data-copy]');
    if (!button || !root.contains(button)) return;
    event.preventDefault();
    try { await clipboard.writeText(button.dataset.copy); button.dataset.copied = 'true'; setTimeout(() => { delete button.dataset.copied; }, 1200); }
    catch { button.dataset.copied = 'failed'; }
  };
  root.addEventListener('click', onClick);
  return () => root.removeEventListener('click', onClick);
}
