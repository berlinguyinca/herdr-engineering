"""Reviewer pool (review gate, plan 3): config, sizing, supervisor."""

import pytest
import yaml

from herdr_engineering.review_gate.pool import (
    PoolConfig,
    eligible_depth,
    load_pool_config,
    wanted_slots,
)
from herdr_engineering.review_gate.worker import ModelConfig, WorkerConfig, WorkerConfigError

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
