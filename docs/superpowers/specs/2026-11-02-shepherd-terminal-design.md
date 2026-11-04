# Shepherd Terminal (piece 2) — design

Piece 2 of five (see `2026-11-01-shepherd-agents-tab-design.md`). Read side exists: the Agents tab lists agents and shows each one's conversation. This piece adds the ability to **type to an agent and see its real terminal**, with a full terminal feel, on phone and iPad.

## Intent

The owner works in the herdr terminal. Away from the desk they want the same thing: open an agent, see its pane exactly as `herdr agent attach` shows it, type, press Ctrl-C, answer a prompt. Not a chat approximation of a terminal.

## Who and how safe

One person: the owner. Tailnet only, HTTPS, locked phone, passwordless owner session (`open_owner`). The owner has ruled this is enough for the terminal too. No extra gates. Every attach, and every input session's start and end, is written to the audit log (`rakazo.bridge.terminal.attach` / `.detach`). Input bytes are not logged.

## Goals

- A real terminal view: full-colour ANSI, cursor, alt-screen apps (vim, htop, the agent TUIs), scrollback, correct width/height for the device, resize on rotate and on iPad window changes.
- Typing that feels native: soft keyboard on phone, hardware keyboard on iPad with Ctrl, Esc, arrows, Tab, Option-as-Meta, Cmd-C copy and Cmd-V paste, and a compact **key row** above the soft keyboard (Esc, Ctrl, Tab, arrows, `|`, `~`, `/`, `-`) that costs one line of height, not a panel.
- Opens in the agent screen as a second view next to Chat. Default is smart: Terminal when the agent is blocked on a prompt, Chat otherwise, remembered per agent.
- iPad: terminal fills the right pane; list stays on the left. Keyboard shortcuts work without focusing a button.
- Survives a dropped connection: the session stays on the host and the app reattaches, showing the screen as it is now.

## Non-goals

- Not a general shell to the host. The only thing a phone can attach to is an agent pane that herdr already knows about.
- No file transfer, no port forwarding, no multiple simultaneous owners.
- No recording or sharing of terminal sessions.

## How it works (my call; the owner has not seen this)

`terminal_attach.py` states today that the server never spawns a terminal; it only builds the command for the viewer to run. A phone cannot run it, so this piece deliberately **changes that rule for the owner-only bridge**: the control plane starts the attach on the owner's behalf and relays bytes.

- **Host side.** The control plane (on bender) runs `herdr --session <s> agent attach <pane>` locally, or `ssh -t <host> "bash -lc 'herdr --session <s> agent attach <pane>'"` for another host, in a pseudo-terminal, reusing the exact command builder and charset checks in `terminal_attach.py`. Nothing else can be run: the pane id must come from a verified session record.
- **Relay.** A WebSocket `GET /terminal/<id>` on the bridge, owner session only, origin `rakazo://` or the app. Binary frames carry the terminal's output; text frames carry `{type: "input", data}` and `{type: "resize", cols, rows}`. One owner client per pane at a time; a second connection takes over and the first is told.
- **Session persistence.** The pty is kept for 5 minutes after the last client leaves, with a 200 KB screen buffer so a reattach repaints immediately. After that it is closed.
- **App.** A terminal emulator component (xterm.js in a WebView, which is the same engine as the web terminal and gives alt-screen, colours and selection correctly) fed by the WebSocket. The key row and hardware keys are native and write straight to the socket.

## Decisions for the builder

- Terminal engine inside the WebView: xterm.js, bundled with the app (no CDN, the phone is on a tailnet and offline use must work for the shell, not the content).
- Keep-alive, reconnection backoff and the 200 KB buffer size are the builder's call.
- Colours follow the app's light and dark themes.

## Risks to check first (a spike before the plan)

1. **Can bender reach every host?** The relay needs bender to run `ssh -t <host>` without a password for each fabric host, including mac.lan and beast. If it cannot today, the fix is key setup, not code. This is the first thing to verify.
2. **herdr `agent attach` over a non-tty relay.** It needs a real pty and honours resize signals. A short spike must prove that a pty-backed process on bender renders correctly through a WebSocket into xterm.js, including resize.
3. **WebSocket through the existing HTTPS proxy** (`local.metabolomics.us`). Confirm the proxy forwards upgrades and long-lived connections.

## Out of scope for this piece

Assistant tab (3), rooms (4), iPad sidebar redesign beyond the Terminal pane (5).

## Review focus

- Closing the app mid-session, then reopening after a minute, shows the live screen.
- Pasting a large block (100 KB) does not freeze the app or the host.
- Rotating the phone or resizing the iPad window resizes the pty and the view agrees.
- An agent that disappears while attached shows "session ended" and returns to the list, not a spinner.
- Ctrl-C and Esc reach the agent (the two keys people most need on a phone).

## Amendment 2026-11-02 (owner direction): the terminal replaces Moshi, and runs on the host

The owner uses this app **instead of Moshi** (an SSH/mosh client) and used Moshi only to manage agents, so the in-app terminal stays. The review of the first build showed that a pty started *by the control plane* cannot work as deployed (the web service runs in a container with no `herdr`, no ssh client and no keys) and would need a key that opens a shell on every fleet host. The owner asked why the assistants and agents that already run on each host cannot do this. They can, and it is the better design. **Superseded in part by Amendment 2: the pty mechanism below (a relay running `herdr agent attach` in a pty, the `TerminalSession` core, fd hardening) was never shipped; the relay mirrors the pane instead.** This section replaces "How it works" above; everything else (goals, non-goals, the phone and iPad experience, the app) is unchanged.

### How it works now
- **Host relay.** A small service on each fleet host (shipped as `deploy/herdr-terminal-relay/` (a systemd user unit and a launchd plist, installed per its README; it is not part of `deploy/deploy-fleet-agents.sh`), using the host's own `host.json` credentials) (Amendment 1 had it run `herdr --session <s> agent attach <pane>` locally in a pty; Amendment 2 replaced that.) No ssh, no keys, nothing inbound.
- **Outbound only.** The relay keeps a long-poll open to the control plane (`GET /api/v1/host/terminal/wait`) to receive `open | input | resize | close` commands for sessions on its host, and pushes output chunks with `POST /api/v1/host/terminal/<id>/output`. It uses its existing key, like steering delivery does.
- **Control plane = broker.** It stores a numbered 200 KB buffer per terminal and a command queue per host, and never starts a process. The phone-facing procedures (`terminal/open|input|resize|close|subscribe`) and the app do not change. Replay, takeover, input numbering, the 4-session cap and the 5-minute keep stay as built, now enforced in the broker.
- **Host offline.** The broker knows when a host last polled; `terminal/open` for a host that is not connected answers at once with a plain "host not connected" message and the chat view keeps working.
- **Validation twice.** The control plane and the relay both check the pane id against the same strict charset; the relay attaches only panes that herdr on that host already lists.

### What it fixes
No credentials in the web service or on the phone; no container changes (no ssh client, no herdr, no host-name or DNS workarounds); the Mac and every other host work the same way once the relay is installed. The control plane still has power over the fleet, and this must be said plainly: anyone who controls it (a compromised web service) or holds the owner's token can open agent panes and type into them, which amounts to code execution as the herdr user on that host. What the design removes is a standing key that opens a shell on every host, not that reach.

### What it costs
A new host-side component to ship and keep running on bender, beast and the Mac. The server's terminal manager becomes a broker (the pty and fd-hardening work was dropped by Amendment 2; the owner-only gate, the rate-limit exemption for input/resize and the narrower `open_owner` stay as already built). A host that is offline or not yet updated cannot open terminals.

### Mosh-style roaming
Moshi's selling point is a session that survives network changes. Here the pty lives on the host and the app reattaches from the saved buffer, so a drop, a sleep or a network switch shows the live screen again without losing the agent.

## Amendment 2, same day: mirror and drive the herdr pane, do not attach a pty

The owner pointed out that nearly all agents already run in herdr tabs and panes, so herdr already owns their terminals. Checked on bender (herdr 0.9.1, read-only commands): herdr has a socket API with `pane.read`, `pane.send_input`, `pane.send_keys`, `pane.send_text`, `agent.prompt`, `pane.scroll`, `pane.get` and events (`pane_output_changed`, `pane.updated`, `pane.agent_status_changed`), plus `herdr machine` / `--machine` for reaching other hosts through herdr's own saved ssh machines. `herdr agent attach` is an exclusive client (it has `--takeover`), so a phone attaching could kick the desktop session off the pane or resize it.

This amendment replaces the pty in Amendment 1. The host relay does **not** start `herdr agent attach`:
- **Screen out:** the relay subscribes to `pane_output_changed` for the open pane (herdr emits no per-redraw event, only `pane.output_matched`, so in practice the mirror polls every 100 ms), reads `pane read --source visible --format ansi` (measured on bender: 4 ms, about 8 KB for a 97-row pane) and sends a frame only when it changed. The app clears and redraws the xterm screen with each frame. Scrollback (`--source recent --lines N`) is not implemented: the mirror shows the visible screen only.
- **Keys in:** printable text goes through `pane send_text`, special keys and Ctrl combinations through `pane send_keys`, and raw bytes through `pane send_input` (confirmed: the socket API takes bytes; `HerdrPaneClient.send_input`).
- **No second client:** nothing attaches, so the desktop session is never kicked, resized or disturbed, and there is no pty, fd handling or process lifecycle to harden.
- **Everything else in Amendment 1 holds:** outbound-only host relay with the host's own credentials, control plane as a broker, the phone-facing procedures and the app unchanged, the owner-only gate, the input/resize rate-limit exemption and the narrowed `open_owner`.

### Limits to be honest about
- It is a screen mirror, not a raw byte stream: mouse reporting and graphics are out; a full-screen program shows as its current frame.
- The pane keeps its own size (the sample panes are 46 to 97 rows). The phone cannot resize it without affecting the desktop, so the app shows the pane at its real size, fits it to the screen width and scrolls, with a pinch/zoom control.
- Settled by testing (see below): herdr reports no cursor position (the app draws none), the `send_keys` vocabulary is as listed, `send_input` takes raw bytes, and there is no per-redraw output event, so the mirror polls every 100 ms and coalesces frames.
- A host with an older herdr, or that is offline, cannot open terminals.

### Confirmed on bender (throwaway workspace, since closed)
- `pane read --source visible --format ansi` returns the screen with colours; `pane run` sends a line plus Enter; `pane send-text` sends literal text.
- `pane send-keys` accepts `esc tab enter backspace space up down left right f1`, `shift+tab`, `ctrl+<letter>` (also `C-c`; `ctrl+c`, `ctrl+z`, `ctrl+a`, `ctrl+l`, `ctrl+e`, `ctrl+u`, `ctrl+r`, `ctrl+]`, `ctrl+space`), `alt+<letter>`, `alt+left`, `ctrl+left`. It rejects `ctrl-c` (hyphen), `C-d`, `C-z`, `M-b`, `S-Tab`, and Home, End, PageUp, PageDown, Delete, Insert under every name tried.
- So: letters and Ctrl combinations go through `send-keys` with the `ctrl+x` form; printable text through `send-text`; Home, End, PageUp, PageDown, Delete and anything else unsupported go as raw bytes through the socket API `pane.send_input` (confirmed by the pane client).
- No cursor position appears in any read format: the app draws no cursor unless the socket API reports one.
- `pane layout` reports each pane's rect and the workspace area (the area comes from the open herdr window; with none it is 120x40). `pane resize` changes a split ratio (worked); it cannot set an exact column count. The owner allows the phone to resize panes: the app offers zoom and split-ratio changes, and otherwise shows the pane at its real size, fitted to the screen width.
- Frames are sent as full-screen replacements marked `reset: true`, which the built app client already treats as "clear, then draw", so the app's data path does not change.

## Deployment requirements (host relay)

Nothing to configure on the control plane. Both host terminal endpoints (`GET /api/v1/host/terminal/wait`,
`POST /api/v1/host/terminal/<id>/output`) authenticate with the per-host `X-Host-Token` from the fleet file, the token that
`herdr-eng fleet join` writes into the host's `host.json` and that the fleet routes already use, so they work in open mode
(`--allow-open`) as deployed. The host id comes from the token: a `host_id` that names another host is refused with 403, a
token can only poll and push for its own host, and every API key (any role) and the open listener without a token are
refused. Per host: install the relay unit (`deploy/herdr-terminal-relay/README.md`) as the user that runs herdr.
