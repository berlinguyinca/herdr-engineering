import { DENSITIES, sanitizeDensity } from './density.js';

// herdr-web-components — <herdr-app-shell> sidebar shell (Spec §12-16).
// routes is a JSON attribute: [{"path":"/missions","label":"Missions","group":"Work","match":["/missions"]}].
// `group` heads a section of the sidebar; `match` lists the other paths that keep the entry active (tabbed sections).
// The active route is driven by hashchange. The shell owns its template and
// the shared document density preference. Content projects into the main region.

export class HerdrAppShell extends HTMLElement {
  static get observedAttributes() {
    return ['routes', 'brand'];
  }

  constructor() {
    super();
    this._nav = [];
    this._menuOpen = false;
    this._badges = new Map();
    this._density = 'comfortable';
    this.attachShadow({ mode: 'open' });
  }

  attributeChangedCallback() {
    this._parse();
    this._render();
    if (this.isConnected) this._syncActive();
  }

  connectedCallback() {
    let stored;
    try { stored = localStorage.getItem('herdr-density'); } catch { /* Storage can be unavailable. */ }
    this._applyDensity(stored);
    this._parse();
    this._render();
    window.addEventListener('hashchange', this._syncActive);
    this._syncActive();
  }

  disconnectedCallback() {
    window.removeEventListener('hashchange', this._syncActive);
  }

  _applyDensity(value) {
    this._density = sanitizeDensity(value);
    document.documentElement.dataset.density = this._density;
    try { localStorage.setItem('herdr-density', this._density); } catch { /* Apply even without storage. */ }
  }

  _parse() {
    try {
      this._nav = JSON.parse(this.getAttribute('routes') || '[]');
    } catch {
      this._nav = [];
    }
  }

  _syncActive = () => {
    const hash = window.location.hash.replace(/^#/, '') || '/';
    const path = hash.split('?')[0];
    const links = this.shadowRoot.querySelectorAll('.hd-shell__link');
    links.forEach((a) => {
      const paths = [a.getAttribute('data-path'), ...JSON.parse(a.getAttribute('data-match') || '[]')];
      const active = paths.some((p) => path === p || (path !== '/' && path.startsWith(p + '/')));
      a.classList.toggle('active', active);
    });
    this._menuOpen = false;
    const nav = this.shadowRoot.querySelector('.hd-shell__nav');
    const menu = this.shadowRoot.querySelector('.hd-shell__menu');
    const returnFocus = nav?.contains(this.shadowRoot.activeElement);
    nav?.setAttribute('data-open', 'false');
    menu?.setAttribute('aria-expanded', 'false');
    if (returnFocus) menu?.focus();
  };

  // A count shown beside a nav label (for example open attention items). `badge` is
  // {count, critical}; null or a zero count removes it. Updated in place so focus and an open menu survive.
  setBadge(path, badge) {
    const count = badge && Number.isFinite(badge.count) && badge.count > 0 ? Math.floor(badge.count) : 0;
    if (count) this._badges.set(path, { count, critical: Number(badge.critical) > 0 });
    else this._badges.delete(path);
    const link = [...this.shadowRoot.querySelectorAll('.hd-shell__link')].find((a) => a.getAttribute('data-path') === path);
    if (!link) return;
    link.querySelector('.hd-shell__badge')?.remove();
    link.insertAdjacentHTML('beforeend', this._badgeHtml(path));
  }

  _badgeHtml(path) {
    const b = this._badges.get(path);
    if (!b) return '';
    const label = `${b.count} open${b.critical ? `, ${b.critical} critical` : ''}`;
    return `<span class="hd-shell__badge" data-tone="${b.critical ? 'critical' : 'normal'}" role="img" aria-label="${escapeAttr(label)}">${b.count > 99 ? '99+' : b.count}</span>`;
  }

  _render() {
    const brand = this.getAttribute('brand') || 'HerdR';
    const link = (n) => `<a class="hd-shell__link" href="#${escapeAttr(n.path)}" data-path="${escapeAttr(n.path)}"` +
      `${n.match ? ` data-match="${escapeAttr(JSON.stringify(n.match))}"` : ''}>${escapeHtml(n.label)}${this._badgeHtml(n.path)}</a>`;
    const groups = [];
    for (const n of this._nav) {
      const name = n.group || '';
      let g = groups.find((x) => x.name === name);
      if (!g) groups.push(g = { name, items: [] });
      g.items.push(n);
    }
    const links = groups.map((g) => `<div class="hd-shell__group"${g.name ? ` role="group" aria-label="${escapeAttr(g.name)}"` : ''}>` +
      `${g.name ? `<div class="hd-shell__group-label" aria-hidden="true">${escapeHtml(g.name)}</div>` : ''}${g.items.map(link).join('')}</div>`).join('');
    this.shadowRoot.innerHTML = `
      <style>${SHARED}</style>
      <div class="hd-shell">
        <aside class="hd-shell__sidebar">
          <div class="hd-shell__brand"><span>${escapeHtml(brand)}</span><button class="hd-shell__menu" type="button" aria-expanded="false" aria-controls="primary-navigation">Menu</button></div>
          <nav class="hd-shell__nav" id="primary-navigation" data-open="false" aria-label="Primary">${links}</nav>
          <div class="hd-shell__display">
            <label class="hd-shell__density">Density
              <select aria-label="Density">${DENSITIES.map(value => `<option value="${value}" ${value === this._density ? 'selected' : ''}>${value[0].toUpperCase() + value.slice(1)}</option>`).join('')}</select>
            </label>
            <slot name="controls"></slot>
          </div>
        </aside>
        <main class="hd-shell__main"><div class="hd-shell__content">
          <slot></slot>
        </div></main>
      </div>`;
    this.shadowRoot.querySelector('.hd-shell__density select').addEventListener('change', event => {
      this._applyDensity(event.target.value);
    });
    this.shadowRoot.querySelector('.hd-shell__menu')?.addEventListener('click', () => {
      this._menuOpen = !this._menuOpen;
      this.shadowRoot.querySelector('.hd-shell__nav')?.setAttribute('data-open', String(this._menuOpen));
      this.shadowRoot.querySelector('.hd-shell__menu')?.setAttribute('aria-expanded', String(this._menuOpen));
    });
  }
}

// The shell's own styles (referencing the global theme tokens).
const SHARED = `
:host { display: block; }
.hd-shell { display: flex; min-height: 100vh; }
.hd-shell__sidebar { width: var(--sidebar-width); background: var(--surface);
  flex-shrink: 0; border-right: 1px solid var(--border-subtle);
  display: flex; flex-direction: column; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
.hd-shell__brand { display: flex; align-items: center; padding: 14px 16px;
  justify-content: space-between; gap: 12px;
  font-weight: 600; font-size: 0.95rem;
  border-bottom: 1px solid var(--border-subtle); }
.hd-shell__menu { display: none; font: inherit; padding: 6px 10px; color: var(--text-secondary);
  background: var(--surface); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); cursor: pointer; }
.hd-shell__nav { display: flex; flex-direction: column; gap: 8px; padding: 8px 8px; flex: 1; }
.hd-shell__group { display: flex; flex-direction: column; gap: 1px; }
.hd-shell__group-label { padding: 0 10px 3px; font-size: .6875rem; letter-spacing: .06em; text-transform: uppercase; color: var(--text-tertiary, var(--text-secondary)); font-weight: 600; }
.hd-shell__link { display: flex; padding: calc(var(--density-row-padding, 8px) - 4px) 10px; line-height: 1.3; border-radius: var(--radius-sm);
  color: var(--text-secondary); text-decoration: none; font-size: 0.875rem; }
.hd-shell__badge { margin-left: auto; min-width: 20px; padding: 0 6px; border-radius: 10px; font-size: .75rem; line-height: 20px; text-align: center; background: var(--accent-weak); color: var(--accent); font-weight: 600; }
.hd-shell__badge[data-tone=critical] { background: var(--status-failed); color: #fff; }
.hd-shell__link:hover { background: var(--surface-hover); text-decoration: none; }
.hd-shell__link.active { background: var(--accent-weak); color: var(--accent); font-weight: 500; }
.hd-shell__display { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; padding: 10px 14px 14px; border-top: 1px solid var(--border-subtle); position: sticky; bottom: 0; background: var(--surface); }
.hd-shell__density { display: flex; align-items: center; gap: 6px; font-size: .75rem; color: var(--text-secondary); }
.hd-shell__density select { min-width: 0; font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.hd-shell__main { flex: 1; min-width: 0; }
.hd-shell__content { width: auto; max-width: none; margin: 0; padding: clamp(20px,1.25vw,48px); }
@media (max-width: 900px) {
  .hd-shell { flex-direction: column; }
  .hd-shell__sidebar { width: 100%; border-right: none; position: static; height: auto; overflow: visible;
    border-bottom: 1px solid var(--border-subtle); }
  .hd-shell__nav[data-open=false] ~ .hd-shell__display { display: none; }
  .hd-shell__display { position: static; }
  .hd-shell__brand { padding: 10px 16px; }
  .hd-shell__menu { display: inline-flex; min-height: 44px; align-items: center; }
  .hd-shell__density select, .hd-shell__link { min-height: 44px; align-items: center; }
  .hd-shell__nav { display: none; grid-template-columns: repeat(2,minmax(0,1fr)); max-height: 55vh; overflow-y: auto; }
  .hd-shell__nav[data-open=true] { display: grid; }
  .hd-shell__content { max-width: none; padding: 16px; }
}`;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function escapeAttr(s) {
  return escapeHtml(s);
}
