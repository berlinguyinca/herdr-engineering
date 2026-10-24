// <herdr-freshness interval="5000">: "live · updated Ns ago". Amber past 2x interval, red after a failed touch.
export class HerdrFreshness extends HTMLElement {
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
    let state = 'fresh';
    let text = `live · ${ago}`;
    if (this._error != null) {
      state = 'error';
      text = `error · ${this._error} · ${ago}`;
    } else if (age != null && age * 1000 > 2 * this.interval) {
      state = 'stale';
    }
    if (state !== this._state) { this._state = state; this.setAttribute('data-state', state); }
    if (text !== this._text) { this._text = text; this.textContent = text; }
  }
}

if (typeof customElements !== 'undefined' && !customElements.get('herdr-freshness')) {
  customElements.define('herdr-freshness', HerdrFreshness);
}
