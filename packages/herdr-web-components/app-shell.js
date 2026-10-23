// herdr-web-components — <herdr-app-shell> sidebar shell (Spec §12-16).
// routes is a JSON attribute: [{"path":"/missions","label":"Missions"}].
// The active route is driven by hashchange; the shell emits no DOM writes
// beyond its own template. Content is projected into the main region.

export class HerdrAppShell extends HTMLElement {
  static get observedAttributes() {
    return ['routes', 'brand'];
  }

  constructor() {
    super();
    this._nav = [];
    this._menuOpen = false;
    this.attachShadow({ mode: 'open' });
  }

  attributeChangedCallback() {
    this._parse();
    this._render();
  }

  connectedCallback() {
    this._parse();
    this._render();
    window.addEventListener('hashchange', this._syncActive);
    this._syncActive();
  }

  disconnectedCallback() {
    window.removeEventListener('hashchange', this._syncActive);
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
      const active = path === a.getAttribute('data-path') ||
        (path !== '/' && path.startsWith(a.getAttribute('data-path') + '/'));
      a.classList.toggle('active', active);
    });
    this._menuOpen = false;
    const nav = this.shadowRoot.querySelector('.hd-shell__nav');
    const menu = this.shadowRoot.querySelector('.hd-shell__menu');
    nav?.setAttribute('data-open', 'false');
    menu?.setAttribute('aria-expanded', 'false');
  };

  _render() {
    const brand = this.getAttribute('brand') || 'HerdR';
    const links = this._nav.map((n) =>
      `<a class="hd-shell__link" href="#${escapeAttr(n.path)}" ` +
      `data-path="${escapeAttr(n.path)}">${escapeHtml(n.label)}</a>`).join('');
    this.shadowRoot.innerHTML = `
      <style>${SHARED}</style>
      <div class="hd-shell">
        <aside class="hd-shell__sidebar">
          <div class="hd-shell__brand"><span>${escapeHtml(brand)}</span><button class="hd-shell__menu" type="button" aria-expanded="false" aria-controls="primary-navigation">Menu</button></div>
          <nav class="hd-shell__nav" id="primary-navigation" data-open="false" aria-label="Primary">${links}</nav>
        </aside>
        <main class="hd-shell__main"><div class="hd-shell__content">
          <slot></slot>
        </div></main>
      </div>`;
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
.hd-shell__nav { display: flex; flex-direction: column; gap: 2px; padding: 8px; }
.hd-shell__link { display: flex; padding: 8px 10px; border-radius: var(--radius-sm);
  color: var(--text-secondary); text-decoration: none; font-size: 0.875rem; }
.hd-shell__link:hover { background: var(--surface-hover); text-decoration: none; }
.hd-shell__link.active { background: var(--accent-weak); color: var(--accent); font-weight: 500; }
.hd-shell__main { flex: 1; min-width: 0; }
.hd-shell__content { max-width: min(1600px, calc(100vw - var(--sidebar-width) - 48px)); margin: 0 auto; padding: 24px; }
@media (max-width: 720px) {
  .hd-shell { flex-direction: column; }
  .hd-shell__sidebar { width: 100%; border-right: none;
    border-bottom: 1px solid var(--border-subtle); }
  .hd-shell__brand { padding: 10px 16px; }
  .hd-shell__menu { display: inline-flex; }
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
