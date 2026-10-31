# Collaboration room editor: recon (Task C0, 2026-10-24)

What exists today, before tasks C1-C3 change it, and what the pi harness does with the text the
room hands it. Line numbers are from `origin/main` at 1923912.

## 1. Agent names: where they are stored and validated

Server, `herdr_engineering/control_plane/collab.py`:

- An agent's handle is `self._agents[aid]["name"]`. It is set when the `agent_enrolled` event is
  applied (`p.get("name") or _default_handle(harness_type, agent_id)`) and changed only by
  `agent_renamed`. The old name lives only in that event's `old_name`. The name is not otherwise
  kept, and old `@mentions` stay plain text.
- `_handle()` (around line 5340) validates every name a person chooses: 1-32 characters of
  `[a-z0-9-]` (`_HANDLE_BAD`), not starting with `-`. It is used by `connect_agent` (`name`),
  join tokens (`name_prefix`, at most 24) and `rename_agent`.
- `_default_handle()` builds `<harness>-<last 4 of agent id>`, for example `claude-code-a3f2`.
- `_agent_names()` gives fleets the names `prefix-1`, `prefix-2` and so on.
- Uniqueness has two parts. `_name_free()` requires one handle per room among granted agents.
  `rename_agent` also requires one handle per owner among that owner's live agents.
- Owner = `agent["owner"]`, the user id whose credential created the agent: enrollment code,
  `connect`, join token or spawn. People have no handle at all. A person is a user id plus
  `_user_email[uid]`, which `identity_linked` fills from a verified id_token.
- `get_room` lists members (with emails) only to the room owner. `room_state` gives every
  non-guest the agents roster (`list_room_agents`: id, name, harness_type, owner_user_id,
  actions, external). It gives no people roster.
- Presence: `_presence` is fed only by external agents' heartbeats. A person or an internal agent
  is "online" only in the sense that it holds a `room_state` long-poll (`_state_waiters`).
- Messages: `post_message` (around line 4635) accepts only `_MESSAGE_FIELDS = {text,
  idempotency_key, in_reply_to, context}`. Any other field is a 400; `actor_*`, `sender_*` and
  similar fields are also counted and audited as forged identity. The sender is always derived
  from the credential. Per-sender and per-owner rate limits apply. A committed message bumps the
  room sequence, and that already wakes every `room_state` long-poll.

Client:

- `packages/collab-mention.js`: `NAME = /^[a-z0-9-]{0,32}$/`, plus `mentionAt`, `suggest`,
  `applyMention` and `quiet`. Its only consumer is `packages/herdr-web-client/collab-mention.test.js`.
  Neither view imports it any more.
- `packages/conversation-composer.js` has its own `completionAt(text, caret)`, which returns
  `{start, query, trigger}` for `@` and `/`, and a `suggestions` option (flat list, substring
  filter, 8 rows).
- `packages/collab-view.js` `onSend` treats `^@([a-z0-9-]{1,32})\s+(.+)$` as a task.
  `api.createTask(room, agent, rest)` drops the `@name ` prefix. An unknown or duplicate name is an
  error, and nothing is sent. The rename form uses `pattern="[a-z0-9]+(-[a-z0-9]+)*"`, `maxlength=32`.
- `packages/session-workspace.js` mounts the same composer with
  `referenceSuggestions()` (`session-workspace-model.js`): agents `@<handle>`, recorded files
  `@<path>` and GitHub links.

## 2. Attachments: how the composer uploads, and what storage exists

- `mountComposer` uploads only through `options.upload(file) -> artifact`. Without that option the
  Attach button is disabled and the composer says "Attachments are not supported by this
  connection." `onSend(text, artifacts)` receives what the adapter returned.
- The collab room passes no `upload`, so the room has no attachment path today. The session
  workspace passes none either, and its `onSend` refuses attachments ("This agent transport
  cannot deliver files").
- The existing byte store is the artifact store. `api.py` `_upload_artifact` takes a raw body plus
  `?filename=&mime=`, content-addresses it in RustFS when one is wired (`rustfs.put` gives the
  storage key and sha256) and otherwise keeps it in the repo's in-memory stand-in.
  `artifacts.py` `ArtifactStore` holds the same sha256-keyed model. C5 (room files) should follow
  it. Collab itself (the event log) stores no file bytes.

## 3. Can agents report skills at registration?

Not today. There is no `skills` field anywhere in `collab.py`. The agent-side registration bodies
are:

- `POST agents/enrollment/complete` (`complete_enrollment`: code, device_id, instance_id);
- `POST agents/join` (`redeem_join_token`: token, instance_id; the external path rejects any
  unknown field);
- `POST agents/connect` (`connect_agent`, signed-in person: harness_type, name, parent_agent_id).

Plan (C2): add an optional `skills` list to the internal enrollment, join and connect bodies. It
is validated, capped and stored in the `agent_enrolled` payload, and the room lists it as given.
Each agent reports its skills in its own harness's literal form, so the menu inserts `/` plus
that string and needs no per-harness translation. For example, pi reports `skill:pdf-tools`.

## 4. pi 1.3.0: in-chat syntax (from the installed package, not memory)

Source: `~/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent` (version 1.3.0), its
`docs/` and `dist/`.

| Syntax | What pi does | Status |
|---|---|---|
| `/skill:name args` | `AgentSession._expandSkillCommand` replaces the text with the skill's `SKILL.md` block, then appends `args`, but **only when the prompt text starts with `/skill:`** (`text.startsWith("/skill:")`). An unknown skill passes through unchanged. Docs: `docs/skills.md`, `docs/slash-commands.md`. | verified in `dist/core/agent-session.js` |
| `/template args` | Prompt templates are expanded by `expandPromptTemplate`, again from the start of the text. | verified (same call site) |
| other `/cmd` (e.g. `/reload`, `/name`) | Interactive commands, handled by the TUI or extensions before prompting. | docs only |
| `@` in the editor | "Type `@` to search for a file and add it to your prompt" (`docs/usage.md`). This is a TUI editor completion. `prompt()` has no `@path` expansion; grepping `dist/core` and `dist/modes/interactive` finds none. | **unverified** whether the editor inserts the bare path or `@path`; the model sees plain text either way |
| `@file` as CLI argument | `pi @a.md "msg"` attaches file contents (`dist/cli/args.js`). Applies to argv only, not to chat text. | verified |
| `!cmd` / `!!cmd` | Runs a shell command; `!` sends its output to the model, `!!` does not (`docs/usage.md`). | docs only |

Consequence for the room (rule: literal text, never rewritten). If a room message `@gert.pi
/skill:review src/` reaches pi verbatim, pi does **not** expand the skill, because the text does
not start with `/skill:`. The room passes it literally anyway. Expansion depends on the agent-side
bridge: when it hands a message to its own harness, it may drop its own leading `@address `. That
is a bridge concern and belongs outside C1-C3. It is recorded here so nobody "fixes" it in the
editor.

`$` has no meaning in pi chat (`$@`/`$ARGUMENTS` appear only inside template files). `!` belongs to
the harness and is not completed.

## 5. Decisions taken from this recon for C1-C3

- Keep `_handle` (the custom name, 1-32 of `[a-z0-9-]`) as it is. It becomes the agent's
  **alias**. The canonical address `<owner>.<harness>[-n]` is derived and never typed in.
  Only the mention and address grammar widens, to `[a-z0-9._-]{1,48}`.
- A person's handle is the local part of their verified email, sanitized to `[a-z0-9-]` (`.`
  becomes `-` so it cannot look like `owner.harness`). It is assigned the first time an email is
  linked, made unique with `-2`, `-3` and so on, and then frozen. A user with no linked email
  has no handle.
- Online, for `@here`, means a principal holds or recently held a `room_state` long-poll for the
  room, or an external agent's heartbeat is fresh.
