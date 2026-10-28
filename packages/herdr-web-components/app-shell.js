import { DENSITIES, sanitizeDensity } from './density.js';

// herdr-web-components — <herdr-app-shell> sidebar shell (Spec §12-16).
// routes is a JSON attribute: [{"path":"/missions","label":"Missions"}].
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

  _openGroups() {
    try { const v = JSON.parse(localStorage.getItem('herdr-nav-groups') || '{}'); return v && typeof v === 'object' ? v : {}; } catch { return {}; }
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
    const within = (base) => path === base || (path !== '/' && path.startsWith(base + '/'));
    const links = this.shadowRoot.querySelectorAll('.hd-shell__link');
    links.forEach((a) => {
      const members = (a.getAttribute('data-members') || a.getAttribute('data-path')).split(' ');
      const active = members.some(within);
      a.classList.toggle('active', active);
      if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      // The group holding the current page is always open, so the user never loses their place.
      if (active) a.closest('details.hd-shell__group')?.setAttribute('open', '');
    });
    // Within one tab bar only the most specific match is current (/github/projects beats /github).
    this.shadowRoot.querySelectorAll('.hd-shell__tabs').forEach((bar) => {
      const tabs = [...bar.querySelectorAll('.hd-shell__tab')];
      const best = tabs.filter((a) => within(a.getAttribute('data-path'))).sort((x, y) => y.getAttribute('data-path').length - x.getAttribute('data-path').length)[0];
      tabs.forEach((a) => { a.classList.toggle('active', a === best); if (a === best) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
      bar.hidden = !best;
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
    // Entries sharing a `family` are one sidebar link (the first member) and become tabs above the content.
    const families = new Map();
    for (const n of this._nav) if (n.family) families.set(n.family, [...(families.get(n.family) || []), n]);
    const seen = new Set();
    const linkHtml = (n) => {
      const members = n.family ? families.get(n.family) : [n];
      const label = n.family ? (members[0].familyLabel || n.label) : n.label;
      return `<a class="hd-shell__link" href="#${escapeAttr(members[0].path)}" data-path="${escapeAttr(members[0].path)}" ` +
        `data-members="${escapeAttr(members.map((m) => m.path).join(' '))}">${escapeHtml(label)}${this._badgeHtml(members[0].path)}</a>`;
    };
    const groups = [];
    for (const n of this._nav) {
      if (n.family) { if (seen.has(n.family)) continue; seen.add(n.family); }
      const name = n.group || '';
      let g = groups.find((x) => x.name === name);
      if (!g) { g = { name, html: [] }; groups.push(g); }
      g.html.push(linkHtml(n));
    }
    const stored = this._openGroups();
    const links = groups.map((g) => g.name
      ? `<details class="hd-shell__group" data-group="${escapeAttr(g.name)}" ${stored[g.name] === false ? '' : 'open'}><summary>${escapeHtml(g.name)}</summary>${g.html.join('')}</details>`
      : g.html.join('')).join('');
    const tabHtml = [...families.values()].filter((m) => m.length > 1).map((m) =>
      `<nav class="hd-shell__tabs" aria-label="${escapeAttr(m[0].familyLabel || 'Section')}" hidden>${m.map((x) =>
        `<a class="hd-shell__tab" href="#${escapeAttr(x.path)}" data-path="${escapeAttr(x.path)}">${escapeHtml(x.tab || x.label)}</a>`).join('')}</nav>`).join('');
    this.shadowRoot.innerHTML = `
      <style>${SHARED}</style>
      <div class="hd-shell">
        <aside class="hd-shell__sidebar">
          <div class="hd-shell__brand"><span>${escapeHtml(brand)}</span><button class="hd-shell__menu" type="button" aria-expanded="false" aria-controls="primary-navigation">Menu</button></div>
          <nav class="hd-shell__nav" id="primary-navigation" data-open="false" aria-label="Primary">${links}</nav>
          <details class="hd-shell__display"><summary>Display</summary>
            <label class="hd-shell__density">Density
              <select aria-label="Density">${DENSITIES.map(value => `<option value="${value}" ${value === this._density ? 'selected' : ''}>${value[0].toUpperCase() + value.slice(1)}</option>`).join('')}</select>
            </label>
            <slot name="controls"></slot>
          </details>
        </aside>
        <main class="hd-shell__main"><div class="hd-shell__content">
          ${tabHtml}
          <slot></slot>
        </div></main>
      </div>`;
    this.shadowRoot.querySelector('.hd-shell__density select').addEventListener('change', event => {
      this._applyDensity(event.target.value);
    });
    this.shadowRoot.querySelectorAll('details.hd-shell__group').forEach((d) => d.addEventListener('toggle', () => {
      const open = this._openGroups(); open[d.getAttribute('data-group')] = d.open;
      try { localStorage.setItem('herdr-nav-groups', JSON.stringify(open)); } catch { /* Not persisted without storage. */ }
    }));
    this._syncActive();
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
  display: flex; flex-direction: column; }
.hd-shell__brand { display: flex; align-items: center; padding: 16px;
  justify-content: space-between; gap: 12px;
  font-weight: 600; font-size: 0.95rem;
  border-bottom: 1px solid var(--border-subtle); }
.hd-shell__menu { display: none; font: inherit; padding: 6px 10px; color: var(--text-secondary);
  background: var(--surface); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); cursor: pointer; }
.hd-shell__nav { display: flex; flex-direction: column; gap: 2px; padding: var(--density-gap, 8px); flex: 1; overflow-y: auto; }
.hd-shell__group { margin-top: 6px; }
.hd-shell__group > .hd-shell__link { margin-bottom: 2px; }
.hd-shell__group > summary { list-style: none; cursor: pointer; padding: 6px 10px; font-size: .6875rem; letter-spacing: .06em;
  text-transform: uppercase; color: var(--text-secondary); opacity: .8; display: flex; justify-content: space-between; align-items: center; border-radius: var(--radius-sm); }
.hd-shell__group > summary::-webkit-details-marker { display: none; }
.hd-shell__group > summary::after { content: '\\25B8'; font-size: .75rem; transition: transform .12s; }
.hd-shell__group[open] > summary::after { transform: rotate(90deg); }
.hd-shell__group > summary:hover { background: var(--surface-hover); }
.hd-shell__display { border-top: 1px solid var(--border-subtle); padding: 4px var(--density-gap, 8px) 8px; font-size: .8125rem; color: var(--text-secondary); }
.hd-shell__display > summary { cursor: pointer; padding: 8px 10px; border-radius: var(--radius-sm); list-style: none; }
.hd-shell__display > summary::-webkit-details-marker { display: none; }
.hd-shell__display > summary:hover { background: var(--surface-hover); }
.hd-shell__tabs { display: flex; flex-wrap: wrap; gap: 4px; margin: -4px 0 18px; padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); }
.hd-shell__tabs[hidden] { display: none; }
.hd-shell__tab { padding: 6px 12px; border-radius: 999px; font-size: .8125rem; color: var(--text-secondary); text-decoration: none; }
.hd-shell__tab:hover { background: var(--surface-hover); text-decoration: none; }
.hd-shell__tab.active { background: var(--accent-weak); color: var(--accent); font-weight: 500; }
.hd-shell__link { display: flex; padding: var(--density-row-padding, 8px) 10px; border-radius: var(--radius-sm);
  color: var(--text-secondary); text-decoration: none; font-size: 0.875rem; }
.hd-shell__badge { margin-left: auto; min-width: 20px; padding: 0 6px; border-radius: 10px; font-size: .75rem; line-height: 20px; text-align: center; background: var(--accent-weak); color: var(--accent); font-weight: 600; }
.hd-shell__badge[data-tone=critical] { background: var(--status-failed); color: #fff; }
.hd-shell__link:hover { background: var(--surface-hover); text-decoration: none; }
.hd-shell__link.active { background: var(--accent-weak); color: var(--accent); font-weight: 500; }
.hd-shell__density { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; font-size: .8125rem; color: var(--text-secondary); }
.hd-shell__density select { flex: 1; min-width: 0; font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.hd-shell__main { flex: 1; min-width: 0; }
.hd-shell__content { width: auto; max-width: none; margin: 0; padding: clamp(20px,1.25vw,48px); }
@media (max-width: 900px) {
  .hd-shell { flex-direction: column; }
  .hd-shell__sidebar { width: 100%; border-right: none;
    border-bottom: 1px solid var(--border-subtle); }
  .hd-shell__brand { padding: 10px 16px; }
  .hd-shell__menu { display: inline-flex; min-height: 44px; align-items: center; }
  .hd-shell__density { padding: 8px 16px; }
  .hd-shell__density select, .hd-shell__link { min-height: 44px; align-items: center; }
  .hd-shell__nav { display: none; max-height: 60vh; overflow-y: auto; }
  .hd-shell__nav[data-open=true] { display: flex; }
  .hd-shell__display { padding: 0 8px; }
  /* The open group's links sit two to a row so the whole menu fits without long scrolling. */
  .hd-shell__group::details-content { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 2px; }
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
