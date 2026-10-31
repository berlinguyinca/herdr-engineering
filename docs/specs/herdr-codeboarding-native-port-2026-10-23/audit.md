# Audit — Herdr Architecture Explorer (A01–A04)

Date: 2026-10-23. Auditor: Claude (Sonnet 5.5) on `bender`. Read-only audit; no implementation code was written before this file.

## A01 Pinned revisions

| Repo | Revision | Notes |
|---|---|---|
| herdr-engineering | `origin/main` `8c29a13e724cc9b74f21fbfd1968cabde9496be9` | Implementation worktree `../herdr-engineering-arch`, branch `feat/architecture-explorer`. |
| herdr-engineering (user checkout) | `main` `88c4d5e9efe700f9641bc408e5b3504fc124c0b0`, 77 commits behind origin | Untouched. Untracked: `docs/superpowers/plans/2026-10-22-review-gate-core.md`, `docs/superpowers/specs/2026-10-22-automated-review-merge-gate-design.md`. |
| pi-engineering | `origin/main` `96dfd2be259747739cdb4618e96af3f709d96409` | User checkout on `fix/pr372-reconstructed-candidate-replay` @ `513f7819`, untracked `.autospec/`. Untouched; read via `git show origin/main:`. |
| CodeBoarding | `647fe6a728169ac442ae24a5feeba50784ea2231` (2026-10-22) | v0.15.2, MIT, "Copyright (c) 2025 CodeBoarding". No NOTICE file. |

Spec archive SHA-256 `30ecc02c02ba8f15e5623625f37e0b6848e2d2462f50c4a7290372e77d620b99` (verified Mac == bender).

### Upstream licensing, dependencies, network, telemetry
- MIT: must keep copyright + permission notice in vendored files; add `THIRD_PARTY_NOTICES` entry pinned to `647fe6a7`. Runtime deps carry their own licences (pyright MIT, tree-sitter MIT, networkx BSD). Downloaded servers (jdtls EPL, kotlin-lsp) are NOT covered and are not shipped.
- **Telemetry is ON by default** (`infra/monitoring/telemetry/service.py:28,79`, PostHog US, machine fingerprint, `$exception` may include source snippets). Call sites inside the subset path: `static_analyzer/scanner.py:6`, `static_analyzer/__init__.py` (`lsp_analysis_result`). Decision: **reject** the telemetry package; vendor without it and stub `track_*`. Do not rely on `CODEBOARDING_TELEMETRY=false` alone.
- Network/installers (reject): `infra/tool_registry/*`, `infra/install.py`, `workspace_dependencies.py` (npm/pnpm/yarn install in analysed repo; lifecycle scripts risk), `dotnet_sdk.py`, `codeboarding_workflows/sources/remote.py`, `upload_onboarding_materials`, `clone_repository`.
- **Security defect upstream:** `static_analyzer/analysis_cache.py:237` does `pickle.load` of `<repo>/.codeboarding/static_analysis.pkl` — attacker-controlled when analysing a hostile repo. **Reject**; port the design only, with JSON cache in worker-owned storage outside the repo.
- No upstream engine emits import edges (`EdgeKind.IMPORT` unused; `package_relations` is `{}`). Call edges require a live LSP (Python: `pyright-langserver`, needs Node + npm package).
- LLM-dependent (reject): `agents/`, `diagram_analysis/diagram_generator.py`, `health/`, langchain stack, component names/descriptions in `analysis.json`.

### Upstream reuse decisions
| Upstream component | Decision |
|---|---|
| `static_analyzer/cfg/*`, `node.py`, `graph_definitions.py`, models | Port selectively (data shapes: node line_start/line_end/col, `Edge.call_sites {line,column}`) |
| `engine/source_inspector.py` (tree-sitter) | Deferred (P1): candidate for call-site spans |
| `engine/*` LSP pipeline + `python_adapter.py` | Deferred (P1, optional capability `calls`); requires node+pyright in worker image, no network |
| `agents/content_hash.py`, `codeboarding_workflows/fingerprint_diff.py` | Port (E01 incremental) |
| `analysis_cache.py` | Design only; replace pickle |
| `clustering/` | Deferred; verify no-LLM path (unconfirmed) |
| everything in the Reject list above | Reject |

## A02 Herdr audit (HEAD 8c29a13)

Nothing architecture-related exists (no code-graph, tree-sitter, snapshot of code). `SnapshotStore`/`state_snapshots` (`control_plane/snapshots.py`) are **mutable** upserted recovery caches → rejected for graphs.

| Concern | Existing symbol | Verdict |
|---|---|---|
| Event envelope/idempotency | `events.Event` (`events.py:13`), `make_event` (`:30`), `EventStore.append` (`:70`, idempotent by `event_id`, swallows errors→False), `ControlPlaneRepo.record_event` (`repo.py:1567`, `require_durable`) | **Reuse**; deterministic `event_id` from snapshot key; `require_durable=True` for transitions |
| Forged-stream protection | `ingest.py:55` + `review_gate/entities.RESERVED_ENTITY_PREFIXES` | **Extend**: reserve `architecture*` so `/api/v1/ingest` can't forge results |
| Artifacts | `RustFSClient` (`rustfs.py`), `artifacts`/`artifact_bindings` tables, `ControlPlaneRepo.register_artifact` (`repo.py:1368`), `/api/v1/artifacts`, `docs/contracts/artifact-ref.md` | **Reuse** for graph blob (content-addressed ⇒ graph hash) |
| Job mechanism | `review_gate/queue.py` `ReviewQueue` (event-sourced lease, dedupe); `worker.py` is only a stale-record sweeper | **Extend pattern** (not worker.py) |
| Repo identity / SHA validation | `project_links.canonical_repository` (`:43`), `ingest._git_identity` (`:19`) | **Reuse** |
| Routing/auth | `_api_get`/`_api_post` if-ladder, `_require(role)`, Origin+JSON guard (`api.py:769-776`), `_AGENT_POST` (`:34`), `_qint` | **Extend** with `resource == "architecture"` |
| Host/repo/guest ACL | Legacy `/api/v1`: viewer/operator/ingest/agent only, **no repo/host ACL**. HCN `ROLE_ACTIONS`/`_authorize`/`_effective` (`collab.py:40,651,668`) has guest/contributor roles | Gap. Decision below |
| Feature flag | fleet config `control_plane.<section>` strict loader (`review_gate/config.py`), `_health` capabilities (`api.py:342`) | **Reuse pattern**: `architecture.enabled=false`, capability `architecture_v1` |
| Pagination | `limit/offset/has_more` (reviews), cursor (inspector, collab) | **Reuse** cursor+limit + explicit `incomplete` |
| Migrations | `deploy/control-plane/migrations/NNNN_name.sql`, forward-only, `db.Migrator`; no down-scripts | No table needed for P0 (events + artifact). Optional additive 0003 |
| Web nav | `packages/app.js` `NAV` (`:41`), `ROUTES` (`:77`), `mountX(root, client, opts)→{dispose}` | **Extend**; new `architecture-view.js` |
| Graph rendering | only `collab-flow.js` (layered, ≤4 levels, people/agents) | **Reuse conventions** (pure layout + SVG renderer, keyboard/ARIA, `esc`); **new** pure DAG layout; accessible table via existing list patterns. No charting lib |
| Timeline | `herdr-timeline` component | **Reuse** (D02) |
| UI states | `herdr-spinner/-empty/-error/-status` | **Reuse**; add stale/partial/unsupported |
| Design tokens | `theme.css`; **no dark theme** (dark only in `collab.css` via prefers-color-scheme) | **Extend** with dark tokens for the new view |
| Client | `HerdrClient.request` (`client.js`), `connectEvents` | **Extend** |
| Collab | `CollabService`; messages have no entity link; guests read-only, no flow | **Extend** (D03) with link record |
| GitHub/CI | `github.py` (repos/issues only), `CIMonitor`, `project_links.annotate_ci`, `review_gate` `PrKey`/`github_app` | **Reuse**; PR changed-files is a gap (D01) |
| Missions/lineage | `mission_activity*`, `sessions.parent_session_id`, `linkedAgents`, `record-state.js` | **Reuse**; `worktree_id` is a gap |

Tests/commands: `python3 -m pytest -q tests/control_plane tests/review_gate` (6 sample tests pass); `npm run test:client` works without node_modules; Playwright (`npm run test:ui`) needs `npm ci` or shared `node_modules` (present in the user's main checkout); browsers in `~/.cache/ms-playwright`; fixture servers in `tests/playwright/server.py` (prints `PORT=`).

## A03 Pi audit (origin/main 96dfd2be)

No code graph/snapshot/symbol IDs exist (only regex `RepoIntel.symbolIndex`). Identities: platform `Run` (`RUN-`), `Worker` (`WRK-`, `parentWorkerId`, free-form `worktree`), orchestration `Mission`/`Task`/`Execution`, Herdr runtime `HERD-xxxxxx`. **Platform Run is not linked to Mission/Task.** `resolveWorktreeIdentity()` (`src/runtime/isolation/WorktreeIdentity.ts:23-37`; `sha256(realpath(gitCommonDir)\0realpath(worktreeRoot))[:24]`) is the only stable worktree id and is unused on the Herdr path. Pi→Herdr ingest (`ControlPlaneIngest.ts`, `POST /api/v1/ingest`) has no event_id/idempotency key (entity upsert only); entities `host/mission/session/inspector`; PR only via `InspectorAssociation.pr`.

Correlation mapping and gaps (fields with NO source today, to be stored as unknown/null, never invented): `issue_ref`, `room_ref` (derivable from workspace/pane link only), `autospec_ref` (partial), `commit_sha` (not on events; from git), `pr_ref` (transient). `worktree_id`: compute on owning host via the same algorithm (port in Python) — decision: Herdr-side `worktree_id` = identical sha256 scheme so ids join with Pi.

**Pi changes: none planned.** Attribution is built Herdr-side from existing `mission`/`session` records (repository_id, commit_sha, cwd, parent_session_id).

## A04 Decisions, conflicts, flag, rollback

**Integration design (vertical slice B+C first):**
1. Host-local runner (`herdr_engineering/architecture/`): `analyze_worktree(path)` → normalized graph. P0 analyzer is a **no-LLM, no-network, stdlib `ast`** Python extractor producing file/module/class/function nodes with `contains` and `imports` edges, source spans, `extractor` + `confidence`; unresolved imports emitted as `inferred`/unresolved, never invented. Upstream-derived pieces (node/edge shape, content hashing) are ported with notices; the LSP call-graph is an optional later capability (`calls`), absent ⇒ snapshot `completeness: partial` with diagnostic. This follows the spec ("port selectively", Python worker, no rewrite) and avoids making Node/pyright a hard dependency.
2. Isolation: subprocess worker with sanitized env, `resource.setrlimit` (CPU/AS/FSIZE), wall timeout, cancellation, no network (analyzer does none; `unshare -n` when available), symlink/path-escape refusal via `realpath` containment, never executes repo code, file-size and count caps.
3. Snapshot identity: `snapshot_id = sha256(host_id, repository_id, commit_sha, worktree_id, analyzer_version, config_hash, toolchain_hash)`; graph is canonical JSON, graph hash = sha256 of normalized form; stored via existing artifact path; published via `architecture.snapshot.published` with deterministic `event_id`. Immutable; dirty worktrees are rejected or marked `dirty` with a tree hash in `config`.
4. API under `/api/v1/architecture/*` gated by flag + `architecture_v1` capability; reads = viewer, mutations = operator + Origin/JSON guard; `architecture` entity prefix reserved from generic ingest.
5. UI: `NAV` + `ROUTES` entry, `architecture-view.js`, `architecture-layout.js` (pure), table alternative.

**Authorization decision:** the legacy surface has no repo ACL, so P0 inherits existing roles (viewer reads, operator analyzes) and the whole surface is behind the off-by-default flag. Guest/collab scoping is implemented in D03 via HCN `_authorize` — **guests are denied on the legacy architecture routes by construction** (public listener exposes only collab paths, `api.py _public`). Acceptance gate "unauthorized guests cannot view/invoke/comment" will be tested at that boundary and recorded honestly.

**Conflicts with ongoing work:** sibling worktrees (`review-gate-core`, `collab-network`) touch `api.py`, `app.js`, `ingest.py`. Mitigation: keep edits to those files minimal (one branch/route hook each) and put logic in new modules.

**Feature flag / rollback:** `architecture.enabled: false` default; route returns 404 and capability absent when off; no migration in P0 (events + artifacts only) so rollback = flip flag / revert branch. If 0003 is added later it is additive `CREATE TABLE IF NOT EXISTS` with a documented drop in the rollback note.

**Honest scope limits recorded up front:** TESTS.md gates for full-vs-incremental parity, cross-repo, PR diff with GitHub, collaboration, and LLM explanations map to phases D/E and will be reported as not done unless implemented and passing. `calls` edges and non-Python languages are unsupported in P0.

## Why no duplicate is introduced
No existing architecture graph, renderer, snapshot, or analysis job exists (repo-wide grep, A02). Events, artifacts, routing, auth roles, flags, nav, components, timeline, client and GitHub/CI correlation are all reused; the only new code is the analyzer, snapshot identity/normalization, a DAG layout, and thin route/view hooks.
