"""Spend estimate: a strict YAML price table, first-match pricing, and no guessing for unpriced models."""
import datetime as dt

import pytest

from herdr_engineering.control_plane import usage_spend as u
from herdr_engineering.control_plane.repo import ControlPlaneRepo

DAY1 = dt.datetime(2026, 10, 27, 12, tzinfo=dt.UTC).timestamp()
DAY2 = dt.datetime(2026, 10, 28, 1, tzinfo=dt.UTC).timestamp()


def table(*entries):
    return u.usage_prices_config({"prices": list(entries)})


def rec(model, i=0, o=0, c=0):
    return {"model": model, "input_tokens": i, "output_tokens": o, "cache_read_tokens": c}


def test_no_table_means_no_estimate():
    assert u.usage_prices_config(None) == [] and u.usage_prices_config({}) == []
    assert u.estimate([(DAY1, rec("m", 5, 5))], []) == {"configured": False}


@pytest.mark.parametrize("bad", [
    "x", {"prices": "x"}, {"prices": ["x"]}, {"pricez": []}, {"prices": [{"model": "m", "input": 1}]},
    {"prices": [{"model": "", "input": 1, "output": 1}]}, {"prices": [{"model": "m", "input": -1, "output": 1}]},
    {"prices": [{"model": "m", "input": True, "output": 1}]}, {"prices": [{"model": "m", "input": float("nan"), "output": 1}]},
    {"prices": [{"model": "m", "input": 1, "output": 1, "extra": 1}]},
    {"prices": [{"model": "m", "input": 1, "output": 1, "cached_in_input": "yes"}]},
])
def test_bad_tables_are_refused_naming_the_key(bad):
    with pytest.raises(ValueError, match="control_plane.usage"):
        u.usage_prices_config(bad)


def test_first_matching_entry_prices_and_zero_is_a_real_price():
    prices = table({"model": "qwen-big", "input": 2, "output": 4}, {"model": "qwen*", "input": 1, "output": 1},
                   {"model": "local", "input": 0, "output": 0})
    out = u.estimate([(DAY1, rec("qwen-big", 1_000_000, 1_000_000)), (DAY1, rec("qwen-small", 1_000_000, 0)),
                      (DAY1, rec("local", 9, 9))], prices)
    by = {m["model"]: m for m in out["by_model"]}
    assert by["qwen-big"]["cost"] == 6.0 and by["qwen-big"]["match"] == "qwen-big"
    assert by["qwen-small"]["cost"] == 1.0 and by["local"]["cost"] == 0.0
    assert out["total"] == 7.0 and out["unpriced"] == [] and out["estimate"] is True


def test_cache_reads_use_their_own_rate_or_the_input_rate_and_are_not_double_charged():
    separate = table({"model": "a", "input": 10, "output": 0, "cached": 1})
    assert u.estimate([(DAY1, rec("a", 1_000_000, 0, 1_000_000))], separate)["total"] == 11.0
    no_rate = table({"model": "a", "input": 10, "output": 0})
    assert u.estimate([(DAY1, rec("a", 1_000_000, 0, 1_000_000))], no_rate)["total"] == 20.0   # never cheaper by guess
    inside = table({"model": "a", "input": 10, "output": 0, "cached": 1, "cached_in_input": True})
    assert u.estimate([(DAY1, rec("a", 1_000_000, 0, 400_000))], inside)["total"] == 6.4        # 600k at 10 + 400k at 1


def test_unpriced_models_are_listed_with_tokens_and_left_out_of_the_total():
    out = u.estimate([(DAY1, rec("known", 1_000_000, 0)), (DAY1, rec("mystery", 7, 3)), (DAY1, {"input_tokens": 2})],
                     table({"model": "known", "input": 1, "output": 1}))
    assert out["total"] == 1.0
    assert {m["model"] for m in out["unpriced"]} == {"mystery", "unknown"}
    assert out["days"][0]["unpriced_tokens"] == 12


def test_days_are_utc_and_bad_token_values_count_as_zero():
    prices = table({"model": "m", "input": 1_000_000, "output": 0})
    out = u.estimate([(DAY1, rec("m", 1)), (DAY2, rec("m", 2)), (DAY2, {"model": "m", "input_tokens": True}),
                      (DAY2, {"model": "m", "input_tokens": -5})], prices)
    assert [(d["day"], d["cost"]) for d in out["days"]] == [("2026-10-27", 1.0), ("2026-10-28", 2.0)]


def test_repo_spend_reads_the_window_and_is_off_without_a_table():
    repo = ControlPlaneRepo()
    repo.record_token_usage_row({"schema": 2, "project": "p", "model": "qwen", "route": "r", "stream": True,
                                 "usage_known": True, "status": 200, "input_tokens": 1000, "output_tokens": 500,
                                 "duration_ms": 10})
    assert repo.llm_spend("24h", []) == {"configured": False}
    out = repo.llm_spend("24h", table({"model": "qwen", "input": 1000, "output": 2000}))
    assert out["configured"] and out["source"] == "llm-proxy" and out["by_model"][0]["cost"] == 2.0
    with pytest.raises(ValueError):
        repo.llm_spend("nonsense", table({"model": "qwen", "input": 1, "output": 1}))
