# Assistant management CLI and graph contract

Status: implemented except where marked **not built**. Extends `docs/specs/assistant-cli-20261030/README.md`.

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
| Identity from the credential only; no `--user`, `--owner`, `--approved` | Kept. **Transport differs, on purpose:** `assistant agents/tasks/...` use `HERDR_ASSISTANT_*` Bearer tokens against `/api/v1/collab`, but the Personal Assistant API authenticates by API key mapped to a principal and tenant. The new verbs therefore use the `herdr-eng teams` transport (`--control-plane`, `--api-key-env HERDR_API_KEY`, no redirects) and the `assistant` group's envelope |
| Mutations are sent once and never retried; `--idempotency-key` makes an uncertain result replay-safe (`agents create`, `tasks create`) | Same. `create` takes an optional `--idempotency-key`; when omitted the CLI derives one from `(verb, name, type, parent)`, so a repeat replays instead of duplicating. `link`/`unlink` are idempotent by nature (a repeat is a no-op) and take none |
| A proposal is only a pending request; no CLI operation turns it into authority (`approvals request`) | `link` and `create` never create grants. See Authority |
| `teams` is human-readable by default, with `--json` | Not followed: the `assistant` group is JSON by default. One addition, `graph --tree`, prints text for humans |
| IDs are the addressable thing (`AGENT_ID`, `TASK_ID`) | An assistant is addressed by `asst_...` id, or by exact name when that name is unique among the caller's assistants. An ambiguous name fails and lists the ids |

## Commands

```sh
herdr-eng assistant types
herdr-eng assistant list [--type TYPE] [--parent REF] [--json]
herdr-eng assistant inspect REF [--json]
herdr-eng assistant create NAME --type TYPE [--parent REF] [--idempotency-key KEY]
herdr-eng assistant place --type TYPE [--require CAPS]
herdr-eng assistant link CHILD --parent PARENT
herdr-eng assistant unlink CHILD
herdr-eng assistant archive REF
herdr-eng assistant graph [--tree] [--json]
```

`REF` is an id or a unique name. Examples:

```sh
herdr-eng assistant create "Fleet PA" --type rakazo
herdr-eng assistant create "Secretary" --type pi-secretary --parent "Fleet PA"
herdr-eng assistant link "Mail assistant" --parent "Fleet PA"      # existing assistant, explicit association
herdr-eng assistant place --type scout --require desktop
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
- `GET /api/v1/assistant-placement?type=...&require=a,b` runs the one resolver for the CLI and the dashboard, so they
  always agree. Runtime creation (`assistant agents create`) is unchanged and still names a host; connecting that to
  the resolver belongs to self-provisioning and is **not built**.
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
display, a desktop agent a desktop session, others an internet path or a VM boundary. Placement is **always automatic**: resources and
capabilities decide, and nobody types a host, so there is no `--host` flag. Where an agent runs is shown, not chosen.

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

Requirements are `type.requires` plus any `--require`/`require=`; a caller can only add to a type's minimum, never relax
it. Resources come from the type's `resources` (`cpus`, `memory_mb`, `gpus`).

A candidate (a fabric host or a Slurm partition) qualifies when all of these hold:

1. It is `ready`: a fresh, unused attestation that the operator has not disabled (hosts), or a partition that is up (Slurm).
2. Its attested or configured capabilities contain every requirement.
3. Its free resources cover the type's resources.
4. It lists the sandbox profile the type uses (`task-restricted-v1`) and the type's role binding.

Qualifying candidates are ordered: **fabric hosts before Slurm** (a host can start now), then freshest attestation, then
most free capacity, then id, so the same inputs choose the same place. If nothing qualifies, `chosen` is null and every
rejected candidate carries its reasons (`missing display`, `not enough cpus`, `no sandbox profile ...`, `role not bound`).

Creating a specialist **never fails because nothing qualifies**. The assistant exists with status `needs_setup`, and
the graph shows `unplaceable` with those reasons. Placement is computed from current state each time it is read, so it
cannot go stale: the dashboard shows where an agent *would run now* (`planned`), and will show where it actually runs once
a runtime exists.

### Slurm

A cluster takes part only when the operator has configured it for assistant use:

```yaml
personal_assistant:
  placement:
    slurm:
      clusters:
        - id: hive
          label: Hive
          login_host: login1.example
          assistant_use: true            # without this the cluster is invisible to placement
          partitions:
            low:
              capabilities: [os:linux]
              profiles: [task-restricted-v1]     # only what the operator has attested on that partition
              role_bindings: ["procedure:secretary-system@1.0.0:sha256:..."]
```

Capacity is read-only: one `sinfo -N -h -o '%P|%a|%n|%T|%C|%e|%m|%G'` over `ssh` (a fixed command, quoted as one remote
string, with an operator-validated host name), cached 60 s. It is modelled **per node**, because a runtime lives on one
node: a partition lists its usable nodes' idle CPUs, free memory (capped at the node's size) and GPUs, and a request
fits only when a single node covers all of it at once. Only `idle` and `mixed` nodes count; `allocated`, `down`,
`drained` and `draining` never do. `sinfo` does not report GPU use, so GPUs are counted only on a node with no allocated
CPU. `sinfo` also cannot attest a display, an internet path or a sandbox, so those come from configuration and are never
inferred. A partition with no declared profile never carries a specialist. An unreachable cluster is `unavailable`,
never an error to the caller.

#### Validated against `whiteale.fiehnlab.ucdavis.edu`

Read-only, on 2026-10-31 (Slurm 25.11.8; partitions `compute`, `bigmem`, `mpi`, `high`, `low`; 24-core nodes, 57 GB or
245 GB; no GPUs; not part of the dev fabric). The capture is `tests/fixtures/slurm/whiteale-sinfo-nodes.txt`. It showed
that real `sinfo` uses full state words (`allocated`, `mixed`), appends `+` to memory, and that a busy cluster can have
no fully idle node while mixed nodes still have free cores, which is why capacity is per node. A live probe also exposed
an `ssh` quoting bug (the `|` in the format was read as a pipe by the remote shell) that no unit test caught; it is fixed
and covered. The live check is opt-in and read-only: `HERDR_LIVE_SLURM_HOST=whiteale.fiehnlab.ucdavis.edu pytest
tests/control_plane/test_placement.py -k live`.

#### Sandbox smoke test on whiteale (2026-10-31)

One 3-minute, 1-CPU, 512 MB `sbatch` job on `low` ran a read-only preflight on compute node `kvm-node-9`. It installed
nothing. `sbatch --test-only` and the job were accepted without any account setup. Result on the compute node:

| Check | Result |
|---|---|
| `bwrap` (bubblewrap) | **missing** on the compute node (present on the login node) |
| `node` | **missing** on the compute node |
| `systemd-run --user --scope` | **fails** inside a job |
| cgroup v2 | present, but `cgroup.subtree_control` is **not writable**: Slurm's `task/cgroup` and `proctrack/cgroup` own the hierarchy |
| `python3` | present |

So `task-restricted-v1` (which attests bubblewrap, cgroup v2 limits, broker-only egress and task-only access) **cannot
be attested on whiteale as it stands**, and whiteale stays unplaceable, which is what placement already reports. This
confirms the placement gate against a real cluster. Slurm does contain the job with its own cgroup limits, but that is a
different mechanism and not the attested profile; treating it as equivalent would be a security decision of its own.

Two further facts: the login node (`kvm-node-12`) has a **local** `/home`, while compute nodes mount BeeGFS at `/home`
(the login node sees it at `/mnt/beegfs`), so a runner cannot assume the login node and the job share files and must
report back over the network; and `--cpus-per-task=1` was allocated as two logical processors.

Caveats on the evidence: the preflight's `bwrap_no_network` and `bwrap_hides_home` lines are meaningless here, because
they treated any failure as "enforced" and bubblewrap was missing; only the missing `bwrap`, the failed systemd scope and
the non-delegated cgroup are real findings. A follow-up job meant to read results interactively was malformed (`srun -q`
takes an argument, so `-p` was read as the QoS and `low` as the command); it never ran and is not evidence of anything.

What a Slurm profile would need, none of it built: bubblewrap (or an equivalent such as Apptainer, to be checked) on the
compute nodes, with a reviewed profile that states what Slurm's cgroup containment does and does not guarantee. The same
question applies to the planned GPU cluster, which will be inside the fabric and so can be provisioned for it.

Configured honestly (no profile attested there), whiteale is correctly *unplaceable*: every partition is rejected with
`no sandbox profile task-restricted-v1`. Declaring a profile it has not attested would make it plannable, which is
exactly what must not be done until a runner is attested there.

**Not built:** launching a runtime onto Slurm. That needs the runner installed and attested on the cluster
(bubblewrap, cgroup and broker enforcement on compute nodes), which cannot be proven without one. Until then a Slurm
partition can be *planned* onto and is visible in the graph, but nothing is submitted. The existing no-touch rule for
compute nodes still applies.

### CLI

```sh
herdr-eng assistant place --type scout                    # chosen place, ranked candidates, and why others were rejected
herdr-eng assistant place --type scout --require desktop  # can only tighten
herdr-eng assistant graph --tree                          # shows "on <host>" per specialist and a "Where agents run" section
```

`place` and `graph` change nothing. There is no `hosts list --require`: `hosts` is the collab-token, room-scoped owner view,
a different credential world, and `place` answers the same question.

### Graph and dashboard

The graph gains `host` nodes (status, backend `host` or `slurm`, capabilities, free resources) and a `placed_on` edge with
`state: planned` from a specialist to its host. A specialist that cannot be placed carries `placement.state: unplaceable`
with the per-candidate reasons. The dashboard labels a placement **planned**, shows the backend and capability chips on hosts, and
lists the reasons for an unplaceable specialist.

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
- Placement tests (`tests/control_plane/test_placement.py`): capability superset match, unknown capability rejected,
  `--require` cannot relax a type minimum, stale hosts excluded, deterministic ordering, per-candidate rejection reasons,
  a host attesting `internet` never changes a profile, a runner that reports no capabilities matches nothing, Slurm config
  validation, `sinfo` parsing, probe caching and failure, and a cluster without a declared profile never carrying a specialist.
- Replay test: repeating a command with the same (or derived) key creates one assistant and one audit entry.

## Open decisions (defaults chosen)

- **Verb placement:** verbs on `assistant` itself, as proposed. Alternative: a separate `assistants` top-level group
  (like `teams`), rejected because two near-identical group names would confuse.
- **`link` direction:** `link CHILD --parent PARENT`, so the subject being changed is the first argument.
- **Parent type:** only `rakazo`. Rakazo-to-Rakazo links are allowed (for example `Mail assistant` under `Fleet PA`).
- **Output:** JSON default, as the `assistant` group does today.
- **Capability source:** runner attestation only; fleet inventory is a hint.
- **Placement is never user-chosen:** no `--host`, no `--dry-run` (every `place` is a dry run).
- **Browsing precondition:** no `browser` specialist can be created until a sandbox profile that permits `display` and
  brokered `internet` is designed and reviewed. Until then `place --type browser` reports no host qualifies, which is
  the correct answer.
