"""Attention/notification tests (spec 0160)."""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.attention import AttentionCenter, NotificationPolicy


def test_raise_and_lifecycle():
    ac = AttentionCenter()
    item = ac.raise_item(kind="test-failure", severity="error",
                         title="tests failing", summary="2 failed")
    assert item.lifecycle == "open"
    assert ac.acknowledge(item.id) is True
    assert ac.resolve(item.id) is True
    assert ac.open_items() == []


def test_dedupe_coalesces():
    ac = AttentionCenter()
    a = ac.raise_item(kind="k", severity="error", title="t", summary="s",
                      dedupe_key="same", links=["/a"])
    b = ac.raise_item(kind="k", severity="error", title="t", summary="s",
                      dedupe_key="same", links=["/b"])
    assert a.id == b.id  # coalesced
    assert "/b" in b.links


def test_quiet_hours_suppress_notification():
    sent = []
    ac = AttentionCenter(policy=NotificationPolicy(
        minimum_severity="notice", quiet_hours={"start": 0, "end": 23}),
        notifier=lambda i: sent.append(i))
    ac.raise_item(kind="k", severity="error", title="t", summary="s")
    assert sent == []  # suppressed during quiet hours


def test_quiet_hours_all_day_covers_hour_23():
    # Regression: a half-open [start, end) range skipped hour 23, so
    # {start: 0, end: 23} did not actually mean "all day" and the suppression
    # test flaked whenever the wall clock was in hour 23.
    att = __import__("herdr_engineering.attention", fromlist=["time"])
    real_localtime = att.time.localtime
    att.time.localtime = lambda ts: time.struct_time(
        (2026, 1, 1, 23, 30, 0, 4, 1, 0))  # fixed 23:xx regardless of clock
    try:
        sent = []
        ac = AttentionCenter(policy=NotificationPolicy(
            minimum_severity="notice", quiet_hours={"start": 0, "end": 23}),
            notifier=lambda i: sent.append(i))
        ac.raise_item(kind="k", severity="error", title="t", summary="s")
        assert sent == []  # still suppressed at hour 23
    finally:
        att.time.localtime = real_localtime


def test_minimum_severity_filter():
    ac = AttentionCenter(policy=NotificationPolicy(minimum_severity="error"))
    ac.raise_item(kind="k", severity="info", title="low", summary="s")
    assert ac.open_items(min_severity="error") == []


def test_action_executes_in_owner_system_and_is_authorized():
    ac = AttentionCenter()
    item = ac.raise_item(kind="ci", severity="error", title="t", summary="s",
                         action_capability="ci.retry")
    assert item.action_authorized is False
    item.action_authorized = True
    ok = ac.execute_action(item.id, lambda i: "retried")
    assert ok is True
    assert item.lifecycle == "resolved"  # resolved on success
