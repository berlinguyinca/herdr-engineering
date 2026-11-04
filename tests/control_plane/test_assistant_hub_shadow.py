"""Shadow judge records (store) and the async recorder.

Raw-socket listeners exist only to produce hostile TRANSPORT behavior (accept and never answer); there is no fake
System One.  Nothing here touches real user config directories.
"""
from __future__ import annotations

import dataclasses
import json
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant import AssistantStore
from herdr_engineering.control_plane.assistant_hub.jev import JevClient
from herdr_engineering.control_plane.assistant_hub.shadow import (
    JudgeRecord,
    ShadowJob,
    ShadowRecorder,
    label_decision,
    mark_regret,
)
from herdr_engineering.control_plane.assistant_hub.verbs import (
    ActionKey,
    FlagClass,
    RiskClass,
    Verb,
)

T0 = datetime(2026, 11, 1, tzinfo=UTC)
CANARY = "CANARY_CMD_rm_-rf_/secret"


def _rec(i: int, tenant: str = "t", user: str = "u", **kw: Any) -> JudgeRecord:
    base: dict[str, Any] = dict(record_id=f"jr{i}", tenant_id=tenant, user_id=user, worker_id="w", request_id=f"req{i}",
                                key_id="k", judge_version="v1", status="ok",
                                created_at=T0 + timedelta(seconds=i))
    base.update(kw)
    return JudgeRecord(**base)


def _key(target: str = "sha256:abc") -> ActionKey:
    return ActionKey("bash", Verb.GIT_PUSH, "org/repo", target, FlagClass.FORCE, RiskClass.REMOTE_REVERSIBLE)


def _job(n: int, tenant: str = "t", user: str = "u", target: str = "sha256:abc") -> ShadowJob:
    return ShadowJob(tenant, user, "w", "s", "task", f"req{n}", _key(target))


# ------------------------------------------------------------------ store


def test_round_trip_snapshot_and_real_persist_file(tmp_path: Path) -> None:
    path = str(tmp_path / "store.json")
    s = AssistantStore(persist_path=path)
    s.add_judge_record(_rec(1, decided_at=T0, owner_decision="approved", covers=0.25))
    fresh = AssistantStore()
    fresh.restore(json.loads(Path(path).read_text()))
    got = fresh.get_judge_record_by_request("t", "u", "req1")
    assert got == s.get_judge_record_by_request("t", "u", "req1")
    assert got is not None and got.decided_at == T0 and got.covers == 0.25
    other = AssistantStore()
    other.restore(json.loads(json.dumps(s.snapshot())))
    assert len(other.list_judge_records("t", "u")) == 1


def test_old_snapshot_without_key_loads() -> None:
    s = AssistantStore()
    s.restore({"assistants": []})
    assert s.list_judge_records("t", "u") == []


def test_cap_drops_oldest_and_survives_restore() -> None:
    s = AssistantStore()
    s.max_action_rows_per_tenant = 3
    for i in range(5):
        s.add_judge_record(_rec(i))
    assert [r.record_id for r in s.list_judge_records("t", "u")] == ["jr2", "jr3", "jr4"]
    snap = s.snapshot()
    snap["judge_records"] = [dataclasses.asdict(_rec(i)) | {"created_at": {"$dt": T0.isoformat()}}
                             for i in range(7)]
    fresh = AssistantStore()
    fresh.max_action_rows_per_tenant = 3
    fresh.restore(snap)
    assert [r.record_id for r in fresh.list_judge_records("t", "u")] == ["jr4", "jr5", "jr6"]


def test_default_cap_is_5000() -> None:
    assert AssistantStore().max_action_rows_per_tenant == 5000


def test_cap_is_per_tenant() -> None:
    s = AssistantStore()
    s.max_action_rows_per_tenant = 2
    for i in range(4):
        s.add_judge_record(_rec(i, tenant="a"))
    s.add_judge_record(_rec(9, tenant="b"))
    assert len(s.list_judge_records("a", "u")) == 2 and len(s.list_judge_records("b", "u")) == 1


def test_scoping_tenant_and_user_on_every_access() -> None:
    s = AssistantStore()
    s.add_judge_record(_rec(1))
    assert s.get_judge_record_by_request("t", "u", "req1") is not None
    for tenant, user in (("t2", "u"), ("t", "u2")):
        assert s.get_judge_record_by_request(tenant, user, "req1") is None
        assert s.list_judge_records(tenant, user) == []
        assert s.update_judge_label(tenant, user, "req1", regret=True) is False
        assert mark_regret(s, tenant, user, "k", "w", T0) == 0
    assert s.get_judge_record_by_request("t", "u", "req1").regret is False   # type: ignore[union-attr]


def test_duplicate_request_id_is_one_record_but_other_user_not_blocked() -> None:
    s = AssistantStore()
    assert s.add_judge_record(_rec(1)) is True
    assert s.add_judge_record(_rec(2, request_id="req1")) is False
    assert len(s.list_judge_records("t", "u")) == 1


def test_defensive_copies() -> None:
    s = AssistantStore()
    s.add_judge_record(_rec(1))
    s.get_judge_record_by_request("t", "u", "req1").regret = True      # type: ignore[union-attr]
    s.list_judge_records("t", "u")[0].label_note = "x"
    got = s.get_judge_record_by_request("t", "u", "req1")
    assert got is not None and got.regret is False and got.label_note == ""
    original = _rec(2)
    s.add_judge_record(original)
    original.regret = True
    assert s.get_judge_record_by_request("t", "u", "req2").regret is False   # type: ignore[union-attr]


def test_list_filters_by_judge_version() -> None:
    s = AssistantStore()
    s.add_judge_record(_rec(1, judge_version="a"))
    s.add_judge_record(_rec(2, judge_version="b"))
    assert [r.record_id for r in s.list_judge_records("t", "u", "b")] == ["jr2"]
    assert len(s.list_judge_records("t", "u")) == 2


def test_update_label_rejects_non_label_fields() -> None:
    s = AssistantStore()
    s.add_judge_record(_rec(1))
    with pytest.raises(ValueError):
        s.update_judge_label("t", "u", "req1", tenant_id="evil")


def test_label_decision_and_mark_regret() -> None:
    s = AssistantStore()
    s.add_judge_record(_rec(1))
    s.add_judge_record(_rec(2, key_id="other"))
    assert label_decision(s, lambda: {}, "t", "u", "req1", True, T0) is True
    r = s.get_judge_record_by_request("t", "u", "req1")
    assert r is not None and r.owner_decision == "approved" and r.decided_at == T0
    assert label_decision(s, lambda: {}, "t", "u", "req1", False, T0 + timedelta(1)) is True
    assert s.get_judge_record_by_request("t", "u", "req1").owner_decision == "denied"   # type: ignore[union-attr]
    assert label_decision(s, lambda: {}, "t", "u", "nope", True, T0) is False       # absent: ignored
    assert mark_regret(s, "t", "u", "k", "w", T0) == 1
    assert mark_regret(s, "t", "u", "k", "other-worker", T0) == 0
    assert s.get_judge_record_by_request("t", "u", "req1").regret is True       # type: ignore[union-attr]
    assert s.get_judge_record_by_request("t", "u", "req2").regret is False      # type: ignore[union-attr]


def test_record_has_no_command_text_fields() -> None:
    names = {f.name for f in dataclasses.fields(JudgeRecord)}
    assert not names & {"command", "display", "key_canonical", "target", "canonical"}


# --------------------------------------------------------------- recorder


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    p = tmp_path / "k"
    p.write_text("sk-test-not-real\n")
    p.chmod(0o600)
    return p


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Listener:
    """Accepts, records the request, then runs a hostile behavior (here: hold the line open)."""

    def __init__(self, behavior: Callable[[socket.socket], None]) -> None:
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = int(self._srv.getsockname()[1])
        self._behavior = behavior
        self._stop = threading.Event()
        self.received = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._srv.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            try:
                conn.recv(65536)
                self.received.set()
                self._behavior(conn)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._srv.close()


def _cfg(enabled: bool = True) -> dict[str, Any]:
    return {"enabled": enabled, "model": "laya-auto",
            "thresholds": {"destructive": 0.3, "irreversible": 0.3, "severity_score": 2.0}}


def _recorder(store: AssistantStore, key_file: Path, port: int, enabled: bool = True, queue_size: int = 100,
              timeout: float = 1.0) -> ShadowRecorder:
    return ShadowRecorder(store, lambda: _cfg(enabled),
                          lambda: JevClient(f"http://127.0.0.1:{port}", str(key_file), "laya-auto", timeout),
                          lambda: T0, queue_size=queue_size)


def _wait(cond: Callable[[], bool], seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _workers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "assistant-shadow-judge"]


def test_disabled_judge_submits_nothing_and_starts_no_thread(key_file: Path) -> None:
    s = AssistantStore()
    r = _recorder(s, key_file, _free_port(), enabled=False)
    r.start()
    assert r.submit(_job(1)) is False
    assert _workers() == [] and s.list_judge_records("t", "u") == []
    r.stop()


def test_unreachable_endpoint_writes_unavailable_and_worker_survives(key_file: Path) -> None:
    s = AssistantStore()
    r = _recorder(s, key_file, _free_port())
    r.start()
    try:
        assert r.submit(_job(1)) and r.submit(_job(2))
        assert _wait(lambda: len(s.list_judge_records("t", "u")) == 2)
        recs = s.list_judge_records("t", "u")
        assert all(x.status == "unavailable" and x.reason_code for x in recs)
        assert recs[0].verb == "git.push" and recs[0].risk_class == RiskClass.REMOTE_REVERSIBLE.value
        assert recs[0].key_id == _key().key_id() and recs[0].model == "laya-auto"
        assert r.submit(_job(3)) and _wait(lambda: len(s.list_judge_records("t", "u")) == 3)
    finally:
        r.stop()


def test_factory_returning_none_or_raising_never_kills_worker(key_file: Path) -> None:
    s = AssistantStore()
    calls = {"n": 0}

    def factory() -> JevClient | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        raise RuntimeError("boom")

    r = ShadowRecorder(s, lambda: _cfg(), factory, lambda: T0)
    r.start()
    try:
        r.submit(_job(1))
        r.submit(_job(2))
        assert _wait(lambda: len(s.list_judge_records("t", "u")) == 2)
        assert {x.status for x in s.list_judge_records("t", "u")} == {"unavailable"}
        assert len(_workers()) >= 1
    finally:
        r.stop()


def test_store_failure_does_not_kill_worker(key_file: Path) -> None:
    s = AssistantStore()
    s.audit_sink = None
    boom = {"on": True}
    real_add = s.add_judge_record

    def flaky(rec: JudgeRecord) -> bool:
        if boom["on"]:
            boom["on"] = False
            raise OSError("disk full")
        return real_add(rec)

    s.add_judge_record = flaky      # type: ignore[method-assign]
    r = _recorder(s, key_file, _free_port())
    r.start()
    try:
        r.submit(_job(1))
        r.submit(_job(2))
        assert _wait(lambda: len(s.list_judge_records("t", "u")) == 1)
    finally:
        r.stop()


def test_queue_overflow_drops_oldest_and_counts(key_file: Path) -> None:
    s = AssistantStore()
    release = threading.Event()
    li = Listener(lambda conn: release.wait(5))
    r = _recorder(s, key_file, li.port, queue_size=2, timeout=5.0)
    try:
        r.start()
        assert r.submit(_job(0))
        assert li.received.wait(5)               # worker is busy on job 0; the queue is now empty
        for n in (1, 2, 3, 4):
            assert r.submit(_job(n)) is True
        assert r.dropped == 2
        release.set()
        assert _wait(lambda: len(s.list_judge_records("t", "u")) == 3, 30)
        ids = {x.request_id for x in s.list_judge_records("t", "u")}
        assert ids == {"req0", "req3", "req4"}   # req1 and req2 (the oldest queued) were dropped
    finally:
        release.set()
        r.stop()
        li.close()


def test_stop_joins_thread_and_blocks_submit(key_file: Path) -> None:
    s = AssistantStore()
    r = _recorder(s, key_file, _free_port())
    r.start()
    assert len(_workers()) == 1
    r.stop(5)
    assert _workers() == []
    assert r.submit(_job(1)) is False
    r.start()
    assert _workers() == []


def test_two_submits_same_request_id_one_record(key_file: Path) -> None:
    s = AssistantStore()
    r = _recorder(s, key_file, _free_port())
    r.start()
    try:
        r.submit(_job(1))
        r.submit(_job(1))
        r.submit(_job(2))
        assert _wait(lambda: len(s.list_judge_records("t", "u")) == 2)
        time.sleep(0.2)
        assert sorted(x.request_id for x in s.list_judge_records("t", "u")) == ["req1", "req2"]
    finally:
        r.stop()


def test_no_store_lock_held_during_consult(key_file: Path) -> None:
    s = AssistantStore()
    release = threading.Event()
    li = Listener(lambda conn: release.wait(10))
    r = _recorder(s, key_file, li.port, timeout=10.0)
    got: list[bool] = []

    def probe() -> None:
        ok = s._lock.acquire(timeout=1.0)
        got.append(ok)
        if ok:
            s._lock.release()

    try:
        r.start()
        assert r.submit(_job(1))
        assert li.received.wait(5)               # consult is now in flight against the slow listener
        t = threading.Thread(target=probe)
        t.start()
        t.join(5)
        assert got == [True]                     # another thread could take the store lock mid-call
        assert s.list_judge_records("t", "u") == []   # and the call really was still in flight
    finally:
        release.set()
        r.stop()
        li.close()


def test_record_holds_no_command_text(key_file: Path) -> None:
    s = AssistantStore()
    r = _recorder(s, key_file, _free_port())
    r.start()
    try:
        r.submit(ShadowJob("t", "u", "w", "s", "task", "reqc", ActionKey(
            "bash", Verb.GIT_PUSH, "org/repo", CANARY, FlagClass.FORCE, RiskClass.REMOTE_REVERSIBLE)))
        assert _wait(lambda: len(s.list_judge_records("t", "u")) == 1)
    finally:
        r.stop()
    assert CANARY not in json.dumps(s.snapshot(), default=str)
    assert CANARY not in repr(s.list_judge_records("t", "u"))


