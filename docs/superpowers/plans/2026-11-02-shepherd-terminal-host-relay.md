# Shepherd Terminal: Host Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Shepherd terminal mirrors and drives the herdr pane an agent already runs in, through a small relay on each host that talks to herdr's own socket and connects outbound to the control plane. It replaces the server-run pty of `2026-11-02-shepherd-terminal.md`.

**Architecture:** A relay service on each fleet host (Python, shipped by `deploy/deploy-fleet-agents.sh`, using the host's own credentials) long-polls the control plane for terminal commands and pushes screen frames. It reads panes with herdr's socket API (`pane.read`, events) and types with `pane.send_text`, `pane.send_keys` and `pane.send_input`. The control plane becomes a broker (command queue per host, numbered frame buffer per terminal) behind the SAME five phone procedures. The app's data path is unchanged: each frame is a `reset: true` chunk, which the built client already draws as clear-then-write.

**Tech Stack:** Python 3.12 stdlib (`socket`, `selectors`, `json`, `threading`, `urllib`), herdr socket API (protocol 22), existing bridge and host-key auth, Expo app (xterm page), pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-11-02-shepherd-terminal-design.md` including Amendments 1 and 2 and "Confirmed on bender" (owner-approved direction: terminal replaces Moshi, runs on the host, mirrors the herdr pane).

## Global Constraints

- Nothing inbound to hosts; the relay uses the host's own existing credentials (`host.json`), like steering delivery. No ssh, no keys, no pty anywhere.
- The control plane never starts a process. The relay attaches no client: it never runs `herdr agent attach`.
- Owner-only gate (`bridge.is_owner`), input/resize exempt from the 120/min limiter, `open_owner` narrowed to loopback + tailnet: already built on branch `feat/shepherd-terminal` (commit `ce52a5f2`) and kept.
- The phone-facing procedures and their wire shapes do not change: `terminal/open {id, rows, cols} -> {terminalId, seq: 0, rows, cols}`, `terminal/input {terminalId, data, n}`, `terminal/resize`, `terminal/close`, `terminal/subscribe {terminalId, cursor}` with frames `{seq, data}`, `{seq, data, reset: true}`, `{seq, ended}`, `{seq, taken_over}`. Limits: 4 live terminals, 64 KB per input call, 200 KB buffer, 5 minutes kept after the last stream closes, stream poll 0.03 s, audit `rakazo.bridge.terminal.attach` / `.detach` (never input or output bytes).
- herdr facts (confirmed on bender, herdr 0.9.1): socket `~/.config/herdr/herdr.sock` (0600, same user); `send-keys` names `esc tab enter backspace space up down left right f1 shift+tab ctrl+<letter> alt+<letter> alt+left ctrl+left` (reject `ctrl-c`, `C-d`, Home, End, PageUp, PageDown, Delete, Insert); unsupported keys go as raw bytes via `pane.send_input`; no cursor position is reported; `pane read` takes 4 ms.
- Pane ids validated by the strict charset in `terminal_attach.py` on BOTH the control plane and the relay, and the relay serves only panes herdr on that host lists.
- Match surrounding code style and comment density. Tests are RED first; names below are the contract between tasks.

## Review Focus

- A host that goes offline mid-session ends the terminal with a plain message within 10 s, never a spinner.
- A busy pane (a build streaming output) does not flood the broker: frames coalesce to at most 10 per second and the buffer stays bounded.
- A frame is never sent twice for an unchanged screen; a repaint after reattach shows the current screen at once.
- Typing arrives in order across relay restarts or a dropped long-poll; a retried input with the same `n` is typed once.
- Home, End, PageUp, PageDown and Delete reach the agent; Ctrl-C and Esc reach the agent.
- A hostile pane (escape sequences in output) cannot make the relay run anything: output is only forwarded.

---

### Task 1: herdr socket client (host side)

**Files:**
- Create: `herdr_engineering/herdr_pane_client.py`
- Test: `tests/test_herdr_pane_client.py`

**Interfaces:**
- Produces: `class HerdrPaneClient(socket_path: str | None = None, *, timeout: float = 3.0)` with `list_panes() -> list[dict]` (pane_id, agent, agent_status, scroll.viewport_rows, tab and workspace ids), `layout(pane_id) -> {"width": int, "height": int, "rect": {...}}`, `read_visible(pane_id) -> str` (ANSI text of the visible screen), `read_recent(pane_id, lines: int) -> str`, `send_text(pane_id, text)`, `send_keys(pane_id, keys: list[str])`, `send_input(pane_id, data: bytes)`, `zoom(pane_id, on: bool)`, `resize_split(pane_id, direction, amount: float)`, `subscribe_output(pane_id) -> Iterator[dict]` yielding output-changed events (None when the socket does not push). Socket path default `~/.config/herdr/herdr.sock` (override env `HERDR_SOCKET_PATH`). Raises `HerdrError(code, message)` for error responses.
- The first step discovers the real wire format of herdr's socket API (protocol 22): `herdr api schema --json` on bender lists request/response/event shapes; record the discovered request envelope in a module docstring. If a method is not reachable over the socket, fall back to the `herdr` CLI via `subprocess` with an argv list (never a shell) for that method only, and say so in a comment.

- [ ] **Step 1: Discover and pin the wire format.** Read the schema (`ssh bender 'bash -lc "herdr api schema --json"'`), write a failing test against a fake unix-socket server that speaks the discovered envelope for `pane.list`, `pane.read`, `pane.send_text`, `pane.send_keys`, `pane.send_input` and an output-changed subscription.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the client (one connection per request unless the schema defines a persistent session; a dedicated connection for the subscription), with strict argument validation (pane id charset, key names limited to the known list, `send_input` up to 4 KB per call).
- [ ] **Step 4: Run to verify they pass**, then run it ONCE against the real herdr on bender in a throwaway workspace (create with `herdr workspace create --cwd /tmp --label shepherd-spike --no-focus`, read its pane, send `ctrl+c`, send raw `\x1b[H`, close the workspace) and record the outcome in the task report.
- [ ] **Step 5: Commit** `"Host relay: herdr socket client"`.

### Task 2: Screen mirror and input translation (host side)

**Files:**
- Create: `herdr_engineering/terminal_mirror.py`
- Test: `tests/test_terminal_mirror.py`

**Interfaces:**
- Consumes: `HerdrPaneClient` from Task 1.
- Produces: `class PaneMirror(client, pane_id, *, now=time.monotonic, min_interval=0.1)` with `frame() -> dict | None` returning `{"data": base64(str ANSI frame), "rows": int, "cols": int}` only when the screen changed (hash of the visible read) and at most every `min_interval`, a frame being `"\x1b[0m\x1b[H\x1b[2J" + visible text with lines joined by "\r\n"`; `wait_for_change(timeout) -> bool` using the event subscription when available else a 200 ms poll. `def translate_input(data: bytes) -> list[Action]` where `Action` is `("text", str)`, `("keys", [names])` or `("raw", bytes)`: printable UTF-8 runs become text; `\r` -> `enter`; `\x7f` -> `backspace`; `\t` -> `tab`; `\x1b` alone -> `esc`; `\x1b[A/B/C/D` and `\x1bOA..D` -> `up down right left`; `\x1b[Z` -> `shift+tab`; control bytes 0x01-0x1a (except 0x09, 0x0d) -> `ctrl+<letter>`; `\x1b` followed by a printable letter -> `alt+<letter>`; `\x1b[1;3D`/`\x1b[1;5D` -> `alt+left`/`ctrl+left`; Home, End, PageUp, PageDown, Delete and every other escape sequence -> `("raw", bytes)`. Bracketed-paste markers `\x1b[200~ ... \x1b[201~` pass the inside as text and the markers as raw.

- [ ] **Step 1: Failing tests** with a fake client: unchanged screen yields no frame; a change yields one frame with `reset`-style clear prefix; coalescing at `min_interval`; the translation table above row by row (including mixed runs like `ls\r`, UTF-8 text, a 100 KB paste split into text actions of at most 4 KB, an incomplete escape sequence left pending until the next call, and invalid UTF-8 sent raw).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `PaneMirror` and `translate_input` (stateful object `InputTranslator` if a pending partial sequence needs state; `translate_input` may be a thin wrapper).
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** `"Host relay: pane mirror and input translation"`.

### Task 3: Broker in the control plane

**Files:**
- Modify: `herdr_engineering/control_plane/rakazo_terminal.py` (replace pty ownership with a broker; delete `TerminalSession` and `attach_argv`), `api.py` (host endpoints below), `tests/control_plane/test_rakazo_terminal_*.py`
- Create: `tests/control_plane/test_terminal_broker_host_api.py`

**Interfaces:**
- Produces host endpoints (ingest API keys only, each EXPLICITLY listed in `control_plane.architecture.agent_hosts` for the host id it acts for, whatever `require_host_binding` says; agent, operator, viewer and unlisted keys get 403): `GET /api/v1/host/terminal/wait?host_id=<host>&timeout=25&after=<last command id>` returns `{"commands": [{"id", "kind": "open"|"input"|"resize"|"zoom"|"close", "terminal": id, "pane_id", "rows", "cols", "n", "data": base64, "zoom": bool}]}` (zoom is its own command; resize carries rows and cols only). Delivery is at-least-once: ids are `<epoch>-<counter>-<random>` (epoch random per control-plane process); commands stay queued until acknowledged by the next wait's `after` (a cursor from another epoch or beyond anything issued is ignored and acks nothing) and expire after 60 s (an expired input, open or resize ends its terminal with reason `stale`). Long-poll: returns at once when commands are queued, empty list on timeout, and `[]` when a newer wait for the same host replaced it (one live wait per host, 64 overall, 429 beyond). `POST /api/v1/host/terminal/<id>/output?host_id=<host>` (host_id may also be in the body) with `{"frames": [{"data", "rows", "cols"} | {"ended": true, "exit": int|null, "reason": str}]}` stores them in the numbered buffer (each data frame becomes a `{seq, data, reset: true}` chunk; the buffer keeps the last 200 KB, always at least the newest frame; at most 20 frames and 160 KB encoded each). `Broker.host_connected(host_id) -> bool` is true while a wait is blocked for the host or for 10 s after its last wait ended; a host silent for those 10 s loses its open terminals (`host_offline`). `ended` reasons the relay may see on the phone side: `host_offline`, `input_overflow` (the per-host command queue of 256 was full: all that terminal's queued inputs are dropped), `stale`. A takeover queues `close` for the old terminal id, then `open` for a new id (n restarts at 1).
- Relay contract (Task 4): dedupe by command id AND by `(terminal, n)`, refuse a gap in `n`, send `after=<last handled id>` on every wait, and run exactly one relay per host (two would dedupe separately).
- The five phone procedures keep their contracts and tests; `terminal/open` no longer spawns: it validates the agent (verified session record, host_id, pane id via `terminal_attach` charset checks), refuses with a plain 503 `HOST_NOT_CONNECTED` when `host_connected` is false, queues an `open` command and returns at once; `terminal/input` queues `input` commands in `n` order; `terminal/subscribe` streams buffer chunks as before; a host that stops polling for 10 s while terminals are open ends them with `{ended: true, reason: "host_offline"}`. Reuse of a live terminal, takeover, the 4-terminal cap, idle expiry and the audit rows stay as built.

- [ ] **Step 1: Failing tests** for the host endpoints (auth, long-poll wake-up, output stored with seq, buffer cap, a viewer or agent key refused) and the broker behaviour through the existing phone-procedure tests rewritten against a fake host that answers queued commands; host offline detection; stuck-input cannot block (no process, so only queue bounds: a command queue per host capped at 256; on overflow the oldest-input terminal loses all queued inputs and ends with `input_overflow`, never leaving a gap).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the broker, wire the endpoints in `api.py` next to the steering endpoints (same auth helper), delete the pty code and its tests (`test_rakazo_terminal_core.py`, the spawn fakes), keep the gate/limiter/open_owner work.
- [ ] **Step 4: Run to verify they pass** (contract, terminal, agents, bridge, steering suites).
- [ ] **Step 5: Commit** `"Terminal: broker with host relay endpoints, no server-side processes"`.

### Task 4: Relay service (host side)

**Files:**
- Create: `herdr_engineering/terminal_relay.py` (the loop), a `herdr-eng host terminal-relay` command in `herdr_engineering/cli.py`, `deploy/herdr-terminal-relay/herdr-terminal-relay.service` (systemd user unit) and a launchd plist for macOS
- Test: `tests/test_terminal_relay.py`

**Interfaces:**
- Consumes: Tasks 1 to 3. Produces: `class TerminalRelay(config, client, http)` with `run_forever()`: long-polls `GET /api/v1/host/terminal/wait?host_id=&timeout=&after=` (acking by command id, deduping by command id and `(terminal, n)`, refusing gaps in `n`), executes `open` (validate pane id, check the pane exists via `list_panes`, start a `PaneMirror` thread for it, push the first frame at once), `input` (idempotent by `n`, `translate_input` then `send_text`/`send_keys`/`send_input`), `resize`/`zoom` (zoom and split ratio only), `close`; pushes frames and `ended` (pane closed, herdr gone) with at most 10 frames per second; reconnects with backoff 0.5 s doubling to 8 s; reads credentials from the host's existing `host.json` like the other host agents do.

- [ ] **Step 1: Failing tests** with a fake control plane and fake client: open then frames; input order and `n` idempotence; a long-poll error reconnects without losing queued terminals; a pane that disappears yields `ended`; invalid pane ids refused; a closed terminal stops its mirror thread; hostile output is only forwarded.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the relay, the CLI command and the unit files (the unit runs as the same user that runs herdr, restarts on failure).
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** `"Host relay: terminal relay service and unit files"`.

### Task 5: App: frame rendering, fit and pane controls

**Files:**
- Modify: `apps/mobile/scripts/terminal-page.js` (and regenerate `lib/terminal-html.generated.ts`), `components/terminal-view.tsx`, `lib/terminal-bridge.ts`, `lib/terminal.ts` as needed; locales for new strings
- Test: matching `*.test.ts`/`.tsx`

**Interfaces:**
- Consumes: the open reply and frames (`rows`, `cols` now describe the pane, not the phone). Produces: the page renders the pane at its real `cols x rows` (xterm `resize(cols, rows)`), scales font to fit the width with horizontal pan and a pinch zoom (clamped), shows no cursor (cursor hidden), and the app no longer sends `terminal/resize` from the page `size` message (the pane owns its size); a Zoom pane toggle in the key row calls `terminal/resize {zoom: true|false}` (the broker forwards it as a `zoom` command).

- [ ] **Step 1: Failing tests** for the pure decisions (fit scale for pane size vs container, pinch clamp, zoom message, `size` message no longer resizing the pty).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** and regenerate the page.
- [ ] **Step 4: Run to verify they pass**; apps/mobile suite once.
- [ ] **Step 5: Commit** `"Terminal view: render pane frames at the pane's size"`.

### Task 6: Deploy and live check

**Files:** none created; deploy and verification.

- [ ] **Step 1:** merge the server PR after CI (needs the owner's go-ahead); deploy the control plane with `deploy/control-plane/deploy.sh --ref <sha> --no-fetch`; confirm health.
- [ ] **Step 2:** ship the relay with `deploy/deploy-fleet-agents.sh --hosts bender` first; check the unit is active and the host shows as connected.
- [ ] **Step 3:** with one probe token, `terminal/open` a harmless idle agent pane on bender, read frames, send `echo ok`, `ctrl+c`, Home/End, confirm the audit rows.
- [ ] **Step 4:** roll the relay to beast and the Mac; repeat the probe on each.
- [ ] **Step 5:** build the app for the iPad (A16) and the iPhone simulators and check, by `simctl` screenshots and the owner's own taps, the terminal, keys, zoom and reattach; report results and open questions.
