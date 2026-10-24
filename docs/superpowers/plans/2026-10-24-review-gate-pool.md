# Review Gate Reviewer Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One command runs a pool of review workers that grows with the review queue's depth and retires idle workers.

**Architecture:** `herdr-eng review-gate pool --config FILE` runs one `ReviewWorker` per slot, each on its own thread with its own worker id, in a single process. A supervisor loop reads `GET /api/v1/review-gate/queue` (the `reviewer` key already may), counts the jobs this pool's model may claim, and adds or retires slots between `min_workers` and `max_workers`. Nothing new on the control plane.

**Tech Stack:** Python 3, stdlib threading, PyYAML (as in `worker.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-automated-review-merge-gate-design.md` (Reviewer pool, line 56; "scaling algorithm ... left to configuration with conservative defaults", line 79). Plan 2: `docs/superpowers/plans/2026-10-23-review-gate-reviewer.md`.

## Rulings (recorded here because the spec leaves them open)

- The spec says pool workers are herdr agent sessions started like `collab-spawn.py`. The review worker is a text-only HTTP loop with no harness, so a herdr workspace per worker buys nothing. Slots are threads in one process. More hosts or more models = more pool processes, each with its own `worker_id` prefix and `max_workers`. Cost if wrong: a pool process is one failure domain; a crash drops all its slots (their leases expire and the jobs are re-offered).
- Scaling rule: `wanted = clamp(len(slots) + min(eligible_depth, max_workers - len(slots)), min, max)` when `eligible_depth > 0`, else retire idle slots one per evaluation down to `min_workers`. Idle slots already claim queued jobs, so a non-zero queue means every slot is busy or backing off. Cost if wrong: a slot in failure backoff counts as busy and can trigger a scale-up; bounded by `max_workers`.
- A job is eligible for this pool when its `state` is `queued` and its `author_model` differs from the pool's (redacted) model, mirroring `ReviewQueue.claim`.

## Global Constraints

- No environment variables; YAML config only (operator rule 2026-10-21).
- The pool must never claim a job and then abandon it because of scaling: a slot is retired only between claim cycles.
- A 401/403 from any slot stops the whole pool with the same message and exit code as `worker`.
- Keys are read per call by `ReviewWorker`; the pool never reads or logs key contents.
- Log lines carry class names and counts only, like `worker.py`.
- Reuse `worker.py` parsing helpers (`_mapping`, `_keys`, `_int`, `_ident`, `_check_url`, `_key_file`); do not duplicate them.

## Review Focus

- A scale-down racing a claim: a slot marked for retirement must not claim afterwards.
- `/queue` unreachable, 401, malformed or enormous body: the pool keeps its current size, never crashes, backs off.
- Queue jobs the pool's model may not claim (it authored them): must not grow the pool.
- `min_workers: 0` with an empty queue, then a job appears: the pool must start a slot.
- Config nonsense: `min > max`, `max` above 64, bool as int, duplicate keys, unknown keys.

---

### Task 1: Pool config and sizing function

**Files:**
- Create: `herdr_engineering/review_gate/pool.py`
- Test: `tests/review_gate/test_pool.py`

**Interfaces:**
- Produces: `PoolConfig(worker: WorkerConfig, min_workers: int, max_workers: int, idle_seconds: int, interval_seconds: int)`; `load_pool_config(path: str) -> PoolConfig` (raises `WorkerConfigError`); `wanted_slots(current: int, eligible_depth: int, idle: int, cfg: PoolConfig) -> int`; `eligible_depth(queue: dict, model: str) -> int`.
- Consumes: `WorkerConfig`, `WorkerConfigError`, `load_worker_config` internals from `worker.py`.

Pool YAML is the worker YAML plus a `pool` mapping: `min_workers` (default 1, 0..64), `max_workers` (default 4, 1..64, must be >= min), `idle_seconds` (default 120, 10..86400), `interval_seconds` (default 10, 1..3600). `worker_id` is the prefix; slot ids are `<worker_id>-<n>` and must still satisfy the worker id rules (check length at load time with the largest n).

- [ ] **Step 1: Tests** (names): `test_pool_config_defaults`, `test_pool_config_rejects_min_above_max`, `test_pool_config_rejects_bool_and_unknown_keys`, `test_pool_config_rejects_overlong_worker_id_prefix`, `test_wanted_slots_grows_by_eligible_depth_up_to_max`, `test_wanted_slots_retires_one_idle_slot_down_to_min`, `test_wanted_slots_starts_one_from_zero`, `test_eligible_depth_skips_claimed_and_self_authored`, `test_eligible_depth_ignores_malformed_rows`.
- [ ] **Step 2: Run, expect failures.** `PYTHONPATH=. pytest tests/review_gate/test_pool.py -q`
- [ ] **Step 3: Implement** the config loader (reuse `load_worker_config` for the worker part by splitting the `pool` key out of the mapping before validation) and the two pure functions. `idle` is the count of slots idle longer than `idle_seconds`; retire at most one per call.
- [ ] **Step 4: Tests pass.**
- [ ] **Step 5: Commit** `feat(review-gate): pool config and sizing`.

### Task 2: Worker hooks and the pool supervisor

**Files:**
- Modify: `herdr_engineering/review_gate/worker.py` (`ReviewWorker.run` only)
- Modify: `herdr_engineering/review_gate/pool.py`
- Test: `tests/review_gate/test_pool.py`, `tests/review_gate/test_worker.py`

**Interfaces:**
- Produces: `ReviewWorker.run(stop, *, before_cycle=None, after_cycle=None)` where `before_cycle() -> bool` (False = exit without claiming) is called immediately before each `run_once`, and `after_cycle(claimed: bool)` right after; `ReviewPool(cfg, *, http=None, clock=time.monotonic, worker_factory=ReviewWorker)` with `run(stop: threading.Event) -> None` and `evaluate() -> None` (one scaling decision, callable from tests).
- Consumes: Task 1.

Behaviour: each slot thread runs `worker.run(slot_stop, before_cycle=..., after_cycle=...)`. A per-slot lock makes "mark retiring" and "before_cycle" mutually exclusive, and retiring is only allowed while the slot is outside a cycle; a retiring slot's `before_cycle` returns False. `after_cycle(True)` resets the slot's idle clock; `after_cycle(False)` leaves it. The supervisor fetches `GET <control_plane_url>/api/v1/review-gate/queue` with `X-API-Key` (same transport, 1 MiB cap, no redirects, http only on loopback, like the worker), calls `evaluate` every `interval_seconds`, starts slots up to `wanted`, reaps finished threads. A `WorkerAuthError` or `WorkerConfigError` from a slot sets the pool's stop and is re-raised by `run`. On a queue fetch failure keep the size and wait `interval_seconds` doubled up to 300 s. On start the pool starts `min_workers` slots. On stop it sets every slot's stop and joins (a slot mid-job abandons it, as `worker.run` documents).

- [ ] **Step 1: Tests**: in `test_worker.py`, `test_run_calls_hooks_around_each_cycle` and `test_before_cycle_false_exits_without_claiming`; in `test_pool.py`, with a fake `http` and fake workers: `test_pool_starts_min_workers`, `test_pool_grows_when_queue_has_eligible_jobs`, `test_pool_does_not_grow_for_self_authored_jobs`, `test_pool_retires_one_idle_slot_per_evaluation_to_min`, `test_retiring_slot_never_claims`, `test_pool_keeps_size_when_queue_unreachable`, `test_pool_stops_all_slots_on_auth_error`, `test_pool_starts_slot_from_zero_min`, `test_pool_ignores_oversized_or_malformed_queue_body`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the hooks (default None keeps current behaviour exactly) and `ReviewPool`. Slot worker ids are unique and never reused while a thread with that id is alive.
- [ ] **Step 4: Tests pass; the existing `test_worker.py` still passes unchanged.**
- [ ] **Step 5: Commit** `feat(review-gate): reviewer pool supervisor`.

### Task 3: CLI, docs and end-to-end

**Files:**
- Modify: `herdr_engineering/cli.py` (new `review-gate pool --config FILE` beside `worker`, same error handling and exit codes)
- Modify: `docs/review-gate.md`
- Test: `tests/review_gate/test_e2e_loop.py` (one new test), `tests/test_cli.py` or the file that covers `review-gate worker` (parser test)

**Interfaces:** Consumes `load_pool_config`, `ReviewPool`.

- [ ] **Step 1: Tests**: `test_pool_command_parses_and_runs_with_stop` (mirrors the worker command test); `test_e2e_pool_drains_a_burst` in the e2e file: enqueue 6 PRs against the existing live-loop fixture, run a pool with min 1 / max 3 and a fake model, assert all six reach a verdict, the pool grew above 1 and retired back to `min_workers` once the queue emptied (bound the wait with a deadline, not a sleep).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the command; document in `docs/review-gate.md`: the pool YAML with an example, the scaling rule, one pool per host/model, a systemd unit example (no environment), the failure behaviour (queue unreachable, auth error exit), and that pool size is per process.
- [ ] **Step 4: Run the gate tests and `ruff check` / `ruff format --check` on touched files.** `PYTHONPATH=. pytest tests/review_gate tests/control_plane/test_review_gate_api.py tests/control_plane/test_review_gate_reviewer_api.py -q -n0`
- [ ] **Step 5: Commit** `feat(review-gate): pool command and docs`.
