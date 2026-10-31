# Assistant management CLI and graph contract

Status: proposed. Extends `docs/specs/assistant-cli-20261030/README.md`; nothing here is implemented yet except the
read-only graph route (`GET /api/v1/assistant-graph`, commit `979d7189`).

## Goal

One way to create, associate and inspect assistants, in the CLI first, with the dashboard as a reflection of the same
API. The CLI is the reference client: every dashboard action is the same API call, and every state the dashboard shows
is readable with `herdr-eng assistant graph`. Management never grants authority.

Two kinds of thing are being managed, and they must not be confused:

| Word | What it is | Owner | Today |
|---|---|---|---|
| **assistant** | A Rakazo-based assistant on `#/personal-assistant` (`AssistantService`, `/api/v1/assistants`) | the person | created in the UI with "+ New" |
| **agent** | A collaboration agent / Pi runtime in a room (`/api/v1/collab/...`) | the person, via rooms | `herdr-eng assistant agents ...` |

Specialists (Pi Secretary and later ones) are assistants of a non-`rakazo` **type**. A specialist's runtime is an agent
underneath it. The graph shows assistants; the runtime stays an implementation detail of the specialist.

## Consistency with the existing tools

Audit of the conventions already in `herdr-eng`, and what this contract does with each.

| Convention (where it comes from) | This contract |
|---|---|
| `herdr-eng assistant <noun> <verb>`; nouns are plural: `agents`, `tasks`, `packages`, `approvals`, `hosts` | Keep the nouns for what is attached. Add **verbs directly on `assistant`** for the assistant itself: `create list inspect link unlink archive graph types`. `assistant agents create` (queue a Pi runtime) is unchanged and stays the low-level verb |
| Words as flags, not prose: `--room`, `--package`, `--host`, `--name` | `--type`, `--parent`, not `type <t>` / `to <p>`. Subject is a positional, as in `teams create "Name"` |
| `assistant` group prints a versioned envelope `{"schema_version":1,"ok":...}`; `--json` is accepted and is the default | Same envelope, same `--json` no-op. Exit `0` on success, `1` on any operational error (envelope on stdout) |
| Identity from the credential only; no `--user`, `--owner`, `--approved` (`HERDR_ASSISTANT_URL`, `_TOKEN_FILE`, HTTPS or loopback, no redirects) | Unchanged. New verbs use `AssistantClient` and the same `AssistantConfig` |
| Mutations are sent once and never retried; `--idempotency-key` makes an uncertain result replay-safe (`agents create`, `tasks create`) | Same. `create` and `link` take an optional `--idempotency-key`; when omitted the CLI derives `sha256(verb, name, type, parent)`, so a repeat of the same command replays instead of duplicating |
| A proposal is only a pending request; no CLI operation turns it into authority (`approvals request`) | `link` and `create` never create grants. See Authority |
| `teams` is human-readable by default, with `--json` | Not followed: the `assistant` group is JSON by default. One addition, `graph --tree`, prints text for humans |
| IDs are the addressable thing (`AGENT_ID`, `TASK_ID`) | An assistant is addressed by `asst_...` id, or by exact name when that name is unique among the caller's assistants. An ambiguous name fails and lists the ids |

## Commands

```sh
herdr-eng assistant types
herdr-eng assistant list [--type TYPE] [--parent REF] [--json]
herdr-eng assistant inspect REF [--json]
herdr-eng assistant create NAME --type TYPE [--parent REF] [--require CAPS] [--host HOST_ID] [--dry-run] [--idempotency-key KEY]
herdr-eng assistant place --type TYPE [--require CAPS] [--json]
herdr-eng assistant hosts list [--require CAPS] [--room ROOM_ID]
herdr-eng assistant link CHILD --parent PARENT [--idempotency-key KEY]
herdr-eng assistant unlink CHILD
herdr-eng assistant archive REF
herdr-eng assistant graph [--tree] [--json]
```

`REF` is an id or a unique name. Examples:

```sh
herdr-eng assistant create "Fleet PA" --type rakazo
herdr-eng assistant create "Secretary" --type pi-secretary --parent "Fleet PA"
herdr-eng assistant link "Mail assistant" --parent "Fleet PA"      # existing assistant, explicit association
herdr-eng assistant graph --tree
```

### `types`

Lists the types the control plane will accept. They come from `personal_assistant.delegation.targets`, never from free
text, plus the built-in `rakazo`:

| Type | Meaning | Can be a parent | Needs setup |
|---|---|---|---|
| `rakazo` | A personal assistant: chat, connections, approvals | yes | none |
| `pi-secretary` | Specialist with the digest-pinned Secretary role, no tools | no | a bound runtime (see Specialist setup) |
| `pi` | Engineering handoff through the existing consumer | no | none |
| `browser` | Specialist that drives a real browser | no | a host with `display` and `internet`, and a sandbox profile that allows both (none exists yet) |

Each specialist type declares the capabilities it **requires**. `types` prints them.

### `create`

`create NAME --type rakazo` is the same operation as the UI's "+ New": it calls `POST /api/v1/assistants`. With a
specialist type it also records the type and, when `--parent` is given, the link in the same request. Existing assistants
without a type read as `rakazo`.

### `link` / `unlink`

Records an explicit association between two assistants the caller already owns. Rules, all enforced by the server:

- Both assistants belong to the caller's tenant and user. Linking across users is refused; each person owns an
  independent system.
- The parent is type `rakazo`. A child has one parent; linking again moves it.
- No cycles, depth at most 3.
- Archived assistants cannot be linked.
- **A link is management only.** It confers no grant, connection, model or budget. Authority never expands down the
  tree, and capability inheritance stays a separate, explicit decision.

`unlink` removes the association and nothing else. Delegations already made keep their record.

### `graph`

Prints the same projection the dashboard renders. `--tree` renders it as indented text.

## Authority

None of these commands can create a grant or approve anything.

- `delegate` remains a sensitive tool behind the existing approval flow. After `link`, the first delegation attempt by
  the parent raises the normal approval in the dashboard, exactly as today. The CLI has no way to answer it.
- `create` and `link` are owner actions authorised by the user credential, like creating an assistant in the UI.
- Operator keys keep their current, separate role. A user token cannot link into or read another user's tree.

## Data model and API

Additive; existing assistants and clients are unaffected.

- `Assistant` gains `type: str = "rakazo"` and `parent_id: str | None = None`. Both appear in `as_dict()`; `version`
  increments on link/unlink.
- `POST /api/v1/assistants` accepts optional `type`, `parent_id`, `idempotency_key`.
- `POST /api/v1/assistants/{id}/link` with `{"parent_id": ...}`, and `POST /api/v1/assistants/{id}/unlink` with `{}`.
- The host attestation gains `capabilities` (closed vocabulary above); `_assistant_runtime_host_json` returns it.
- Specialist type definitions in `personal_assistant.delegation.targets` gain an optional `requires` list.
- `POST` runtime creation keeps taking an explicit `host_id`; the resolver runs in the CLI and in a server-side
  `GET /api/v1/assistant-placement?type=...&require=...`, so the dashboard and CLI choose identically. The server
  re-checks the pinned host when the runtime is created and does not trust the client's choice.
- Every mutation writes the existing audit entries: `assistant.created`, `assistant.linked`, `assistant.unlinked`.
- The graph gains an edge `{"from": parent, "to": child, "kind": "linked"}` and each assistant node gains `type` and
  `parent_id`. Edge kinds are then: `owns` (derived), `linked` (explicit), `delegated`, `runs_on` (runtime facts).

## Dashboard

The dashboard stays a projection of the same API and adds no state of its own.

- The graph panel draws `linked` edges as solid lines and runtime edges (`delegated`, `runs_on`) as lighter ones, so a
  configured association is distinguishable from something that actually ran.
- "+ New" gains a type selector fed by `GET /api/v1/assistant-types`; choosing a parent calls the same link route.
- Each assistant shows the equivalent command (`herdr-eng assistant link ...`) so the two surfaces teach each other.

## Placement

Some specialists run remotely inside the fabric on hosts that differ in what they can offer: a browsing agent needs a
display, a desktop agent a desktop session, others an internet path or a VM boundary. Creating an assistant locates a
host from what it needs; nobody types a host id unless they want to pin one.

### Capabilities

A closed, versioned vocabulary. A host **attests** the ones it can provide; a type or the caller **requires** them.

| Capability | Meaning |
|---|---|
| `display` | A display the agent can render to without a person present (for example a virtual framebuffer) |
| `desktop` | An interactive desktop session; implies `display` |
| `internet` | Outbound access, always through the operator egress broker; never direct |
| `vm` | The runtime executes inside a VM isolation boundary |
| `gpu` | A usable GPU |
| `os:linux`, `os:macos` | Host operating system |
| `arch:x86_64`, `arch:arm64` | CPU architecture |

Unknown capability names are rejected, so a typo cannot silently match nothing or everything.

### Capabilities are constraints, never grants

This is the rule the rest depends on. A host attesting `internet` does not give any agent internet: the sandbox profile
the runtime runs under decides what the agent can reach, and the existing `task-restricted-v1` profile has no network
and no display. Placement only answers "which host can carry this", and never widens a profile, grants a tool, or
opens egress. A type that needs `display` and `internet` therefore also needs a reviewed profile that permits them. That
profile is separate security work and is a precondition to creating a `browser` specialist, not something this
resolver can supply.

### Where capabilities come from

The operator-side runner reports them in its host attestation, through the same signed channel that already reports
`supported_runtime_kinds`, `sandbox_profiles` and `models`. The attestation gains a `capabilities` list. A request, a
package or an agent can never assert a host's capabilities. Fleet inventory may be shown as a hint, but only what the
runner attests counts for placement.

### Resolution

`create` computes requirements as `type.requires` plus any `--require`. `--require` can only add to a type's minimum,
never relax it, so a browser specialist cannot be placed on a host without a display.

A host is a candidate when all of these hold:

1. Its attestation is fresh and unused (`status: ready`), and the operator has not disabled it.
2. It supports the runtime kind and a sandbox profile the type permits.
3. Its attested capabilities contain every requirement.
4. It carries the type's role binding and approved model, where the type has them.

Candidates are ordered by freshest attestation, then `host_id`, so the same inputs choose the same host. With
`--host HOST_ID` the host is pinned, and it must still pass the checks above; pinning is never a way around a
requirement. If no host qualifies, `create` fails before anything is created and says which requirement eliminated
which host.

The chosen `host_id` is recorded on the runtime and returned in the result. It is not re-chosen silently: a running
runtime stays where it is, and if its host goes stale the existing lease logic fails it. Creating again places again,
with the requirements part of the idempotency fingerprint. The assistant's identity does not depend on the host.

### CLI

```sh
herdr-eng assistant hosts list --require display,internet
herdr-eng assistant place --type browser                 # ranked candidates, and why each rejected host was rejected
herdr-eng assistant create "Web scout" --type browser --parent "Fleet PA" --dry-run
herdr-eng assistant create "Web scout" --type browser --parent "Fleet PA" --require vm
```

`place` and `--dry-run` change nothing. `hosts list` stays the existing room-scoped owner view, with capabilities added
to each host and `--require` as a filter. Output follows the existing envelope.

### Graph and dashboard

The graph gains `host` nodes (status, capabilities) and a `placed_on` edge from a specialist to its host. A specialist
with unmet requirements shows what is missing, for example "needs display and internet: no host qualifies", rather than
a bare "not set up". The dashboard shows capability chips on hosts and the same reason text.

## Specialist setup

Linking a specialist does not make it runnable. `pi-secretary` reads "not set up" in the graph until a runtime is bound
for that person. Two stages, deliberately separate:

1. **This contract:** types, create, link, graph. A specialist can exist, be linked and be seen as not set up.
2. **Self-provisioning (separate decision):** the control plane binds the person's Pi runtime itself, replacing operator
   `pi_bindings`. It must bind only to the authenticated user, require the normal dashboard approval, and use no
   calendar or email access. It is not part of this contract.

## Validation

- CLI contract tests drive the real parser against a real control-plane server (the pattern in
  `tests/test_assistant_cli.py`): create, link, unlink, graph, ambiguous name, unknown type, cross-user, cycle, depth.
- Parity test: whatever `create`/`link` do through the CLI appears in `GET /api/v1/assistant-graph` and in
  `renderAssistantGraph` output.
- Negative tests: no grant, approval or connection is created or changed by any verb; a second user sees nothing.
- Placement tests: capability superset match, unknown capability rejected, `--require` cannot relax a type minimum,
  stale/used/disabled hosts excluded, deterministic tie-break, pinned host that fails a requirement is refused, no
  candidate reports the eliminating requirement per host, and a host attesting `internet` never changes a profile.
- Replay test: repeating a command with the same (or derived) key creates one assistant and one audit entry.

## Open decisions (defaults chosen)

- **Verb placement:** verbs on `assistant` itself, as proposed. Alternative: a separate `assistants` top-level group
  (like `teams`), rejected because two near-identical group names would confuse.
- **`link` direction:** `link CHILD --parent PARENT`, so the subject being changed is the first argument.
- **Parent type:** only `rakazo`. Rakazo-to-Rakazo links are allowed (for example `Mail assistant` under `Fleet PA`).
- **Output:** JSON default, as the `assistant` group does today.
- **Capability source:** runner attestation only; fleet inventory is a hint.
- **Browsing precondition:** no `browser` specialist can be created until a sandbox profile that permits `display` and
  brokered `internet` is designed and reviewed. Until then `place --type browser` reports no host qualifies, which is
  the correct answer.
