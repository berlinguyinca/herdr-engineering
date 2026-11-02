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
