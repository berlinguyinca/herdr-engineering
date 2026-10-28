// <herdr-freshness interval="5000">: "live · updated Ns ago". Red after a failed touch. Amber only
// when the page may be out of date: a quiet fleet sends no events, so while the live stream is in
// contact (its keepalive arrives every ~10 s) an old refresh is still current. Without stream
// contact the chip goes amber past 2x interval since the last refresh.
import { loadingProgress } from './motion.js';

// The server's idle keepalive cadence is 10 s; two and a half periods of silence mean the stream is gone.
export const STREAM_SILENCE_MS = 25000;

const streamContact = { connected: false, at: null };

// app.js reports stream contact here: connected=true on open and on every frame (keepalives included),
// connected=false when the stream errors. The last contact time is kept across a disconnect.
export function noteStreamContact(connected, at = Date.now()) {
  streamContact.connected = Boolean(connected);
  if (connected) streamContact.at = at;
}

// Pure state rule, shared by the element and its tests.
export function freshnessState({ now, last, error = null, interval = 5000, contact = streamContact }) {
  if (error != null) return 'error';
  if (last == null) return 'fresh';
  if (contact?.connected && contact.at != null && now - contact.at <= STREAM_SILENCE_MS) return 'fresh';
  return now - last > 2 * interval ? 'stale' : 'fresh';
}

const Base = typeof HTMLElement === 'undefined' ? class {} : HTMLElement;

// A chip that several independent sources feed: each calls ok/fail and the chip is red as long as
// any source is failing, green once every source has reported ok (touch(true) also marks it live).
export function freshnessSources(chip) {
  const failing = new Map();
  const report = () => {
    const el = chip();
    if (!el || typeof el.touch !== 'function') return;
    if (failing.size) el.touch(false, [...failing.values()].join('; '));
    else el.touch(true);
  };
  return {
    ok(source) { failing.delete(source); report(); },
    fail(source, message) { failing.set(source, String(message || `${source} could not refresh`)); report(); },
    get failing() { return failing.size > 0; },
  };
}

export class HerdrFreshness extends Base {
  static get observedAttributes() { return ['interval']; }

  constructor() {
    super();
    this._last = null;
    this._error = null;
    this._timer = null;
    this._text = null;
    this._state = null;
    this._detail = '';
    this._warn = false;
  }

  get interval() {
    const n = Number(this.getAttribute('interval'));
    return Number.isFinite(n) && n > 0 ? n : 5000;
  }

  connectedCallback() {
    this.setAttribute('role', 'status');
    this._render();
    this._timer = setInterval(() => this._render(), 1000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
  }

  attributeChangedCallback() { if (this.isConnected) this._render(); }

  touch(ok, message) {
    if (ok) {
      this._last = Date.now();
      this._error = null;
    } else {
      this._error = String(message || 'refresh failed');
    }
    this._render();
  }

  setDetail(text = '', { warn = false } = {}) {
    const detail = String(text || '');
    if (detail === this._detail && !!warn === this._warn) return;
    this._detail = detail;
    this._warn = !!warn;
    this._render();
  }

  _render() {
    const age = this._last == null ? null : Math.max(0, Math.floor((Date.now() - this._last) / 1000));
    const ago = age == null ? 'not yet updated'
      : `updated ${age < 60 ? `${age}s` : age < 3600 ? `${Math.floor(age / 60)}m` : `${Math.floor(age / 3600)}h`} ago`;
    let state = freshnessState({ now: Date.now(), last: this._last, error: this._error, interval: this.interval });
    if (state !== 'error' && this._warn) state = 'stale';
    let text = state === 'error' ? `error · ${this._error} · ${ago}` : state === 'stale' ? `stale · ${ago}` : age == null ? 'waiting for first update' : `live · ${ago}`;
    if (this._detail) text += ` · ${this._detail}`;
    if (state !== this._state) { this._state = state; this.setAttribute('data-state', state); }
    if (text !== this._text) {
      this._text = text; this.textContent = text;
      if (this._last == null && this._error == null) this.insertAdjacentHTML('beforeend', loadingProgress('first update'));
    }
  }
}

if (typeof customElements !== 'undefined' && !customElements.get('herdr-freshness')) {
  customElements.define('herdr-freshness', HerdrFreshness);
}
