// Client for the member web API (plan 5, Task 11). Session: `Authorization: Member <session>`, never a cookie.
//   * the session lives in memory and in sessionStorage (guarded; never localStorage); the Cognito tokens in memory only
//   * a 401 re-exchanges the session once and retries once, else the caller is sent to sign-in (onSignedOut)
//   * 429 honours Retry-After (the uploader owns its own retry for runs); every write returns a ticket that is polled
import { abortError } from './member-upload.js';

export const SESSION_KEY = 'herdr-member-session';
const POLL_MS = [250, 500, 1000, 2000];
const RESULT_DEADLINE_MS = 120000;

export class ApiError extends Error {
  constructor(status, code, message, retryAfter) {
    super(message || code || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code || '';
    this.retryAfter = retryAfter;
  }
}

const defaultSleep = (ms, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(abortError());
  const t = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(t); reject(abortError()); }, { once: true });
});

/** In-memory session with a best-effort sessionStorage copy (so a reload keeps it until it expires). */
export function createSessionStore(storage) {
  let session = null;
  const box = () => {
    try { return storage !== undefined ? storage : globalThis.sessionStorage; } catch { return null; }
  };
  return {
    get() {
      if (session && session.expires_at * 1000 > Date.now() + 5000) return session.session;
      try {
        const raw = box()?.getItem(SESSION_KEY);
        const got = raw ? JSON.parse(raw) : null;
        if (got && typeof got.session === 'string' && got.expires_at * 1000 > Date.now() + 5000) {
          session = got;
          return got.session;
        }
      } catch { /* unreadable: treated as signed out */ }
      session = null;
      return null;
    },
    set(value) {
      session = { session: value.session, expires_at: value.expires_at };
      try { box()?.setItem(SESSION_KEY, JSON.stringify(session)); } catch { /* memory only */ }
    },
    clear() {
      session = null;
      try { box()?.removeItem(SESSION_KEY); } catch { /* nothing to clear */ }
    },
  };
}

/**
 * @param {object} o
 * @param {{get():string|null}} o.sessions   the session store
 * @param {() => Promise<void>} o.refresh   re-exchange the session (from the in-memory tokens); rejects when it cannot
 * @param {() => void} o.onSignedOut        called once a 401 cannot be repaired
 */
export function createMemberApi({ sessions, refresh, onSignedOut, base = '', fetchFn = (...a) => fetch(...a), sleep = defaultSleep }) {
  async function request(method, path, { body, headers = {}, signal, retry429 = true, rawBody } = {}) {
    let reauthed = false;
    let throttled = 0;
    for (;;) {
      const session = sessions.get();
      if (!session) {
        if (!reauthed) {                       // an expired session is renewed from the in-memory tokens when there are any
          reauthed = true;
          try { await refresh(); continue; } catch { /* no tokens: sign in */ }
        }
        onSignedOut?.();
        throw new ApiError(401, 'unauthorized', 'sign in again');
      }
      const h = { Authorization: `Member ${session}`, ...headers };
      let payload;
      if (rawBody !== undefined) payload = rawBody;
      else if (body !== undefined) { h['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
      const r = await fetchFn(base + path, { method, headers: h, body: payload, signal, cache: 'no-store', credentials: 'omit' });
      if (r.status === 401 && !reauthed) {
        reauthed = true;
        try { await refresh(); } catch { onSignedOut?.(); throw new ApiError(401, 'unauthorized', 'sign in again'); }
        continue;
      }
      if (r.status === 429 && retry429 && throttled < 3) {
        throttled += 1;
        const wait = Math.min(60, Math.max(1, Number(r.headers.get('Retry-After')) || 10 * throttled));
        await sleep(wait * 1000, signal);
        continue;
      }
      let data = null;
      try { data = await r.json(); } catch { /* an empty or non-JSON body */ }
      if (r.status === 401) { onSignedOut?.(); throw new ApiError(401, 'unauthorized', 'sign in again'); }
      if (!r.ok) {
        const ra = Number(r.headers.get('Retry-After'));
        throw new ApiError(r.status, data?.error, data?.message, Number.isFinite(ra) && ra > 0 ? ra : undefined);
      }
      return data;
    }
  }

  const A = (id) => `/api/member/artifacts/${encodeURIComponent(id)}`;

  /** A write answers with a ticket; poll it (seconds=0) until it is done. */
  async function settle(first, { signal } = {}) {
    let doc = first;
    const started = Date.now();
    let step = 0;
    while (doc && doc.status === 'pending') {
      if (Date.now() - started > RESULT_DEADLINE_MS) throw new ApiError(504, 'timeout', 'the request is still being processed');
      await sleep(POLL_MS[Math.min(step++, POLL_MS.length - 1)], signal);
      doc = await request('GET', `/api/member/results/${encodeURIComponent(doc.ticket)}?seconds=0`, { signal });
    }
    return doc;
  }

  const key = () => crypto.randomUUID();
  const write = async (method, path, body, opts) => settle(await request(method, path, { body, ...opts }), opts);

  return {
    request, settle,
    me: (o) => request('GET', '/api/member/me', o),
    links: (o) => request('GET', '/api/member/links', o),
    history: (id, o) => request('GET', `${A(id)}/history?limit=200`, o),
    create: (title, filename, profile, o) => write('POST', '/api/member/artifacts', { title, filename, profile, idempotency_key: key() }, o),
    publish: (id, files, message, baseVersionId, o) => write('POST', `${A(id)}/publish`,
      { files, message, ...(baseVersionId ? { base_version_id: baseVersionId } : {}), idempotency_key: key() }, o),
    remove: (id, o) => write('POST', `${A(id)}/delete`, { confirm: true }, o),
    shareCreate: (id, body, o) => write('POST', `${A(id)}/shares`, { ...body, idempotency_key: key() }, o),
    shareApprove: (id, sid, o) => write('POST', `${A(id)}/shares/${encodeURIComponent(sid)}/approve`, { confirm: true, idempotency_key: key() }, o),
    shareRevoke: (id, sid, o) => write('POST', `${A(id)}/shares/${encodeURIComponent(sid)}/revoke`, undefined, o),
    shareEvents: (id, sid, o) => write('POST', `${A(id)}/shares/${encodeURIComponent(sid)}/events`, undefined, o),
    // the uploader's client contract (member-upload.js)
    status: (id, body, o) => request('POST', `${A(id)}/uploads/status`, { body, signal: o?.signal }),
    putRun: (id, uploadId, firstIndex, bytes, meta) => request('PUT',
      `${A(id)}/uploads/${encodeURIComponent(uploadId)}/runs/${firstIndex}`, {
        rawBody: bytes, signal: meta.signal, retry429: false,
        headers: { 'Content-Type': 'application/octet-stream', 'X-Name': encodeURIComponent(meta.name),
          'X-Size-Bytes': String(meta.size_bytes), 'X-File-SHA256': meta.file_sha256, 'X-Run-SHA256': meta.run_sha256 },
      }),
    abort: (id, uploadId, o) => request('POST', `${A(id)}/uploads/${encodeURIComponent(uploadId)}/abort`, o),
  };
}
