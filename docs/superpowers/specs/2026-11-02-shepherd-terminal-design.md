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

The owner uses this app **instead of Moshi** (an SSH/mosh client) and used Moshi only to manage agents, so the in-app terminal stays. The review of the first build showed that a pty started *by the control plane* cannot work as deployed (the web service runs in a container with no `herdr`, no ssh client and no keys) and would need a key that opens a shell on every fleet host. The owner asked why the assistants and agents that already run on each host cannot do this. They can, and it is the better design. This section replaces "How it works" above; everything else (goals, non-goals, the phone and iPad experience, the app) is unchanged.

### How it works now
- **Host relay.** A small service on each fleet host (shipped by `deploy/deploy-fleet-agents.sh` next to the existing `herdr-cp-sessions` plugin and started by systemd, using the host's own `host.json` credentials) runs `herdr --session <s> agent attach <pane>` **locally** in a pty (the `TerminalSession` core already built, run on the host). No ssh, no keys, nothing inbound.
- **Outbound only.** The relay keeps a long-poll open to the control plane (`GET /api/v1/host/terminal/wait`) to receive `open | input | resize | close` commands for sessions on its host, and pushes output chunks with `POST /api/v1/host/terminal/<id>/output`. It uses its existing key, like steering delivery does.
- **Control plane = broker.** It stores a numbered 200 KB buffer per terminal and a command queue per host, and never starts a process. The phone-facing procedures (`terminal/open|input|resize|close|subscribe`) and the app do not change. Replay, takeover, input numbering, the 4-session cap and the 5-minute keep stay as built, now enforced in the broker.
- **Host offline.** The broker knows when a host last polled; `terminal/open` for a host that is not connected answers at once with a plain "host not connected" message and the chat view keeps working.
- **Validation twice.** The control plane and the relay both check the pane id against the same strict charset; the relay attaches only panes that herdr on that host already lists.

### What it fixes
No credentials in the web service or on the phone; no container changes (no ssh client, no herdr, no host-name or DNS workarounds); the Mac and every other host work the same way once the relay is installed; the web service can no longer become a shell on the fleet.

### What it costs
A new host-side component to ship and keep running on bender, beast and the Mac. The server's terminal manager becomes a broker (the pty and fd-hardening work moves to the host side; the owner-only gate, the rate-limit exemption for input/resize and the narrower `open_owner` stay as already built). A host that is offline or not yet updated cannot open terminals.

### Mosh-style roaming
Moshi's selling point is a session that survives network changes. Here the pty lives on the host and the app reattaches from the saved buffer, so a drop, a sleep or a network switch shows the live screen again without losing the agent.
