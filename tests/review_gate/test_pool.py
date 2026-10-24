"""Reviewer pool (review gate, plan 3): config, sizing, supervisor."""

import json
import threading
import time

import pytest
import yaml

from herdr_engineering.review_gate.pool import (
    PoolConfig,
    ReviewPool,
    _Slot,
    eligible_depth,
    load_pool_config,
    wanted_slots,
)
from herdr_engineering.review_gate.worker import (
    MAX_RESPONSE_BYTES,
    ModelConfig,
    WorkerAuthError,
    WorkerConfig,
    WorkerConfigError,
)

CP = "https://cp.example.org"
MODEL_URL = "https://llm.example.org"
REVIEW_MODEL = "qwen3-review"


def write_key(tmp_path, name="cp.key", text="rk-reviewer-SECRET"):
    path = tmp_path / name
    path.write_text(text + "\n")
    path.chmod(0o600)
    return str(path)


def good_yaml(tmp_path, **pool):
    d = {
        "control_plane_url": CP,
        "api_key_file": write_key(tmp_path),
        "worker_id": "pool-1",
        "model": {"base_url": MODEL_URL, "model": REVIEW_MODEL},
    }
    if pool:
        d["pool"] = pool
    return d


def write_yaml(tmp_path, data):
    path = tmp_path / "pool.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


def make_pool_config(tmp_path, **over):
    worker = WorkerConfig(
        control_plane_url=CP,
        api_key_file=write_key(tmp_path),
        worker_id="pool-1",
        model=ModelConfig(base_url=MODEL_URL, model=REVIEW_MODEL, api_key_file=None),
        poll_seconds=1,
    )
    vals = {"min_workers": 1, "max_workers": 4, "idle_seconds": 120, "interval_seconds": 10}
    vals.update(over)
    return PoolConfig(worker=worker, **vals)


# ---------------------------------------------------------------------- config
def test_pool_config_defaults(tmp_path):
    cfg = load_pool_config(write_yaml(tmp_path, good_yaml(tmp_path)))
    assert (cfg.min_workers, cfg.max_workers) == (1, 4)
    assert (cfg.idle_seconds, cfg.interval_seconds) == (120, 10)
    assert cfg.worker.worker_id == "pool-1" and cfg.worker.model.model == REVIEW_MODEL
    cfg = load_pool_config(
        write_yaml(
            tmp_path,
            good_yaml(tmp_path, min_workers=0, max_workers=64, idle_seconds=10, interval_seconds=1),
        )
    )
    assert (cfg.min_workers, cfg.max_workers, cfg.idle_seconds, cfg.interval_seconds) == (
        0,
        64,
        10,
        1,
    )


def test_pool_config_rejects_min_above_max(tmp_path):
    path = write_yaml(tmp_path, good_yaml(tmp_path, min_workers=3, max_workers=2))
    with pytest.raises(WorkerConfigError):
        load_pool_config(path)


@pytest.mark.parametrize(
    "pool",
    [
        {"max_workers": 65},
        {"max_workers": 0},
        {"min_workers": -1},
        {"max_workers": True},
        {"min_workers": "1"},
        {"idle_seconds": 9},
        {"interval_seconds": 0},
        {"surprise": 1},
    ],
)
def test_pool_config_rejects_bool_and_unknown_keys(tmp_path, pool):
    with pytest.raises(WorkerConfigError):
        load_pool_config(write_yaml(tmp_path, good_yaml(tmp_path, **pool)))


def test_pool_config_rejects_non_mapping_pool_duplicates_and_worker_errors(tmp_path):
    d = good_yaml(tmp_path)
    d["pool"] = [1]
    with pytest.raises(WorkerConfigError):
        load_pool_config(write_yaml(tmp_path, d))
    dup = tmp_path / "dup.yaml"
    dup.write_text("pool: {max_workers: 2, max_workers: 3}\n")
    with pytest.raises(WorkerConfigError):
        load_pool_config(str(dup))
    d = good_yaml(tmp_path)
    d["poll_secs"] = 1
    with pytest.raises(WorkerConfigError):
        load_pool_config(write_yaml(tmp_path, d))


def test_pool_config_rejects_overlong_worker_id_prefix(tmp_path):
    d = good_yaml(tmp_path, max_workers=10)
    d["worker_id"] = "a" * 126  # "<prefix>-10" is 129 characters
    with pytest.raises(WorkerConfigError):
        load_pool_config(write_yaml(tmp_path, d))
    d["worker_id"] = "a" * 125
    assert load_pool_config(write_yaml(tmp_path, d)).worker.worker_id == "a" * 125


# --------------------------------------------------------------------- sizing
def test_wanted_slots_grows_by_eligible_depth_up_to_max(tmp_path):
    cfg = make_pool_config(tmp_path, min_workers=1, max_workers=4)
    assert wanted_slots(1, 1, 0, cfg) == 2
    assert wanted_slots(1, 2, 0, cfg) == 3
    assert wanted_slots(2, 99, 0, cfg) == 4
    assert wanted_slots(4, 5, 0, cfg) == 4


def test_wanted_slots_retires_one_idle_slot_down_to_min(tmp_path):
    cfg = make_pool_config(tmp_path, min_workers=1, max_workers=4)
    assert wanted_slots(3, 0, 3, cfg) == 2
    assert wanted_slots(2, 0, 2, cfg) == 1
    assert wanted_slots(1, 0, 1, cfg) == 1
    assert wanted_slots(3, 0, 0, cfg) == 3  # nobody idle long enough: keep


def test_wanted_slots_starts_one_from_zero(tmp_path):
    cfg = make_pool_config(tmp_path, min_workers=0, max_workers=4)
    assert wanted_slots(0, 1, 0, cfg) == 1
    assert wanted_slots(0, 0, 0, cfg) == 0
    assert wanted_slots(1, 0, 1, cfg) == 0


# --------------------------------------------------------------- eligible depth
def row(state="queued", author="claude-opus"):
    return {"job_id": "x", "state": state, "author_model": author}


def test_eligible_depth_skips_claimed_and_self_authored():
    queue = {
        "depth": 5,
        "jobs": [
            row(),
            row(author=None),
            row(state="claimed"),
            row(author=REVIEW_MODEL),
            row(author="other"),
        ],
    }
    assert eligible_depth(queue, REVIEW_MODEL) == 3


def test_eligible_depth_ignores_malformed_rows():
    queue = {"jobs": [row(), 7, None, "x", {"state": 3}, row(author=5), {"author_model": "a"}]}
    assert eligible_depth(queue, REVIEW_MODEL) == 1
    for bad in ([], {"jobs": "nope"}, {}, None):
        with pytest.raises(ValueError):
            eligible_depth(bad, REVIEW_MODEL)


# ------------------------------------------------------------------ supervisor
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeQueueHttp:
    def __init__(self):
        self.jobs = []
        self.status = 200
        self.raw = None
        self.exc = None
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        if self.exc is not None:
            raise self.exc
        raw = self.raw if self.raw is not None else json.dumps({"jobs": self.jobs}).encode()
        return self.status, {}, raw


class FakeWorker:
    """Honours the hooks like ReviewWorker.run; ``claims`` counts cycles that started."""

    made: list = []

    def __init__(self, config, *, http=None):
        self.config = config
        self.claims = 0
        self.busy = None  # a threading.Event: while set, the cycle blocks
        self.error = None
        self.exited = threading.Event()
        FakeWorker.made.append(self)

    def run(self, stop, *, before_cycle=None, after_cycle=None):
        try:
            if self.error is not None:
                raise self.error
            while not stop.is_set():
                if not before_cycle():
                    return
                self.claims += 1
                while self.busy is not None and self.busy.is_set() and not stop.is_set():
                    time.sleep(0.005)
                after_cycle(self.busy is not None)
                stop.wait(0.005)
        finally:
            self.exited.set()


@pytest.fixture
def made():
    FakeWorker.made = []
    return FakeWorker.made


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


def make_pool(tmp_path, http=None, clock=None, **over):
    cfg = make_pool_config(tmp_path, **over)
    http = http or FakeQueueHttp()
    pool = ReviewPool(cfg, http=http, clock=clock or FakeClock(), worker_factory=FakeWorker)
    return pool, http


def stop_all(pool):
    for s in pool._slots:
        s.stop.set()
    for s in pool._slots:
        s.thread.join(5)


def run_in_thread(pool):
    stop = threading.Event()
    box = {}

    def target():
        try:
            pool.run(stop)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return stop, t, box


def test_pool_starts_min_workers(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=2, interval_seconds=1)
    stop, t, box = run_in_thread(pool)
    assert wait_for(lambda: len(made) == 2)
    assert sorted(w.config.worker_id for w in made) == ["pool-1-1", "pool-1-2"]
    stop.set()
    t.join(5)
    assert not t.is_alive() and all(w.exited.is_set() for w in made) and "error" not in box


def test_pool_grows_when_queue_has_eligible_jobs(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=1, max_workers=3)
    pool.evaluate()
    http.jobs = [row(), row(), row()]
    pool.evaluate()
    assert len(made) == 3
    assert len({w.config.worker_id for w in made}) == 3
    method, url, headers, body = http.calls[-1]
    assert (method, url, body) == ("GET", CP + "/api/v1/review-gate/queue", None)
    assert headers == {"X-API-Key": "rk-reviewer-SECRET"}
    stop_all(pool)


def test_pool_does_not_grow_for_self_authored_jobs(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=1, max_workers=3)
    http.jobs = [row(author=REVIEW_MODEL), row(state="claimed")]
    pool.evaluate()
    pool.evaluate()
    assert len(made) == 1
    stop_all(pool)


def test_pool_retires_one_idle_slot_per_evaluation_to_min(tmp_path, made):
    clock = FakeClock()
    pool, http = make_pool(tmp_path, clock=clock, min_workers=1, max_workers=3, idle_seconds=30)
    pool.evaluate()
    http.jobs = [row(), row()]
    pool.evaluate()
    assert len(pool._active()) == 3
    http.jobs = []
    pool.evaluate()
    assert len(pool._active()) == 3  # not idle long enough yet
    clock.t += 31
    pool.evaluate()
    assert len(pool._active()) == 2
    assert wait_for(lambda: sum(w.exited.is_set() for w in made) == 1)
    pool.evaluate()
    assert len(pool._active()) == 1
    pool.evaluate()
    assert len(pool._active()) == 1  # never below min
    stop_all(pool)


def test_busy_slot_is_not_retired(tmp_path, made):
    clock = FakeClock()
    pool, http = make_pool(tmp_path, clock=clock, min_workers=0, max_workers=2, idle_seconds=30)
    busy = threading.Event()
    busy.set()
    pool._factory = lambda config, http=None: _busy_worker(config, busy)
    http.jobs = [row()]
    pool.evaluate()
    (w,) = made
    assert wait_for(lambda: w.claims >= 1)
    http.jobs = []
    clock.t += 100
    pool.evaluate()
    assert len(pool._active()) == 1  # mid-cycle: not idle, not retired
    busy.clear()
    assert wait_for(lambda: pool._slots[0].idle_since == clock.t)  # after_cycle(True) reset it
    stop_all(pool)


def test_retiring_slot_never_claims(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=1, max_workers=1)
    pool.evaluate()
    slot = pool._slots[0]
    assert wait_for(lambda: made[0].claims >= 1)
    assert slot.try_retire()
    assert slot.before_cycle() is False
    assert wait_for(lambda: made[0].exited.is_set())
    claims = made[0].claims
    time.sleep(0.05)
    assert made[0].claims == claims
    # A slot inside a cycle cannot be marked retiring.
    other = _Slot("x", 0.0)
    assert other.before_cycle() is True
    assert other.try_retire() is False and other.retiring is False


def test_pool_keeps_size_when_queue_unreachable(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=1, max_workers=3, interval_seconds=10)
    pool.evaluate()
    http.jobs = [row(), row()]
    pool.evaluate()
    assert len(pool._active()) == 3
    for failure in (
        {"exc": OSError("boom")},
        {"exc": None, "status": 500},
        {"exc": None, "status": 200, "raw": b"{not json"},
    ):
        http.exc, http.status, http.raw = (
            failure.get("exc"),
            failure.get("status", 200),
            failure.get("raw"),
        )
        pool.evaluate()
        assert len(pool._active()) == 3
    assert pool._queue_failures == 3
    assert pool._wait_seconds() == 80
    pool._queue_failures = 30
    assert pool._wait_seconds() == 300
    http.exc, http.status, http.raw = None, 200, None
    pool.evaluate()
    assert pool._queue_failures == 0
    stop_all(pool)


def test_pool_ignores_oversized_or_malformed_queue_body(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=1, max_workers=3)
    pool.evaluate()
    for raw in (b"x" * (MAX_RESPONSE_BYTES + 1), b"[]", b'{"jobs": "x"}', b"{}", b"NaN"):
        http.raw = raw
        pool.evaluate()
        assert len(pool._active()) == 1
    stop_all(pool)


def test_pool_starts_slot_from_zero_min(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=0, max_workers=2)
    pool.evaluate()
    assert made == []
    http.jobs = [row()]
    pool.evaluate()
    assert len(made) == 1
    stop_all(pool)


def test_pool_stops_all_slots_on_auth_error(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=2, interval_seconds=1)
    stop, t, box = run_in_thread(pool)
    assert wait_for(lambda: len(made) == 2)
    # The next slot the supervisor starts dies with an auth error: the pool stops.
    pool._factory = lambda config, http=None: _failing_worker(config)
    http.jobs = [row()]
    assert wait_for(lambda: not t.is_alive())
    assert isinstance(box.get("error"), WorkerAuthError)
    assert all(w.exited.is_set() for w in made)


def _busy_worker(config, busy):
    w = FakeWorker(config)
    w.busy = busy
    return w


def _failing_worker(config):
    w = FakeWorker(config)
    w.error = WorkerAuthError("refused")
    return w


def test_pool_stops_on_queue_401(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=1, interval_seconds=1)
    http.status = 401
    stop, t, box = run_in_thread(pool)
    assert wait_for(lambda: not t.is_alive())
    assert isinstance(box.get("error"), WorkerAuthError)
    assert all(w.exited.is_set() for w in made)


def test_pool_replaces_a_crashed_slot_up_to_min(tmp_path, made):
    pool, http = make_pool(tmp_path, min_workers=1, max_workers=2)
    pool._factory = lambda config, http=None: _crashing_worker(config)
    pool.evaluate()
    assert wait_for(lambda: not pool._alive())
    pool._factory = FakeWorker
    pool.evaluate()
    assert wait_for(lambda: len(pool._active()) == 1)
    assert len(made) == 2
    stop_all(pool)


def _crashing_worker(config):
    w = FakeWorker(config)
    w.error = RuntimeError("x")
    return w
