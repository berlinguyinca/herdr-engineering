# UI gates for the collaboration workbench (T16)

These gates check the collaboration page (`packages/collab.html`) as a person uses it: through the **real** integrated control
plane (`ControlPlaneServer` with the collaboration service, the real ingest path and the real static-file allowlist), not only
through mocked API responses. They cover accessibility, reduced motion, keyboard and mobile use, large-graph performance and
screenshot regression. They are necessary, not sufficient, for WCAG 2.2 AA: see "What is not covered".

Nothing here uses environment variables. Budgets live in `tests/playwright/ui-gates.yaml`; the autospec rule mapping lives in
`.autospec/design-gates.yml`; the server fixture takes a JSON argument.

## Commands

```bash
# every UI gate (about 8 minutes on a busy host)
npm run test:ui-gates

# one gate at a time
node --test --test-concurrency=1 tests/playwright/ui-gates-checker.test.mjs    # the checker itself flags known-bad markup
node --test --test-concurrency=1 tests/playwright/ui-gates-real.test.mjs      # a11y, motion, reflow, keyboard traps, XSS, stale evidence
node --test --test-concurrency=1 tests/playwright/ui-gates-keyboard.test.mjs  # keyboard-only flows, pointer events fail the test
node --test --test-concurrency=1 tests/playwright/ui-gates-mock.test.mjs      # error, empty, loading, 409 conflict, 320px overflow
node --test --test-concurrency=1 tests/playwright/ui-gates-perf.test.mjs      # 2k / 5k / 10k nodes through the real ingest
node --test --test-concurrency=1 tests/playwright/ui-gates-tokens.test.mjs    # raw colour, spacing, z-index, font tokens
node --test --test-concurrency=1 tests/playwright/ui-gates-visual.test.mjs    # screenshots vs docs/assets/screenshots

# screenshots
npm run ui-gates:baselines:check      # compare, exit 1 on a miss; diffs go to .autospec/reports/visual/
npm run ui-gates:baselines:update     # rewrite every baseline, then review the PNG diff in git

# autospec design gates (necessary-not-sufficient rule runner)
bash <autospec>/scripts/autospec-design-gates.sh --repo-root . [--strict]

# client unit tests
npm run test:client
```

The browser is the Playwright Chromium already installed on the machine (`~/.cache/ms-playwright`, 153.0.8010.12 when measured).
There is no new dependency: no axe-core, no `@playwright/test`, no image library (the pixel compare uses the browser's canvas).

## What is gated, by surface

| Surface | Gate |
|---|---|
| Rooms, chat, tabs, remote agent tabs (T08, T10) | accessibility audit (light and dark, owner and viewer), tablist arrow keys, no tab trap, screenshot at 3 viewports x 2 schemes |
| Lineage, Gantt, replay (T09) | keyboard graph (one Tab stop, arrow keys), table alternative above the draw cap, replay by keyboard, stale-evidence edges drawn differently, perf at 2k to 10k nodes |
| Comments, steering, forks (T12, T13) | keyboard-only comment, propose redirect, propose fork, owner confirms what was proposed by someone else; focus is never dropped to the body |
| Proposals and quarantine (T11) | untrusted content is inert (hostile markup is text), reject by keyboard through the confirm dialog, focus kept |
| Context, consult, links (T14) | audit, 320px reflow with 300-character unbroken labels, screenshot |
| Attention and progress (T15) | acknowledge by keyboard, audit, screenshot |
| Workspaces (T07) | audit and screenshot of the links tab |

Every real-page test also fails on any console error, CSP violation, failed request or HTTP status of 400 or more.

The in-house checker (`tests/playwright/a11y-checks.mjs`, proven by `ui-gates-checker.test.mjs`) covers accessible names, ARIA
reference integrity, tab and tablist structure, duplicate ids, heading order, `lang`/title/viewport zoom, form labels, contrast
against the computed background, target size (24px, with the spacing exception), hover-only reveals, `cursor:pointer` on elements a
keyboard cannot reach, focus indicators, keyboard traps, horizontal overflow (1.4.10 at 320px and at 200% zoom) and motion.

Motion: with `prefers-reduced-motion: reduce` nothing animates; by default the transitions do run; no animation is endless without
a pause control (the flow view has a "Pause motion" button with `aria-pressed`); nothing flashes more than three times a second.

## Budgets (`tests/playwright/ui-gates.yaml`)

| Budget | Value |
|---|---|
| Viewports | desktop 1280x800, tablet 768x1024, mobile 375x667, reflow 320x568 |
| Visual diff | 0.1% of pixels (autospec `ux-ui-workstream.md`), per-channel tolerance 6 |
| First render of a large graph | 6000 ms |
| Filter key to next frame, p95 | 500 ms |
| Long tasks while loading and filtering | at most 25, none above 1500 ms |
| Raw hex colours in collab CSS and HTML | ratchet 5 (may only go down) |
| Off-grid spacing values | ratchet 72 (may only go down) |
| z-index off the scale, font not from tokens | 0 |

## Measured numbers

Measured with `ui-gates-perf.test.mjs` against the real server, headless Chromium 153, AMD Threadripper PRO 3945WX (24 threads,
504 GB), Linux 6.8. **The host was heavily loaded by other agents (load average 42 to 60 on 24 threads), so every time is inflated;
treat the numbers as an upper bound and re-measure on an idle machine or the CI image.**

| Nodes in room | Server ingest | First render | Load more p50 / p95 | Key to frame p50 / p95 | Long tasks (max) | JS heap |
|---|---|---|---|---|---|---|
| 2,000 | 0.5 s | 345 ms | 117 / 176 ms | 22 / 32 ms | 1 (306 ms) | 6 MB |
| 5,000 | 1.4 s | 416 ms | 255 / 475 ms | 23 / 27 ms | 3 (554 ms) | 7 MB |
| 10,000 | 3.4 s | 896 ms | 458 / 1197 ms | 22 / 32 ms | 2 (323 ms) | 8 MB |

(An earlier run on the same loaded host gave first renders of 473, 394 and 988 ms and key-to-frame p95 of 38, 108 and 40 ms; the
spread between runs is the host load.) The first page is about 147 KB (200 nodes drawn as SVG). Above 300 loaded nodes the SVG is
skipped (the page says "more than the 300 drawn") and the table alternative is shown, at most 1000 rows per table.

Before the fixes below, typing in the lineage filter took p50 482 ms / p95 611 ms per key at 2,000 nodes and p95 2417 ms at 10,000.

The server stops a lineage scan at 3,000 nodes (`truncated_reason=scan_budget`, no `next_offset`). For rooms above that the UI
now says so ("The server stopped reading here, so more nodes may exist") instead of looking complete; the test asserts this.

## Screenshots

PNGs live in `docs/assets/screenshots/<surface>__<viewport>[-dark].png` (9 surfaces x 3 viewports x light and dark = 54 files,
about 3.8 MB). Capture is made deterministic: the server clock and ingest stamps are fixed, the browser clock is fixed, reduced
motion and `animation:none` are forced, the caret is hidden and random server ids are rewritten to stable tokens. Two fresh
captures of the same surface differ by well under a fifth of the budget (the test asserts this); a deliberate one-colour change
and a layout shift both exceed the budget (the "teeth" test).

Baselines are tied to this Chromium build and this host's fonts. When the CI image differs, run `npm run ui-gates:baselines:update`
there and review the PNG diff in the PR. A miss writes `<name>.actual.png` and `<name>.diff.png` under `.autospec/reports/visual/`
(not committed).

## Defects the gates found, and fixed here

Each has a test that fails without the fix.

1. `/collab-context.js` and `/collab-workbench.js` were 404 on the real listener (missing from the static allowlist in
   `control_plane/api.py`), so the page could not start. Mocked tests never saw it.
2. The favicon request produced a console 404.
3. Contrast: muted text 3.2:1, white on accent 3.2:1, accent on accent-weak 2.64:1 (light mode). Tokens adjusted for `.cx-page`.
4. Focus was dropped to the body after "Post comment" and "Propose".
5. Focus was dropped after accepting or rejecting a proposal.
6. Filtering was 0.5 to 2.4 s per keystroke at 2,000 to 3,000 nodes (full relayout per key). Layout is skipped above the draw cap,
   typing is debounced above 300 loaded nodes.
7. A graph cut at the server scan budget looked complete.
8. Flow task lines were mouse-only; they are now focusable and operable with Enter and Space.
9. The flow `svg` had `role=img` around interactive children; it is a `group` now.
10. The flow's endless animation had no pause control (WCAG 2.2.2); added "Pause motion".
11. z-index values off the scale.
12. Long unbroken labels in the inspector link lists overflowed the page by 2161 px at 320px.

## What is not covered (honest gaps)

- **Sign-in is not exercised.** The real server runs with a test authenticator and placeholder client id and hosted-UI domain; the
  Cognito redirect is never followed. Tokens are injected into session storage.
- **axe-core is not used.** The in-house checker is a subset and is not a conformance claim. Rules marked `check: vlm` or
  `check: review` in autospec (reading order, alternative-text quality, link text, 1.4.11 non-text contrast, text spacing) are a
  checklist, not automated.
- **No screen reader was run, and only Chromium.** Firefox and WebKit were not tried.
- **Ratchets are debt.** 5 raw hex colours and 72 off-grid spacing values remain; the tests only stop them growing.
  `motion.duration-from-scale` is deliberately unmapped because DESIGN.md has no motion scale (the autospec runner reports
  1 unmapped rule, and still passes in `--strict`, so it is not a blocker rule).
- **Known accessibility rough edges left alone:** accept and reject use the browser's `window.confirm`; sub-tab accessible names
  include the badge count (for example "Tasks1"); sub-tab panels have no `aria-controls` or `tabpanel` role.
- **Search is client-side** and covers only loaded nodes; the server caps lineage paging at 3,000 nodes.
- **Performance numbers come from a loaded host** (see above).
- **Scenario 7 checks that belong to the e2e harness, not here:** cross-repo issues, the Plannotator digest and PR head SHA checks.
  The UI half (stale-head evidence is displayed distinctly from current evidence) is gated here.
- **Pre-existing failures outside this change:** three CI-dashboard tests already fail on the base branch
  ("CI reports publish over HTTP...", "CI catalog keeps filters...", "failed CI run sends debugging context...").
