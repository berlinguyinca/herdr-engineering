# HerdR Control Plane — Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the durable foundation of the `dev.lan` control plane: PostgreSQL schema + migrations, RustFS object store client, stable entity IDs, an ordered/idempotent event protocol, mission/session/service models, state snapshots, the artifact model, and a background-worker scaffold — as a new `herdr_engineering/control_plane/` subpackage in the existing `herdr-engineering` repo.

**Architecture:** A new Python subpackage (`herdr_engineering/control_plane/`) with small, focused modules. PostgreSQL is the store of structured state; RustFS (S3-compatible) is the store of artifact bytes; a versioned SQL migration runner brings the schema up from day one. All modules are designed around dependency injection so tests run against in-memory/fake doubles — no live Postgres or RustFS needed in CI. Compose integration (adding `postgres` + `rustfs` + `control-plane-worker` services to `deploy/fabric-stack/compose.yaml`) is included but the live stack is **not** torn down.

**Tech Stack:** Python 3.12, `asyncpg` (Postgres), `boto3` (S3/RustFS), stdlib (no web framework this phase). New deps: `asyncpg>=0.29`, `boto3>=1.34`.

**Spec:** `docs/specs/0190-herdr-control-plane-program.md` (roadmap) and the user-supplied 116-section control-plane spec (§4, §49–58, §90, §95, §108, §114, §115).

## Global Constraints

- No SQLite anywhere. No large binaries in PostgreSQL (bytes go to RustFS).
- No Redis/NATS/Kafka. Prefer existing HerdR fabric/event mechanisms.
- Identity is stable; location is dynamic. Never identify entities by hostname, PID, container ID, filesystem path, or Slurm job ID.
- Events: globally-unique `event_id`, `schema_version`, `source_timestamp` + `ingest_timestamp` (UTC), `entity_type/id`, optional `mission_id`/`session_id`, `sequence`, `event_type`, `payload`. Idempotent ingestion. Deterministic ordering per logical stream.
- Artifacts: `artifact_id` is the application identity; RustFS key is content-addressed `sha256/ab/cd/<full-sha256>`; canonical bucket `herdr-artifacts`; SHA-256 integrity verified before `available`.
- Do not expose permanent RustFS/S3 credentials to agents (Phase 8 concern; Phase 1 just keeps them out of model/event payloads).
- Do not delete entities on lost connectivity (lease/heartbeat semantics, `healthy|degraded|unavailable|unknown` + reason).
- Migrations from day one. UTC time. Versioned API prefix `/api/v1/...` reserved for Phase 3.
- Follow repo conventions: `ruff` clean (line-length 100), `pytest` in `tests/`, no SQLite, `from __future__ import annotations`, type hints, `docs/specs/` numbering (this is 0190).

---

## File Structure

```
deploy/control-plane/
  migrations/
    0001_control_plane.sql        # full Phase-1 schema (all Spec §53 tables)

herdr_engineering/
  control_plane/
    __init__.py                   # version + package marker
    ids.py                        # stable entity IDs (new_id / is_valid_id)
    db.py                         # asyncpg connection + versioned migration runner
    events.py                     # Event envelope, sequencing, idempotent ingestion
    models.py                     # Mission/Session/Service stores + provenance
    snapshots.py                  # state snapshot save/load + replay-after
    rustfs.py                     # S3/RustFS content-addressed client + integrity
    artifacts.py                  # Artifact model + bindings + materialization
    worker.py                     # background worker scaffold (leases/snapshots/GC)

tests/
  control_plane/
    test_ids.py
    test_db.py
    test_events.py
    test_models.py
    test_snapshots.py
    test_rustfs.py
    test_artifacts.py
    test_worker.py
```

`control_plane` modules never import one another's internals except through
the `interfaces` documented in each task below; tests use fakes.

---

### Task 1: Package scaffolding, dependencies, config

**Files:**
- Modify: `pyproject.toml` (add `asyncpg>=0.29`, `boto3>=1.34`)
- Create: `herdr_engineering/control_plane/__init__.py`
- Create: `herdr_engineering/control_plane/db.py`
- Test: `tests/control_plane/test_db.py` (config + dsn parsing only)

**Interfaces:**
- Produces: `control_plane/db.py` exposes `control_plane_config(cfg: dict) -> dict` returning a normalized `{"postgres_dsn": str, "rustfs_endpoint": str, "rustfs_bucket": str, "rustfs_region": str}` with sensible defaults.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_db.py
from herdr_engineering.control_plane import db

def test_control_plane_config_defaults():
    c = db.control_plane_config({})
    assert c["postgres_dsn"].startswith("postgresql://")
    assert c["rustfs_bucket"] == "herdr-artifacts"
    assert c["rustfs_region"] == "us-east-1"

def test_control_plane_config_overrides():
    c = db.control_plane_config({"control_plane": {
        "postgres_dsn": "postgresql://x:y@h:5432/db",
        "rustfs_endpoint": "http://127.0.0.1:9000",
        "rustfs_bucket": "artifacts",
    }})
    assert c["postgres_dsn"] == "postgresql://x:y@h:5432/db"
    assert c["rustfs_endpoint"] == "http://127.0.0.1:9000"
    assert c["rustfs_bucket"] == "artifacts"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/wohlgemuth/IdeaProjects/herdr-engineering && .venv/bin/python -m pytest tests/control_plane/test_db.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'herdr_engineering.control_plane'`).

- [ ] **Step 3: Implement**

```python
# herdr_engineering/control_plane/__init__.py
__version__ = "0.1.0"
```

```python
# herdr_engineering/control_plane/db.py
"""Control-plane configuration normalization."""
from __future__ import annotations
from typing import Any

_DEFAULTS = {
    "postgres_dsn": "postgresql://herdr:herdr@127.0.0.1:5432/herdr",
    "rustfs_endpoint": "http://127.0.0.1:9000",
    "rustfs_bucket": "herdr-artifacts",
    "rustfs_region": "us-east-1",
}


def control_plane_config(cfg: dict[str, Any]) -> dict[str, str]:
    """Return normalized control-plane settings from a config dict."""
    cp = cfg.get("control_plane") or {}
    out = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if key in cp and cp[key]:
            out[key] = str(cp[key])
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_db.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml herdr_engineering/control_plane/ tests/control_plane/test_db.py
git commit -m "feat(control-plane): scaffold package, deps, and config normalization"
```

---

### Task 2: Stable entity IDs

**Files:**
- Create: `herdr_engineering/control_plane/ids.py`
- Test: `tests/control_plane/test_ids.py`

**Interfaces:**
- `new_id(kind: str) -> str` — returns `f"{kind}_{ulid}"` (26-char Crockford-base32 ULID, lowercase, sortable by time). `kind` in `{"mission","session","service","artifact","host","agent","plan","worktree"}`.
- `is_valid_id(candidate: str, kind: str) -> bool` — checks the `kind_` prefix and a 26-char lowercase `[0-9a-z]` suffix.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_ids.py
from herdr_engineering.control_plane import ids

def test_new_id_prefix_and_shape():
    v = ids.new_id("mission")
    assert v.startswith("mission_")
    assert len(v) == len("mission_") + 26

def test_ids_are_time_ordered():
    a = ids.new_id("session")
    b = ids.new_id("session")
    assert a < b  # ULID suffix is time-sortable

def test_is_valid_id():
    assert ids.is_valid_id(ids.new_id("service"), "service")
    assert not ids.is_valid_id("mission_XXXX", "mission")
    assert not ids.is_valid_id(ids.new_id("mission"), "service")

def test_invalid_kind_rejected():
    import pytest
    with pytest.raises(ValueError):
        ids.new_id("bogus")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_ids.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# herdr_engineering/control_plane/ids.py
"""Stable entity IDs (identity is stable, location is dynamic)."""
from __future__ import annotations
import time
from typing import Final

_KINDS: Final[frozenset[str]] = frozenset({
    "mission", "session", "service", "artifact",
    "host", "agent", "plan", "worktree",
})
_CROCKFORD: Final[str] = "0123456789abcdefghjkmnpqrstvwxyz"
_TS_BITS = 48  # ms since epoch
_SUFFIX_LEN = 26


def new_id(kind: str) -> str:
    if kind not in _KINDS:
        raise ValueError(f"unknown entity kind: {kind!r}")
    ts = int(time.time() * 1000)
    # 48-bit timestamp -> 10 Crockford chars (5 bits each)
    ts_chars = []
    for _ in range(10):
        ts_chars.append(_CROCKFORD[ts & 31])
        ts >>= 5
    ts_chars.reverse()
    suffix = "".join(ts_chars) + _random_suffix(16)
    return f"{kind}_{suffix}"


def is_valid_id(candidate: str, kind: str) -> bool:
    prefix = f"{kind}_"
    if not candidate.startswith(prefix):
        return False
    suffix = candidate[len(prefix):]
    if len(suffix) != _SUFFIX_LEN:
        return False
    return all(c in _CROCKFORD for c in suffix)


def _random_suffix(length: int) -> str:
    import secrets
    return "".join(_CROCKFORD[b & 31] for b in secrets.token_bytes(length))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_ids.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add herdr_engineering/control_plane/ids.py tests/control_plane/test_ids.py
git commit -m "feat(control-plane): stable time-ordered entity IDs"
```

---

### Task 3: Versioned SQL migration runner

**Files:**
- Create: `herdr_engineering/control_plane/db.py` (append)
- Test: `tests/control_plane/test_db.py` (append)

**Interfaces:**
- `class Migrator` with `__init__(self, conn: Any)` where `conn` is any object exposing `async execute(sql, *args)` and `async fetch(sql, *args) -> list[dict]` (a real `asyncpg.Connection` or a fake).
- `async Migrator.ensure_schema()` — creates `schema_migrations(version text primary key, applied_at timestamptz)` if absent.
- `async Migrator.applied() -> list[str]` — returns applied versions ascending.
- `async Migrator.apply(migrations_dir: str) -> list[str]` — applies each `NNNN_*.sql` file not yet applied, in filename order, recording each in `schema_migrations`; returns the applied version list. Uses a single transaction per file.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_db.py (append)
import asyncio

class _FakeConn:
    def __init__(self): self.log = []
    async def execute(self, sql, *a):
        self.log.append(("exec", sql, a))
    async def fetch(self, sql, *a):
        if "schema_migrations" in sql and "select" in sql:
            return [{"version": v} for v in self._applied]
        return []
    async def fetchrow(self, sql, *a): return None

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)

def test_migrator_applies_in_order_and_skips_applied(tmp_path):
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "0001_a.sql").write_text("CREATE TABLE a (id int);")
    (mdir / "0002_b.sql").write_text("CREATE TABLE b (id int);")
    fc = _FakeConn(); fc._applied = ["0001_a.sql"]
    mig = db.Migrator(fc)
    done = _run(mig.apply(str(mdir)))
    assert done == ["0002_b.sql"]
    # schema created, migration recorded, files applied in order
    sqls = [s for k, s, _ in fc.log if k == "exec"]
    assert any("schema_migrations" in s for s in sqls)
    assert any("0002_b.sql" in s for s in sqls)
    assert not any("0001_a.sql" in s for s in sqls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_db.py::test_migrator_applies_in_order_and_skips_applied -q`
Expected: FAIL (`AttributeError: module '...db' has no attribute 'Migrator'`).

- [ ] **Step 3: Implement**

```python
# herdr_engineering/control_plane/db.py (append)
import os
from pathlib import Path

class Migrator:
    def __init__(self, conn: Any):
        self._conn = conn

    async def ensure_schema(self) -> None:
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version text PRIMARY KEY,"
            "  applied_at timestamptz NOT NULL DEFAULT now()"
            ")"
        )

    async def applied(self) -> list[str]:
        rows = await self._conn.fetch(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        return [r["version"] for r in rows]

    async def apply(self, migrations_dir: str) -> list[str]:
        await self.ensure_schema()
        already = set(await self.applied())
        files = sorted(p.name for p in Path(migrations_dir).glob("*.sql"))
        applied: list[str] = []
        for name in files:
            if name in already:
                continue
            sql = Path(migrations_dir, name).read_text(encoding="utf-8")
            await self._conn.execute("BEGIN")
            try:
                await self._conn.execute(sql)
                await self._conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", name
                )
                await self._conn.execute("COMMIT")
            except Exception:
                await self._conn.execute("ROLLBACK")
                raise
            applied.append(name)
        return applied
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_db.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add herdr_engineering/control_plane/db.py tests/control_plane/test_db.py
git commit -m "feat(control-plane): versioned SQL migration runner"
```

---

### Task 4: Phase-1 schema migration

**Files:**
- Create: `deploy/control-plane/migrations/0001_control_plane.sql`
- Test: `tests/control_plane/test_db.py` (append — DDL sanity: every Spec §53 table name present)

**Interfaces:**
- Produces the DDL that later tasks' stores query. Table names (Spec §53): `hosts`, `host_samples`, `missions`, `mission_events`, `mission_stage_runs`, `plans`, `plan_revisions`, `sessions`, `session_events`, `agents`, `agent_runs`, `services`, `service_events`, `artifacts`, `artifact_bindings`, `artifact_materializations`, `token_usage`, `resource_usage`, `test_runs`, `test_results`, `reviews`, `review_findings`, `pull_requests`, `audit_events`, `fabric_events`, `state_snapshots`.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_db.py (append)
REQUIRED_TABLES = {
    "hosts","host_samples","missions","mission_events","mission_stage_runs",
    "plans","plan_revisions","sessions","session_events","agents","agent_runs",
    "services","service_events","artifacts","artifact_bindings",
    "artifact_materializations","token_usage","resource_usage","test_runs",
    "test_results","reviews","review_findings","pull_requests","audit_events",
    "fabric_events","state_snapshots",
}

def test_phase1_schema_contains_all_spec_tables():
    sql = (Path(__file__).parents[2] / "deploy" / "control-plane"
           / "migrations" / "0001_control_plane.sql").read_text()
    for table in REQUIRED_TABLES:
        assert f"CREATE TABLE {table}" in sql, f"missing table {table}"

def test_phase1_schema_indexes_heavily_queried_fields():
    sql = (Path(__file__).parents[2] / "deploy" / "control-plane"
           / "migrations" / "0001_control_plane.sql").read_text()
    for col in ("mission_id", "session_id", "host_id", "event_type"):
        assert f"CREATE INDEX" in sql
        assert col in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_db.py::test_phase1_schema_contains_all_spec_tables -q`
Expected: FAIL (`FileNotFoundError`).

- [ ] **Step 3: Implement the migration SQL** — create every table with stable-ID PKs (`text`), JSONB payloads where appropriate, FKs, and indexes on `mission_id`, `session_id`, `host_id`, `event_type`, `timestamp`, `status`, `repository`. Representative core tables:

```sql
-- deploy/control-plane/migrations/0001_control_plane.sql
-- Phase-1 control-plane schema (Spec §53). Identity is stable; location is dynamic.

CREATE TABLE hosts (
  host_id         text PRIMARY KEY,
  tailnet_ip      text,
  hostname        text,
  status          text NOT NULL DEFAULT 'unknown',   -- healthy|degraded|unavailable|unknown
  reason          text,
  detail          text,
  last_heartbeat  timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE host_samples (
  sample_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  host_id         text NOT NULL REFERENCES hosts(host_id),
  sampled_at      timestamptz NOT NULL DEFAULT now(),
  cpu_percent     double precision,
  load_avg        double precision,
  mem_used_bytes  bigint,
  mem_total_bytes bigint,
  disk_free_bytes bigint,
  disk_total_bytes bigint,
  gpu_util_percent double precision,
  vram_used_bytes bigint,
  payload         jsonb
);
CREATE INDEX idx_host_samples_host_time ON host_samples(host_id, sampled_at DESC);

CREATE TABLE missions (
  mission_id      text PRIMARY KEY,
  title           text NOT NULL,
  purpose         text,
  repository      text,
  branch          text,
  worktree        text,
  owner           text,
  stage           text NOT NULL DEFAULT 'planning',
  status          text NOT NULL DEFAULT 'active',
  progress        double precision NOT NULL DEFAULT 0,
  current_activity jsonb,
  blocked         boolean NOT NULL DEFAULT false,
  blocking_reason text,
  blocking_detail text,
  blocked_since   timestamptz,
  active_session_id text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  outcome         text
);
CREATE INDEX idx_missions_status ON missions(status);
CREATE INDEX idx_missions_repository ON missions(repository);

CREATE TABLE mission_events (
  event_id        text PRIMARY KEY,
  mission_id      text NOT NULL REFERENCES missions(mission_id),
  sequence        bigint NOT NULL,
  event_type      text NOT NULL,
  payload         jsonb,
  source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(),
  UNIQUE (mission_id, sequence)
);
CREATE INDEX idx_mission_events_type ON mission_events(event_type);

CREATE TABLE mission_stage_runs (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  mission_id      text NOT NULL REFERENCES missions(mission_id),
  stage           text NOT NULL,
  attempt         int NOT NULL DEFAULT 1,
  started_at      timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  duration_ms     bigint,
  status          text,
  session_id      text,
  model           text
);

CREATE TABLE plans (
  plan_id         text PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  planator_plan_id text,
  version         int NOT NULL DEFAULT 1,
  status          text NOT NULL DEFAULT 'draft',
  payload         jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  approved_at     timestamptz
);
CREATE TABLE plan_revisions (
  revision_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id         text NOT NULL REFERENCES plans(plan_id),
  version         int NOT NULL,
  payload         jsonb,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
  session_id      text PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  title           text,
  purpose         text,
  agent_role      text,          -- planner|implementer|tester|reviewer|repair
  model           text,
  host_id         text REFERENCES hosts(host_id),
  status          text NOT NULL DEFAULT 'active',
  stage           text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  last_activity   timestamptz,
  ended_at        timestamptz,
  payload         jsonb
);
CREATE INDEX idx_sessions_mission ON sessions(mission_id);
CREATE INDEX idx_sessions_host ON sessions(host_id);
CREATE INDEX idx_sessions_status ON sessions(status);

CREATE TABLE session_events (
  event_id        text PRIMARY KEY,
  session_id      text NOT NULL REFERENCES sessions(session_id),
  sequence        bigint NOT NULL,
  event_type      text NOT NULL,
  payload         jsonb,
  source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, sequence)
);

CREATE TABLE agents (
  agent_id        text PRIMARY KEY,
  session_id      text REFERENCES sessions(session_id),
  model           text,
  role            text,
  status          text NOT NULL DEFAULT 'active',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE agent_runs (
  run_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_id        text NOT NULL REFERENCES agents(agent_id),
  started_at      timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  status          text,
  model           text,
  payload         jsonb
);

CREATE TABLE services (
  service_id      text PRIMARY KEY,
  service_name    text NOT NULL,
  route           text,                 -- e.g. http://dev.lan:18000
  backend         text,
  port            int,
  host_id         text REFERENCES hosts(host_id),
  owner           text,
  started_by      text,
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  workspace_id    text,
  repository      text,
  worktree        text,
  branch          text,
  purpose         text,
  health          text NOT NULL DEFAULT 'unknown',
  started_at      timestamptz NOT NULL DEFAULT now(),
  last_seen       timestamptz,
  stopped_at      timestamptz,
  runtime         jsonb             -- pid/container_id/systemd_unit/slurm_job_id (metadata only)
);
CREATE INDEX idx_services_host ON services(host_id);
CREATE INDEX idx_services_mission ON services(mission_id);
CREATE INDEX idx_services_session ON services(session_id);
CREATE INDEX idx_services_repository ON services(repository);
CREATE TABLE service_events (
  event_id        text PRIMARY KEY,
  service_id      text NOT NULL REFERENCES services(service_id),
  sequence        bigint NOT NULL,
  event_type      text NOT NULL,
  payload         jsonb,
  source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now(),
  UNIQUE (service_id, sequence)
);

CREATE TABLE artifacts (
  artifact_id     text PRIMARY KEY,
  created_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text,
  original_filename text,
  display_name    text,
  mime_type       text,
  artifact_type   text NOT NULL DEFAULT 'other',
  source          text,
  size_bytes      bigint,
  sha256          text,
  storage_backend text NOT NULL DEFAULT 'rustfs',
  storage_key     text,
  status          text NOT NULL DEFAULT 'uploading', -- uploading|verifying|available|failed
  retention_class text NOT NULL DEFAULT 'temporary', -- temporary|session|mission|permanent
  payload         jsonb
);
CREATE INDEX idx_artifacts_sha256 ON artifacts(sha256);

CREATE TABLE artifact_bindings (
  binding_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  artifact_id     text NOT NULL REFERENCES artifacts(artifact_id),
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  agent_id        text REFERENCES agents(agent_id),
  stage           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  created_by      text,
  UNIQUE (artifact_id, mission_id, COALESCE(session_id, ''), COALESCE(stage, ''))
);

CREATE TABLE artifact_materializations (
  materialization_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  artifact_id     text NOT NULL REFERENCES artifacts(artifact_id),
  host_id         text REFERENCES hosts(host_id),
  session_id      text REFERENCES sessions(session_id),
  local_path      text,
  status          text NOT NULL DEFAULT 'pending',
  sha256_verified boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE token_usage (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  agent_id        text REFERENCES agents(agent_id),
  model           text,
  provider        text,
  stage           text,
  request_id      text,
  input_tokens    bigint,
  output_tokens   bigint,
  reasoning_tokens bigint,
  cache_read_tokens bigint,
  cache_write_tokens bigint,
  duration_ms     bigint,
  host_id         text REFERENCES hosts(host_id),
  retry_number    int,
  success         boolean,
  error_code      text
);
CREATE INDEX idx_token_usage_mission ON token_usage(mission_id, occurred_at);
CREATE INDEX idx_token_usage_model ON token_usage(model);

CREATE TABLE resource_usage (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  kind            text,
  value           double precision,
  unit            text,
  payload         jsonb
);

CREATE TABLE test_runs (
  test_run_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  repository      text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  status          text,
  passed          int,
  failed          int,
  skipped         int,
  total           int
);
CREATE TABLE test_results (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  test_run_id     bigint NOT NULL REFERENCES test_runs(test_run_id),
  name            text,
  status          text,
  duration_ms     bigint,
  message         text
);

CREATE TABLE reviews (
  review_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  session_id      text REFERENCES sessions(session_id),
  reviewer        text,
  status          text,
  verdict         text,
  started_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz
);
CREATE TABLE review_findings (
  finding_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  review_id       bigint NOT NULL REFERENCES reviews(review_id),
  severity        text,
  summary         text,
  detail          text,
  status          text
);

CREATE TABLE pull_requests (
  pr_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  mission_id      text REFERENCES missions(mission_id),
  repository      text,
  number          int,
  url             text,
  status          text,
  branch          text,
  base            text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
  event_id        text PRIMARY KEY,
  actor           text,
  action          text NOT NULL,
  entity_type     text,
  entity_id       text,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  metadata        jsonb
);

CREATE TABLE fabric_events (
  event_id        text PRIMARY KEY,
  sequence        bigint NOT NULL,
  event_type      text NOT NULL,
  entity_type     text,
  entity_id       text,
  payload         jsonb,
  source_timestamp timestamptz NOT NULL,
  ingest_timestamp timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE state_snapshots (
  snapshot_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_kind     text NOT NULL,       -- mission|session|host|service
  entity_id       text NOT NULL,
  sequence        bigint NOT NULL DEFAULT 0,
  state           jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity_kind, entity_id, sequence)
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_db.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add deploy/control-plane/migrations/0001_control_plane.sql tests/control_plane/test_db.py
git commit -m "feat(control-plane): Phase-1 schema (Spec §53 tables, indexes)"
```

---

### Task 5: Event envelope, sequencing, idempotent ingestion

**Files:**
- Create: `herdr_engineering/control_plane/events.py`
- Test: `tests/control_plane/test_events.py`

**Interfaces:**
- `class Event` — frozen dataclass: `event_id`, `schema_version: int = 1`, `source_timestamp: datetime`, `ingest_timestamp: datetime`, `entity_type`, `entity_id`, `mission_id: str|None`, `session_id: str|None`, `sequence: int`, `event_type`, `payload: dict`.
- `make_event(entity_type, entity_id, event_type, *, payload=None, mission_id=None, session_id=None, source=None) -> Event` — fills ids/timestamps/sequence=0 (sequence assigned by store).
- `class EventStore` with `__init__(self, conn: Any, *, table: str = "fabric_events")`.
  - `async next_sequence(entity_type: str, entity_id: str) -> int` — returns next sequence for the logical stream (max+1).
  - `async append(event: Event) -> bool` — INSERT; returns `False` if a row with that `event_id` already exists (idempotent), else `True`.
  - `async read_stream(entity_type: str, entity_id: str, after: int = 0) -> list[Event]` — ordered by `sequence`, skipping `<= after`.
- `class StreamKey` helper: `stream_key(entity_type, entity_id) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_events.py
import asyncio
from datetime import datetime, timezone
from herdr_engineering.control_plane import events

class _FakeStore:
    def __init__(self): self.rows = []
    async def fetch(self, sql, *a):
        # naive stream read: SELECT ... WHERE entity_type=$1 AND entity_id=$2 ORDER BY sequence
        if "ORDER BY sequence" in sql:
            et, eid = a[0], a[1]
            rows = [r for r in self.rows if r["entity_type"] == et and r["entity_id"] == eid]
            return sorted(rows, key=lambda r: r["sequence"])
        if "max(sequence)" in sql:
            return [{"max": max((r["sequence"] for r in self.rows), default=0)}]
        return []
    async def execute(self, sql, *a):
        if sql.strip().startswith("INSERT"):
            ev = a[0]
            if any(r["event_id"] == ev["event_id"] for r in self.rows):
                return 0  # idempotent: conflict, no insert
            self.rows.append(dict(ev)); return 1
        return 1

def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)

def test_make_event_envelope():
    e = events.make_event("mission", "m1", "MissionCreated",
                          payload={"title": "x"}, mission_id="m1")
    assert e.event_id and e.schema_version == 1
    assert e.entity_type == "mission" and e.entity_id == "m1"
    assert isinstance(e.source_timestamp, datetime) and e.source_timestamp.tzinfo is not None

def test_append_is_idempotent_and_sequences():
    store = events.EventStore(_FakeStore())
    e1 = events.make_event("mission", "m1", "MissionCreated", mission_id="m1")
    seq1 = _run(store.next_sequence("mission", "m1"))
    e1 = events.Event(**{**e1.__dict__, "sequence": seq1})
    assert _run(store.append(e1)) is True
    assert _run(store.append(e1)) is False  # duplicate event_id ignored

def test_read_stream_ordered_and_resumable():
    store = events.EventStore(_FakeStore())
    for i in range(3):
        ev = events.make_event("session", "s1", f"Event{i}", session_id="s1")
        seq = _run(store.next_sequence("session", "s1"))
        ev = events.Event(**{**ev.__dict__, "sequence": seq})
        _run(store.append(ev))
    full = _run(store.read_stream("session", "s1"))
    assert [e.sequence for e in full] == [0, 1, 2]
    tail = _run(store.read_stream("session", "s1", after=1))
    assert [e.sequence for e in tail] == [2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_events.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# herdr_engineering/control_plane/events.py
"""Event envelope, sequencing, and idempotent ingestion (Spec §48-51)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import secrets


@dataclass(frozen=True)
class Event:
    event_id: str
    schema_version: int = 1
    source_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ingest_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entity_type: str = ""
    entity_id: str = ""
    mission_id: str | None = None
    session_id: str | None = None
    sequence: int = 0
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def make_event(entity_type: str, entity_id: str, event_type: str, *,
               payload: dict[str, Any] | None = None,
               mission_id: str | None = None,
               session_id: str | None = None,
               source: datetime | None = None) -> Event:
    now = source or datetime.now(timezone.utc)
    return Event(
        event_id=f"ev_{secrets.token_hex(16)}",
        source_timestamp=now,
        ingest_timestamp=datetime.now(timezone.utc),
        entity_type=entity_type,
        entity_id=entity_id,
        mission_id=mission_id,
        session_id=session_id,
        event_type=event_type,
        payload=payload or {},
    )


def stream_key(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}:{entity_id}"


class EventStore:
    """Persistent event log with per-stream sequence + idempotent append.

    ``conn`` is any object exposing async ``fetch(sql, *args)`` and
    ``execute(sql, *args)`` (a real asyncpg connection or a test fake).
    """

    def __init__(self, conn: Any, *, table: str = "fabric_events"):
        self._conn = conn
        self._table = table

    async def next_sequence(self, entity_type: str, entity_id: str) -> int:
        row = await self._conn.fetch(
            f"SELECT COALESCE(max(sequence),0) AS max FROM {self._table} "
            "WHERE entity_type=$1 AND entity_id=$2",
            entity_type, entity_id,
        )
        return int(row[0]["max"]) + 1

    async def append(self, event: Event) -> bool:
        try:
            await self._conn.execute(
                f"INSERT INTO {self._table} "
                "(event_id, sequence, event_type, entity_type, entity_id, "
                " mission_id, session_id, payload, source_timestamp, "
                " ingest_timestamp, schema_version) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
                "ON CONFLICT (event_id) DO NOTHING",
                event.event_id, event.sequence, event.event_type,
                event.entity_type, event.entity_id, event.mission_id,
                event.session_id, event.payload, event.source_timestamp,
                event.ingest_timestamp, event.schema_version,
            )
        except Exception:
            return False
        return True

    async def read_stream(self, entity_type: str, entity_id: str,
                          after: int = 0) -> list[Event]:
        rows = await self._conn.fetch(
            f"SELECT * FROM {self._table} "
            "WHERE entity_type=$1 AND entity_id=$2 AND sequence > $3 "
            "ORDER BY sequence",
            entity_type, entity_id, after,
        )
        return [_row_to_event(r) for r in rows]


def _row_to_event(row: dict[str, Any]) -> Event:
    return Event(
        event_id=row["event_id"],
        schema_version=row.get("schema_version", 1),
        source_timestamp=row["source_timestamp"],
        ingest_timestamp=row["ingest_timestamp"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        mission_id=row.get("mission_id"),
        session_id=row.get("session_id"),
        sequence=row["sequence"],
        event_type=row["event_type"],
        payload=row.get("payload") or {},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_events.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add herdr_engineering/control_plane/events.py tests/control_plane/test_events.py
git commit -m "feat(control-plane): ordered, idempotent event ingestion"
```

---

### Task 6: Mission / Session / Service stores

**Files:**
- Create: `herdr_engineering/control_plane/models.py`
- Test: `tests/control_plane/test_models.py`

**Interfaces:**
- `class MissionStore(conn)` — `async create(title, *, purpose=None, repository=None, branch=None, worktree=None, owner=None) -> str` (stable `mission_id`); `async get(mission_id) -> dict|None`; `async list(status=None) -> list[dict]`; `async update(mission_id, **fields) -> None`.
- `class SessionStore(conn)` — `async create(mission_id=None, *, title=None, agent_role=None, model=None, host_id=None) -> str`; `async get(session_id) -> dict|None`; `async list(status=None) -> list[dict]`; `async touch(session_id) -> None` (updates `last_activity`).
- `class ServiceStore(conn)` — `async register(*, service_name, host_id, port, route=None, mission_id=None, session_id=None, owner=None, purpose=None) -> str`; `async get(service_id) -> dict|None`; `async list(host_id=None, mission_id=None) -> list[dict]`; `async update_health(service_id, health) -> None`; `async mark_stopped(service_id) -> None`.
- All stores accept a `conn` fake with `async fetchrow(sql,*a)->dict|None`, `async fetch(sql,*a)->list[dict]`, `async execute(sql,*a)`. Fakes record inserts/updates in-memory.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_models.py
import asyncio
from herdr_engineering.control_plane import models

class _MemConn:
    def __init__(self):
        self.tables = {"missions": {}, "sessions": {}, "services": {}}
    async def fetchrow(self, sql, *a):
        if "FROM missions" in sql: return self.tables["missions"].get(a[0])
        if "FROM sessions" in sql: return self.tables["sessions"].get(a[0])
        if "FROM services" in sql: return self.tables["services"].get(a[0])
        return None
    async def fetch(self, sql, *a):
        if "FROM missions" in sql:
            rows = list(self.tables["missions"].values())
            return [r for r in rows if (not a or r["status"] == a[0])]
        if "FROM sessions" in sql: return list(self.tables["sessions"].values())
        if "FROM services" in sql:
            rows = list(self.tables["services"].values())
            return rows
        return []
    async def execute(self, sql, *a):
        if sql.strip().startswith("INSERT INTO missions"):
            self.tables["missions"][a[0]] = dict(mission_id=a[0], title=a[1])
            return 1
        if sql.strip().startswith("INSERT INTO sessions"):
            self.tables["sessions"][a[0]] = dict(session_id=a[0], mission_id=a[1])
            return 1
        if sql.strip().startswith("INSERT INTO services"):
            self.tables["services"][a[0]] = dict(service_id=a[0], service_name=a[1])
            return 1
        return 1

def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)

def test_mission_crud_and_list_filter():
    conn = _MemConn(); ms = models.MissionStore(conn)
    mid = _run(ms.create("Dynamic Model Aliases", purpose="alias resolution"))
    assert mid.startswith("mission_")
    assert _run(ms.get(mid))["title"] == "Dynamic Model Aliases"
    assert len(_run(ms.list(status="active"))) == 1
    _run(ms.update(mid, status="complete"))
    assert _run(ms.get(mid))["status"] == "complete"
    assert len(_run(ms.list(status="active"))) == 0

def test_session_touch_and_service_lifecycle():
    conn = _MemConn()
    sid = _run(models.SessionStore(conn).create(mission_id="mission_x", agent_role="implementer"))
    assert sid.startswith("session_")
    svc = models.ServiceStore(conn)
    svcid = _run(svc.register(service_name="web", host_id="host_h", port=8080, mission_id="mission_x", session_id=sid))
    assert svcid.startswith("service_")
    _run(svc.update_health(svcid, "healthy"))
    assert _run(svc.get(svcid))["health"] == "healthy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_models.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** — each store is a thin parameterized-SQL accessor using `ids.new_id`, `fetchrow`/`fetch`/`execute`, with stable-ID PKs. Full implementation:

```python
# herdr_engineering/control_plane/models.py
"""Mission / Session / Service stores (Spec §19, §26-27, §45)."""
from __future__ import annotations
from typing import Any
from . import ids


class _Base:
    def __init__(self, conn: Any):
        self._conn = conn


class MissionStore(_Base):
    async def create(self, title: str, *, purpose=None, repository=None,
                     branch=None, worktree=None, owner=None) -> str:
        mid = ids.new_id("mission")
        await self._conn.execute(
            "INSERT INTO missions (mission_id, title, purpose, repository, "
            " branch, worktree, owner) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            mid, title, purpose, repository, branch, worktree, owner)
        return mid

    async def get(self, mission_id: str) -> dict | None:
        return await self._conn.fetchrow(
            "SELECT * FROM missions WHERE mission_id=$1", mission_id)

    async def list(self, status: str | None = None) -> list[dict]:
        if status is None:
            return await self._conn.fetch("SELECT * FROM missions")
        return await self._conn.fetch(
            "SELECT * FROM missions WHERE status=$1", status)

    async def update(self, mission_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=${i+1}" for i, k in enumerate(fields))
        await self._conn.execute(
            f"UPDATE missions SET {cols}, updated_at=now() "
            f"WHERE mission_id=${len(fields)+1}", *fields.values(), mission_id)


class SessionStore(_Base):
    async def create(self, mission_id=None, *, title=None, agent_role=None,
                     model=None, host_id=None) -> str:
        sid = ids.new_id("session")
        await self._conn.execute(
            "INSERT INTO sessions (session_id, mission_id, title, agent_role, "
            " model, host_id) VALUES ($1,$2,$3,$4,$5,$6)",
            sid, mission_id, title, agent_role, model, host_id)
        return sid

    async def get(self, session_id: str) -> dict | None:
        return await self._conn.fetchrow(
            "SELECT * FROM sessions WHERE session_id=$1", session_id)

    async def list(self, status: str | None = None) -> list[dict]:
        if status is None:
            return await self._conn.fetch("SELECT * FROM sessions")
        return await self._conn.fetch(
            "SELECT * FROM sessions WHERE status=$1", status)

    async def touch(self, session_id: str) -> None:
        await self._conn.execute(
            "UPDATE sessions SET last_activity=now() WHERE session_id=$1", session_id)


class ServiceStore(_Base):
    async def register(self, *, service_name: str, host_id: str, port: int,
                       route=None, mission_id=None, session_id=None,
                       owner=None, purpose=None) -> str:
        svcid = ids.new_id("service")
        await self._conn.execute(
            "INSERT INTO services (service_id, service_name, route, port, host_id, "
            " mission_id, session_id, owner, purpose) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            svcid, service_name, route, port, host_id, mission_id,
            session_id, owner, purpose)
        return svcid

    async def get(self, service_id: str) -> dict | None:
        return await self._conn.fetchrow(
            "SELECT * FROM services WHERE service_id=$1", service_id)

    async def list(self, host_id=None, mission_id=None) -> list[dict]:
        clauses, args = [], []
        if host_id:
            clauses.append(f"host_id=${len(args)+1}"); args.append(host_id)
        if mission_id:
            clauses.append(f"mission_id=${len(args)+1}"); args.append(mission_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self._conn.fetch(f"SELECT * FROM services{where}", *args)

    async def update_health(self, service_id: str, health: str) -> None:
        await self._conn.execute(
            "UPDATE services SET health=$1, last_seen=now() WHERE service_id=$2",
            health, service_id)

    async def mark_stopped(self, service_id: str) -> None:
        await self._conn.execute(
            "UPDATE services SET status='stopped', stopped_at=now() "
            "WHERE service_id=$1", service_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_models.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add herdr_engineering/control_plane/models.py tests/control_plane/test_models.py
git commit -m "feat(control-plane): mission/session/service stores with provenance"
```

---

### Task 7: State snapshots + replay-after

**Files:**
- Create: `herdr_engineering/control_plane/snapshots.py`
- Test: `tests/control_plane/test_snapshots.py`

**Interfaces:**
- `class SnapshotStore(conn)`:
  - `async save(entity_kind: str, entity_id: str, state: dict, sequence: int) -> None` — upsert into `state_snapshots` (replace if same `sequence`, else insert).
  - `async load_latest(entity_kind: str, entity_id: str) -> tuple[dict, int] | None` — returns `(state, sequence)` of the max-`sequence` snapshot for that entity.
  - `async replay(entity_kind, entity_id, event_store, after: int) -> list[Event]` — convenience: delegates to `event_store.read_stream(entity_kind, entity_id, after=after)`.
- `async recover(entity_kind, entity_id, snapshot_store, event_store) -> dict` — loads latest snapshot, replays later events, folds each `Event.payload` over the state, returns final state.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_snapshots.py
import asyncio
from herdr_engineering.control_plane import snapshots, events

class _SnapConn:
    def __init__(self): self.snaps = []
    async def execute(self, sql, *a):
        if "INSERT INTO state_snapshots" in sql:
            self.snaps.append({"entity_kind": a[0], "entity_id": a[1],
                               "sequence": a[2], "state": a[3]})
        return 1
    async def fetchrow(self, sql, *a):
        cands = [s for s in self.snaps if s["entity_kind"]==a[0] and s["entity_id"]==a[1]]
        if not cands: return None
        best = max(cands, key=lambda s: s["sequence"])
        return {"state": best["state"], "sequence": best["sequence"]}

def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)

def test_snapshot_save_load_latest():
    conn = _SnapConn(); ss = snapshots.SnapshotStore(conn)
    _run(ss.save("mission", "m1", {"progress": 0.5}, 10))
    _run(ss.save("mission", "m1", {"progress": 0.9}, 20))
    state, seq = _run(ss.load_latest("mission", "m1"))
    assert state == {"progress": 0.9} and seq == 20

def test_recover_folds_later_events():
    conn = _SnapConn(); ss = snapshots.SnapshotStore(conn)
    _run(ss.save("mission", "m1", {"progress": 0.5, "stage": "implementation"}, 10))
    es = events.EventStore(_FakeEvents())
    state = _run(snapshots.recover("mission", "m1", ss, es))
    assert state["progress"] == 0.7 and state["stage"] == "review"

class _FakeEvents:
    def __init__(self): pass
    async def read_stream(self, entity_type, entity_id, after=0):
        return [
            events.Event(event_id="a", entity_type=entity_type, entity_id=entity_id,
                         sequence=11, event_type="MissionProgressUpdated",
                         payload={"progress": 0.6}),
            events.Event(event_id="b", entity_type=entity_type, entity_id=entity_id,
                         sequence=12, event_type="MissionStageChanged",
                         payload={"stage": "review"}),
            events.Event(event_id="c", entity_type=entity_type, entity_id=entity_id,
                         sequence=13, event_type="MissionProgressUpdated",
                         payload={"progress": 0.7}),
        ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_snapshots.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# herdr_engineering/control_plane/snapshots.py
"""State snapshots + replay-after (Spec §55)."""
from __future__ import annotations
from typing import Any


class SnapshotStore:
    def __init__(self, conn: Any):
        self._conn = conn

    async def save(self, entity_kind: str, entity_id: str,
                   state: dict[str, Any], sequence: int) -> None:
        await self._conn.execute(
            "INSERT INTO state_snapshots (entity_kind, entity_id, sequence, state) "
            "VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (entity_kind, entity_id, sequence) "
            "DO UPDATE SET state=EXCLUDED.state, created_at=now()",
            entity_kind, entity_id, sequence, state)

    async def load_latest(self, entity_kind: str, entity_id: str):
        row = await self._conn.fetchrow(
            "SELECT state, sequence FROM state_snapshots "
            "WHERE entity_kind=$1 AND entity_id=$2 "
            "ORDER BY sequence DESC LIMIT 1", entity_kind, entity_id)
        if row is None:
            return None
        return (row["state"], row["sequence"])

    async def replay(self, entity_kind: str, entity_id: str,
                     event_store: Any, after: int = 0):
        return await event_store.read_stream(entity_kind, entity_id, after=after)


async def recover(entity_kind: str, entity_id: str,
                  snapshot_store: SnapshotStore, event_store: Any) -> dict[str, Any]:
    """Load latest snapshot, replay later events, fold payloads over state."""
    loaded = await snapshot_store.load_latest(entity_kind, entity_id)
    state: dict[str, Any] = {}
    after = 0
    if loaded is not None:
        state, after = loaded
    for event in await snapshot_store.replay(entity_kind, entity_id, event_store, after=after):
        for key, value in event.payload.items():
            state[key] = value
    return state
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_snapshots.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add herdr_engineering/control_plane/snapshots.py tests/control_plane/test_snapshots.py
git commit -m "feat(control-plane): state snapshots + replay-after recovery"
```

---

### Task 8: RustFS content-addressed client

**Files:**
- Create: `herdr_engineering/control_plane/rustfs.py`
- Test: `tests/control_plane/test_rustfs.py`

**Interfaces:**
- `content_key(data: bytes) -> str` — returns `f"sha256/{hex[0:2]}/{hex[2:4]}/{hex}"` (content-addressed, Spec §57).
- `class RustFSClient` with `__init__(self, *, endpoint: str, bucket: str, region: str = "us-east-1", s3: Any | None = None, sha: Any = hashlib.sha256)`:
  - `put(data: bytes) -> tuple[str, str]` — computes sha256, `put_object(Bucket, Key=content_key, Body=data)`, returns `(content_key, sha256_hex)`.
  - `get(content_key: str, expected_sha256: str) -> bytes` — `get_object` body; raises `IntegrityError` if `sha256(body) != expected_sha256` (Spec §69).
  - `head(content_key: str) -> bool` — exists check.
  - `delete(content_key: str) -> None`.
- `class IntegrityError(Exception)`.
- Tests use a fake `s3` recording `put_object`/`get_object`/`head_object`/`delete_object`.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_rustfs.py
import hashlib
from herdr_engineering.control_plane import rustfs

class _FakeS3:
    def __init__(self): self.objects = {}; self.calls = []
    def put_object(self, *, Bucket, Key, Body):
        self.calls.append(("put", Key)); self.objects[Key] = Body
    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Key))
        if Key not in self.objects: raise KeyError(Key)
        return {"Body": self.objects[Key]}
    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Key)); return Key in self.objects
    def delete_object(self, *, Bucket, Key):
        self.calls.append(("del", Key)); self.objects.pop(Key, None)

def _client():
    return rustfs.RustFSClient(endpoint="http://x", bucket="herdr-artifacts", s3=_FakeS3())

def test_content_key_layout():
    data = b"hello"
    hex_ = hashlib.sha256(data).hexdigest()
    assert rustfs.content_key(data) == f"sha256/{hex_[:2]}/{hex_[2:4]}/{hex_}"

def test_put_get_roundtrip_and_integrity():
    c = _client(); data = b"payload-bytes"
    key, sha = c.put(data)
    assert c.get(key, sha) == data
    assert c.head(key) is True

def test_get_rejects_corruption():
    c = _client(); data = b"payload-bytes"
    key, _ = c.put(data)
    c._s3.objects[key] = b"tampered"  # corrupt after store
    try:
        c.get(key, hashlib.sha256(data).hexdigest())
        raise AssertionError("expected IntegrityError")
    except rustfs.IntegrityError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_rustfs.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# herdr_engineering/control_plane/rustfs.py
"""RustFS / S3 content-addressed object store client (Spec §56-57, §69)."""
from __future__ import annotations
import hashlib
from typing import Any


class IntegrityError(Exception):
    """SHA-256 verification failed for an object retrieved from RustFS."""


def content_key(data: bytes) -> str:
    hex_ = hashlib.sha256(data).hexdigest()
    return f"sha256/{hex_[:2]}/{hex_[2:4]}/{hex_}"


class RustFSClient:
    """Thin S3-compatible client over RustFS. ``s3`` is a boto3 client or fake."""

    def __init__(self, *, endpoint: str, bucket: str,
                 region: str = "us-east-1", s3: Any | None = None,
                 sha: Any = hashlib.sha256):
        self._bucket = bucket
        self._sha = sha
        if s3 is not None:
            self._s3 = s3
        else:
            import boto3
            self._s3 = boto3.client(
                "s3", endpoint_url=endpoint,
                region_name=region,
                aws_access_key_id="herdr", aws_secret_access_key="herdr",
            )

    def put(self, data: bytes) -> tuple[str, str]:
        digest = self._sha(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return key, digest

    def get(self, content_key: str, expected_sha256: str) -> bytes:
        body = self._s3.get_object(Bucket=self._bucket, Key=content_key)["Body"]
        data = body.read() if hasattr(body, "read") else bytes(body)
        if self._sha(data).hexdigest() != expected_sha256:
            raise IntegrityError(f"sha256 mismatch for {content_key}")
        return data

    def head(self, content_key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=content_key)
            return True
        except Exception:
            return False

    def delete(self, content_key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=content_key)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_rustfs.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add herdr_engineering/control_plane/rustfs.py tests/control_plane/test_rustfs.py
git commit -m "feat(control-plane): content-addressed RustFS client with integrity"
```

---

### Task 9: Artifact model + bindings + materialization

**Files:**
- Create: `herdr_engineering/control_plane/artifacts.py`
- Test: `tests/control_plane/test_artifacts.py`

**Interfaces:**
- `class ArtifactStore(conn, rustfs)`:
  - `async register(*, original_filename, mime_type, data: bytes, artifact_type="other", created_by=None, mission_id=None, session_id=None, retention_class="temporary") -> str` — puts bytes to RustFS (content-addressed), inserts artifact row with `storage_key`, `sha256`, `size_bytes`, status `verifying`→`available`.
  - `async get(artifact_id) -> dict|None`
  - `async download(artifact_id) -> bytes` — reads `storage_key`, verifies sha256, raises `IntegrityError` on mismatch.
  - `async attach(artifact_id, *, mission_id=None, session_id=None, stage=None, created_by=None) -> int` — insert into `artifact_bindings`.
  - `async materialize(artifact_id, host_id, session_id=None, local_path=None, verified=False) -> int` — insert into `artifact_materializations`.
  - `async mark_available(artifact_id) -> None`.
- `class ArtifactError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_artifacts.py
import asyncio, hashlib
from herdr_engineering.control_plane import artifacts, rustfs as rustfs_mod

class _FakeS3:
    def __init__(self): self.objects = {}
    def put_object(self, *, Bucket, Key, Body): self.objects[Key] = Body
    def get_object(self, *, Bucket, Key):
        return {"Body": self.objects.get(Key, b"")}
    def head_object(self, *, Bucket, Key): return Key in self.objects
    def delete_object(self, *, Bucket, Key): self.objects.pop(Key, None)

class _Conn:
    def __init__(self): self.art = {}; self.bindings = []; self.mats = []
    async def execute(self, sql, *a):
        if "INSERT INTO artifacts" in sql:
            self.art[a[0]] = dict(artifact_id=a[0], original_filename=a[1])
        elif "INSERT INTO artifact_bindings" in sql:
            self.bindings.append(a)
        elif "INSERT INTO artifact_materializations" in sql:
            self.mats.append(a)
        elif "UPDATE artifacts SET status" in sql:
            self.art[a[0]] = {**self.art[a[0]], "status": a[1]}
        return 1
    async def fetchrow(self, sql, *a):
        return self.art.get(a[0])
    async def fetch(self, sql, *a): return list(self.art.values())

def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)
def _store():
    rf = rustfs_mod.RustFSClient(endpoint="http://x", bucket="b", s3=_FakeS3())
    return artifacts.ArtifactStore(_Conn(), rf), rf

def test_register_makes_available_with_metadata():
    store, rf = _store()
    aid = _run(store.register(original_filename="shot.png", mime_type="image/png",
                              data=b"PNG", artifact_type="screenshot"))
    assert aid.startswith("artifact_")
    row = _run(store.get(aid))
    assert row["status"] == "available" and row["size_bytes"] == 3
    assert row["storage_key"].startswith("sha256/")
    assert _run(store.download(aid)) == b"PNG"

def test_download_rejects_tampered_bytes():
    store, rf = _store()
    aid = _run(store.register(original_filename="a.txt", mime_type="text/plain", data=b"safe"))
    rf._s3.objects[rf._s3.objects and list(rf._s3.objects.keys())[0]] = b"evil"
    try:
        _run(store.download(aid)); raise AssertionError("expected IntegrityError")
    except artifacts.IntegrityError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_artifacts.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# herdr_engineering/control_plane/artifacts.py
"""Artifact model + bindings + materialization (Spec §56-74)."""
from __future__ import annotations
import hashlib
from typing import Any
from . import ids
from .rustfs import IntegrityError, RustFSClient


class ArtifactError(Exception):
    pass


class ArtifactStore:
    def __init__(self, conn: Any, rustfs: RustFSClient):
        self._conn = conn
        self._rf = rustfs

    async def register(self, *, original_filename: str, mime_type: str,
                       data: bytes, artifact_type: str = "other",
                       created_by: str | None = None,
                       mission_id: str | None = None,
                       session_id: str | None = None,
                       retention_class: str = "temporary") -> str:
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self._rf.put(data)
        aid = ids.new_id("artifact")
        await self._conn.execute(
            "INSERT INTO artifacts (artifact_id, original_filename, mime_type, "
            " artifact_type, created_by, size_bytes, sha256, storage_backend, "
            " storage_key, status, retention_class) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,'rustfs',$8,'available',$9)",
            aid, original_filename, mime_type, artifact_type, created_by,
            len(data), digest, key, retention_class)
        if mission_id or session_id:
            await self.attach(aid, mission_id=mission_id,
                              session_id=session_id, created_by=created_by)
        return aid

    async def get(self, artifact_id: str) -> dict | None:
        return await self._conn.fetchrow(
            "SELECT * FROM artifacts WHERE artifact_id=$1", artifact_id)

    async def download(self, artifact_id: str) -> bytes:
        row = await self.get(artifact_id)
        if not row:
            raise ArtifactError(f"no artifact {artifact_id}")
        data = self._rf.get(row["storage_key"], row["sha256"])
        return data

    async def attach(self, artifact_id: str, *, mission_id=None,
                     session_id=None, stage=None, created_by=None) -> int:
        await self._conn.execute(
            "INSERT INTO artifact_bindings (artifact_id, mission_id, session_id, "
            " stage, created_by) VALUES ($1,$2,$3,$4,$5)",
            artifact_id, mission_id, session_id, stage, created_by)
        return 1

    async def materialize(self, artifact_id: str, host_id: str,
                          session_id=None, local_path=None,
                          verified=False) -> int:
        await self._conn.execute(
            "INSERT INTO artifact_materializations (artifact_id, host_id, "
            " session_id, local_path, status, sha256_verified) "
            "VALUES ($1,$2,$3,$4,'materialized',$5)",
            artifact_id, host_id, session_id, local_path, verified)
        return 1

    async def mark_available(self, artifact_id: str) -> None:
        await self._conn.execute(
            "UPDATE artifacts SET status='available' WHERE artifact_id=$1", artifact_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_artifacts.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add herdr_engineering/control_plane/artifacts.py tests/control_plane/test_artifacts.py
git commit -m "feat(control-plane): artifact model, bindings, materialization"
```

---

### Task 10: Background worker scaffold

**Files:**
- Create: `herdr_engineering/control_plane/worker.py`
- Test: `tests/control_plane/test_worker.py`

**Interfaces:**
- `async def run_one_pass(conn, *, mission_store=None, session_store=None, service_store=None, snapshot_store=None, artifact_store=None) -> dict` — performs one bounded reconciliation pass:
  - **stale leases**: services with `last_seen` older than `lease_ttl` (default 90s) and not stopped → `update_health(service_id, "unavailable")` (never delete).
  - **host health**: hosts with `last_heartbeat` older than `host_ttl` (default 300s) → mark `unavailable` (never delete).
  - **snapshots**: for each active mission, `save(entity_kind="mission", entity_id, state=mission_row, sequence=<max mission_event seq>)` via `SnapshotStore`.
  - **artifact GC**: delete RustFS objects whose `retention_class` is `temporary` and that have no bindings and are older than `artifact_ttl` (default 3600s) — reference/reachability-based (Spec §72).
  - returns a summary dict of what it did.
- `async def run_worker(conn, *, interval: float = 30.0, stop: asyncio.Event | None = None, **stores) -> None` — infinite loop calling `run_one_pass`, sleeping `interval`, bounded (never raises out; logs and continues), exits when `stop` set.
- Stores are the classes from `models.py` / `snapshots.py` / `artifacts.py`, each defaulting to a no-op if not supplied (so `run_one_pass` is testable with a subset).

- [ ] **Step 1: Write the failing test**

```python
# tests/control_plane/test_worker.py
import asyncio
from datetime import datetime, timezone, timedelta
from herdr_engineering.control_plane import worker

class _Svc:
    def __init__(self): self.updated = []
    async def list(self, host_id=None, mission_id=None):
        return [{"service_id": "s1", "health": "healthy",
                 "last_seen": datetime.now(timezone.utc) - timedelta(seconds=999)}]
    async def update_health(self, service_id, health): self.updated.append((service_id, health))

class _Host:
    def __init__(self): self.updated = []
    async def fetch(self, sql, *a):  # list hosts
        return [{"host_id": "h1",
                 "last_heartbeat": datetime.now(timezone.utc) - timedelta(seconds=9999)}]
    async def execute(self, sql, *a): self.updated.append(sql)

def _run(coro): return asyncio.get_event_loop().run_until_complete(coro)

def test_worker_marks_stale_services_unavailable_not_deleted():
    svc = _Svc()
    summary = _run(worker.run_one_pass(None, service_store=svc))
    assert summary["stale_services"] == ["s1"]
    assert svc.updated == [("s1", "unavailable")]

def test_worker_loop_obeys_stop_and_never_raises():
    class _Bad:
        async def list(self, **k): raise RuntimeError("boom")
    stop = asyncio.Event()
    async def stopper():
        stop.set()
    loop = asyncio.get_event_loop()
    loop.call_later(0.1, lambda: loop.create_task(stopper()))
    _run(worker.run_worker(None, interval=0.01, stop=stop,
                           service_store=_Bad(), mission_store=None,
                           session_store=None, host_store=None,
                           snapshot_store=None, artifact_store=None))
    # completes without raising
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/control_plane/test_worker.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# herdr_engineering/control_plane/worker.py
"""Background worker scaffold (Spec §95): stale leases, health reconcile,
snapshots, artifact GC. Bounded, never raises, run outside the web frontend."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)
LEASE_TTL = 90.0
HOST_TTL = 300.0
ARTIFACT_TTL = 3600.0


async def run_one_pass(conn, *, mission_store=None, session_store=None,
                       service_store=None, host_store=None,
                       snapshot_store=None, artifact_store=None,
                       lease_ttl=LEASE_TTL, host_ttl=HOST_TTL,
                       artifact_ttl=ARTIFACT_TTL) -> dict:
    summary: dict[str, Any] = {}

    # Stale services -> unavailable (never delete).
    stale_services = []
    if service_store is not None:
        for svc in await service_store.list():
            last = svc.get("last_seen")
            if last is None:
                continue
            if datetime.now(timezone.utc) - last > timedelta(seconds=lease_ttl) \
                    and svc.get("health") != "unavailable" \
                    and svc.get("status") != "stopped":
                await service_store.update_health(svc["service_id"], "unavailable")
                stale_services.append(svc["service_id"])
    summary["stale_services"] = stale_services

    # Stale hosts -> unavailable (never delete).
    stale_hosts = []
    if host_store is not None:
        rows = await host_store.fetch("SELECT host_id, last_heartbeat FROM hosts")
        for row in rows:
            hb = row.get("last_heartbeat")
            if hb is None:
                continue
            if datetime.now(timezone.utc) - hb > timedelta(seconds=host_ttl):
                await host_store.execute(
                    "UPDATE hosts SET status='unavailable' WHERE host_id=$1",
                    row["host_id"])
                stale_hosts.append(row["host_id"])
    summary["stale_hosts"] = stale_hosts

    # Snapshots for active missions.
    snapshots = 0
    if mission_store is not None and snapshot_store is not None:
        for m in await mission_store.list(status="active"):
            await snapshot_store.save("mission", m["mission_id"],
                                      dict(m), sequence=0)
            snapshots += 1
    summary["snapshots"] = snapshots

    # Artifact GC: temporary, unbound, older than artifact_ttl -> delete object.
    gc = 0
    if artifact_store is not None:
        # Phase 1 scaffold: mark-only hook; real reachability GC in Phase 5.
        summary["artifact_gc"] = gc
    else:
        summary["artifact_gc"] = gc
    return summary


async def run_worker(conn, *, interval: float = 30.0, stop: asyncio.Event | None = None,
                     **stores) -> None:
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            await run_one_pass(conn, **stores)
        except Exception as exc:  # bounded: never crash the worker
            log.exception("worker pass failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/control_plane/test_worker.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add herdr_engineering/control_plane/worker.py tests/control_plane/test_worker.py
git commit -m "feat(control-plane): bounded background worker scaffold"
```

---

### Task 11: Compose integration + full-suite validation + docs

**Files:**
- Modify: `deploy/fabric-stack/compose.yaml` (add `postgres`, `rustfs`, `control-plane-worker` services; keep `coredns`/`fabric-gateway`/`herdr-web` unchanged)
- Modify: `deploy/fabric-stack/README.md` (document new services + that the live stack is not torn down)
- Modify: `CHANGELOG.md`
- Test: full suite

**Interfaces:**
- New compose services (Phase 1 scaffolding; the live stack on bender is left untouched until an explicit deploy):
  - `postgres`: `postgres:16-alpine`, named volume `postgres-data`, healthcheck `pg_isready`.
  - `rustfs`: S3-compatible object store image serving `herdr-artifacts` on an internal port, named volume `rustfs-data`.
  - `control-plane-worker`: `herdr-engineering:0.1.0`, `network_mode: host`, runs `herdr-eng control-plane worker` (new CLI subcommand added in Task 10's commit), env DSN pointing at postgres.

- [ ] **Step 1: Add the new compose services** (append to `compose.yaml`)

```yaml
  postgres:
    image: postgres:16-alpine
    container_name: herdr-control-postgres
    restart: unless-stopped
    network_mode: host
    environment:
      POSTGRES_USER: herdr
      POSTGRES_PASSWORD: herdr
      POSTGRES_DB: herdr
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ../control-plane/migrations:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U herdr"]
      interval: 10s
      timeout: 3s
      retries: 5

  rustfs:
    image: rustfs:latest
    container_name: herdr-control-rustfs
    restart: unless-stopped
    network_mode: host
    environment:
      RUSTFS_BUCKET: herdr-artifacts
    volumes:
      - rustfs-data:/data
    command: ["serve", "s3://:9000"]

  control-plane-worker:
    image: herdr-engineering:0.1.0
    container_name: herdr-control-plane-worker
    restart: unless-stopped
    network_mode: host
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      HERDR_ENGINEERING_CONFIG: /app/config.yaml
    command: ["control-plane", "worker"]

volumes:
  herdr-state:
  postgres-data:
  rustfs-data:
```

- [ ] **Step 2: Add the `control-plane worker` CLI entrypoint** in `herdr_engineering/cli.py` (a thin subcommand calling `control_plane.worker.run_worker` against a configured DSN). Wire `control_plane_config` for the DSN.

- [ ] **Step 3: Run the full suite + ruff**

Run: `.venv/bin/python -m pytest -q` and `.venv/bin/ruff check herdr_engineering tests`
Expected: all tests pass (existing 136 + new ~24), ruff clean.

- [ ] **Step 4: Update `CHANGELOG.md`** with a Phase 1 entry, and `deploy/fabric-stack/README.md` noting the new services and that the live fleet stack was not torn down.

- [ ] **Step 5: Commit**

```bash
git add deploy/fabric-stack/compose.yaml deploy/fabric-stack/README.md \
        CHANGELOG.md herdr_engineering/cli.py
git commit -m "feat(control-plane): compose services + worker CLI entrypoint (Phase 1)"
```

- [ ] **Step 6: Push**

```bash
git push origin main
```

---

## Self-Review

**1. Spec coverage — Phase 1 scope.** Every Phase-1 requirement has a task:
- Docker Compose additions (postgres, rustfs, worker) → Task 11.
- PostgreSQL + migrations from day one, all §53 tables, JSONB/FK/indexes → Tasks 3, 4.
- Entity IDs (stable identity) → Task 2.
- Event envelope / sequencing / idempotent ingestion / UTC → Task 5.
- Mission/session/service models + provenance → Task 6.
- State snapshots + replay-after → Task 7.
- RustFS content-addressed + integrity → Task 8.
- Artifact model / bindings / materialization → Task 9.
- Background worker (leases, health reconcile, snapshots, GC) → Task 10.

**2. Placeholder scan.** No TBD/TODO stubs in test or implementation code. The only intentional scaffold hook is `artifact_gc` in the worker, which is explicitly deferred to Phase 5 (documented, not a placeholder for a required behavior). The `rustfs` compose image tag is a placeholder pending validation — flagged as a known open item for the deploy task.

**3. Type consistency.** `new_id` returns `f"{kind}_<26-char>"`; `is_valid_id` validates the same shape — used consistently in Task 6 and Task 9 (`mission_`, `session_`, `service_`, `artifact_` prefixes). `RustFSClient.put` returns `(content_key, sha256_hex)` and `get(key, expected_sha256)` — Task 9 consumes them consistently. `EventStore.append` returns `bool`; `read_stream(after)` returns ordered `list[Event]`; `SnapshotStore.load_latest` returns `(state, sequence)`; `recover` folds `event.payload` keys over state — consistent across Tasks 5–7.

## Known open items (resolve in execution)
- **RustFS container image**: verify the exact `rustfs` image/tag and its S3 endpoint entrypoint before deploying (Task 11 Step 1). If RustFS has no maintained image, use a drop-in S3-compatible store (e.g. `minio/minio`) for Phase 1 and note the swap — the client is image-agnostic (boto3/S3).
- **CLI entrypoint for the worker** (Task 11 Step 2): the new subcommand must wire the DSN from `control_plane_config`; keep it consistent with the existing `cli.py` argparse pattern.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-10-06-herdr-control-plane-phase1-foundation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
