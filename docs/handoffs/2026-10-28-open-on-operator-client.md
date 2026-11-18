# New interactive task: open artifacts on the operator's actual client

## User request

Create a HerdR Engineering skill, working interactively with the user, that works
in Claude Code, Codex, Pi and OpenCode. When the user asks to open an image,
Markdown file or website, it should open on the machine the person is actually
using, even when the agent executes on another host in the same fabric.

This session is separate from benchmark frontend implementation. Do not modify
InferWeave or take over its UI work. Start with repository/fabric discovery and
a concise proposed workflow; collaborate with the user on real UX choices.

## Concrete failure and successful recovery

The agent executes on bender (Linux), but the operator is on a Mac. Calling
`xdg-open` on bender reported success without opening anything on the user's Mac.
The operator corrected that files must first be copied to the Mac and opened there.

Successful one-off recovery:

- Tailscale listed one online macOS peer, `macbook-pro.tail0c50da.ts.net`.
- SSH to that host confirmed Darwin and the logged-in user's home.
- Two requested PNGs were copied to the user's `Downloads/InferWeave-mockups` folder.
- `open -a Preview` was invoked remotely for those two local-on-Mac files.
- The user confirmed both mockups looked good.

This is a reproduction, not a universal host mapping. Discover the actual attached
client from authenticated fabric/session context. Never assume the executor, the
first Mac peer, or a hard-coded hostname is the correct person/device.

## Scope and desired outcomes

1. Reuse existing native HerdR client/session identity, artifact transfer and
   service URL routing capabilities. Inspect the current implementation first.
   Related local checkouts include `herdr-url-fabric` and `herdr-handoff`.
2. Design a discoverable cross-harness skill plus deterministic helper/CLI where
   justified. One maintained workflow should serve all four harnesses without
   overriding existing commands. Consider the existing `/herdr::...` namespace.
3. Resolve source executor versus destination operator client. Prefer an explicit
   target or verified attached-client identity; ask a concise target question if
   multiple active clients make the intended device ambiguous.
4. Copy/materialize files only as needed, verify transfer integrity, preserve
   meaningful filenames and handle spaces/Unicode/metacharacters without shell
   injection. Use private fabric transport and an owned bounded cache/destination.
5. Dispatch to the actual desktop user/session and default viewer/editor/browser.
   macOS `open`, Linux desktop opening and Windows behavior should be capability-
   based; don't equate an SSH server's OS with a usable graphical session.
6. Markdown should open in a suitable existing default app or an agreed rendered
   preview. Discuss preferred behavior with the user rather than silently
   installing applications. Images and websites use existing client capabilities.
7. Executor-local web URLs need fabric routing/private forwarding before opening
   on the client. The client's localhost is not the executor's localhost. Avoid
   new public hosting, public tunnels, or new exposure without explicit authority.
8. Respect source permissions, principal/device ownership and existing authorization.
   No credential copying, no unrelated file transfers or raw transcript sharing.
9. Report copy/open outcomes separately. A command's zero exit code is not proof
   that the intended human device opened it. Return meaningful target/path/app
   metadata without leaking secrets, and handle offline/locked/no-GUI clients.
10. Add behavioral tests for wrong-executor avoidance, ambiguity, private URL
    routing, filename safety, transfer/open failures and all harness entry points.
    Forward-test the skill independently if its complexity warrants that.

## Workspace and instructions

- Worktree: `/home/wohlgemuth/IdeaProjects/herdr-engineering-fabric-open`
- Branch: `feat/fabric-open-client`, initially based on local main `06e3d50`.
- Canonical `/home/wohlgemuth/IdeaProjects/herdr-engineering` is heavily dirty with
  other sessions' changes. Preserve those; do not copy or reset that checkout.
- Read applicable repository instructions and the installed `skill-creator`,
  `herdr`, `herdr-handoff`, and relevant skill-authoring guidance.
- Skills must work in Claude/Codex/Pi/OpenCode; do not solve only Codex's skill
  discovery path. Reuse the repo's actual installation/distribution mechanisms.
- No particular model was requested. Use the session's existing defaults.

## Session launch / handoff facts

The leader keeps the benchmark redesign task in the original session. This is a
new interactive Codex session in a new tab of the existing HerdR workspace w10.
The portable handoff CLI is available via
`python3 /home/wohlgemuth/IdeaProjects/herdr-handoff/src/herdr_handoff_cli.py`;
there is no `herdr-handoff` executable currently in PATH. Its filesystem outbox
can create/inspect/transition/resume packages. Do not claim a control-plane queue
or remote synchronization unless actually verified.

First response should orient the user to this specific new task, inspect existing
fabric/client routing, and raise at most one material UX question while continuing
independent discovery. The user wants collaboration, not repeated permission for
routine reversible work.
