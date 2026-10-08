// herdr-web-components — register all components for browser use.
import {
  HerdrStatus, HerdrSpinner, HerdrEmpty, HerdrError,
  HerdrIdent, HerdrMetric,
} from './components.js';
import { HerdrAppShell } from './app-shell.js';

const registry = {
  'herdr-status': HerdrStatus,
  'herdr-spinner': HerdrSpinner,
  'herdr-empty': HerdrEmpty,
  'herdr-error': HerdrError,
  'herdr-ident': HerdrIdent,
  'herdr-metric': HerdrMetric,
  'herdr-app-shell': HerdrAppShell,
};

export function defineComponents() {
  for (const [name, ctor] of Object.entries(registry)) {
    if (!customElements.get(name)) {
      customElements.define(name, ctor);
    }
  }
  return registry;
}

export { HerdrStatus, HerdrSpinner, HerdrEmpty, HerdrError, HerdrIdent, HerdrMetric, HerdrAppShell };
