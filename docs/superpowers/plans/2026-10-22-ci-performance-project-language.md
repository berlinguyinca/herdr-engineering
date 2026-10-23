# Rust CI reuse and project/language performance implementation plan

> **For agentic workers:** Use native bounded implementation lanes; leader owns integration, independent review, live verification and rollout. Do not recursively delegate.

**Goal:** Extend the existing CI cache work to a measured Rust executable pilot and show meaningful run-time distributions and project/language summaries on HerdR's CI page.

**Architecture:** Keep compiled caches allocation-local and use bounded immutable Quobyte snapshots. Reuse HerdR's existing CI history, identity/coverage and chart components. Repository language is authoritative provider metadata or the cached GitHub primary language; missing metadata stays unknown. User-approved scope is implementation and validation, with reversible deployment and reviewable PRs.

**Tech stack:** Existing Python stdlib runner helpers; HerdR Python, plain JavaScript/SVG and Playwright. No new dependencies.

## Shared contracts

- Chart records retain `project_id`, canonical repository/provider identity and run number; add `language: string | null`, `language_source: provider_repository | github_primary_language | unknown`.
- Display language as current primary repository language, not the language used by every historical build step. Do not guess from repository names or count multilingual projects in several languages.
- Successful, deduplicated runs with finite positive start-to-finish durations form the performance sample. Failed, canceled, skipped, active and unmeasured runs do not bias successful averages.
- Run execution includes setup/build/test/cache stages and excludes queue time. Do not label it compile-only duration. Show counts, period/selection and partial/stale/memory-only history limits.
- Existing repository, branch, period, search, mission and session filters apply consistently. Preserve disclosure/focus state on refresh and mobile accessibility.
- Archive and compiler operations on Hive run in compute allocations via shared node-policy wrappers with GPU exclusions. No shared active Rust target directories, login-node builds, new dependencies or global cache-limit increases.

## Task 1: Rust reuse (separate woodpecker-rust-cache worktree)

- [x] Inspect compiler cache availability and choose a bounded opt-in compiled-cache path compatible with existing safety guarantees.
- [x] Test compatibility keys, timestamp preservation and local/path-package invalidation before implementing; old Go behavior remains unchanged.
- [x] Prove cold save and reuse in two independent allocations using a real Rust executable, including a source-edit case that must rebuild and change output.
- [x] Measure restore + build + save; preserve test coverage and compare artifact metadata/behavior. Report unhelpful or unsafe approaches rather than claim an unsupported speedup.
- [x] Prepare the Rust pilot integration and docs; independent review before deployment.

## Task 2: Language provenance (backend lane)

- [x] Add GitHub primary language to existing bulk repository discovery; expose a bounded cached metadata lookup with credential-generation isolation.
- [x] Propagate provider/cached GitHub language through both `annotate_ci` and `annotate_ci_history`; add API wiring without outbound network work in handlers.
- [x] Regression tests: known Rust/Go, missing/malformed/unknown, unavailable/stale metadata, rotation, explicit provider precedence and distinct forge identities.

## Task 3: HerdR performance charts (frontend lane)

- [x] Extend existing CI charts with histogram and per-project/per-language mean, median, p95 and sample count; reuse design tokens and SVG/table patterns.
- [x] Deduplicate recent/history observations consistently, including provider origin from `provider_url` or run URL. Preserve distinct providers and canonical repository identities.
- [x] Verify known statistics, histogram boundaries, invalid timestamps, success-only samples, unknown language, filters, escaping, responsive layout and refresh focus through unit/browser tests.
- [x] Keep exact accessible data for groups omitted from bounded plots; show honest empty/loading/partial/stale states.

## Task 4: Integration and delivery (leader)

- [x] Run targeted Python/JS suites, lint/syntax and relevant browser checks; use fresh evidence, not installed stale package sources (`PYTHONPATH=.` in worktree).
- [x] Independent reviews of Rust cache safety, backend identity/provenance and UI statistics; fix material findings.
- [x] Inspect real configured HerdR endpoint/data and verify visible charts with screenshots and true provider metadata.
- [x] Stage additive Rust helpers/pilot and HerdR app using existing deployment tooling only after verification; preserve concurrent edits and collect rollback/evidence.
- [x] Publish reviewable changes and report scope, measured Rust results, UI validation and any rollout limits.

## Evidence and rollout notes

Compiled Cargo target reuse was measured in independent Hive allocations and rejected for rollout: registry-only was faster after snapshot overhead. The experiment and timings remain in the runner proof directory; InferWeave receives registry reuse and per-stage timing wrappers without changing test commands or target-directory guards.

Frontend tests cover successful-only statistics, provider prefixes, row identity, histogram boundaries and refresh/mobile behavior. Backend tests cover cache-only language enrichment, credential rotation, retained reports and conflicting identities. The latest main deployment is integrated before rollout to preserve concurrent agent-summary work. Fresh command outputs and live deployment evidence are collected by the leader.

## Delivered

- HerdR PR #156; runtime commit `a3447bfad12a`, healthy through the official control-plane rollout. Latest main agent-summary routing retained.
- Live CI page: 261 measured successful runs, 101 projects, 17 current-primary-language groups at verification. Rust: 10 runs, mean 42m21s (whole pipeline). Desktop/mobile browser proof: no exceptions or horizontal overflow. API discovery: 118 repositories, 93 with known current language.
- Live validation exposed provider PR/commit URLs being mistaken for repository identities; added realistic context-URL regressions and verified the correction after redeployment.
- Fresh checks: 124 targeted Python tests before the context fix, 42 project-link/API tests after it; 264 JS/client/component/browser tests after rebase; Ruff, compilation/syntax, independent review.
- Rust pilot PR #1205 remains unmerged; full-workspace Woodpecker check is pending. Four independent small executable allocations completed; compiled-target reuse rejected after registry controls. Runner PR #13 includes bounded evidence.
- Evidence and screenshots: `/home/wohlgemuth/IdeaProjects/artifacts/herdr-ci-performance-20261022/`. No default-branch merge performed.
