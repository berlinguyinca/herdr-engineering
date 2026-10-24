// <herdr-freshness interval="5000">: "live · updated Ns ago". Amber ("stale · …") past 2x interval, red ("error · …")
// after a failed touch, "waiting for first update" before any data.
// setDetail(text, {warn}) appends a source-specific note (e.g. GitHub sync state); warn keeps it amber.
// Views importing freshnessSources also load in Node (logic tests), where HTMLElement is absent.
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
    // Only a chip that has data and is fresh says "live"; amber says "stale", and a chip with no
    // data yet says it is waiting.
    let state = 'fresh';
    let text = age == null ? 'waiting for first update' : `live · ${ago}`;
    if (this._error != null) {
      state = 'error';
      text = `error · ${this._error} · ${ago}`;
    } else if (this._warn || (age != null && age * 1000 > 2 * this.interval)) {
      state = 'stale';
      text = `stale · ${ago}`;
    }
    if (this._detail) text += ` · ${this._detail}`;
    if (state !== this._state) { this._state = state; this.setAttribute('data-state', state); }
    if (text !== this._text) { this._text = text; this.textContent = text; }
  }
}

// Multi-source views: one chip, many sources. The chip is red while any source's latest read
// failed (the message names them all) and turns fresh only once every source has recovered, so a
// healthy source answering never hides a failing one.
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

if (typeof customElements !== 'undefined' && !customElements.get('herdr-freshness')) {
  customElements.define('herdr-freshness', HerdrFreshness);
}
