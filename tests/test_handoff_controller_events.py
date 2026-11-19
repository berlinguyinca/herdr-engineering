"""Controller config, event log and chain colour: real files, real processes."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_controller.config import ControllerConfig
from herdr_engineering.handoff_controller.events import Colour, EventLog, HandoffEvent, chain_colour

REPO = str(Path(__file__).resolve().parents[1])


def ev(name: str = "handoff.triggered", chain: str = "c1", **kw: object) -> HandoffEvent:
    fields: dict[str, object] = dict(
        event=name, at="2026-11-18T10:00:00+00:00", chain_id=chain, pane_id="%1", session_id="s1",
        harness=Harness.CLAUDE, handoff_id=None, predecessor_id=None, successor_id=None, colour=None,
        reason=None, context_pct=81.5)
    fields.update(kw)
    return HandoffEvent(**fields)  # type: ignore[arg-type]


def test_ceiling_is_limit_plus_12() -> None:
    assert ControllerConfig().ceiling_pct == 92.0
    assert ControllerConfig(drain_limit_pct=70.0).ceiling_pct == 82.0


def test_event_roundtrip_jsonl(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "store")
    first = ev("handoff.succeeded", colour=Colour.GREEN, reason="ok", handoff_id="h1")
    log.append(first)
    log.append(ev("handoff.drained"))
    assert log.read() == [first, ev("handoff.drained")]
    assert len((tmp_path / "store" / "events.jsonl").read_text().splitlines()) == 2


WRITER = """
import sys
from pathlib import Path
from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_controller.events import EventLog, HandoffEvent
log = EventLog(Path(sys.argv[1]))
tag = sys.argv[2]
for i in range(200):
    log.append(HandoffEvent(event="handoff.triggered", at="t", chain_id=tag, pane_id="%1", session_id=None,
        harness=Harness.PI, handoff_id=None, predecessor_id=None, successor_id=None, colour=None,
        reason="x" * 300 + str(i), context_pct=None))
"""


def test_append_is_one_line_and_concurrent_appends_do_not_interleave(tmp_path: Path) -> None:
    store = tmp_path / "store"
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(store), tag], cwd=REPO,
                              env={"PYTHONPATH": REPO, "PATH": "/usr/bin:/bin"}) for tag in ("a", "b")]
    for p in procs:
        assert p.wait(timeout=30) == 0
    lines = (store / "events.jsonl").read_text().splitlines()
    assert len(lines) == 400
    assert all(isinstance(json.loads(line), dict) for line in lines)
    log = EventLog(store)
    assert len(log.read("a")) == 200 and len(log.read("b")) == 200


def test_read_by_chain_id(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    for e in (ev(chain="x"), ev("handoff.drained", chain="y"), ev("handoff.drained", chain="x")):
        log.append(e)
    assert [e.event for e in log.read("x")] == ["handoff.triggered", "handoff.drained"]
    assert log.read("zzz") == []
    assert EventLog(tmp_path / "missing").read() == []


def test_read_skips_half_written_last_line(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    log.append(ev())
    with (tmp_path / "events.jsonl").open("a") as f:
        f.write('{"event": "handoff.dra')
    assert log.read() == [ev()]


def test_chain_colour_green_after_succeeded() -> None:
    assert chain_colour([ev(), ev("handoff.succeeded", colour=Colour.GREEN), ev("handoff.wip_committed")]) == Colour.GREEN


def test_chain_colour_orange_after_failed_compacted() -> None:
    assert chain_colour([ev("handoff.failed_compacted", colour=Colour.ORANGE)]) == Colour.ORANGE


def test_chain_colour_red_after_stuck_then_none_if_no_terminal() -> None:
    assert chain_colour([ev(), ev("handoff.stuck", colour=Colour.RED, reason="r")]) == Colour.RED
    assert chain_colour([ev(), ev("handoff.drained")]) is None
    assert chain_colour([]) is None


def test_chain_colour_last_terminal_wins() -> None:
    events = [ev("handoff.stuck", colour=Colour.RED, reason="r"), ev("handoff.triggered"), ev("handoff.succeeded", colour=Colour.GREEN)]
    assert chain_colour(events) == Colour.GREEN


def test_unknown_event_name_rejected() -> None:
    with pytest.raises(ValueError):
        ev("handoff.bogus")


def test_large_event_is_written_whole(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    big = ev(reason="x" * 2_000_000)
    log.append(big)
    assert log.read() == [big]


def test_terminal_events_require_their_colour() -> None:
    ev("handoff.succeeded", colour=Colour.GREEN)
    ev("handoff.failed_compacted", colour=Colour.ORANGE)
    ev("handoff.stuck", colour=Colour.RED, reason="r")
    for name, wrong in (("handoff.succeeded", Colour.RED), ("handoff.failed_compacted", None), ("handoff.stuck", Colour.GREEN)):
        with pytest.raises(ValueError):
            ev(name, colour=wrong, reason="r")


def test_non_terminal_events_have_no_colour() -> None:
    with pytest.raises(ValueError):
        ev("handoff.drained", colour=Colour.GREEN)


def test_stuck_and_skipped_require_a_reason() -> None:
    for name, colour in (("handoff.stuck", Colour.RED), ("handoff.skipped", None)):
        for reason in (None, ""):
            with pytest.raises(ValueError):
                ev(name, colour=colour, reason=reason)
    ev("handoff.skipped", reason="no usage yet")


def test_read_skips_a_valid_json_line_that_fails_validation(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    log.append(ev())
    bad = json.loads(ev("handoff.succeeded", colour=Colour.GREEN).to_json())
    bad["colour"] = None  # a terminal event without its colour
    with (tmp_path / "events.jsonl").open("a") as f:
        f.write(json.dumps(bad) + "\n")
    log.append(ev("handoff.drained"))
    assert [e.event for e in log.read()] == ["handoff.triggered", "handoff.drained"]
