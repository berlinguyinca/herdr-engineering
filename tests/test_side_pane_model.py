"""The side pane's model: a real event log and real-shaped handoff records in a store under tmp_path."""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from herdr_engineering.handoff_adapters.base import Harness, SessionRef
from herdr_engineering.handoff_controller.events import Colour, EventLog, HandoffEvent, iso
from herdr_engineering.side_pane.model import build_model

FIXTURES = Path(__file__).parent / "fixtures" / "side_pane"
ROOT_RECORD = json.loads((FIXTURES / "record_root.json").read_text())  # captured from herdr-handoff 0.5.0
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC).timestamp()


def ago(minutes: float) -> str:
    return iso(NOW - minutes * 60)


def hid(n: int) -> str:
    return f"handoff_{n:032x}"


def write_record(store: Path, n: int, *, pred: int | None = None, succ: int | None = None, pane: str, session: str,
                 goal: str = "a goal", created: str | None = None, updated: str | None = None, child: str | None = None,
                 child_session: str | None = None, phase: str | None = None, fallback: bool = False,
                 harness: str = "claude", predecessor_id: str | None = None, branch: str | None = None,
                 repo: str | None = None, **context: list[str] | str) -> str:
    record = copy.deepcopy(ROOT_RECORD)
    record["handoff_id"] = hid(n)
    record["predecessor_id"] = predecessor_id if predecessor_id is not None else (hid(pred) if pred else None)
    record["successor_id"] = hid(succ) if succ else None
    record["source"] = {**record["source"], "pane_id": pane, "session_id": session, "harness": harness}
    record["context"] = {**record["context"], "goal": goal, **context}
    record["provenance"] = {"fallback": True} if fallback else {}
    record["workspace"] = {**record.get("workspace", {}), **({"branch": branch} if branch else {}), **({"repo": repo} if repo else {})}
    record["created_at"] = created or ago(10)
    record["updated_at"] = updated or created or ago(10)
    if child:
        record["takeover"] = {"phase": phase or "source_closed", "child_pane": child}
        record["target"] = {**record["target"], "pane_id": child, "session_id": child_session}
    (store / "outbox").mkdir(parents=True, exist_ok=True)
    (store / "outbox" / f"{hid(n)}.json").write_text(json.dumps(record))
    return hid(n)


def event(name: str, *, chain: str, pane: str, session: str | None, at: str, handoff: int | None = None,
          reason: str | None = None, pct: float | None = None, live: int | None = None,
          commit: str | None = None) -> HandoffEvent:
    colour = {"handoff.succeeded": Colour.GREEN, "handoff.failed_compacted": Colour.ORANGE,
              "handoff.stuck": Colour.RED}.get(name)
    return HandoffEvent(name, at, chain, pane, session, Harness.CLAUDE, hid(handoff) if handoff else None, None, None,
                        colour, reason, pct, subagents_live=live, commit=commit)


def session_ref(pane: str, session: str | None) -> SessionRef:
    return SessionRef(Harness.CLAUDE, pane, Path("/work"), session, None)


def three_session_store(store: Path) -> None:
    """s1 (p-1) -> s2 (p-2) -> s3 (p-3, live): two green handoffs."""
    write_record(store, 1, succ=2, pane="p-1", session="s1", goal="brainstorm the guard now", created=ago(200),
                 child="p-2", child_session="s2")
    write_record(store, 2, pred=1, succ=None, pane="p-2", session="s2", goal="guard PR and CI", created=ago(100),
                 child="p-3", child_session="s3")
    log = EventLog(store)
    for n, (pane, session, start) in enumerate((("p-1", "s1", 200), ("p-2", "s2", 100)), start=1):
        log.append(event("handoff.triggered", chain=f"c{n}", pane=pane, session=session, at=ago(start + 5), pct=81.0))
        log.append(event("handoff.briefing_written", chain=f"c{n}", pane=pane, session=session, at=ago(start), handoff=n))
        log.append(event("handoff.succeeded", chain=f"c{n}", pane=pane, session=session, at=ago(start - 1), handoff=n))


def test_chain_rows_follow_predecessor_links(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert [r.session_number for r in model.rows] == [1, 2, 3]
    assert [r.depth for r in model.rows] == [0, 1, 2]
    assert [r.live for r in model.rows] == [False, False, True]
    assert [r.title for r in model.rows][:2] == ["brainstorm the guard now", "guard PR and CI"]
    assert all(r.harness is Harness.CLAUDE for r in model.rows)
    # asking from the middle of the chain finds the same chain
    assert [r.session_number for r in build_model(session_ref("p-2", "s2"), tmp_path, {}, NOW).rows] == [1, 2, 3]


def test_chain_without_a_successor_link_still_joins_by_predecessor(tmp_path: Path) -> None:
    write_record(tmp_path, 1, pane="p-1", session="s1", child="p-2", child_session="s2")  # successor_id not written yet
    write_record(tmp_path, 2, pred=1, pane="p-2", session="s2")
    assert [r.session_number for r in build_model(session_ref("p-2", "s2"), tmp_path, {}, NOW).rows] == [1, 2]


def test_colours_from_events(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    write_record(tmp_path, 3, pred=2, pane="p-3", session="s3", goal="third", created=ago(50))
    log = EventLog(tmp_path)
    log.append(event("handoff.triggered", chain="c3", pane="p-3", session="s3", at=ago(55)))
    log.append(event("handoff.briefing_written", chain="c3", pane="p-3", session="s3", at=ago(50), handoff=3))
    log.append(event("handoff.failed_compacted", chain="c3", pane="p-3", session="s3", at=ago(48), handoff=3,
                     reason="takeover refused"))
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert [r.colour for r in model.rows] == [Colour.GREEN, Colour.GREEN, Colour.ORANGE]
    assert model.rows[2].live and model.rows[2].reason == "takeover refused"
    assert model.rows[0].reason is None


def test_red_row_keeps_its_reason(tmp_path: Path) -> None:
    write_record(tmp_path, 1, pane="p-1", session="s1", created=ago(20))
    log = EventLog(tmp_path)
    log.append(event("handoff.triggered", chain="c1", pane="p-1", session="s1", at=ago(25)))
    log.append(event("handoff.briefing_written", chain="c1", pane="p-1", session="s1", at=ago(20), handoff=1))
    log.append(event("handoff.stuck", chain="c1", pane="p-1", session="s1", at=ago(19), handoff=1, reason="on_main_with_changes"))
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert [(r.colour, r.reason, r.live) for r in model.rows] == [(Colour.RED, "on_main_with_changes", True)]
    assert model.attention


def test_totals(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert (model.totals.handoffs, model.totals.compactions, model.totals.failures) == (2, 0, 0)
    assert model.totals.sessions == 3
    # known durations: s2 from the first handoff (199 min ago) to the second (99 min ago), s3 from then to now
    assert model.rows[0].duration_s is None  # the root's start is not recorded anywhere
    assert model.rows[1].duration_s == 100 * 60
    assert model.rows[2].duration_s == 99 * 60
    assert model.totals.total_s == 199 * 60


def test_live_row_carries_the_context_gauge(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    EventLog(tmp_path).append(event("handoff.skipped", chain="c0", pane="p-3", session="s3", at=ago(1),
                                    reason="below_limit", pct=42.0))
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert model.rows[2].context_pct == 42.0
    assert model.rows[1].context_pct is None


def test_dangling_predecessor_marked(tmp_path: Path) -> None:
    write_record(tmp_path, 2, predecessor_id=hid(99), pane="p-2", session="s2", goal="imported")  # record 99 is not here
    model = build_model(session_ref("p-2", "s2"), tmp_path, {}, NOW)
    assert [r.dangling for r in model.rows] == [True]
    three_session_store(tmp_path / "ok")
    assert not any(r.dangling for r in build_model(session_ref("p-3", "s3"), tmp_path / "ok", {}, NOW).rows)


def test_no_session_gives_empty_model(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    model = build_model(None, tmp_path, {}, NOW)
    assert model.rows == () and model.summary is None and not model.attention
    assert (model.totals.handoffs, model.totals.sessions) == (0, 0)


def test_session_without_records_has_no_rows_and_a_missing_store_is_fine(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    assert build_model(session_ref("p-9", "s9"), tmp_path, {}, NOW).rows == ()
    assert build_model(session_ref("p-1", "s1"), tmp_path / "nowhere", {}, NOW).rows == ()


def test_corrupt_record_is_skipped(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    (tmp_path / "outbox" / f"{hid(7)}.json").write_text("{not json")
    assert len(build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW).rows) == 3


def test_attention_true_while_draining(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    assert not build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW).attention  # two green handoffs, nothing open
    EventLog(tmp_path).append(event("handoff.triggered", chain="c3", pane="p-3", session="s3", at=ago(1), pct=82.0))
    assert build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW).attention


def test_waiting_for_subagents_label(tmp_path: Path) -> None:
    write_record(tmp_path, 1, pane="p-1", session="s1", goal="filter toggles")
    log = EventLog(tmp_path)
    log.append(event("handoff.triggered", chain="c1", pane="p-1", session="s1", at=ago(5), pct=83.0))
    log.append(event("handoff.waiting", chain="c1", pane="p-1", session="s1", at=ago(2), reason="subagents", live=2))
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert model.rows[-1].label == "waiting for 2 subagents"
    assert model.rows[-1].title == "filter toggles"
    assert model.attention
    log.append(event("handoff.waiting", chain="c1", pane="p-1", session="s1", at=ago(1), reason="subagents", live=1))
    assert build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW).rows[-1].label == "waiting for 1 subagent"


def test_open_chain_without_a_record_still_shows_the_live_session(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    log.append(event("handoff.triggered", chain="c1", pane="p-1", session="s1", at=ago(5), pct=83.0))
    log.append(event("handoff.waiting", chain="c1", pane="p-1", session="s1", at=ago(2), reason="subagents", live=3))
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert [r.label for r in model.rows] == ["waiting for 3 subagents"]
    assert model.attention


def test_chain_is_capped_at_ten_rows_with_absolute_numbers(tmp_path: Path) -> None:
    for n in range(1, 13):
        write_record(tmp_path, n, pred=n - 1 or None, succ=n + 1 if n < 12 else None, pane=f"p-{n}", session=f"s{n}",
                     created=ago(300 - n), child=f"p-{n + 1}", child_session=f"s{n + 1}")
    model = build_model(session_ref("p-13", "s13"), tmp_path, {}, NOW)
    assert len(model.rows) == 10
    assert (model.rows[0].session_number, model.rows[-1].session_number) == (4, 13)
    assert model.hidden == 3
    assert model.totals.sessions == 13


def test_summary_comes_from_the_latest_briefing(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    write_record(tmp_path, 3, pred=2, pane="p-3", session="s3", goal="Filter chips become toggles", created=ago(5),
                 completed=["toggle groups + tests"], remaining=["push, PR, CI"], next_commands=[], resume_point="full vitest run",
                 fallback=True)
    summary = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW).summary
    assert summary is not None
    assert summary.goal == "Filter chips become toggles"
    assert summary.done == ("toggle groups + tests",)
    assert summary.now == "full vitest run"
    assert summary.next == ("push, PR, CI",)
    assert summary.fallback is True
    assert summary.handoff_id == hid(3)


def test_old_event_without_new_fields_is_read(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    line = json.dumps({"event": "handoff.triggered", "at": ago(1), "chain_id": "old", "pane_id": "p-3", "session_id": "s3",
                       "harness": "claude", "handoff_id": None, "predecessor_id": None, "successor_id": None,
                       "colour": None, "reason": None, "context_pct": 50.0})
    with (tmp_path / "events.jsonl").open("a") as f:
        f.write(line + "\n")
    assert build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW).rows[-1].context_pct == 50.0
