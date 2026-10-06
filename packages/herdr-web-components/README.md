# herdr-web-components

Shared Web Components for every HerdR-ecosystem web interface (Spec §12-16,
§85, §91-92). Framework-agnostic custom elements styled by the shared design
system tokens (`herdr-design-system/theme.css`). **Build-free** — import the
ES modules directly; no bundler required.

## Components

| Element | Purpose |
|---------|---------|
| `<herdr-status stage="…">` | Semantic status badge. Label + icon are the primary signal; color is decorative (never color-alone — a11y). |
| `<herdr-spinner label="…">` | Loading state. |
| `<herdr-empty message="…">` | Empty state. |
| `<herdr-error message="…">` | Error / degraded state. |
| `<herdr-ident kind="…" id="…">` | Stable-identity monospace label (shortened). |
| `<herdr-metric label="…" value="…">` | Labeled stat tile. |

Every stateful component renders a loading / empty / error state (Spec §91).

## Pure, testable modules

- `status.js` — canonical stage/status → semantic token + label mapping.
- `format.js` — bytes / duration / timestamp / token formatting.
- `util.js` — `escapeHtml`, `shortenId`.

These are DOM-free and unit-tested with `node --test`:

```bash
node --test herdr-web-components/components.test.js
```

## Usage

```html
<link rel="stylesheet" href="packages/herdr-design-system/theme.css">
<script type="module">
  import { defineComponents } from './packages/herdr-web-components/index.js';
  defineComponents();
</script>
<herdr-status stage="implementation"></herdr-status>
```
