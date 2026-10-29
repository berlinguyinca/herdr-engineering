# AUDIT — Herdr Project Intelligence & Autonomous Operations v1

Run id `20261028T175102Z-3897266`. Branch `feat/project-intelligence-autonomy-20261028T175102Z-3897266`, worktree `~/IdeaProjects/herdr-pia-<run-id>`, cut from `origin/main` @ `ad0aa79`.
Bundle SHA-256 `a27866b4498da36d825e78fdc17b3d2db1f2f1fbc9dc1226746bbfd43923081b` (Mac `~/Downloads` and bender copy match).

## Audited repositories
| Repo | Branch | SHA |
|---|---|---|
| herdr-engineering (audit base) | origin/main | `ad0aa79` (local main checkout `06e3d50`, dirty, ~40 uncommitted files — untouched) |
| herdr-artifacts | main | `c6156ea` |
| herdr-memory | main | `c4e0c64` |
| pi-engineering | fix/pr372-reconstructed-candidate-replay | `940a931` |
| inferweave | fix/upstream-scheme-strip | `1e03d13d` |

**Deployed capabilities: NOT VERIFIED.** `/api/v1/health` needs an API key; none was used. Everything below is "present in code", not "deployed".

## Capability matrix
| Capability | Source path | Status | Reuse approach | Gap / risk | Tests |
|---|---|---|---|---|---|
| Event log + envelope v2 | `control_plane/events.py`, `envelope.py`, `repo.record_event`, `sink.py`, migration 0003_event_envelope_v2 | present | Use as THE event log (`fabric_events`) | No tenant_id, supersedes_id, security_label; no inbox/outbox; no central event-type list | replay dedupe, envelope conflict |
| Inbox/outbox | — | missing | New tables (connector inbox, outbox) | exactly-once effect needs idempotent consumers | crash-between-effect-and-ack |
| Lineage graph | `graph.py` + `docs/contracts/lineage-events.md` | present, in-memory, explicit-only | Explicit links keep flowing through lineage events | Contract forbids inferred edges and a second source of truth | see DECISIONS D1 |
| Project / investigation / commitment / decision records | `project_links.py` (repo→project), `collab.py` projects (permission container) | missing as records | New tables; consume `project_links.py` | four meanings of "project" | scope isolation |
| Timeline | `repo.mission_timeline`, `graph.room_timeline`, `journal.py`, `mission-story-view.js` | partial (mission-scoped) | New project read model over events; reuse view style | no as-of queries | as-of, late arrival |
| DB / migrations | `control_plane/db.py`, `deploy/control-plane/migrations` | present | Postgres only; `-- DOWN:` comment convention | no automated down; PR #304 owns 0014–0016 → we use **0020** | migration up/down text test |
| Auth / RBAC | `api.py` roles viewer/operator/ingest/agent/reviewer | present | New routes via `_require` | only `pa_*` is tenant-scoped; assistant identity = trusted `X-Herdr-User` header | scope isolation |
| Feature flags / kill switch | `fleet_config.py`, `control_plane.<x>.enabled`, `collab_workbench` hot kill | present | `control_plane.project_intelligence.*`, default off, hot-read | — | flag-off 404 |
| Artifact registry | `artifacts.py` (`register_logical`, `commit_version`), `rustfs.py`, `artifact_acl.py`, `publication.py` | present | Register originals via `put_stream`+`commit_version` | `commit_version` emits no event; HTTP upload buffers whole body, no size cap | attachment fixture |
| RustFS/S3 | `deploy/control-plane/{config.yaml,compose.yaml}` | present (MinIO stand-in) | content-addressed keys | default creds `herdr` in client | — |
| Attachment safety | `artifact_sandbox.py`, `artifact_preview.py` | partial | Harden | no AV, no magic sniff, no thumbnails, no network isolation/rlimits | zip bomb, SVG script |
| Scientific parsers | — | missing | parser plugin interface only | no mzML/CSV/image parsers | fixture |
| OpenViking | pi-engineering `src/blackhole/OpenViking.ts` | partial, no delete API | see D5 | deletion propagation absent; herdr-memory is Hindsight with `forget()` + tombstones | deletion test |
| CI fix | `ci_fix.py`, `deploy/control-plane/ci-fix-runner.py` | present, click-triggered | Incident = projection over ci_fix records | PAT not App; no auto-trigger; no budgets/breaker | — |
| Review gate | `review_gate/*` | present | Put risk evaluator in front of `decide_merge` | direct App `PUT /merge`, no native auto-merge/queue; no risk classes; `required_checks` unused | negative policy fixtures |
| GitHub | `github.py` (PAT, read), `review_gate/github_app.py` (App) | partial | — | **no webhook receiver/signature verification** | tampered signature |
| Post-merge verify | `review_gate/main_verify.py` | partial | extend | notify only; no rollback request | — |
| Gmail / Calendar | — | missing | assistant `ConnectorAdapter` seam | no OAuth, no cursor, no token store | fake Gmail |
| Assistant tools | `assistant.py` `TOOL_CATALOG`, `request_tool_action` | present | add read tools + `herdr.dashboard.navigate` | `send_message`/`edit_shared`/`delegate` are stubs | contract tests |
| Notifications | `attention.py`, `attention_inbox.py`, `attention-notify.js` | partial (browser only) | add outbox channel | no email, no push, no device registration | dedupe/quiet hours |
| Deep links | `entity-links.js` | present | add kinds (project, investigation, event, evidence, approval) | no universal-link scheme | allowlist test |
| Host placement | `host_policy.py` (macOS → manual_only), `scheduler.py`, `scheduler_service.py` | present | call, never reimplement | scheduler does not launch Pi | host exclusion |
| Local-only egress | `llm_proxy.py` | missing | new data-class gate | none exists today | egress negative test |
| Dashboard | `packages/app.js` NAV/ROUTES, design system, SSE refresh bus | present | one NAV + one ROUTES entry, new view file | `app.js` dirty in main checkout → conflict risk | Playwright |

## In-flight work (no overlap found)
Open PRs: #304, #305, #307, #308, #316–#320, #322, #324, #325 — recovery, safety, architecture explorer, scheduler, memory. None touch timeline, email, CI-repair, project intelligence. #304 adds migrations 0014–0016. #317 touches assistant memory ownership (adjacent to retrieval).

## Environment
Python 3.12.3 (`python3`), Node 22; `python3 -m pytest tests/control_plane/...` works; integration tests need `PA_PG_DSN`. Playwright cache present on host; worktree needs `npm ci`.
