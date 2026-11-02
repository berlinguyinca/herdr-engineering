# STATUS — 2026-11-02

**Release readiness: BLOCKED.** No reachable Hindsight endpoint or credentials on the execution host (`Gerts-MacBook-Pro`); no live cross-host verification has occurred. **All results below are MOCKED / local unit tests unless stated.** Nothing is merged or deployed.

| Phase | PR | Commits | Tests (mocked) | State |
|---|---|---|---|---|
| P00 audit | this branch `feat/unified-hindsight-memory` | 56ae9256 | n/a | done; `audit/LIVE_EVIDENCE.md`, `CONTRACT.md` |
| P01 client | herdr-memory#1 | 3501dd2 4a22fda a014489 813bf23 087bdfe | 57 pass / 4 skip (live integration) — re-run by orchestrator | open |
| P01b Pi provider | pi-engineering#556 | 16a43722 86789116 65023098 6bcc7345 | 18 new pass; 142 targeted pass; full unit suite has 775 macOS-env failures, no baseline diff | open |
| P02 installer | herdr-engineering#560 | 8863390f | 19 pass (re-run); 5+1 failures also on main per agent | open |
| P03 handoff | herdr-handoff#1, herdr-engineering#562 | 65bd45c, 3ff6c7ea | 27 pass (re-run); 43 targeted pass | open; conformed to real P01 CLI (handoff c94c85c, control-plane 0c2b2b4b); 30 pass incl. 2 real-CLI tests vs a fake Hindsight server |
| P04 Explorer | herdr-engineering#563 | 2f44a878 cb5d52ba 33ce4ba2 1b491a94 | 13 API pass (re-run in venv), 1046 JS, 1 Playwright; mocked data | open |
| P05 intelligence | — | — | — | NOT STARTED (needs reliable P01 on a live server) |
| P06 canary | — | — | — | NOT STARTED (needs authorization + endpoint) |

## Known limitations / open items
- Hindsight has no native revision history: `correct` overwrites the document, supersession is metadata + local ledger; `--as-of` cannot reconstruct replaced text. Acceptance #4 only partly met. Assumed endpoints `GET /documents/{id}` and `GET /health` are unverified live.
- Automatic lifecycle: only Claude has native hooks (recall injection); Codex/OpenCode/Pi are MCP/skill fallback; no harness retains automatically.
- Pi promotion flow does not specially handle `queued_durable`; `recallAll()` uses `recall "*"`.
- Install path for the private `herdr-memory` package is undecided; lock pin `c4e0c64` must move to the P01 merge commit.
- Dashboard integration/retrieval-history panels show UNKNOWN (contract v1 has no data). P04 assumes `recall --project` and a `get` shape not in P01 — to verify.
- Rakazo pre-outbox queue drop (`rakazo_hindsight.py`) not yet fixed.
- Handoff CLI usage fixed to match P01 (2 real-CLI tests; Hindsight server still a local fake).
- Cross-host cache invalidation for shared global/infra memory only on reconnect.
- Security: a Context7 API key was printed to an agent's tool output (not persisted); rotation recommended.

## Required to unblock release
1. A reachable Hindsight deployment + scoped credentials (hosts `beast`/`bender` unprobed), authorization to run a single-host canary.
2. Live run of acceptance #2–#6 across two hosts.
3. Review and merge order: herdr-memory#1 → pin bump → #560/#562/#563/handoff#1/pi#556.
