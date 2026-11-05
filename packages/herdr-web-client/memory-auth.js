// Memory-only browser authentication. Configuration comes from the private listener;
// session tokens and pending PKCE transactions never share collaboration storage.
import { TokenStore, pkcePair, randomString, authorizeUrl, exchangeCode } from './collab.js';

const PREFIX = 'herdr.memory.';
const CALLBACK_KEYS = ['code', 'state', 'error', 'error_description'];
const FAILURE = 'Memory sign-in could not be verified. Please try again.';

function httpsOrigin(value) {
  // Validate the raw scheme + authority before URL parsing can normalize it.
  if (typeof value !== 'string' || !/^https:\/\/[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?::[0-9]{1,5})?$/.test(value)) throw new Error(FAILURE);
  const url = new URL(value);
  if (!url.hostname || url.username || url.password || url.origin === 'null') throw new Error(FAILURE);
  return url.origin;
}

export class MemoryAuth {
  constructor({ storage = globalThis.sessionStorage, location = globalThis.location,
    history = globalThis.history, fetchImpl = (...args) => fetch(...args), cryptoImpl = globalThis.crypto } = {}) {
    this.storage = storage; this.location = location; this.history = history;
    this.fetch = fetchImpl; this.crypto = cryptoImpl; this.configuration = null; this.message = '';
    this.tokens = new TokenStore({
      getItem: () => storage.getItem(`${PREFIX}tokens`),
      setItem: (_, value) => storage.setItem(`${PREFIX}tokens`, value),
      removeItem: () => storage.removeItem(`${PREFIX}tokens`),
    });
  }
  get signedIn() { return !!this.tokens.get(); }

  async config() {
    if (this.location.protocol !== 'https:') {
      this.message = 'Open the private HTTPS dashboard to sign in to Memory.';
      return null;
    }
    if (this.configuration) return this.configuration;
    try {
      const res = await this.fetch('/api/v1/memory/auth/config', {
        method: 'GET', redirect: 'error', credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000),
      });
      if (!res.ok) throw new Error(FAILURE);
      const cfg = await res.json();
      const origin = httpsOrigin(cfg.origin);
      if (cfg.enabled !== true || cfg.origin !== origin || origin !== this.location.origin || typeof cfg.client_id !== 'string' || !cfg.client_id.trim()
        || cfg.redirect_uri !== `${origin}/`) throw new Error(FAILURE);
      const hosted = httpsOrigin(cfg.hosted_ui_domain?.replace(/\/$/, ''));
      this.configuration = { ...cfg, origin, hosted_ui_domain: hosted };
      return this.configuration;
    } catch {
      this.message = 'Browser sign-in is unavailable here. Existing Memory access can still be used.';
      return null;
    }
  }

  async beginSignIn() {
    const cfg = await this.config();
    if (!cfg) throw new Error(this.message);
    const { verifier, challenge } = await pkcePair(this.crypto);
    const state = `memory.${randomString(32, this.crypto)}`;
    this.storage.setItem(`${PREFIX}pkce`, JSON.stringify({
      verifier, state, createdAt: Date.now(), origin: cfg.origin, redirectUri: cfg.redirect_uri,
    }));
    this.location.assign(authorizeUrl(cfg, cfg.redirect_uri, state, challenge));
  }

  async completeCallback() {
    const q = new URLSearchParams(this.location.search);
    const pending = this.storage.getItem(`${PREFIX}pkce`);
    const hasCallback = CALLBACK_KEYS.some(k => q.has(k));
    const memoryState = q.getAll('state').some(s => s.startsWith('memory.'));
    if (!hasCallback || !(memoryState || (!q.has('state') && this.location.hash.startsWith('#/memory')))) return false;
    const callbackUri = `${this.location.origin}${this.location.pathname}`;
    // Scrub even malformed/failed callbacks before any network or parsing work.
    this.history.replaceState(null, '', '/#/memory');
    this.storage.removeItem(`${PREFIX}pkce`);
    try {
      const saved = JSON.parse(pending || 'null');
      const age = Date.now() - saved?.createdAt;
      if (!saved || !memoryState || q.getAll('code').length !== 1 || !q.get('code') || q.getAll('state').length !== 1
        || q.has('error') || q.has('error_description') || saved.state !== q.get('state') || !saved.verifier
        || !Number.isFinite(age) || age < 0 || age > 600000 || saved.origin !== this.location.origin
        || this.location.protocol !== 'https:' || saved.redirectUri !== callbackUri) throw new Error(FAILURE);
      const cfg = await this.config();
      if (!cfg || cfg.redirect_uri !== saved.redirectUri || cfg.origin !== saved.origin) throw new Error(FAILURE);
      const tokens = await exchangeCode(cfg, q.get('code'), saved.verifier, cfg.redirect_uri,
        (url, options) => this.fetch(url, { ...options, redirect: 'error', credentials: 'omit', signal: AbortSignal.timeout(15000) }));
      this.tokens.set(tokens); this.message = 'Signed in to Memory.';
    } catch {
      this.tokens.clear(); this.message = FAILURE;
    }
    return true;
  }

  signOut() {
    this.tokens.clear(); this.storage.removeItem(`${PREFIX}pkce`);
    this.message = 'Signed out of Memory in this tab.';
  }

  requestOptions(base, path, method) {
    const cfg = this.configuration;
    if (!cfg || this.location.protocol !== 'https:' || this.location.origin !== cfg.origin || method !== 'GET'
      || (base !== '' && base !== cfg.origin) || typeof path !== 'string'
      || !path.startsWith('/api/v1/memory/') || /[\\#]/.test(path) || /%(?:2f|5c|2e)/i.test(path.split('?')[0])) return {};
    const url = new URL(path, cfg.origin);
    if (url.origin !== cfg.origin || !url.pathname.startsWith('/api/v1/memory/')
      || url.pathname.startsWith('/api/v1/memory/auth/') || url.pathname !== path.split('?')[0]) return {};
    const tokens = this.tokens.get();
    return tokens ? { redirect: 'error', headers: { Authorization: `Bearer ${tokens.access}` } } : {};
  }
}

export async function dispatchAuthCallback(memoryAuth, collabCallback) {
  if (!await memoryAuth.completeCallback()) return collabCallback();
  return true;
}
