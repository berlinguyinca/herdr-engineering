# Active-session scratch retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans task-by-task. Steps use checkbox syntax.

**Goal:** Reclaim verified disposable build output inside active scratch sessions without deleting live or unique work.

**Architecture:** Typed structural discovery and filesystem inspection produce candidates. A policy planner selects by TTL/byte budget/free space, and an exclusively locked executor revalidates/claims/removes them. General session quarantine retains its recovery guard and gets bounded admission. A persistent isolated runtime runs the CLI on a bounded schedule.

**Tech Stack:** Python3.12 stdlib, real Git/lsof/subprocesses/file locks, macOS launchd, pytest and platform CI.

**Spec:** `docs/superpowers/specs/2026-11-13-active-scratch-retention-design.md`

## Global Constraints

- Defaults:6h outputTTL,30min pressure idle grace,32GiB generated-output budget,64GiB free-space floor,48h abandoned-sessionTTL,7day/4GiB quarantine,15min schedule.
- Never follow symlinks or cross devices; protect sources, diagnostics, `.keep`, open/argv/cwd use, active leases and unverifiable state. Dry-run is default.
- No mocks; real filesystem/process/Git tests. Exact-head review and green applicable CI before merge/install.

## Review Focus

- Cache beneath a live UUID must be independently eligible while its other files remain intact.
- A build can hold a directory/argv reference between file opens; guard the whole candidate, not only leaf handles.
- A renamed/replaced path must not cause a different inode's data to be deleted.
- Sparse/symlinked/nested output must not inflate budgets, overlap or escape the root.
- Moving data on the same disk does not reclaim it; distinguish removed blocks, observed free-space gain and admitted quarantine bytes.

### Task1: Discovery, guards and policy

**Files:** create `herdr_engineering/scratch_reaper.py`, `tests/test_scratch_reaper.py` (split filesystem/process tests if needed).

**Interfaces:** typed `Policy`, `Candidate`, `Report`; `run_once(root: Path, quarantine: Path, state_dir: Path, policy: Policy, *, apply: bool=False, gate_state: Path | None=None, live_roots: list[Path] | None=None) -> Report`; `main(argv: list[str] | None=None) -> int`.

- [x] Add real-file recognition tests for numbered/nested DerivedData, Cargo and SwiftPM metadata; source/diagnostic retention, UUID isolation and overlap.
- [x] Add pure-policy/real-files budget and pressure tests with explicit small limits, plus invalid-policy cases; run missing-module RED.
- [x] Implement bounded discovery/inspection and structured status; keep unknowns untouched.
- [x] Add real subprocess/Git/lsof/lock tests for open files, cwd, argv, maintenance exclusion, unreadable state, `.keep`, dirty/unpushed repos and symlink escape.
- [x] Implement fail-closed guards and same-filesystem claim/revalidation/removal; prove RED→GREEN.
- [x] Test general-session quarantine admission, expiry and safety using real files with explicit short test TTLs; preserve7day production default.
- [x] Add real CLI smoke/E2E tests and native compiler artifact acceptance; verify no source/evidence loss and no orphaned processes.

### Task2: Ship and install

**Files:** modify `pyproject.toml` entrypoint; add operations doc, `.coveragerc-scratch-reaper`, platform GitHub workflow and a scheduled-run template as needed.

- [ ] Add lint/type/80% coverage/wheel gates and installer/template verification, commit and obtain exact-head Opus review.
- [ ] Fix findings and required/full applicable CI, merge normally (including updated main), build/install only the merged persistent runtime.
- [ ] Back up old launcher/plist, load new isolated15min job, capture live dry-run then apply reports, and verify pressure/guard outcomes and retained sources/evidence.
- [ ] Save complete test/review/runtime evidence, clean owned worktrees/artifacts, and audit the three-part goal before completing it.

## Implementation decisions from reconnaissance

- `Candidate` must retain the generated-format **anchor** (DerivedData root / Cargo target / SwiftPM scratch root), component path, source-root hint, kind, scope, newest timestamp, allocated bytes and `(device,inode)`. Process references at the anchor protect all its components; a live session UUID alone must not protect those components.
- Inspect Xcode `info.plist` using plistlib; actual samples have `WorkspacePath` and `LastAccessedDate`. The numbered quarantined roots are genuine DerivedData. `v13` is a mixed evidence folder whose `dd`/`ddrel` descendants must be discovered without deleting `v13`.
- Generated DerivedData components: Build, CompilationCache.noindex, Index.noindex, ModuleCache.noindex, SDKExplicitPrecompiledModules, SDKStatCaches.noindex; clean dependency caches may be managed only with Git/edit-state guards. Keep Logs, TestResults and info.plist.
- Split SwiftPM generated platform/plugin output from workspace-state and edited dependency state; do not let bare dependency repositories prevent reclaiming unrelated compiler output. Protect an edited/unverifiable dependency cache rather than deleting its state.
- Include anchor info.plist activity in idle decisions. Own component renames update the parent directory; do not mistake those self-generated ctimes for new build activity. After claim ignore only the claimed root's rename ctime, never descendant activity.
- Use `lsof -F0pcnft` plus process argv. Never print/persist command lines (may contain credentials). Cwd/FD/mmap paths inside the anchor and literal anchor argv references protect it. Compiler cwd/source references also protect the appropriate project. Generic parent cwd such as `/Users` must not pin every descendant.
- Runtime probes must exclude the reaper/inspection subprocesses themselves. A missing/failed process tool or unreadable relevant registry blocks apply. Defaults share the existing build-gates maintenance lock; tests pass an isolated `gate_state`.
- Real fixtures can express session activity with recent UUID-named JSONL files through `live_roots`; no real user log is modified. Keep independent general-session liveness scanning bounded (recent tails rather than64MiB per file).
- General quarantine candidates are UUID session dirs, not whole project wrappers; otherwise an old wrapper could swallow a live sibling. Unknown top-level names stay intact. Apply cache removal first, then recompute general-session safety/admission using current contents.
- Claim only on the same device into private state, verify the inode after rename, and check original and claimed paths for renewed activity. Restore a protected or replaced claim without overwriting a newly created source path. Refuse cross-device copy fallback.
- Each report path/list is bounded; include true aggregate counts even when details are truncated. Store one atomic status file, not an append-only log. Keep the old launcher/plist as rollback evidence.
- Extend tests with real Git remote/push/dirty/stash cases, maintenance/lease locks, missing-tool fail-closed subprocess environment, replaced-inode safety, actual free-space pressure, bounded quarantine, and native Rust/Swift artifacts. Initial13 filesystem/process cases are missing-module RED; no product implementation exists yet.
