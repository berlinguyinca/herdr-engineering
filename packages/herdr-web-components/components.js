// herdr-web-components — Web Components (Spec §12-16, §85, §91-92).
//
// Build-free, framework-agnostic custom elements styled by the shared design
// system tokens. Every stateful component renders loading / empty / error /
// data states. Status is never color-alone (label + icon always present).
import { statusForStage, tokenVar } from './status.js';
import { formatBytes } from './format.js';
import { escapeHtml, shortenId } from './util.js';

const ICON = {
  ok: '✓',
  active: '●',
  waiting: '○',
  attention: '!',
  blocked: '⊘',
  failed: '✕',
  planning: '◈',
  unknown: '?',
};

// <herdr-status stage="implementation"> — semantic status badge.
export class HerdrStatus extends HTMLElement {
  static observedAttributes = ['stage'];

  connectedCallback() {
    this.render();
  }

  attributeChangedCallback() {
    this.render();
  }

  render() {
    const stage = this.getAttribute('stage') || 'unknown';
    const meta = statusForStage(stage);
    const icon = ICON[stage] || ICON.unknown;
    const color = tokenVar(meta.token);
    // Label is the primary signal; color is decorative/secondary.
    this.innerHTML =
      `<span class="hd-status" style="--sc:${color}" ` +
      `role="status" aria-label="${meta.label}">` +
      `<span class="hd-status__dot" aria-hidden="true">${icon}</span>` +
      `<span class="hd-status__label">${meta.label}</span>` +
      `</span>`;
  }
}

// <herdr-spinner label="Loading…"> — loading state (Spec §91).
export class HerdrSpinner extends HTMLElement {
  connectedCallback() {
    const label = this.getAttribute('label') || 'Loading';
    this.innerHTML =
      `<span class="hd-spinner" role="status" aria-live="polite">` +
      `<span class="hd-spinner__ring" aria-hidden="true"></span>` +
      `<span class="hd-spinner__label">${escapeHtml(label)}…</span>` +
      `</span>`;
  }
}

// <herdr-empty message="No missions yet"> — empty state (Spec §91).
export class HerdrEmpty extends HTMLElement {
  connectedCallback() {
    const message = this.getAttribute('message') || 'Nothing here yet';
    this.innerHTML =
      `<div class="hd-empty" role="status">` +
      `<span class="hd-empty__icon" aria-hidden="true">∅</span>` +
      `<span class="hd-empty__message">${escapeHtml(message)}</span>` +
      `</div>`;
  }
}

// <herdr-error message="…"> — error / degraded state (Spec §91).
export class HerdrError extends HTMLElement {
  connectedCallback() {
    const message = this.getAttribute('message') || 'Something went wrong';
    this.innerHTML =
      `<div class="hd-error" role="alert">` +
      `<span class="hd-error__icon" aria-hidden="true">✕</span>` +
      `<span class="hd-error__message">${escapeHtml(message)}</span>` +
      `</div>`;
  }
}

// <herdr-ident kind="mission" id="…"> — stable-identity monospace label.
export class HerdrIdent extends HTMLElement {
  connectedCallback() {
    const id = this.getAttribute('id') || '';
    const kind = this.getAttribute('kind') || '';
    this.innerHTML =
      `<code class="hd-ident" title="${escapeHtml(kind + '_' + id)}">` +
      `${escapeHtml(shortenId(id))}</code>`;
  }
}

// <herdr-metric label="Tokens" value="1,234"> — a labeled stat tile.
export class HerdrMetric extends HTMLElement {
  connectedCallback() {
    const label = this.getAttribute('label') || '';
    const value = this.getAttribute('value') || '—';
    const hint = this.getAttribute('hint') || '';
    this.innerHTML =
      `<div class="hd-metric">` +
      `<div class="hd-metric__value">${escapeHtml(value)}</div>` +
      `<div class="hd-metric__label">${escapeHtml(label)}</div>` +
      (hint ? `<div class="hd-metric__hint">${escapeHtml(hint)}</div>` : '') +
      `</div>`;
  }
}


