# herdr-design-system

Shared design tokens + base styles for every HerdR-ecosystem web interface
(Spec §6-16, §92). **Mandatory** for all HerdR web surfaces — the control
plane, Planator, and any future tooling share this one system.

## Canonical light theme

Light, neutral, clean, ChatGPT-inspired developer UI: white surfaces, light
gray backgrounds, subtle borders. Not Grafana/Kibana/dark-NOC.

Key tokens (from `theme.css`):

| Token | Value | Use |
|-------|-------|-----|
| `--background` | `#f7f7f8` | app background |
| `--surface` | `#ffffff` | cards / panels |
| `--accent` | `#10a37f` | brand / interactive |
| `--text` | `#353740` | primary text |
| `--font-sans` | `Inter, …` | UI text |
| `--font-mono` | `JetBrains Mono, …` | terminal/code/paths/IDs/hashes/logs only |

## Semantic status colors

`--status-ok` (green), `--status-active` (blue), `--status-waiting` (gray),
`--status-attention` (amber), `--status-blocked` (orange), `--status-failed`
(red), plus mission-stage tokens (`--status-planning` violet, etc. — Spec §10).

**Status is never communicated by color alone** — always pair with text and/or
an icon (see `herdr-web-components`).

## Files

- `theme.css` — CSS custom-property tokens (no build step; works with Web
  Components or any framework).
- `base.css` — reset + typography + visible focus ring + reduced-motion.

## Usage

```css
@import url("herdr-design-system/theme.css");
@import url("herdr-design-system/base.css");
```
