# P00 open PRs / worktrees and conflict strategy

herdr-engineering open: #348 (reports, claims migration 0021) <- #349, #351 stacked; #350 dashboard (app.js, attention page); #317 draft assistant memory/grants; #308 event redaction; #322/#327 scheduler; #305/#307 scheduler safety; #304 delivery recovery.
pi-engineering open: #452 (orchestrator.ts, missionStore.ts, scheduler.ts; no runtime/herdr overlap).

| Risk | Strategy |
|---|---|
| Migration numbers | main latest 0020; #348 takes 0021. We use >=0022, re-check before each merge. db.py MIGRATION_ALIASES has duplicate numbers; add new ones idempotent with `-- DOWN:`. |
| Artifact files (#348-351) | P03 starts after they merge; stack on them otherwise. Never reimplement renderers. |
| app.js / attention (#350) | P06/P08 UI work waits for #350 or rebases on it. |
| #317 | P07 memory scopes blocked until #317 merges or is superseded; ask owner. |
| #308 event redaction | P02 graph/event reads must follow its redaction rules; rebase on merge. |
| #452 | P05 room submit avoids orchestrator.ts/missionStore.ts until merged. |
| Dirty main checkout `herdr-engineering` (176 changes incl. collab.py, artifacts.py, untracked migrations 0003-0005) | Not touched. Work only in clean worktrees from origin/main. Owner should commit/discard; reconcile on request. |
| Other dirty worktrees (arch, ci, url-fabric) | Not touched. |
| Pi local checkout 39 behind, dirty | Branch from origin/main in new worktree. |
