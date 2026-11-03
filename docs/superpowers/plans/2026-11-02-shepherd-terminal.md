# Shepherd Terminal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the Shepherd app, open an agent's real terminal, see it with full colour and cursor, and type to it with soft keys, a key row and a hardware keyboard.

**Architecture:** The control plane (bridge) starts `herdr agent attach` in a pseudo-terminal and keeps a numbered ring buffer of output chunks. The app reads output over the existing server-sent-event stream (cursor = chunk number, so reconnects resume exactly) and sends keystrokes and resizes as small POSTs. The app draws it with xterm.js in a WebView. No WebSocket: the control plane's HTTP server has none, and SSE plus POST already works through it.

**Tech Stack:** Python 3.12 stdlib `pty`, existing bridge (`rakazo_bridge.py`, `rakazo_events.event_stream`), Expo/React Native, `react-native-webview`, xterm.js (bundled), vitest, pytest, biome.

**Spec:** `docs/superpowers/specs/2026-11-02-shepherd-terminal-design.md` (owner-approved). Agents design: `docs/superpowers/specs/2026-11-01-shepherd-agents-tab-design.md`.

## Global Constraints

- Owner-only, tailnet, passwordless owner session: no extra gate. Audit `rakazo.bridge.terminal.attach` and `.detach` per session; never log input bytes or output.
- Only an agent pane herdr already records can be attached. Reuse `terminal_attach.attach_commands` (its charset checks and `AttachRefused`) for the command; never build a command from request text.
- Procedures are `POST /rpc/<proc>` in `self._procs`, camelCase outer keys, snake_case only inside agent rows. New procs are fork-only: add them to `FORK_ONLY` and `STREAMED` in `tests/control_plane/test_rakazo_contract.py`.
- Stream rule: call `terminal/open` on app start and after any error or `taken_over`; it returns a NEW `terminalId` (fresh `n` counter) on the live pty and `seq: 0`, which replays the whole kept buffer, so the app clears its screen and replays. After the 600 s clean stream rollover resubscribe from the last seq for the same `terminalId` WITHOUT calling open.
- One owner client per pane: a new `terminal/open` takes over and the previous client's stream ends with a `taken_over` frame.
- Limits: 4 live sessions, 64 KB per input call, 200 KB screen buffer, 5 minutes kept after the last stream closes, stream poll 0.03 s.
- App: match surrounding code (naming, comment density, idiom); no new dependencies beyond `react-native-webview` and `xterm` + `@xterm/addon-fit`; every user-visible string in all seven locale catalogs; no CDN (xterm is bundled).
- Tests are RED first. Names below are the contract between tasks.

## Review Focus

- Closing the app mid-session and reopening after a minute shows the live screen (reattach with buffer).
- A 100 KB paste neither freezes the app nor loses ordering; input arrives in order.
- Rotating the phone or resizing the iPad window resizes the pty and the view agrees on cols and rows.
- An agent that disappears while attached ends with `session ended`, not a spinner.
- Ctrl-C and Esc reach the agent.
- A host the server cannot reach shows a plain message and the chat view still works.

---

### Task 1: Pty relay core (server)

**Files:**
- Create: `herdr_engineering/control_plane/rakazo_terminal.py`
- Test: `tests/control_plane/test_rakazo_terminal_core.py`

**Interfaces:**
- Produces: `class TerminalSession(argv: list[str], rows: int, cols: int, *, buffer_bytes: int = 200_000)` with `start()`, `write(data: bytes)`, `resize(rows, cols)`, `chunks_after(seq: int) -> list[dict]` (each `{"seq": int, "data": str(base64)}`; a final `{"seq": n, "ended": true, "exit": int|None}`), `snapshot_seq() -> int`, `alive: bool`, `close()`. Output is read by one daemon thread; the buffer drops the oldest chunks beyond `buffer_bytes` and `chunks_after` of a dropped cursor starts with a `{"seq": first, "reset": true, ...}` marker so the app clears and repaints.
- Produces: `def attach_argv(session: dict, *, local_host: str, hosts=()) -> list[str]` returning `["herdr","--session",s,"agent","attach",pane]` when `session["host_id"] == local_host`, else `["ssh","-tt","-o","BatchMode=yes","-o","ConnectTimeout=8",host,"bash -lc '<inner>'"]`; raises `AttachRefused` exactly as `attach_commands` does.

- [ ] **Step 1: Write failing tests.** Real pty, fast children (`bash -c`, `cat`): output arrives as base64 chunks with increasing `seq`; `write(b"hi\n")` to `cat` echoes; `resize(40, 120)` is seen by `stty size`; `\x03` interrupts a `sleep`; buffer over the cap drops oldest and a stale cursor gets the `reset` marker; child exit yields the `ended` chunk with the exit code; `attach_argv` local and remote forms, and refusal for a closed or unsafe session (reuse the cases in `tests/control_plane/test_terminal_attach.py`).
- [ ] **Step 2: Run to verify they fail** (`pytest tests/control_plane/test_rakazo_terminal_core.py -q`).
- [ ] **Step 3: Implement** `TerminalSession` with `pty.fork` and `TIOCSWINSZ`, the reader thread, a lock-guarded deque, and `attach_argv` calling `attach_commands` for validation then building argv from the validated fields (never `shell=True`).
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** `git add herdr_engineering/control_plane/rakazo_terminal.py tests/control_plane/test_rakazo_terminal_core.py && git commit -m "Terminal: pty relay core with numbered output buffer"`.

### Task 2: Terminal procedures and stream (server)

**Files:**
- Modify: `herdr_engineering/control_plane/rakazo_terminal.py`, `rakazo_bridge.py` (register like `self.agents`), `api.py` (set `.repo` as agents does), `tests/control_plane/test_rakazo_contract.py` (`FORK_ONLY`, `STREAMED`, exact-list test)
- Test: `tests/control_plane/test_rakazo_terminal_rpc.py`, `test_rakazo_terminal_stream.py`

**Interfaces:**
- Consumes: Task 1; `Agents._sid`, `_missing`, `_stream` pattern; `bridge._audit`; `repo.get_session`.
- Produces: `class Terminal` with `procs()` returning `terminal/open {id, rows, cols} -> {terminalId, seq, rows, cols}`, `terminal/input {terminalId, data: str(base64), n: int}` (`n` is a per-terminal sequence number; out-of-order or repeated `n` is ignored so a retried POST never double-types), `terminal/resize {terminalId, rows, cols}`, `terminal/close {terminalId}`, `terminal/subscribe {terminalId, cursor}` (SSE, frames `{seq, data}` / `{seq, ended}` / `{seq, reset}` / `{seq, taken_over}`, 0.03 s poll). Constructor `Terminal(bridge, error, *, spawn=TerminalSession, local_host=None)`; `local_host` defaults to the machine's hostname lowercased and is compared case-insensitively.
- Error mapping: unknown or closed agent 404 `NOT_FOUND`; `AttachRefused` 409 with its `reason`; session cap 429; unreachable host (child exits within 3 s with ssh code 255) surfaces as an `ended` chunk plus `terminal/open` reporting nothing special: the app shows the exit text.

- [ ] **Step 1: Write failing tests** with a fake `spawn` for speed and one real-pty smoke test: open then subscribe from `seq` replays the buffer; input with `n` repeated is typed once; a second `open` for the same agent ends the first stream with `taken_over`; viewer-role tokens are refused; the 5th session is 429; 64 KB + 1 input is 400; audit entries written for attach and detach and contain no data; idle expiry after `ttl` via an injected clock; `agents/list` is unaffected.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `Terminal`; share the stream mechanics by reusing `Agents._stream` (extract a small shared helper only if it is two lines to do so); add the three proc names to `FORK_ONLY` and `terminal/subscribe` to `STREAMED`.
- [ ] **Step 4: Run to verify they pass**, then the whole `tests/control_plane` directory.
- [ ] **Step 5: Commit** `"Terminal: open, input, resize, close and subscribe procedures"`.

### Task 3: App client and contract

**Files:**
- Create: `apps/mobile/lib/terminal.ts`, `packages/contracts/src/terminal.ts`
- Modify: `packages/contracts/src/rpc.ts`, `apps/mobile/lib/api.ts` (reuse `streamRpc`)
- Test: `apps/mobile/lib/terminal.test.ts`

**Interfaces:**
- Consumes: Task 2 procedures; `streamRpc`/`readRpcStream` in `lib/api.ts`.
- Produces: `openTerminal(agentId, rows, cols)`, `useTerminal(agentId)` returning `{state: "connecting"|"live"|"ended"|"taken_over"|"error", write(bytes: Uint8Array), resize(rows, cols), onOutput(cb)}`. `write` batches within 8 ms, numbers calls with `n`, keeps order, retries a failed POST with the same `n`. Reconnect: after an error or `taken_over`, call `terminal/open` again (new `terminalId`, `n` restarts at 1), clear the screen and replay from the returned `seq`; after a clean stream end resubscribe from the last seq without open; backoff 0.5 s doubling to 8 s.

- [ ] **Step 1: Failing tests** (fake `fetch`/stream): ordered `n`; retry reuses `n`; 100 KB write is split into 32 KB calls in order; resize is debounced 150 ms to the last value; a `reset` frame clears via `onOutput`; an error reconnect re-opens (new id, n restarts at 1) and replays from the returned `seq`; a clean rollover resubscribes from the last seq without open; `ended` stops reconnecting.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the zod schemas, rpc entries and the hook.
- [ ] **Step 4: Run to verify they pass** (vitest, biome, tsc for the touched files).
- [ ] **Step 5: Commit** `"Terminal: app client with ordered input and resumable output"`.

### Task 4: Terminal view (xterm in a WebView)

**Files:**
- Create: `apps/mobile/components/terminal-view.tsx`, `apps/mobile/assets/terminal/terminal.html` (xterm + fit addon inlined by a small build script `scripts/build-terminal-html.mjs`), `apps/mobile/lib/terminal-bridge.ts`
- Modify: `apps/mobile/package.json` (`react-native-webview`, `xterm`, `@xterm/addon-fit`)
- Test: `apps/mobile/lib/terminal-bridge.test.ts`

**Interfaces:**
- Produces: `<TerminalView agentId focused />`; `terminal-bridge.ts` exports `encodeToWeb(msg)` / `decodeFromWeb(raw)` for the message protocol: native→web `{t:"out", b64}`, `{t:"reset"}`, `{t:"theme", dark}`; web→native `{t:"in", b64}`, `{t:"size", rows, cols}`, `{t:"ready"}`.
- Colours follow the app's light and dark themes; font is the system monospaced font at the user's text size; selection and copy work.

- [ ] **Step 1: Failing tests** for the bridge codec (round trip, malformed input ignored, base64 of arbitrary bytes, 1 MB message).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the codec, the HTML build script (output committed under `assets/terminal/`), and `TerminalView` wiring `useTerminal` to the WebView.
- [ ] **Step 4: Run tests, then check on the simulator** with `simctl` screenshots only (no UI automation): a blocked agent renders colours and the cursor.
- [ ] **Step 5: Commit** `"Terminal: xterm view in a WebView"`.

### Task 5: Keys, paste and copy

**Files:**
- Create: `apps/mobile/components/terminal-key-row.tsx`, `apps/mobile/lib/terminal-keys.ts`
- Modify: `apps/mobile/components/terminal-view.tsx`
- Test: `apps/mobile/lib/terminal-keys.test.ts`

**Interfaces:**
- Produces: `keyBytes(key: KeyName, mods: {ctrl: boolean, alt: boolean, shift: boolean}) -> Uint8Array` for Esc, Tab, arrows, Home, End, PgUp, PgDn, Enter, Backspace, `|`, `~`, `/`, `-`, letters with Ctrl (`ctrl+c` → `0x03`), Alt as a leading Esc; `KEY_ROW: KeyName[]`. The key row is one line high; Ctrl is a sticky toggle that applies to the next key. Hardware keys (iPad) go through the same function and bypass the soft keyboard; Cmd-C copies the selection, Cmd-V pastes bracketed when the app asked for bracketed paste.

- [ ] **Step 1: Failing tests** for every table row above, sticky Ctrl, Alt-prefix, application-cursor mode arrows (`ESC O A`) when the terminal asks for it.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** `terminal-keys.ts`, the key row, and hardware key handling in the view.
- [ ] **Step 4: Run tests and check by screenshot** that the key row costs one line.
- [ ] **Step 5: Commit** `"Terminal: key row, hardware keys, paste and copy"`.

### Task 6: Agent screen switch and iPad/phone sizing

**Files:**
- Create: `apps/mobile/lib/agent-view-mode.ts`
- Modify: `apps/mobile/app/agent.tsx`, `apps/mobile/components/agent-row.tsx` (none expected), seven locale catalogs
- Test: `apps/mobile/lib/agent-view-mode.test.ts`

**Interfaces:**
- Produces: `defaultViewMode(status): "terminal"|"chat"` (blocked → terminal, otherwise chat), `useViewMode(agentId)` remembering the choice per agent in secure storage; a Chat/Terminal segmented control in the agent header. On iPad (split) the terminal fills the right pane; the soft keyboard and key row pin to the bottom without hiding the cursor line; rotate or pane resize recomputes rows and cols and sends `terminal/resize`.

- [ ] **Step 1: Failing tests** for `defaultViewMode` and the remembered override.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement**, with the new strings in all seven catalogs.
- [ ] **Step 4: Run the full mobile suite, biome and tsc.**
- [ ] **Step 5: Commit** `"Agents: Chat and Terminal views with a smart default"`.

### Task 7: Live check and deploy

**Files:** none created; deploy and verification only.

- [ ] **Step 1:** merge the server PR after CI, deploy with `deploy/control-plane/deploy.sh --ref <sha> --no-fetch` from `~/herdr-engineering-deploy` on bender; confirm health and zero ingest failures for 10 minutes.
- [ ] **Step 2:** with one probe token (the open-session limit is 30 s per IP), `terminal/open` an agent on bender and on beast; read the stream, send `echo ok`, confirm the echo and the audit rows; a Mac-hosted agent must give the plain unreachable message until the Mac's address is set up.
- [ ] **Step 3:** on the iPad (A16) and the iPhone simulators, by `simctl` screenshots, confirm the terminal, key row, rotation resize, and reattach after a minute.
- [ ] **Step 4:** report results and open questions; do not merge the app PR without the owner's word.
