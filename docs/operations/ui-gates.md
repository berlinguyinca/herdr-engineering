# UI gates for the collaboration workbench (T16)

These gates check the collaboration page (`packages/collab.html`) as a person uses it: through the **real** integrated control
plane (`ControlPlaneServer` with the collaboration service, the real ingest path and the real static-file allowlist), not only
through mocked API responses. They cover accessibility, reduced motion, keyboard and mobile use, large-graph performance and
screenshot regression. They are necessary, not sufficient, for WCAG 2.2 AA: see "What is not covered".

Nothing here uses environment variables. Budgets live in `tests/playwright/ui-gates.yaml`; the autospec rule mapping lives in
`.autospec/design-gates.yml`; the server fixture takes a JSON argument.

## Commands

```bash
# every UI gate
npm run test:ui-gates

# one gate at a time
node --test --test-concurrency=1 tests/playwright/ui-gates-checker.test.mjs    # the checker itself flags known-bad markup
node --test --test-concurrency=1 tests/playwright/ui-gates-real.test.mjs      # a11y, motion, reflow, keyboard traps, XSS, stale evidence
node --test --test-concurrency=1 tests/playwright/ui-gates-keyboard.test.mjs  # keyboard-only flows, pointer events fail the test
node --test --test-concurrency=1 tests/playwright/ui-gates-mock.test.mjs      # error, empty, loading, 409 conflict, 320px overflow
node --test --test-concurrency=1 tests/playwright/ui-gates-perf.test.mjs      # 2k / 5k / 10k nodes through the real ingest
node --test --test-concurrency=1 tests/playwright/ui-gates-tokens.test.mjs    # raw colour, spacing, z-index, font tokens
node --test --test-concurrency=1 tests/playwright/ui-gates-states.test.mjs    # every control in every state (inspector forms, Gantt, replay, review...)
node --test --test-concurrency=1 tests/playwright/ui-gates-xss.test.mjs       # hostile text injected into every API field, every surface and state
node --test --test-concurrency=1 tests/visual/ui-gates-visual.test.mjs        # screenshots vs docs/assets/screenshots (not in plain `npm test`)

# screenshots
npm run ui-gates:baselines:check      # compare, exit 1 on a miss; diffs go to .autospec/reports/visual/
npm run ui-gates:baselines:update -- --i-understand-this-rewrites-baselines   # needs a clean git tree; review the PNG diff in git

# autospec design gates (necessary-not-sufficient rule runner)
bash <autospec>/scripts/autospec-design-gates.sh --repo-root . [--strict]

# client unit tests
npm run test:client
```

The browser is whatever Chromium Playwright resolves (`chromium.executablePath()`, 156.0.8078.4 when the baselines were made); no
path is hardcoded in the gate library. There is no new dependency: no axe-core, no `@playwright/test`, no image library (the pixel
compare uses the browser's canvas). The visual suite lives in `tests/visual/`, so a plain `npm test` / `npm run test:ui` does not
run it; `npm run test:ui-gates` does.

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

The states test (`ui-gates-states.mjs`) opens each place where controls live, because the default view of a tab holds only a
fraction of them: a selected lineage node with its comment, steering, consult and link forms; the fork form; the steering confirm
step; the Gantt; the replay bar; the table alternative; a proposal under review; expanded attention items; a remote agent tab;
links; chat. Each state names selectors that must exist once open (a state that fails to open is a failure, not a skip) and a least
number of audited controls. It runs at desktop light, desktop dark and phone 375, each on its own server (acknowledging an
attention item is server state).

Every real-page test also fails on any console error, CSP violation, failed request or HTTP status of 400 or more.

The in-house checker (`tests/playwright/a11y-checks.mjs`, proven by `ui-gates-checker.test.mjs`) covers accessible names, ARIA
reference integrity, tab and tablist structure, duplicate ids, heading order, `lang`/title/viewport zoom, form labels, contrast
against the computed background, target size (24px, with the spacing exception), hover-only reveals, `cursor:pointer` on elements a
keyboard cannot reach, focus indicators, keyboard traps, horizontal overflow (1.4.10 at 320px and at 200% zoom) and motion.
Added after the independent review (each rule has a known-bad and a known-good fixture in the checker test):

- contrast through ancestor opacity, SVG text against the shape under it, `::before`/`::after`, `::placeholder`, and rendered-pixel
  sampling (1st percentile) for image and gradient backgrounds;
- structure: `aria-hidden` on a focusable element, interactive inside interactive, a `role=img` wrapping controls, invalid roles,
  positive `tabindex`, live-region politeness and validity, `transition: all`, a `main` landmark;
- focus: the indicator must reach 3:1 (outline, shadow ring, or a border, background or colour change), and focus on a transparent
  or off-screen control is a failure;
- `liveAnnouncementAudit`: acting on a control must change a live region (dynamic, not only markup).

The checker evaluates through string expressions with a serialised colour library, so the page CSP does not matter, and every audit
returns `.stats` (controls, text nodes, pixels sampled, focused) so a test can assert the audit really covered the page.

Hostile text (`ui-gates-xss.test.mjs`): a rewriting proxy prefixes a canary payload to every display field of every collaboration
GET response and swaps URL fields for `javascript:`, across all 11 surfaces and every state, against a real server seeded with a
`rich` fixture (redirect instruction, consultation question, external heartbeat activity). It asserts that nothing executes, no
element or handler is created, and that canaries reached the screen for the required fields (otherwise "inert" proves nothing).
`workspace_id` is validated by the server to `[A-Za-z0-9._:-]`, so only the proxy can exercise it.

Keyboard: after any action, focus returns to the same control (or its successor: confirm lands on "Confirm and send", cancel on
"Review and confirm", a decided proposal on its row), asserted exactly rather than "not the body". The flow diagram is one Tab stop
(roving tabindex; arrows, Home and End).

Motion: with `prefers-reduced-motion: reduce` nothing animates; by default the transitions do run; no animation is endless without
a pause control (the flow view has a "Pause motion" button with `aria-pressed`); nothing flashes more than three times a second.

## Budgets (`tests/playwright/ui-gates.yaml`)

| Budget | Value |
|---|---|
| Viewports | desktop 1280x800, tablet 768x1024, mobile 375x667, reflow 320x568 |
| Visual diff, page | 0.01% of pixels (autospec allows 0.1%; a whole-page percentage hid edges-gone at 0.05%, invisible inspector at 0.09%), per-channel tolerance 2 |
| Visual diff, component | 0.05% of the component's own area (graph, node, inspector, logo, sub-tabs, log, Gantt, attention item, review pane) |
| Baseline size | at most 6144 KB in total, 300 KB per file (3209 KB, biggest 101 KB now) |
| First render of a large graph | 6000 ms |
| Filter key to next frame, p95 | 500 ms |
| Type-filter toggle above the draw cap, median | 250 ms (measured 32 ms) |
| Load more, p95 | 3000 ms (loose: it goes through the server) |
| CPU calibration | a fixed loop is timed in the page; budgets are multiplied by max(1, measured / `calibration_ref_ms` 100) so a slow CI host does not flake |
| Long tasks while loading and filtering | at most 25, none above 1500 ms |
| Raw hex colours in collab CSS and HTML | ratchet 5 (may only go down) |
| Off-grid spacing values | ratchet 72 (may only go down) |
| z-index off the scale, font not from tokens | 0 |

## Measured numbers

Measured with `ui-gates-perf.test.mjs` against the real server, headless Chromium 153 (these figures predate the move to Playwright's Chromium 156 and were not re-measured; the budgets are loose multiples), AMD Threadripper PRO 3945WX (24 threads,
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

PNGs live in `docs/assets/screenshots/`: 9 surfaces x 3 viewports x light and dark page captures, plus component captures cut by
bounding box (72 files, 3209 KB). Capture is deterministic: the server clock and ingest stamps are fixed, the browser clock is
fixed, reduced motion and `animation:none` are forced, the caret is hidden, scroll is reset on the lineage and Gantt surfaces, and
random server ids are rewritten to stable tokens. **Fonts are bundled** (Liberation Sans and Mono, Regular and Bold, subset, under
`tests/playwright/fonts/` with `OFL.txt`) and injected as `@font-face` data URLs for capture only (`bypassCSP`, because the page CSP
is `default-src 'none'`); a CDP `CSS.getPlatformFontsForNode` check asserts the bundled fonts were really used. Three fresh loads
agree within half the page budget (asserted).

The suite has teeth (14 injected defects, each of which must fail on the page or the component): hidden edges, transparent
inspector text, hidden logo and glyphs, missing badges, an invisible Send button, a +7 colour shift, a 1px shift, a 1px wider button,
a card with one more pixel of padding, 4px of tab-bar padding, borderless nodes and a wrongly coloured failed node.

Baseline policy:

- `update` needs `--i-understand-this-rewrites-baselines` and a clean git tree, so a baseline cannot be regenerated by accident or
  to hide an unreviewed change. Say why in the PR (browser build, font, intended CSS).
- Total size and per-file size are capped (yaml) and enforced by a test and by `check` and `update`.
- Baselines are tied to the Chromium build Playwright resolves and to the bundled fonts. Residual host dependence: emoji and symbol
  glyphs that the bundled fonts do not cover still fall back to the system. Another Chromium build needs an `update` with review.
- A miss writes `<name>.actual.png` and `<name>.diff.png` under `.autospec/reports/visual/` (not committed).
- A one-colour change of +1 per channel is below the tolerance of 2 and is not detected, by design.

## Defects the gates found, and fixed here

Each was found by a failing gate (or a measurement taken before the fix) and is covered by a gate now. The revert-the-fix-and-rerun
check was not done for every item; the numbers quoted below (contrast ratios, 2161 px overflow, filter latency) are from the runs that found them.

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

Found by the strengthened gates (second round):

13. The focus ring (`--cx-ring`) was a 1.5:1 tint; it is a 3px accent ring now.
14. Dimmed lineage nodes faded their text to 1.7:1; the shapes fade now, not the text.
15. Dark placeholders were 3.6:1; they use the muted text token at full opacity.
16. At 320px with 300-character labels, Attention and Progress overflowed; they wrap now.
17. Focus dropped to the container when the steering confirm step opened, and a decided proposal did not get focus back on its row;
    focus now returns to the same control or its successor.
18. A graph cut by `edge_budget`, `limit` or `response_bytes` with no next page claimed nothing was missing; the message now says
    which kind of cut it was.
19. The pause-motion button changed its label and aria-pressed together (read twice) and forgot the choice; one label, remembered.
20. The flow diagram was a Tab stop per node and per line; it is one stop with arrow keys.

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
- **Not covered by the pixel gate:** a change of one colour step (+1 per channel), and emoji or symbol glyphs that fall back to the host fonts.
- **Search is client-side** and covers only loaded nodes; the server caps lineage paging at 3,000 nodes.
- **Performance numbers come from a loaded host** (see above).
- **Screenshot determinism is bounded, not proven exact.** The determinism test allows half the page budget between fresh loads. The deterministic server stamps
  ingest with a stepping clock that only advances per `ControlPlaneRepo._now()` call; stamps are fixed once seeding ends, but if
  a later read path starts calling `_now()` before seeding finishes on a slower CI host, Gantt ongoing durations could move.
- **Scenario 7 checks that belong to the e2e harness, not here:** cross-repo issues, the Plannotator digest and PR head SHA checks.
  The UI half (stale-head evidence is displayed distinctly from current evidence) is gated here.
- **Other test files in `tests/playwright/` (not part of these gates) still hardcode a Chromium path** from an earlier machine.
- **Pre-existing failures outside this change:** on the base branch (`feat/t16-release-gates`) three CI-dashboard tests fail too (and one more, "activity uses compact expandable rows...", failed once there under load but passes on this branch)
  ("CI reports publish over HTTP...", "CI catalog keeps filters...", "failed CI run sends debugging context...").
