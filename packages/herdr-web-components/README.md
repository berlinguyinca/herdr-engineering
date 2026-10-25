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

## Live views: conventions

The control-plane SPA (`packages/index.html` + `packages/app.js`) updates pages in place. A page
loads once, then live events and polls patch what changed. The modules below live in `packages/`.
New views should use them rather than swapping `innerHTML`.

### Mounted-view contract

A route's `view*` function in `app.js` renders the page, then returns `{ refresh(hint), dispose() }`.
It may add `interests: ['EventType', …]`.

- `render()` runs only on a route change, meaning any hash change, query included. It shows a
  skeleton, awaits the view function and mounts the returned view.
- Live events never replace the page. They go to `refresh(hint)`.
- `dispose()` stops the view's timers and requests. The router calls it on the next route change.
- A view function that returns nothing (an error or not-found page) is retried in place on the
  next event.
- A dashboard that polls its own data keeps its timer and returns its own `{refresh, dispose}`:
  a live event re-reads its sources at once. Its panels patch in place (`morph`/`patchList`), so
  filters, focus and open disclosures survive; a refresh never shows a loader.
- `liveRefresher(load, apply, chip)` (`refresh-bus.js`) is the usual body for `refresh`. It
  fetches with `load(hint)` and applies the result unless the view was disposed meanwhile. A
  refresh requested mid-flight runs once more afterwards. A failure keeps the page and turns the
  chip red.

### Refresh bus (`refresh-bus.js`)

`createRefreshBus({ throttleMs })` routes SSE events, as hints, to registered views. Each view
gets one trailing throttle: the first event after idle refreshes on the next tick, and later
events in the window coalesce into one refresh at its end. The app uses 5 s, which stays under
the API's per-IP rate limit.

- `register(view, { fresh: true })` makes the first event wait out one window, because the view
  has just loaded.
- A view with `interests` only hears those event types.
- `pause()` and `resume()` follow tab visibility. Resume refreshes each view once.

### Patcher (`patch.js`)

Each function writes only what differs. None of them moves focus, closes an open `<details>` or
drops the text selection.

- `patchText(el, value)` and `patchAttr(el, name, value)` write only on change and return whether
  they wrote. For `patchAttr`, `null`, `undefined` and `false` remove the attribute and `true`
  sets it empty.
- `patchList(container, items, { key, render, update, animate = true, inPlace = false })` is a
  keyed list diff over children with `data-key`:
  - New rows enter with `[data-entering]`.
  - Removed rows leave with `[data-leaving]` and are then removed.
  - Changed rows go through `update(el, item)`. Without `update`, attributes are patched and the
    children are replaced.
  - A row that holds focus, an open `<details>` or the selection waits, unless `inPlace` says
    `update` is focus-safe.
  - Items must be JSON-serialisable: change detection compares `JSON.stringify`.
- `morph(el, fresh)` makes `el` match `fresh`, an Element or an HTML string for its children, with
  the fewest writes. Identical markup writes nothing.
  - Children pair by `data-key`, `data-section`, `data-host-id` or `id`, else by position and tag.
  - Form-control state follows the markup, except on the focused control.
  - Custom elements are leaves.
  - Motion markers (`data-flash`, `data-drawn`, `data-pulse`, enter and leave) are kept.
  - It never writes a `<details>` `open`. A view whose model decides disclosure (Collapse groups,
    a search default) sets `open` itself after the morph.
  - A node the fresh markup no longer has, but that holds focus or the selection, stays. `morph`
    returns these stranded nodes; `handOff(stranded, fallback)` moves focus to `fallback(node)` and
    removes them, so a row that left does not linger while focused. Without a fallback, focus returns
    to the document, as a wholesale replace did.

### Motion (`motion.js`, `motion.css`)

Motion only marks data-driven changes. A first load, or counts that change because the user
filtered or searched, never animate.

- `tween(el, from, to, { format, duration, write })` counts a value up. A later call on the same
  element replaces the running one.
- `flash(el)` sets `[data-flash]` for 600 ms.
- `enter(el)` plays the shared enter transition.
- `easeValue(progress, from, to)` eases a `<progress>`.
- `snapshotText(root, selector, keyOf)` before a redraw and `animateChanges(root, selector, before,
  keyOf)` after it flash the values that changed. A single number counts up from its old value.
- `skeleton(label, { rows, height, bare, inStatus })` is the first-load placeholder
  (`[data-skeleton]`, `aria-busy="true"`). On its own it is a `role="status"` region labelled
  "Loading <label>". Inside an existing `role="status"` region, pass `inStatus`: the placeholder
  then adds no second region and carries a visually hidden "Loading <label>" for the outer one.
  Views replace it when their data lands. A failed first load replaces it with the error text, and
  a live refresh never shows a skeleton again.
- Durations come from `--motion-fast` and `--motion-normal`.

### Calm and dark controls

The shell's controls slot (`theme-controls.js`) holds two selects: Theme (auto, light, dark) and
Calm (auto, on, off).

- **Calm.** `isCalm()` is true when `html[data-calm="on"]` is set, or when the OS asks for reduced
  motion and calm is not `off`. `setCalm('on' | 'off' | 'auto')` stores the choice per viewer under
  `herdr.calm`.
  - Under calm, every helper above writes the final value at once and sets no motion attribute.
  - `motion.css` also turns off every animation and transition.
  - `tests/playwright/calm.test.mjs` guards this on every route.
- **Theme.** `html[data-theme="light" | "dark"]` forces a palette. Without it, the page follows
  `prefers-color-scheme`. The choice is stored under `herdr.theme`.
  - `index.html` applies both stored choices before first paint.
  - Colours are tokens in `herdr-design-system/theme.css`, defined once for light and in both dark
    blocks (the media query, and `data-theme="dark"`).
  - Never hard-code a colour in a view: add a token with its light and dark values.
  - `tests/playwright/dark-theme.test.mjs` checks every route for 4.5:1 text contrast and no
    light fills in dark. Known limit: it measures text against the fills of its DOM ancestors,
    so an absolutely positioned element drawn over a sibling's fill is not measured against what
    it visually covers.

### Freshness chip (`freshness.js`)

`<herdr-freshness interval="5000">` sits in every page head (`.hd-live-head`) and shows
"live · updated Ns ago".

- It turns amber (`data-state="stale"`, text "stale · updated …") past twice the interval, and
  red (`error`, text "error · <message> · …") after `touch(false, message)`. `touch(true)`
  records a successful refresh. Before the first one it reads "waiting for first update"; only a
  fresh chip says "live".
- `setDetail(text, { warn })` appends a note, such as the GitHub sync state. `warn` keeps the chip
  amber.
- A page with several sources wraps the chip in `freshnessSources(() => chip)` and reports each
  source with `ok(source)` or `fail(source, message)`. The chip stays red while any source is
  failing, and the message names each one.
