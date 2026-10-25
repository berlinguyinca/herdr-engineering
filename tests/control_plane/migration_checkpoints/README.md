# Historical migration checkpoints

`Migrator` skips a migration by filename and never re-checks content, so editing a migration that
a database already applied silently diverges that database from a fresh install. Migration 0003
was edited in place during development; these are the exact historical contents that a database
could have applied (`git show <sha>:deploy/control-plane/migrations/0003_collaboration_data_platform.sql`).

| File | Commit | Notes |
| --- | --- | --- |
| `0003_f5e1dad_first_draft.sql` | `f5e1dad` | First draft; lacked grant/expiry/sequence columns and the audit request fingerprint. |
| `0003_dce994e_with_registry.sql` | `dce994e` | Carried the skill registry tables before they moved to 0006. |

Every other 0003 revision is byte-identical to the current file. `tests/control_plane/test_migrations.py`
applies each checkpoint, then the current chain, and requires the schema to equal a fresh install.
