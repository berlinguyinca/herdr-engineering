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
node --test --test-concurrency=1 tests/playwright/ui-gates-render-xss.test.mjs # complete all-hostile models through the renderers, parsed in Chromium
node --test --test-concurrency=1 tests/playwright/ui-gates-interaction.test.mjs  # one-Tab-stop graph, Enter/Space on nodes, aria-expanded, tabpanels, modal editor, @-mention listbox
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
compare uses the browser's canvas). Neither the gates nor the visual suite run in a plain `npm test` / `npm run test:ui`: those
scripts glob `tests/playwright/!(ui-gates-)*.test.mjs` (the glob is quoted, so Node expands it, not the shell), because the gates
start real Python servers, launch Chromium and push 10,000-node graphs with 600 s timeouts. Only `npm run test:ui-gates` (and
`bash scripts/ci/ui-gates.sh`, which calls it) runs `tests/playwright/ui-gates-*.test.mjs` and `tests/visual/`.

## What is gated, by surface

| Surface | Gate |
|---|---|
| Rooms, chat, tabs, remote agent tabs (T08, T10) | accessibility audit (light and dark, owner and viewer), tablist arrow keys, no tab trap, screenshot at 3 viewports x 2 schemes |
| Mission Workbench (S5): inspector facts, pan/zoom/focus, status strip, 64-session chain, 1500 nodes, 375 and 320px | `ui-gates-workbench.test.mjs` on the `workbench` fixture (`real_server.py`), screenshots `collab-workbench*` |
| Lineage, Gantt, replay (T09) | keyboard graph (one Tab stop, arrow keys), bounded graph viewport (at most 240 nodes drawn at any zoom) with the table alternative, replay by keyboard, stale-evidence edges drawn differently, perf at 2k to 10k nodes |
| Comments, steering, forks (T12, T13) | keyboard-only comment, propose redirect, propose fork, owner confirms what was proposed by someone else; focus is never dropped to the body |
| Proposals and quarantine (T11) | untrusted content is inert (hostile markup is text), reject by keyboard through the confirm dialog, focus kept |
| Context, consult, links (T14) | audit, 320px reflow with 300-character unbroken labels, screenshot |
| Attention and progress (T15) | acknowledge by keyboard, audit, screenshot |
| Workspaces (T07) | audit and screenshot of the links tab |

The states test (`ui-gates-states.mjs`) opens each place where controls live, because the default view of a tab holds only a
fraction of them: a selected lineage node with its comment, steering, consult and link forms; the fork form; the steering confirm
step; the Gantt; the replay bar; the table alternative; a proposal under review; expanded attention items; a remote agent tab;
links; chat; an edited comment (History) and a retracted one; the expanded message editor; the @-mention list open. Each state names
selectors that must exist once open (a state that fails to open is a failure, not a skip) and a least number of audited controls and
text nodes. The floor of a state that settles late (the chat states: the room's agents arrive after the chat) is the observed count
minus three controls and minus six text nodes; the floors of the others are 90% of it. The owner total is 571 controls (floor 540) over 21 states: the round-3 review
added the tabs nobody audited (progress with a milestone, the attention inbox, agents, flow, tasks, the room's people) and made the
table state select a node first. Two viewer states (chat, and lineage with a node selected) run as the read-only account. Everything runs at
desktop light, desktop dark and phone 375, each on its own server (acknowledging an attention item is server state).

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

Third round: the name of a `select` or `textarea` must come from a label, `aria-label`, `aria-labelledby` or `title`, never from its
option text or its content (an unlabelled fork select and snooze select passed before); CSS that reorders focusable children
(`order`, `*-reverse`) is flagged (`focus-order`: Tab no longer follows what is seen); `-webkit-text-fill-color` is the paint colour
when it differs from `color` (transparent text was invisible to the contrast rule); the options of a listbox that a combobox
controls are not asked to be focusable themselves.

Widgets (`ui-gates-interaction.test.mjs`, real page): the lineage graph has exactly one node in the Tab order and Tab leaves it; a
flow node answers Enter and answers Space; the acknowledged-items disclosure says `aria-expanded` false, true, false and shows and
hides its items; every sub-tab has `aria-controls`, its panel is a `tabpanel` labelled by it, and its name is the label with the
count after a comma; the expanded message editor is `role=dialog` with `aria-modal` and a name, Tab and Shift+Tab never leave it,
Escape closes it and returns focus to the button; the @-mention input is a combobox whose `aria-activedescendant` names a selected
option of an open listbox, the arrow keys move it, Escape closes it and clears it. The two composer states are also audited in the
states test (they need an agent that may receive tasks: fixture flag `mention_agent`). The favicon special case in the request
watcher is gone: the page must declare its icon (one `link[rel~=icon]`) so the browser never asks for `/favicon.ico`.

Hostile text (`ui-gates-xss.test.mjs`): a rewriting proxy prefixes a canary payload to every display field of every collaboration
GET response and swaps URL fields for `javascript:`, across all 11 surfaces and every state, against a real server seeded with a
`rich` fixture (redirect instruction, consultation question, external heartbeat activity). It asserts that nothing executes, no
element or handler is created, and that canaries reached the screen for the required fields (otherwise "inert" proves nothing).
`workspace_id` is validated by the server to `[A-Za-z0-9._:-]`, so only the proxy can exercise it. Every string the server sends is
tainted unless it is an identifier or a value the page branches on (a key pattern list, plus hex ids and timestamps); a
hand-written list of display fields once left a bounded task's `target_name` untested. The sign-in config response is left alone
(the page finds its server through it). The test lists which tainted fields never reached the screen and requires that list to equal
`UNRENDERED`, so a new unrendered field cannot hide. Round 3 put text where four renderers print it (an external agent's `reported_by` in
the virtual tab, a node's `state_reason` in the inspector, an agent's `owner_email` in a pending request and in the flow detail, the
analytics `basis`: the fixture room now has `min_group_size` 2 and two finished tasks, one reopened) and asserts each canary is visible
and inert, so `UNRENDERED` fell from 21 to 17. Each of the 17 is covered somewhere else, in `ui-gates-render-xss.test.mjs`, which
gives the renderers a complete model in which every string is hostile (once with the fields the renderer branches on intact, once
with those hostile too) and parses the result in Chromium: `error`, `method`, `milestone` and `paths` are printed by a renderer
there and their canary is asserted on screen; `progress` is drawn only as the number of a `<progress>` and is asserted to be 0 when it
is not a number; the other 12 (`bindings`, `categories`, `category`, `changed_files`, `diff_summary`, `head`, `means`, `order`,
`review_quorum`, `self_reports`, `touched_paths`, `value`) are server objects that no page source reads, which a source scan asserts
(it fails the day a renderer reads one, and it has its own known-bad lines; `head` and `value` are matched by pattern: a `.head` other than the live-view flag, and `.value` inside an `esc()` call, so it is a heuristic, not a proof). The same file renders the pane that reports itself as
remote and every proposal kind.

A consultation's asker and target reference nodes carry `attrs.consultation` (the id of their thread node). The inspector lists a node's attributes (at most 40, each value clipped to 240 characters, all escaped), so the canary is asserted visible and inert once the asker node is opened; the target node uses the same renderer. Three other placements are asserted directly: the lineage an external agent reported,
a milestone label (the proxy adds a milestone to an empty progress response, because a real one needs a linked mission; the states
test does the same), and the target of a bounded task. The payload also breaks out of attributes (a quote, an event handler and a marker attribute first).

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
| Baseline size | at most 6144 KB in total, 300 KB per file (3439 KB, biggest 101 KB now) |
| First render of a large graph | 6000 ms |
| Filter key to next frame, p95 | 500 ms |
| Redraws for six quick keystrokes in the filter above the draw cap | at most 2 (counted through CDP coverage, not timed; measured 1) |
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
spread between runs is the host load.) The first page is about 147 KB. These figures were measured when the SVG was skipped above 300
loaded nodes; since the Mission Workbench change (S5) the graph is a viewport that draws at most 240 nodes and 480 links at any zoom, the
table alternative is still at most 1000 rows per table, and the layout is cached by content (a pan, zoom or selection never lays it out
again). The figures above have not been re-measured for the viewport.

Before the fixes below, typing in the lineage filter took p50 482 ms / p95 611 ms per key at 2,000 nodes and p95 2417 ms at 10,000.

The server stops a lineage scan at 3,000 nodes (`truncated_reason=scan_budget`, no `next_offset`). For rooms above that the UI
now says so ("The server stopped reading here, so more nodes may exist") instead of looking complete; the test asserts this.

## Screenshots

PNGs live in `docs/assets/screenshots/`: 10 surfaces x 3 viewports x light and dark page captures, plus component captures cut by
bounding box (88 files, 3616 KB). Pages are captured to their FULL height (no clipping: a page above 10000px is an error, not a
silent cut; the first version clipped at 2400px and never compared the workbench of a selected node, which sits at 2900 to 3900px).
The workbench has its own regions (comments, fork table, steering form, requests) because a paragraph or one button is far below the
page budget: a transparent first comment scored 0.0025% of the page. Capture is deterministic: the server clock and ingest stamps are
fixed, the browser clock is fixed, reduced motion and `animation:none` are forced, the caret is hidden, scroll is reset on the
lineage and Gantt surfaces, and random text (server ids, key ids, short digests such as the steering fence) is rewritten to stable
tokens by a MutationObserver that keeps doing so while the page redraws on its poll.

**Fonts are bundled** under `tests/playwright/fonts/` (licences in `OFL.txt`, `DEJAVU-LICENSE.txt`, `NOTICE.txt`): Liberation Sans and
Mono (Regular, Bold) for text, `Herdr Symbols` (a renamed subset of DejaVu Sans: geometric shapes, dingbats, arrows) and
`Herdr Symbols Two` (a subset of Noto Sans Symbols2: the media glyphs) for the symbols the page paints. They are injected as
`@font-face` rules on the design system's own family names (`Inter`, `JetBrains Mono`, with `unicode-range` for the symbol fonts) as
data URLs for capture only (`bypassCSP`, because the page CSP is `default-src 'none'`). The room tab's plus sign is now an ASCII `+`
(it was U+FF0B, which only a CJK font has). `assertBundledFonts` walks EVERY element and ::before/::after of the page through the
DevTools protocol (`CSS.getPlatformFontsForNode`) and fails the capture if any glyph was painted by a host font; its own test puts a
host-font span (and an emoji) on the page and requires the failure. **What that proves, and what it does not:** the visual suite runs
`baselines:check` in a child process under a host `FONTCONFIG_FILE` that lists only `tests/playwright/fonts` and renders with the
OPPOSITE settings to the baselines (no hinting, grayscale) and requires the whole suite to pass; `fc-list` under that file shows only
the four bundled families. The browser does not read the host's fontconfig at all: `launch()` in `ui-gates-lib.mjs` starts Chromium
under a generated `FONTCONFIG_FILE` with the bundled fonts and the rendering settings pinned to the ones the baselines were made with
(`hintslight`, `rgb` sub-pixel order, anti-aliasing on, `lcddefault`). So the screenshots depend on neither the fonts nor the
rendering settings of the host. This was added after the first Woodpecker run, where a host with other `conf.d` settings moved all 88
baselines by 1-11% of their pixels; before the pin, the same check under the opposite settings failed 88 of 88 on the workstation,
after it 0 of 88. Another Chromium build or another operating system has not been tried.

The suite has teeth (18 injected defects, each of which must fail on the page or the component): hidden edges, transparent
inspector text, hidden logo and glyphs, missing badges, an invisible Send button, a +7 colour shift, a 1px shift, a 1px wider button,
a card with one more pixel of padding, 4px of tab-bar padding, borderless nodes, a wrongly coloured failed node, a Pause motion
label that is gone, a transparent first comment, a hidden fork button, hidden steering request buttons and a hidden submit button.

Baseline policy:

- `update` needs `--i-understand-this-rewrites-baselines` and a clean git tree, so a baseline cannot be regenerated by accident or
  to hide an unreviewed change. Say why in the PR (browser build, font, intended CSS).
- Per-file size and total size are capped (yaml). `check` fails over either cap. `update` refuses a file over the per-file cap, and
  computes the total the directory WOULD have (replaced files counted once, at their new size) before it writes: over the cap it
  writes nothing and exits 1. It also writes nothing if any surface failed to capture, so a failed update never leaves a half
  rewritten set. A test pins the projection.
- Baselines are tied to the Chromium build Playwright resolves (its FreeType and Skia) and to the bundled fonts. No glyph may fall
  back to a host font (asserted per element). The fontconfig rendering settings (hinting level, anti-aliasing, sub-pixel layout)
  are pinned by `launch()`, not read from the host. What a baseline can STILL depend on, honestly: device scale factor 1 and the
  Chromium build. Another Chromium build needs an `update` with the PNG diff reviewed. An emoji on a surface would fail the
  font assertion rather than silently paint with a colour-emoji font.
- `check` fails on an orphan PNG (a baseline no surface produces); `update` and `check` exit 2 on a slug that matches nothing.
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
21. Sub-tabs had no `aria-controls` and their panels no `tabpanel` role; the badge count was glued onto the name ("Tasks1"). The tabs
    are wired to their panels now and the name is "Tasks, 3 waiting".
22. The room-tab "+ New room" used U+FF0B, a glyph only a CJK font has (so its look depended on the host); it is an ASCII plus.

Found by the round-3 review's gates (fourth round):

23. Four room tabs (Tabs, Progress, Lineage, Linked workspaces) went from the page's `h1` straight to `h3` card titles. Each panel now
    starts with a visually hidden `h2` (`.cx-sr`).
24. The agent rename button was 2.25:1 (muted text at 55% opacity); it is the secondary text colour at full opacity.
25. The screenshot server's sign-in tokens expired 24 hours after the FIXED clock instant (2026-10-23 10:00 UTC), and the page compares
    `exp` with the real time, so `baselines:check` and the visual suite began to fail on 2026-10-24 10:00 UTC with nothing changed.
    The fixture's tokens now outlive the fixed clock by fifty years; a test pins it.

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
- **Known accessibility rough edge left alone:** accept and reject use the browser's `window.confirm`.
- **Mutation check of the gates (second round).** Each previously surviving mutant was re-run against the strengthened gates.
  Caught: N10, N1c, N2a, N3b, N3c, N4d, N5a, N5b, N5c, N8a-c, N9b, N9d, N9e, R5b, R7c, R10, R13c, S1-S6, X5-X8. Three mutants
  (N4d, N9e, S3) first survived because the mutant CSS was appended near the top of `collab.css` (line 3) and later rules of equal
  specificity (lines 250, 252, 309) overrode it, so the defect never rendered; they were re-run with `!important`, which is not a
  weakening of the gate. Still surviving, with the reason: N1b (the Attach button also has visible text, so removing its aria-label
  changes nothing: equivalent), N9a (+1 per channel, below the tolerance of 2, by design), R13b (the older wrap rule is redundant
  with the newer one; removing both, R13c, is caught).
- **R7c** (the layout skip above the draw cap) is caught by counting `layoutGraph` calls through CDP precise coverage, not by timing:
  the layout costs tens of milliseconds, which no budget separates from noise.
- **Third-round mutants (27, all caught by a gate that names them):** unlabelled fork and snooze selects, favicon link removed, three
  unescaped renderers (reported-lineage label, milestone label, bounded-task target), every lineage node a Tab stop, flow node ignoring
  Enter and ignoring Space, an attention disclosure that always says expanded, the editor losing `role=dialog`, losing `aria-modal`
  and losing its Tab trap, the mention list without an active descendant and without `aria-expanded`, reversed and reordered focusables,
  four hidden workbench controls (comment text, fork button, steering buttons, submit), a host font on the replay buttons, transparent
  text through `-webkit-text-fill-color`, a tab without `aria-controls`, a panel without `tabpanel`, a count glued into a tab name, and
  a removed debounce. A control mutant that changes nothing (FP1) survives, as it must. Survivors of all rounds: N1b, N9a, R13b (above).
  The first run flagged the Tab-trap mutant only through the mention test; the dialog test could not see it because it started from the
  textarea, whose own Tab handler indents. It now starts at the edges and catches it itself.
- **Fourth-round mutants.** The reviewer's Q1-Q20 were re-run against the gates at this commit: all 20 are caught (Q13, the chip padding
  shrinking from 5px to 1px, first survived: an 18px chip passes WCAG 2.5.8 through its spacing exception, so the People test now also
  requires 28px). Q1-Q4 (unescaped `reported_by` note, `state_reason`, analytics `basis`, request `owner_email`) are caught by the
  hostile-text gates; Q5-Q8, Q10-Q12 and Q15 by the states audit (the role, `aria-pressed` and `aria-current` selectors those states
  now require, `lang`, the title, the comment label and the heading rule); Q9, Q13 and Q14 only by the People page test (a focus ring
  on the chips, their height, the Rooms tab list's name); Q16-Q18 by the pixels. New mutants for the fixes: the old `nameOf` label
  handling, no tab-list name rule, the old rename opacity, a panel without its `h2`, an unescaped fork overlap path, proposal ref path,
  inbox detail, estimate method and progress error, and an `update` that ignores what it writes: all caught. One new mutant survives
  because it is equivalent: heading order restricted to headings whose predecessor is in the same panel changes nothing now that
  every panel starts with its own `h2`. One of mine was not a defect of the gate (the owner e-mail of a request is printed inline in
  `collab-view.js`, not by an exported renderer; the page-level gate catches it as Q4).
- The debounce gate counts `renderLineage` calls (one for six keystrokes) but no longer asserts `layoutGraph` is never called: a filter
  that narrows the result below the draw cap legitimately lays out.
- The X3 milestone is injected by the proxy, not seeded; a seeded one needs a linked mission.
- **The phone states flake was found in round 4.** Two runs in ten failed on the phone viewport under a host load average of 30 to
  60: a viewer chat audited while the log still said "No messages yet." (2 text nodes, not 11), and the agents state counted 7
  controls, not 10, because "Your agents" arrives after the room's agents. Both states now wait for what arrives late (the log's
  placeholder to go, the grant forms and the room's agents to be listed); the chat floors are observed minus three controls and six text nodes. The phone run
  then passed 10 times of 10 (twice, after each fix). One more thing was not stable: my own `Live` is-not-pressed selector in the
  stepped-back replay state (a poll can put the view back to live between two lines of the test); it is gone, and the
  `aria-pressed` on Live is required in the live table state instead.
- **N5b** (the Pause motion label gone, an empty box) was invisible to every gate until the flow surface joined the visual suite.
- **Not covered by the pixel gate:** a change of one colour step (+1 per channel).
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

## CI

`.woodpecker/ui-gates.yml` runs `bash scripts/ci/ui-gates.sh` (which runs `npm run test:ui-gates`, then `npm run ui-gates:baselines:check`,
then fails on any change under `docs/assets/screenshots`) on Woodpecker, for pull requests that touch the page, its tests, the control
plane or the baselines, and by hand. GitHub Actions is disabled organisation-wide and is not used. The definition follows the shape of
the organisation's other pipelines that were read (`image: bash`, `skip_clone` plus a plain git checkout, step logic in a script in
the repository, no docker, apt or sudo on the exec hosts). It has run on a Woodpecker agent: the first run failed
`baselines:check` on all 88 pages because that host's fontconfig rendering settings differed from the workstation's; `launch()` now pins
them (see the fonts section above).

What the host must already have, because the screenshots are compared pixel for pixel and the script installs nothing system-wide:

- Node 22 and Python 3.12 with `venv`; `git`; a writable checkout. The script installs the Python package into a venv, runs `npm ci`
  and `npx playwright install chromium` (the Chromium of Playwright 1.64 is the build the baselines came from);
- Chromium's shared libraries (what Playwright's own `install-deps` would add);
- `/etc/fonts/conf.d` must exist (the script checks it), but its contents do not matter: the browser runs under its own pinned
  fontconfig, so neither the host's fonts nor its rendering settings reach a screenshot.

The suite takes about ten minutes on a workstation and runs one test at a time; give the step that much and one CPU core per Chromium.

### Load tolerance

The real-page gates wait for elements with Playwright's 30 s default and with explicit budgets (15 s for a room to open, 5-8 s in several
interaction tests). `tests/playwright/ui-gates-lib.mjs` stretches both by the host's 1-minute load average per CPU (`loadScale`, clamped
to 1x-3x): `openReal` sets the page default from it, and every explicit `waitFor`/`waitForFunction` budget in the harness is written
`scaled(ms)`. Write new waits the same way: a bare `timeout: 5000` is the thing that flakes on a shared CI host (a page default does not
override an explicit timeout, which is how the first version of this change missed most of the failures). On an idle host nothing changes. A pipeline that fails with a handful of 30-45 s `waitFor` timeouts and
passes on a re-run of the same commit was load, not a regression; a failure that repeats on every run is not covered by this.
A whole-suite retry was considered and rejected: it doubles the time of every failed run and can hide a real flaky test.

### Time budgets and stale bases

A healthy CI run is about 11 minutes (`ui-gates`) and the check itself is about a minute. `scripts/ci/ui-gates.sh` therefore fails fast:
the suite has a 1500 s budget (`UI_GATES_SUITE_BUDGET_SECONDS`) and `baselines:check` has 300 s (`UI_GATES_BASELINES_BUDGET_SECONDS`).
Exceeding a budget names the phase and exits non-zero. There is deliberately no per-test limit: `node --test --test-timeout` applies to
each test *file* as a whole, and `tests/visual/ui-gates-visual.test.mjs` legitimately runs for several minutes (a 300 s limit cancelled
it on the first attempt at this change). On 2026-10-28 a
`networkidle` wait on a polling page added ~10 s to each of 268 captures: 11 min became 49 min, nothing failed, and the slowdown was
mistaken for a contended host. A budget turns that into a clear red run the first time it happens.

Each phase also prints `took Ns (budget Ms)`, so a creeping run is visible before it trips. Woodpecker checks out the PR head commit, not
a merge with `main`, so a run measures the branch as it forked. The script therefore compares the fork point with the current `main` for
the harness files (`.woodpecker`, `scripts/ci`, `tests/playwright/ui-gates-lib.mjs`, `tests/visual`, `package.json`) and prints a NOTICE
listing what `main` has changed since. When a fix to the harness has merged, merge `main` into your branch and push before trusting a
red or slow run: Woodpecker does not re-run a pipeline when only `main` moves.
