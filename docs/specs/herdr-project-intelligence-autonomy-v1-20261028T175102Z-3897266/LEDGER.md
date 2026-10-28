# IMPLEMENTATION LEDGER

| Phase | State | Dependencies | Changed files / commits | Tests / result | Security review | Blockers |
|---|---|---|---|---|---|---|
| P0 discovery | done (this commit) | — | AUDIT.md, INTEGRATION-MAP.md, DECISIONS.md, LEDGER.md, spec bundle | n/a | — | B5 |
| P1 event foundation | IN PROGRESS: core + migration done; API routes, flag wiring, replay-from-fabric_events adapter pending | P0 | `herdr_engineering/control_plane/project_intelligence.py`, `deploy/control-plane/migrations/0020_project_intelligence.sql`, `tests/control_plane/test_project_intelligence.py`, `tests/control_plane/test_project_intelligence_migration.py` | 16 unit pass; migration up/re-run/constraints/down pass on a private throwaway Postgres 18 (integration-marked, self-contained); neighbouring db/events tests 12 pass | pending | B6 (no live DB touched) |
| P2 connectors/artifacts | not started | P1 | | | | B1 B2 |
| P3 intelligence | not started | P1 | | | | |
| P4 dashboard/mobile | not started | P1,P3 | | | | B4 |
| P5 autonomy | not started | P1,P2 | | | | B2 B3 |
| P6 backfill/prod | not started | all | | | | B6 |

Rules in force: read-only/ingest-only defaults; no merges, deploys or live flag flips; synthetic fixtures; no skip/xfail tests.
