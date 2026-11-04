# HerdR handoffs

`berlinguyinca/herdr-handoff` is the portable client and skill installed into
Codex, Claude, Pi, and OpenCode. It writes private JSON packages to the local
`.herdr/handoffs/outbox/` directory, so a handoff remains possible when the
control plane is unavailable.

It is a required HerdR Engineering operational dependency. Run
`scripts/install.sh` on every host; it installs the pinned revision from
`lock/upstreams.yaml`, the CLI into the project virtualenv, and all four
harness skills. Strict validation fails if any part is missing.

The canonical command namespace is namespaced to avoid collisions with native
harness commands:

```text
/herdr::toClaude
/herdr::toPi
/herdr::toCodex
/herdr::toOpenCode
/herdr::artifact
/herdr::handoff
/herdr::review
/herdr::verify
/herdr::fabric
/herdr::publish
/herdr::observe
/herdr::session
```

No top-level `/artifact`, `/review`, or similar aliases are installed.

The `to*` commands are interactive: they split the invoking `HERDR_PANE_ID` to
the right, start the target harness in that child pane, send the bounded resume
prompt, and report the handoff/pane metadata back to Herdr. If the current pane
identity is unavailable, the command fails rather than launching a detached
process.

The control plane accepts those packages at `POST /api/v1/handoffs`. The
operator API key is required. A package is queued before a recipient can claim
it; state changes are immutable events and are visible through
`GET /api/v1/handoffs/tree` and the dashboard's Handoffs view.

```sh
herdr-handoff create --goal "Continue the current session" \
  --source-harness codex --target-harness claude \
  --remaining "Run the review" --artifact art_123

herdr-handoff sync --url "$HERDR_CONTROL_PLANE_URL" --token "$HERDR_API_KEY"
```

The model argument is a hint, not a guarantee. The receiving harness must
report the resolved model. Handoffs do not launch processes or publish
anything automatically. Public sharing remains a separate, human-approved
artifact-publication action.
