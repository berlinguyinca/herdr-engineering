// <herdr-freshness interval="5000">: "live · updated Ns ago". Red after a failed touch. Amber only
// when the page may be out of date: a quiet fleet sends no events, so while the live stream is in
// contact (its keepalive arrives every ~10 s) an old refresh is still current. Without stream
// contact the chip goes amber past 2x interval since the last refresh.

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

export class HerdrFreshness extends Base {
  static get observedAttributes() { return ['interval']; }

  constructor() {
    super();
    this._last = null;
    this._error = null;
    this._timer = null;
    this._text = null;
    this._state = null;
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

  _render() {
    const age = this._last == null ? null : Math.max(0, Math.floor((Date.now() - this._last) / 1000));
    const ago = age == null ? 'not yet updated'
      : `updated ${age < 60 ? `${age}s` : age < 3600 ? `${Math.floor(age / 60)}m` : `${Math.floor(age / 3600)}h`} ago`;
    const state = freshnessState({ now: Date.now(), last: this._last, error: this._error, interval: this.interval });
    const text = state === 'error' ? `error · ${this._error} · ${ago}` : `live · ${ago}`;
    if (state !== this._state) { this._state = state; this.setAttribute('data-state', state); }
    if (text !== this._text) { this._text = text; this.textContent = text; }
  }
}

if (typeof customElements !== 'undefined' && !customElements.get('herdr-freshness')) {
  customElements.define('herdr-freshness', HerdrFreshness);
}
