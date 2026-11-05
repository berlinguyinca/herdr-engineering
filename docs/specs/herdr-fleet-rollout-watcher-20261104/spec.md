# Merge-triggered fleet rollout watcher and "Fabric management" page

Status: **proposed (design only, no feature code)** · Date: 2026-11-04 · Base: `origin/main` @ `5d945ddb`
Repo: `berlinguyinca/herdr-engineering` · Numbering: recent specs live in a dated directory
(`docs/specs/herdr-<topic>-<date>/spec.md`); the `00NN-` files are the frozen manifest-ordered program
(`SPEC_MANIFEST.yaml`), so this spec follows the newer convention and does not touch the manifest.

## 1. Goal

When a PR merges to `main` in `berlinguyinca/autospec`, `pi-engineering`, `herdr-engineering` (and, as a
later phase, `ai-sync` / `claude-config`), every fabric member is updated automatically, one host at a
time, with a health check after each host, and the operator can watch what happened over time in the
dashboard under **Fabric management**, drawn as Mermaid diagrams.

Fabric members (only these have shells, all reachable over tailscale ssh):

| host | OS | role in a rollout |
|---|---|---|
| `beast` | Linux | controller + ai-sync **publisher**; canary (position 1) |
| `bender` | Linux | position 2 |
| `macbook-pro` | macOS | position 3; optional (may be asleep) |

Non-goals: inbound webhooks (the tailnet is private); updating hosts that have no shell; replacing
`herdr-fleet` (declarative installer, owns the lock schema); reloading live Pi sessions (stays in
`fleet rollout`, section 6.4); automatic downgrade across the fleet; any change to branch protection.

## 2. What already exists (extend, do not duplicate)

| Concern | Where | Reuse |
|---|---|---|
| Fleet update (fast-forward `origin/main`, reinstall, Ansible converge, parity check) | `herdr_engineering/fleet.py::fleet_update`, `fleet_status`, `_parity`, `stage_pi_runtime` | The per-host *action*. `fleet_update(host=..., revision=<sha>)` already takes `-l host`, refuses a dirty controller, refuses `origin/main..HEAD != 0`, and requires full 40-char SHAs. |
| Nightly timer, skip-vs-fail semantics, run-state file, flock | `fleet_autoupdate.py`, `scripts/herdr-eng-fleet-update.{service,timer}`, `fleet-auto-update{,-macos}.sh` | Keep as the **safety net** (daily catch-up). The watcher is a second trigger for the same updater, never a second updater. Reuse `SkipUpdate`, `shadowed()`, `runner_identity()`, `record_run()` (#572, #583, #585). |
| Per-version Pi install + session reconcile | `fleet.stage_pi_runtime` (installs under `~/.local/share/herdr-engineering/pi/releases/<sha>`, never reloads), `fleet_rollout.py` (`plan/apply/run/status`), `docs/operations/fleet-runtime-rollout.md` | Pi staging and activation. The watcher only calls *stage*; activation stays a separate explicit/idle-gated step. |
| AutoSpec runtime | `ansible/tasks/autospec-fleet.yml` (checkout `autospec-runtime-main`, `autospec-runtime-install.sh`, "failed refresh leaves the previous generation active") | AutoSpec action; already atomic by generation. |
| ai-sync | `ai_sync_enforce.py` (forced pull + verify, refuses on the publisher marker), `ai_sync_feed.py` | ai-sync / claude-config action. |
| Feed pattern | `ai_sync_feed.py` -> `POST /api/v1/ingest` -> `control_plane/ingest.py::validate_ai_sync` (strict allow-list) -> `repo.record_event` -> `GET /api/v1/ai-sync` (ETag) -> `packages/ai-sync-view.js` mounted in `app.js` | Exactly this path, with a new entity type. Reuse `redact()`. |
| Dashboard shell | `packages/app.js` `ROUTES` / `SECTION_TABS`; Fabric group at lines ~92-108 | New route under the Fabric group. |
| Mermaid | `control_plane/artifact_visual_renderers.py::render_mermaid` (dependency-free, flowchart + sequence -> SVG, unsupported input stays inert source) | Server-side rendering (section 9.1). |
| Fleet lock | `~/.config/herdr-fleet/fleet.lock.yaml` (`repos:` map name -> SHA; also node/herdr/clis/images) | Written only by the guarded step in section 7. |

## 3. In-flight work and overlap (checked 2026-11-04)

Open PRs in `herdr-engineering` (`gh pr list`): #610 `fix(fleet): probe the launchd label the macOS
fabric-agent installer actually writes` (touches `ansible/tasks/fleet-fingerprint.yml`, which is the
data source of per-host version state; **land before PR 2 of this plan**), #608 assistant-hub, #609/#607/
#606/#605/#600/#596 (unrelated; #600 adds artifact features, so edit `artifact_visual_renderers.py` only
after it merges). No open PR in `autospec` / `pi-engineering` concerns fleet rollout (4808/574 are
GitGuardian config).

Recently merged and relied on: #585 (updater must not run the checkout's own code), #583 (macOS
launchd updater), #588/#578/#572 (skip-vs-fail, plist label), #333 (summary skill in fleet status), #95
(fleet version matrix + drift plan), #245 (AutoSpec synced with the fleet), #289 (powerpack snapshot +
rollback in `fleet update`).

**Overlap warning for another Claude session.** The 2026-11-02 hindsight-memory spec pack
(`docs/specs/herdr-unified-hindsight-memory-20261102`, phase P06) defines a "single-host canary and safe
future fleet rollout" and states `herdr-fleet apply` is plan/check only (no activity-aware staging, no
rollback). Anything in that session touching `fleet.py`, `fleet_autoupdate.py`, the fleet timers or
`ansible/tasks/fleet-fingerprint.yml` collides with PRs 1-4 below; coordinate before starting them.
Memory notes also record that bender's updater is *self-blocking* while its checkout is dirty; this spec
makes that visible (section 6.2) instead of fixing it.

## 4. Decisions

**D1 Poll, don't push.** No inbound path exists. A watcher on `beast` polls `GET /repos/{o}/{r}/branches/main`
per watched repo through `gh api -i` with `If-None-Match` (stored ETag). Interval **120 s** with +-20 s
jitter: 4-5 repos x 30 req/h = ~150 req/h, and a `304` conditional response does not consume the
authenticated primary rate limit (5000/h; 3970 remained when measured). A merge is detected as "branch head
moved", which also catches direct pushes; the PR number is resolved afterwards from
`GET /repos/{o}/{r}/commits/{sha}/pulls` (display only, never used for control flow).

**D2 Rate-limit handling.** On `403`/`429` read `Retry-After`, then `x-ratelimit-reset`; sleep until then
(cap 15 min), else exponential backoff 2 min -> 15 min with jitter. `x-ratelimit-remaining < 100` stretches the
interval to 10 min. Network errors back off the same way and are recorded as one `source.degraded` event on
entry and one `source.recovered` on exit (not one per poll). The watcher never uses a token with write scope;
`gh auth` on beast needs read-only repo access.

**D3 Only published history is eligible.** A SHA is rollable only if (a) GitHub reports it as the head of
`main` (it came from the API, not from a local checkout) and (b) after `git fetch origin main` in the
watcher's *own* bare cache clone, `git merge-base --is-ancestor <sha> origin/main` succeeds. Anything else
is refused with a `source.refused` event. The known hazard is real: today `fleet.lock.yaml.bak-20261104`
pinned `autospec: 6b7b8eb0...`, which `GET /repos/berlinguyinca/autospec/compare/6b7b8eb...main` answers
with `404` (the commit does not exist on GitHub - a stray unpushed commit), while the corrected pin
`e535daa1...` returns `identical`. The same check is applied to any SHA on the lock-writing path, not just
watcher-originated ones (section 7).

**D4 One rollout = one target set, coalesced.** The rollout target is the tuple `{repo: sha}` of every
repo whose head differs from the last *converged* SHA. If more merges land while a rollout runs, they do not
start a second rollout; the running one finishes (or halts), then at most one follow-up rollout targets the
newest heads. A rollout superseded before its first host starts is marked `superseded`. Idempotency key:
`rollout_id = sha256(sorted(repo:sha))[:16]`; re-observing the same heads (restart, ETag miss) yields the same
id, finds it terminal-`succeeded`, and does nothing.

**D5 Staged, serial, halt-on-first-failure.** Order is fixed by inventory: `beast` -> `bender` ->
`macbook-pro`. Each host: pre-check -> apply -> health check -> record. The first host whose result is not
`ok` halts the rollout; later hosts get `skipped(reason=halted)`. No parallelism. `macbook-pro`
unreachable is `skipped(reason=optional_unreachable)` and does not fail the rollout (matches
`_absent_optional`), but beast or bender unreachable halts.

**D6 Never run an updater from a dirty or shadowing checkout.** The watcher runs the **installed** build
(`~/.local/bin/herdr-eng`, neutral cwd), never `python -m` inside a checkout (`shadowed()` / #585). Before
touching a host the pre-check requires, on that host's deployment checkout (`herdr_engineering_checkout`
or the controller repo): branch `main`, `git status --porcelain --untracked-files=all` empty,
`origin/main..HEAD == 0`. Failing it records `blocked(reason=dirty_checkout|not_on_main|unpublished_commits)`
with the counts and <=8 example paths (as `fleet_autoupdate` already reports) and **halts** (a canary that
cannot be updated proves nothing). The checkout is never cleaned, stashed or reset.

**D7 Pi self-update never happens mid-session.** The watcher stages the new Pi release into
`.../pi/releases/<sha>` (existing `stage_pi_runtime`, which "never reloads or restarts live sessions") and
records `staged`. Activation = atomic switch of a `current` symlink (`ln -s` to a temp name + `rename`) to the
new release; running Pi processes keep their open files from the old release directory. New sessions pick the
new release; existing sessions adopt it only through the existing `fleet rollout` flow, which is an
explicit operator action (or an opt-in idle-window mode in a later spec). Old releases are retained (keep 3)
so rollback is a symlink flip.

**D8 Failure policy.** A failing host is automatically returned to its `before_sha` **only where that is a
pure pointer flip or an ff-only checkout** (Pi symlink, AutoSpec generation - already atomic; herdr-eng via
`fleet_update(revision=before_sha)` scoped to that host and itself requiring a published SHA). Hosts that
already succeeded stay on the new version by default (`rollback_scope: failed_host`); config
`rollback_scope: all_updated` flips them back in reverse order. A halted rollout keeps the lock at the
last-known-good SHAs, blocks further automatic rollouts *of the same repo set* until an operator `resume`s,
`abandon`s, or a newer head supersedes it (fix-forward re-runs the canary). Never `git reset --hard`.

**D9 Events are the source of truth; the dashboard is a projection.** Every state change is an append-only
event through the existing ingest path. Diagrams are derived from events and regenerated, never stored as
authored content. The watcher keeps its own durable JSON state file for restart safety and an outbox JSONL
that replays unposted events (the control plane may be on the host being updated).

**D10 Mermaid rendered server-side to SVG.** See 9.1 for the justification and fallback.

**D11 Where it runs.** One watcher, on `beast`, as a Linux systemd **user service** (a long-running poller
with a flock single-instance lock) - not a container exception to the "services run as containers" rule: it
is a host-level operator job that must run `ssh`/`herdr-eng` as the user (the same justification the
fabric agent has), and the control plane itself stays a container. It targets the other hosts only via
`ssh <host> herdr-eng fleet host-apply ...` (below). macOS needs no watcher.

## 5. Components

```
GitHub (read-only poll, ETag)                      beast (controller)
 autospec  pi-engineering  herdr-engineering  ───► herdr-eng-fleet-watch.service (new)
 (ai-sync, claude-config: phase 2)                    │ poller   (fleet_watch/poll.py)
                                                       │ verifier (fleet_watch/verify.py: D3 reachability)
                                                       │ planner  (fleet_watch/plan.py: coalesce, rollout_id)
                                                       │ runner   (fleet_watch/run.py: state machine, per-host exec)
                                                       │ emitter  (fleet_watch/events.py: redact, outbox, POST /ingest)
                                                       │ state    ~/.local/state/herdr-engineering/fleet-watch/{state.json,outbox.jsonl,lock}
                                                       ▼ ssh (tailscale, BatchMode, timeouts)
              beast (local) ─► bender ─► macbook-pro:  herdr-eng fleet host-apply --rollout ID --repo R --sha S
                                                       │ (installed build; pre-check, apply, health, JSON result on stdout)
control plane: POST /api/v1/ingest (entity_type=fleet_rollout) ─► repo events ─► GET /api/v1/fleet-rollouts[...]
dashboard:     #/fabric-management  (packages/fleet-rollout-view.js)
```

New module layout is a package `herdr_engineering/fleet_watch/` so `fleet.py` (540 lines) does not grow.
`host-apply` is a thin new `herdr-eng fleet` subcommand that composes existing functions per repo:

| repo | host-apply action | health check (all must pass) |
|---|---|---|
| `herdr-engineering` | pre-check (D6); `fleet_update(host=<this>, revision=sha, pull_controller=<is controller>)` equivalent for a non-controller: ff-only fetch of the deployment checkout, `pip install` into the venv, converge agent; report before/after from `git rev-parse HEAD` and `herdr-eng --version` | installed revision == sha (`fleet_status` fingerprint), fabric-agent unit/launchd label active, gateway registration present, `herdr-eng fleet status` parity ok |
| `pi-engineering` | `stage_pi_runtime(sha)` then symlink switch (D7) | release dir exists and complete, `current` -> sha, `pi --version` smoke from the new release in a throwaway cwd, no session signalled |
| `autospec` | the existing `autospec-fleet.yml` path (ff-only checkout + `autospec-runtime-install.sh`, generation publish) | active generation receipt names sha; `autospec --version` |
| `ai-sync` / `claude-config` (phase 2) | `ai_sync_enforce` forced pull + verify; **beast is the publisher, so its step is verify-only** (the enforcer already refuses there) and the canary shifts to bender for this repo | `ai-sync verify --json` ok, feed `behind == 0` |

Because Ansible is the existing convergence engine, `host-apply` for `herdr-engineering` on remote hosts
may run `ansible-playbook -l <host>` from beast (as `fleet_update` does) rather than on the host; the
interface (before/after, status, health, log tail) is identical either way and is fixed here so the
implementation choice does not leak into events.

## 6. State machine

### 6.1 Rollout states

```
            ┌────────── newer head, not started ──────────► superseded
 observed ─► verified ─► planned ─► running ─┬─ all ok ───────► succeeded ─► (lock written)
   │            │                              ├─ first non-ok ► halted ───┬─ operator resume ─► running
   │            └─ not on origin/main ─► refused                           ├─ operator abandon ─► abandoned
   └─ same heads, already succeeded ─► (no-op)                             └─ newer head ─► superseded + new rollout
```

### 6.2 Per-host states (one row per rollout x host x repo)

`pending -> precheck -> applying -> checking -> ok` and the terminal alternatives
`blocked` (D6), `failed` (apply/health), `unreachable`, `skipped` (halted | optional_unreachable | not_applicable),
`rolled_back` (D8 after `failed`). Timeouts: precheck 60 s, apply 30 min (matches the 1 h unit limit minus
margin), health 120 s, ssh connect 15 s. A timeout is `failed(reason=timeout)`. The runner is a loop over a
durable state file: after a crash it resumes at the first host not in a terminal state, re-running the
idempotent `host-apply` (it compares installed sha with target first and returns `ok(no_change)`).

### 6.3 Concurrency

One watcher (flock). `host-apply` takes the same `fleet-auto-update.lock` the nightly timer uses, so a
timer run and a watcher run on one host cannot overlap; a held lock is `blocked(reason=busy)` and is
retried after 60 s up to 5 times before halting.

### 6.4 Relation to `fleet rollout` (Pi session reload)

`fleet rollout plan/apply/run` reconciles *live sessions* and has its own durable state, receipts and
"operator attention" semantics. The watcher does not call it by default. A later opt-in
(`pi_activation: reconcile`) creates a plan for the staged SHA and records its rollout id on the host event
(`links.pi_session_rollout`), so the dashboard can link to it.

## 7. fleet.lock.yaml semantics

The lock's `repos:` section maps repository name to the SHA the fabric is declared to be on; other sections
pin tool versions. Rules enforced by a single writer function `lock.pin_repos(updates)`:

1. Write only `repos:` keys; leave every other byte and comment alone (round-trip YAML, atomic temp +
   `fsync` + `rename`, mode preserved, previous file saved as `fleet.lock.yaml.bak-<UTC date>`).
2. Every SHA must pass D3 (GitHub says it exists on `main`, and `is-ancestor origin/main` holds); one
   failing SHA refuses the whole write and emits `lock.refused`. This also guards manual pins via
   `herdr-eng fleet lock verify` (read-only; exits non-zero naming each unreachable SHA - would have caught
   `autospec: 6b7b8eb0...`).
3. The lock moves **after** a rollout is `succeeded` (all hosts ok or optional-skipped), so the lock always
   means "converged", never "wanted". A halted rollout leaves it untouched.
4. No downgrade without `--allow-downgrade` (target must not be an ancestor of the current pin).
5. `herdr-fleet apply` is not invoked (plan/check only upstream; see section 3).

## 8. Data model

### 8.1 Events (`entity_type = "fleet_rollout"`, `entity_id = rollout_id`)

Added to ingest like `ai_sync`: `validate_fleet_rollout(entity_id, event_type, payload)` with a strict
allow-list, rejecting unknown fields; one row per event in the existing event store (no new SQL table is
required because `repo.record_event`/`stream` already persist append-only streams; a derived read model is
built in memory like `list_ai_sync`). Envelope `event_id` is deterministic
(`<rollout_id>:<seq>`), so replays from the outbox are idempotent (per `event-id-per-actor-rollout.md`).

Event types: `rollout.observed`, `rollout.planned`, `rollout.started`, `host.started`, `host.step`,
`host.finished`, `rollout.succeeded`, `rollout.halted`, `rollout.superseded`, `rollout.abandoned`,
`rollout.resumed`, `lock.pinned`, `lock.refused`, `source.refused`, `source.degraded`, `source.recovered`.

JSON schema (common fields; per-type subsets in the validator):

```json
{
  "rollout_id": "str[16]",
  "ts": "ISO-8601 UTC",
  "seq": "int >= 0",
  "trigger": {"repo": "autospec|pi-engineering|herdr-engineering|ai-sync|claude-config",
              "sha": "40 hex", "pr": "int|null", "pr_title": "str<=120|null", "merged_at": "ISO|null"},
  "targets": [{"repo": "str", "sha": "40 hex", "before": "40 hex|null"}],
  "host": "beast|bender|macbook-pro (inventory name, [a-z0-9-]{1,40})",
  "repo": "str (for host events)",
  "status": "pending|precheck|applying|checking|ok|blocked|failed|unreachable|skipped|rolled_back",
  "reason": "enum: dirty_checkout|not_on_main|unpublished_commits|busy|timeout|health_failed|apply_failed|ssh_failed|halted|optional_unreachable|not_published|rate_limited|operator",
  "before_sha": "40 hex|null", "after_sha": "40 hex|null",
  "started_at": "ISO", "finished_at": "ISO", "duration_ms": "int >= 0",
  "checks": [{"name": "str<=40", "ok": "bool"}],
  "log_tail": "str<=2048, <=40 lines, redacted",
  "counts": {"dirty_paths": "int", "unpublished_commits": "int", "behind": "int"},
  "links": {"pi_session_rollout": "str|null"},
  "halted_at_host": "str|null"
}
```

Rules: `reason` and `status` are closed enums (the mermaid generator only ever interpolates enums, short
SHAs, inventory host names and dates, never free text); `log_tail` is stored but never placed in a diagram.

**No secrets.** `log_tail` passes through the existing `redact()` (userinfo URLs, 24+ character token-like
runs, non-printables, whitespace-collapsed) plus a denylist for `Authorization`, `token=`, `password=`, `-----BEGIN`;
environment is never logged; the watcher's GitHub credential comes from `gh auth` on beast and never enters
a payload; the ingest validator re-applies the length caps and rejects strings matching the denylist so a buggy
emitter cannot leak.

### 8.2 Read API

- `GET /api/v1/fleet-rollouts?limit=50&repo=` -> `{"rollouts": [summary...]}` (ETag, like ai-sync).
- `GET /api/v1/fleet-rollouts/{id}` -> summary + ordered host rows + events.
- `GET /api/v1/fleet-rollouts/versions` -> `{"hosts":[{"host","repos":{"<repo>":{"sha","since","rollout_id","status"}}}], "main_heads":{...}, "lock":{...}}`
  (projection: latest `host.finished` with `ok|rolled_back` per host x repo; `main_heads` from the latest
  `rollout.observed`; combined with `fleet_status` fingerprints from #95 for hosts that have never been
  rolled by the watcher).
- `GET /api/v1/fleet-rollouts/diagrams?kind=flow|timeline|versions&id=` -> `{"source": "<mermaid>", "svg": "<svg>" | null, "error": null|str}`.

## 9. Dashboard: `#/fabric-management`

Nav: new tab in the Fabric group (`SECTION_TABS`: Hosts, Scheduling, AI sync, **Fabric management**),
with `ROUTES['/fabric-management']`, `match` entry on the Hosts item, `viewFabricManagement` in `app.js`,
module `packages/fleet-rollout-view.js` (pure `rolloutRows(data, now)` + `mountFleetRollouts`) using
`esc()` for every field, `pollWhileVisible`, `<herdr-freshness>`, loading/empty/error states as required by
the UI contract (#601), and a unit test next to it like `ai-sync-view.test.js`. Route
`#/fabric-management/<rollout_id>` opens a rollout.

Layout (top to bottom):

1. **Status strip** - watcher heartbeat (age; warn at 3 poll intervals, stale at 15 min), per-repo
   `main` head vs last converged (short SHA, "N behind"), current rollout badge
   (`running host bender`, `halted at bender: health_failed`) with `Resume` / `Abandon` shown only to an
   authenticated operator (calls a POST that only enqueues an operator command for the watcher; the
   dashboard never runs anything itself).
2. **Diagram A - Rollout flow (per event/rollout).** `flowchart LR` generated for the selected (default
   latest) rollout. Nodes: `Merge repo#PR sha7` -> `Verify on origin/main` -> `beast` -> `bender` ->
   `macbook-pro` -> `Pin lock`; each host node carries a class by status (`ok`, `failed`, `blocked`,
   `skipped`, `running`); on halt the remaining edges are dashed to `Halted: reason`. Source: events of that
   `rollout_id`.
3. **Diagram B - Timeline of rollouts.** `gantt`, one section per host, one bar per rollout x repo from
   `host.started` to `host.finished` (state tags `done`, `crit` for failed, `active`), `dateFormat
   YYYY-MM-DDTHH:mm:ss`, range = last 7 days (selector 24 h / 7 d / 30 d). Source: `GET .../diagrams?kind=timeline`.
4. **Diagram C - Per-host version state.** `flowchart TB`: for each repo a subgraph holding the `origin/main`
   head node and one node per host labelled `host sha7 (age)`, an edge `head --> host` solid when equal,
   dashed red when behind, plus the lock pin as a note node. Source: `.../versions` projection.
5. **Event table** (below the diagrams, because diagrams are lossy): time, rollout, host, repo, status,
   before -> after, duration, expandable redacted log tail. Filters: repo, host, status.
6. **Mermaid source** in a `<details>` per diagram for copy/paste into a PR or issue.

### 9.1 How Mermaid is rendered (decision and justification)

Chosen: **server-side generation of Mermaid text plus server-side SVG**, via the existing dependency-free
renderer, extended with a bounded `gantt` subset (PR 6) and `classDef`/`class` status colouring for
flowcharts if not already supported. Reasons:

- The existing path (`artifact_visual_renderers.render_mermaid`) already does this safely for artifacts and
  deliberately keeps unsupported syntax inert; reuse beats a second diagram stack.
- No new third-party JavaScript in a dashboard that handles operator credentials: no vendored multi-MB
  mermaid bundle to pin, audit and update, no `eval`-style layout code, no need to weaken any CSP
  (`script-src 'self'` stays valid; SVG is inserted from a trusted same-origin API response whose labels are
  enum/short-SHA/host-name only, with the renderer escaping text).
- Works identically on iOS Safari and in the existing no-bundler `packages/*.js` module setup; renders
  deterministically so it is testable with a golden-SVG test.
- Fallback: if the renderer raises `ValueError` the response carries `svg: null, error` and the page shows
  the Mermaid source and the event table, so the page is never blank.

Rejected for v1: vendoring pinned `mermaid@11.x` (supply chain + size + CSP surface; reconsider only if
the bounded renderer cannot express the diagrams); CDN loading (violates "private, no external
dependency"); client-rendered diagrams from untrusted strings.

## 10. Failure and rollback behaviour (summary)

| situation | host status | rollout | action |
|---|---|---|---|
| SHA not on origin/main | n/a | `refused` | event `source.refused`; nothing runs, lock untouched |
| dirty / not main / unpublished commits | `blocked` | `halted` | counts + <=8 paths recorded; operator fixes; `resume` |
| ssh/tailscale auth prompt, host down (beast/bender) | `unreachable` | `halted` | retried only by `resume` or the nightly timer |
| `macbook-pro` down | `skipped(optional_unreachable)` | continues | shown amber; catches up via its launchd timer |
| apply error / health check fails | `failed` -> `rolled_back` (if pointer-flip safe) else `failed` | `halted` | later hosts `skipped(halted)`; lock unchanged |
| two merges in quick succession | n/a | coalesced (D4) | exactly one follow-up |
| control plane down | n/a | unaffected | events spool to outbox, replay by `event_id` |
| watcher crash mid-host | host state durable | resumes | idempotent `host-apply` re-checks installed sha first |
| GitHub rate limit | n/a | none starts | `source.degraded`, backoff per D2 |

## 11. Test plan (real dependencies, no mocks; four layers)

- **Unit** (pytest/vitest, pure functions): ETag/`304` header parsing and backoff schedule from recorded
  real `gh api -i` outputs kept as fixtures; coalescing and `rollout_id` determinism; state-machine
  transitions table (every illegal transition rejected); event validator (unknown field, bad enum, oversize,
  secret denylist, `host_id` bounds); `redact` of tokens/userinfo; lock writer round-trip preserves comments
  and unrelated keys; mermaid generators produce only enum/short-SHA/host text (property test over
  adversarial `pr_title`); `fleet-rollout-view.js` row/escape tests like `ai-sync-view.test.js`.
- **Integration** (real git, real SQLite/Postgres event store, real HTTP): bare repos created in a temp dir
  act as "GitHub" through a real local HTTP server that implements the three used REST endpoints with ETag and
  rate-limit headers (a protocol-faithful fixture server, not a mock of our code); a real `git` pair proves
  D3 (stray unpushed commit refused, ancestor accepted, force-pushed-away SHA refused); real flock contention
  with the nightly updater lock; control plane started for real and queried through `/api/v1/fleet-rollouts*`;
  outbox replay after killing the control plane; dirty-checkout pre-check against a real dirty repo.
- **Smoke** (`scripts/validate.sh` target + CI): `herdr-eng fleet-watch once --dry-run` against the fixture
  server; `fleet lock verify` on a sample lock; unit-file lint (`systemd-analyze verify`) for the service.
- **E2E**: Playwright CLI (`npx playwright test`, never MCP) loads `#/fabric-management` against a control
  plane seeded with a three-host rollout (success, halted-at-bender, superseded) and asserts the three SVG
  diagrams render, status classes, empty/error/stale states, and that an XSS-style `pr_title` appears
  escaped. A **real-host canary** (documented runbook, not CI): disposable branch on a fork repo merged, watcher
  rolls beast then a second Linux target, evidence recorded in `STATUS.md`. macOS path is verified live on
  `macbook-pro` only; CI can lint the plist but cannot prove macOS behaviour, and the PR says so.
- 80% coverage gate applies to the new package; CI must stay green on `main` first (fix-forward policy).

## 12. Phased PR breakdown (each small, mergeable alone, off by default)

1. **Spec** (this PR).
2. **`fleet_watch` core, read-only**: poller (ETag, rate limits), D3 verifier, state file, `fleet-watch once
   --dry-run` printing planned rollouts; no ssh, no events. Needs #610 merged.
3. **Event model**: `validate_fleet_rollout`, ingest dispatch, `repo.list_fleet_rollouts`, read endpoints,
   emitter + outbox. No dashboard yet; curl-testable.
4. **`host-apply` + runner**: D6 pre-check, per-repo actions and health checks, canary order, halt, rollback,
   `fleet-watch run` still manual; real two-host integration test.
5. **Service + lock**: systemd user unit (+ ansible task, off unless `fleet_watch_enabled: true` on beast),
   `lock.pin_repos`, `fleet lock verify`, operator `resume/abandon` commands.
6. **Dashboard page**: route, view module, event table, status strip, version-state diagram (flowchart only).
7. **Diagrams**: server-side diagram endpoints, rollout-flow diagram, `gantt` subset in the renderer
   (after #600 merges), golden-SVG tests, Playwright spec.
8. **Pi activation + ai-sync/claude-config**: symlink-switch activation, optional `pi_activation:
   reconcile`, publisher-aware ai-sync step, docs (`docs/operations/fleet-rollout-watcher.md`) and the
   live canary evidence.

## 13. Open questions

1. Should the lock move only after *all* hosts succeed (proposed, section 7) or per host? Per-host is more
   truthful but makes the lock mean "partially converged".
2. `rollback_scope` default: failed host only (proposed) vs all updated hosts when the canary passes but
   bender fails.
3. Is a 120 s poll acceptable, or should merges to `main` be detected faster by adding a `repository_dispatch`
   relay later (still no inbound hole: a GitHub Action could write a tag the poller sees)? GitHub Actions
   was removed for billing (pi-eng, 2026-10-30), so assumed no.
4. `claude-config`/`ai-sync`: confirm the exact repo names and visibility (`ai-sync` resolves on GitHub;
   `claude-config` resolved to a head SHA via `gh`, access to be confirmed from beast's credential), and that
   a forced ai-sync pull on bender/macbook-pro is acceptable as an *automatic* action (today it is opt-in per
   host via `ai_sync_enforce`).
5. Should a `macbook-pro` that is asleep be retried by the watcher (e.g. hourly for 24 h) or left to its
   launchd timer?
6. Which docs-only or CI-only merges should be ignored (path filter via `compare` API) to avoid pointless
   rollouts? Proposed: none ignored in v1; revisit with data from the timeline.
7. Operator `Resume/Abandon` from the dashboard needs a write path with authentication; confirm the
   existing operator-principal rules in `ingest.py` are sufficient or defer buttons to CLI-only in v1.
8. Should `herdr-eng` on non-controller hosts own a checkout at all, or move to release-directory installs
   (atomic switch like Pi) so the dirty-checkout hazard disappears instead of being detected?

## 14. Decision record: no free text in rollout events (2026-11-05)

Section 8.1 specified a redacted `log_tail` (and a `pr_title`, free-form check names) with a denylist plus the existing
`redact()`. Step 3 implemented that and it was reviewed three times; each round found secret shapes that survived or
were stored (25 of 70 new shapes readable back in round 3, with 17.5% of ordinary lines wrongly withheld). Pattern
redaction of arbitrary command output cannot be made safe, and the event store is readable by everyone with dashboard
access. **The user chose option 1: remove all free text from the event model.**

- `log_tail`, `pr_title` and `checks[].name` as free text are deleted. `checks[]` becomes `{name (closed enum), status
  (ok|failed|skipped), duration_ms}`; the PR is identified by `trigger.pr` (int) and `trigger.sha`.
- A log is a reference `log = {host, path_id (updater|installer|healthcheck), sha256 (64 hex), bytes, lines}`. The text
  stays in a 0600 store on the host that captured it and is read with `herdr-eng fleet-watch log show <rollout_id> --host H`.
- Every remaining string is an enum, a fleet host (`beast|bender|macbook-pro`), lowercase hex, or a UTC timestamp;
  unknown fields are rejected. Secret-free is a property of the schema, proven by tests that put hundreds of
  secret-shaped strings into every field and expect refusal.
- The dashboard (step 6) shows the log reference and the host-side command, not the text.
- Related fix: `seq` counters are never pruned (a resumed or recovered stream must not restart at 0).
