"""Row budgets for incremental checkpoint recovery are configurable and diagnosable."""

import json

import pytest

from herdr_engineering.control_plane import projection_recovery as recovery
from herdr_engineering.control_plane.projection_recovery import (
    CheckpointBudgets,
    checkpoint_budgets,
)
from tests.control_plane.test_projection_checkpoint_e2e import DSN, durable, insert  # noqa: F401

NON_COMPACTABLE = {"probe_big": 12, "probe_mid": 5, "probe_small": 3}


def test_budget_defaults_raise_the_dirty_limit():
    assert checkpoint_budgets(None) == CheckpointBudgets(100_000, 600_000)
    assert checkpoint_budgets({}) == CheckpointBudgets(100_000, 600_000)
    assert checkpoint_budgets({"max_dirty_rows": 7}) == CheckpointBudgets(100_000, 7)


@pytest.mark.parametrize(
    "raw",
    [
        {"max_dirty_rows": 0},
        {"max_dirty_rows": -5},
        {"max_tail_rows": 0},
        {"max_dirty_rows": 1.5},
        {"max_dirty_rows": "600000"},
        {"max_tail_rows": True},
        {"max_dirty_rows": None},
        {"max_dirty": 5},
        ["max_dirty_rows"],
    ],
)
def test_budget_rejects_bad_values(raw):
    with pytest.raises(ValueError, match="projection_checkpoint"):
        checkpoint_budgets(raw)


def test_budget_summary_reports_rows_per_type_and_limit():
    rows = {f"type{n}": 100 - n for n in range(8)}
    text = recovery._budget_summary([("type0", "a")], {}, rows, 500)
    assert "dirty rows 772 > limit 500" in text
    assert "type0=100, type1=99, type2=98, type3=97, type4=96" in text
    assert "type5" not in text and "+3 more types" in text


async def seed(conn):
    n = 0
    for kind, count in NON_COMPACTABLE.items():
        for i in range(count):
            await insert(conn, f"{kind}-{i}", n, {"v": i}, kind=kind, identity=f"{kind}-1")
            n += 1


async def append(conn):
    for kind in NON_COMPACTABLE:
        await insert(conn, f"{kind}-new", 1000, {"v": "new"}, kind=kind, identity=f"{kind}-1")


@pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")
def test_dirty_rows_between_old_and_new_limit_stay_incremental(durable):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    run(append(conn))
    dirty = sum(NON_COMPACTABLE.values()) + len(NON_COMPACTABLE)  # 20 prefix rows + 3 appended
    _, old, _ = recover(budgets=CheckpointBudgets(100, dirty - 1))
    assert old.recovery_info["mode"] == "full_replay"
    _, new, _ = recover(budgets=CheckpointBudgets(100, dirty))
    assert new.recovery_info["mode"] == "checkpoint", new.recovery_info


@pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")
def test_over_limit_rejection_names_row_counts(durable, caplog):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    run(append(conn))
    dirty = sum(NON_COMPACTABLE.values()) + len(NON_COMPACTABLE)
    with caplog.at_level("WARNING"):
        _, source, _ = recover(budgets=CheckpointBudgets(100, 10))
    assert source.recovery_info["mode"] == "full_replay"
    message = " ".join(record.getMessage() for record in caplog.records)
    assert f"dirty rows {dirty} > limit 10" in message, message
    assert "probe_big=13, probe_mid=6, probe_small=4" in message


@pytest.mark.skipif(not DSN, reason="dedicated HERDR_PERF_PG_DSN required")
def test_tail_budget_is_configurable(durable, caplog):  # noqa: F811
    conn, run, recover = durable
    run(seed(conn))
    recover(publish=True)
    run(append(conn))
    with caplog.at_level("WARNING"):
        _, source, _ = recover(budgets=CheckpointBudgets(2, 1000))
    assert source.recovery_info["mode"] == "full_replay"
    assert "3 tail rows > limit 2" in " ".join(r.getMessage() for r in caplog.records)
    json.dumps(source.recovery_info)
