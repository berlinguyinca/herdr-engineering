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
          <div class="hd-shell__brand">${escapeHtml(brand)}</div>
          <nav class="hd-shell__nav" aria-label="Primary">${links}</nav>
        </aside>
        <main class="hd-shell__main"><div class="hd-shell__content">
          <slot></slot>
        </div></main>
      </div>`;
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
  font-weight: 600; font-size: 0.95rem;
  border-bottom: 1px solid var(--border-subtle); }
.hd-shell__nav { display: flex; flex-direction: column; gap: 2px; padding: 8px; }
.hd-shell__link { display: flex; padding: 8px 10px; border-radius: var(--radius-sm);
  color: var(--text-secondary); text-decoration: none; font-size: 0.875rem; }
.hd-shell__link:hover { background: var(--surface-hover); text-decoration: none; }
.hd-shell__link.active { background: var(--accent-weak); color: var(--accent); font-weight: 500; }
.hd-shell__main { flex: 1; min-width: 0; }
.hd-shell__content { max-width: var(--content-max); margin: 0 auto; padding: 24px; }
@media (max-width: 720px) {
  .hd-shell { flex-direction: column; }
  .hd-shell__sidebar { width: 100%; border-right: none;
    border-bottom: 1px solid var(--border-subtle); }
  .hd-shell__nav { flex-direction: row; overflow-x: auto; }
}`;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function escapeAttr(s) {
  return escapeHtml(s);
}
