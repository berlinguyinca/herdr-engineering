# Terminal button: attach your terminal to an agent

The operator browses the dashboard from any machine in the fabric, and an agent's HerdR
server may run on another host (for example beast). A browser cannot open a terminal on the
machine you are browsing from: the dashboard has no in-browser terminal and there is no
URL-scheme handler. The server must also never spawn a terminal itself. So the **Terminal**
button on an agent page only shows the exact command, and you run it in a terminal on your
own machine.

## What the button shows

`POST /api/v1/sessions/<id>/terminal` builds the command from the session record the feed
reported (`host_id`, `herdr_session`, `pane_id`, `terminal_id`). The request body is ignored:
no client-supplied string ever reaches the command. Two forms are always returned:

| Where you are       | Command                                                                  |
|---------------------|--------------------------------------------------------------------------|
| Any other machine   | `ssh -t beast "bash -lc 'herdr --session default agent attach wK:pH'"`   |
| On the agent's host | `herdr --session default agent attach wK:pH`                             |

The server marks one form as suggested when the request's peer address equals a host's
recorded tailnet IP. When it cannot tell (loopback, a proxy, no tailnet IP), it suggests the
ssh form, which works from anywhere, the agent's host included.

Why these forms (herdr 0.9.1):

- `herdr --machine` never forwards interactive attachment and must not be combined with
  `--session`, so the remote form is plain `ssh -t`. The ssh target is the session's
  `host_id`, so it must resolve as an ssh name (e.g. a `~/.ssh/config` alias or tailnet name).
- `agent attach` takes a pane id or agent name. A `term_...` terminal id is not accepted, so
  the terminal id is identity evidence only.
- A non-interactive ssh shell does not have `~/.local/bin` (where herdr is installed) on
  PATH, hence `bash -lc`.

The session record's `attach` field (written by the feed plugin,
`deploy/herdr-cp-feed-plugin/enrich.js`) holds the same ssh form, checked against the same
charsets; it is null when a value fails them. It used to hold `ssh <host> && herdr workspace
focus <ws>`, whose second half ran on the viewer's machine. Records written before the fix keep
that value until the plugin re-enriches the session; the Terminal route above never reads it.

## Refusals

- `404`: unknown session.
- `409 not_live`: the session is stopped or closed (herdr's `idle`, `done`, `working`, `blocked` and `unknown` all count as live), or lacks a host, server, pane or terminal
  identity.
- `409 unsafe_identity`: a recorded field fails its strict charset (host
  `[A-Za-z0-9][A-Za-z0-9.-]{0,62}`, server `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, pane
  `[A-Za-z0-9][A-Za-z0-9:_-]{0,63}`, terminal `term_[A-Za-z0-9_-]{1,128}`). The value is not
  echoed back.
- `401`/`403`: operator only (a viewer key is refused). Cross-origin requests get `403` and
  requests that are not `application/json` get `415`, the same guard as summary fix.
- The public collaboration listener and the collab operator listener do not serve this route
  (`404`).

Liveness is the recorded status, not a live probe. A pane that was moved gets a new pane id,
so after a move the command targets the old id until the feed reports again; refresh the
agent list first.
