# ruff: noqa: F811  (pytest fixtures are imported by name)
"""T16 security, round 5 (independent review after merging main): main's helper and egress events in the
revocation reserve, no lineage side effects while collaboration is degraded, a lock file that names its host,
and a repair that keeps the log's owner and mode."""
from __future__ import annotations

import errno
import os
import socket

import pytest

from herdr_engineering.control_plane import collab_repair, logfile_lock
from herdr_engineering.control_plane.collab import (
    REVOCABLE_CREATE,
    SAFETY_EVENTS,
    CollabError,
    FileEventLog,
)
from tests.control_plane.test_collab_helpers import ask, run, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, svc  # noqa: F401
from tests.control_plane.test_t16_security import _at_the_ordinary_limit


# ======================================================================== BLOCKER 1: helper/egress revocations
def _helpers_and_hosts(w):
    """A running helper, an approved host and a promoted host: the things an operator must be able to stop."""
    s = w.s
    h = ask(w)
    _, claim = run(w, h["helper_id"])
    req = s.request_egress(w.op, w.room, "a1", "approved.example.org", 443)
    s.decide_egress(w.op, req["id"], "approve")
    s.promote_egress(w.op, w.room, "promoted.example.org:443")
    helper_agent = s._helpers[h["helper_id"]]["helper_agent_id"]
    assert s._agents[helper_agent]["status"] == "active"
    allow = s.egress_allowlist(w.room)
    assert "approved.example.org:443" in allow and "promoted.example.org:443" in allow
    return h["helper_id"], helper_agent


def _assert_ordinary_writes_refused(w):
    with pytest.raises(CollabError) as full:
        w.s._commit("room_created", {})
    assert full.value.status == 503


def test_the_panic_switch_and_host_removal_work_on_a_log_that_refuses_ordinary_writes(w):
    s = w.s
    hid, helper_agent = _helpers_and_hosts(w)
    _at_the_ordinary_limit(s)                                   # ~95 %: only the pre-paid reserve is left
    _assert_ordinary_writes_refused(w)

    assert s.set_helpers_paused(w.op, True) == {"paused": True}
    assert s._helpers_paused is True
    assert s._helpers[hid]["status"] == "cancelled"
    assert s._agents[helper_agent]["status"] != "active"        # the helper's credential is gone

    s.remove_egress(w.op, w.room, "approved.example.org:443")
    s.remove_egress(w.op, w.room, "promoted.example.org:443")
    allow = s.egress_allowlist(w.room)
    assert "approved.example.org:443" not in allow and "promoted.example.org:443" not in allow
    assert len(s._log) <= s.max_log_events


def test_an_operator_cancel_ends_the_helper_on_a_full_log(w):
    s = w.s
    hid, helper_agent = _helpers_and_hosts(w)
    _at_the_ordinary_limit(s)
    s.cancel_helper_as_operator(w.op, hid)
    assert s._helpers[hid]["status"] == "cancelled"
    assert s._agents[helper_agent]["status"] != "active"


def test_a_refused_helper_cancel_row_does_not_stop_the_revoke(w, monkeypatch):
    s = w.s
    hid, helper_agent = _helpers_and_hosts(w)
    real = s._commit

    def commit(event_type, payload):
        if event_type == "helper_cancelled":
            raise CollabError(503, "collaboration log is full; ask the operator")
        return real(event_type, payload)
    monkeypatch.setattr(s, "_commit", commit)
    s.set_helpers_paused(w.op, True)
    assert s._agents[helper_agent]["status"] != "active"        # the revoke still happened


def test_the_new_safety_events_commit_nothing_when_repeated(w):
    s = w.s
    _helpers_and_hosts(w)
    s.set_helpers_paused(w.op, True)
    n = len(s._log)
    for _ in range(5):
        s.set_helpers_paused(w.op, True)
    assert len(s._log) == n
    s.remove_egress(w.op, w.room, "promoted.example.org:443")
    n = len(s._log)
    for _ in range(5):
        s.remove_egress(w.op, w.room, "promoted.example.org:443")
        s.remove_egress(w.op, w.room, "never.example.org:443")
    assert len(s._log) == n


def test_resuming_helpers_is_not_a_safety_write(w):
    s = w.s
    s.set_helpers_paused(w.op, True)
    _at_the_ordinary_limit(s)
    with pytest.raises(CollabError) as e:
        s.set_helpers_paused(w.op, False)
    assert e.value.status == 503 and s._helpers_paused is True


def test_helper_and_egress_creations_are_pre_paid_and_counted_from_state(w):
    s = w.s
    assert {"helpers_paused", "helper_cancelled", "egress_removed"} <= SAFETY_EVENTS
    assert REVOCABLE_CREATE["helper_requested"] == "helper"
    assert REVOCABLE_CREATE["egress_decided"] == REVOCABLE_CREATE["egress_promoted"] == "egress"
    base = dict(s._recount())
    h = ask(w)
    assert s._recount()["helper"] == base["helper"] + 1
    req = s.request_egress(w.op, w.room, "a1", "a.example.org", 443)
    s.decide_egress(w.op, req["id"], "approve")
    s.promote_egress(w.op, w.room, "a.example.org:443")          # the same (room, host): one removal covers both
    s.promote_egress(w.op, w.room, "b.example.org:443")
    assert s._recount()["egress"] == base["egress"] + 2
    assert s._outstanding() == sum(s._recount().values())        # the cache was invalidated by each commit
    s.remove_egress(w.op, w.room, "a.example.org:443")
    s.cancel_helper(w.root, h["helper_id"])
    now = s._recount()
    assert now["egress"] == base["egress"] + 1 and now["helper"] == base["helper"]
    assert s._outstanding() == sum(now.values())


def test_owner_listed_hosts_are_pre_paid_too(w):
    s = w.s
    base = s._recount()["egress"]
    s.set_room_profile(w.alice, w.room, {"allowed_families": ["claude-code"],
                                         "permanent_hosts": ["x.example.org:443", "y.example.org:443"]})
    assert s._recount()["egress"] == base + 2
    assert s._creation_cost("room_profile_set", {"room_id": w.room, "profile": {
        "permanent_hosts": ["x.example.org:443", "z.example.org:443"]}}) == 1



# ======================================================================== lock-file fallback names its host
def _no_flock(monkeypatch):
    def fake(fd, op):
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))
    monkeypatch.setattr(logfile_lock, "_flock", fake)


def test_the_fallback_lock_file_records_host_and_pid(tmp_path, monkeypatch):
    _no_flock(monkeypatch)
    log = FileEventLog(str(tmp_path / "collab.jsonl"))
    assert log.lock_mode == "lockfile"
    assert (tmp_path / "collab.jsonl.lock").read_text().split() == [socket.gethostname(), str(os.getpid())]
    log.close()


def test_a_lock_written_on_another_host_is_live_even_if_that_pid_is_free_here(tmp_path, monkeypatch):
    _no_flock(monkeypatch)
    (tmp_path / "collab.jsonl.lock").write_text("other-host.example 999999999")
    with pytest.raises(OSError, match="on host other-host.example") as exc:
        FileEventLog(str(tmp_path / "collab.jsonl"))
    assert "being repaired" not in str(exc.value)
    path = tmp_path / "collab.jsonl"
    path.write_bytes(b"")
    with pytest.raises(collab_repair.LogInUseError, match="other-host.example"):
        collab_repair.repair_file(str(path), apply=True, confirmed=True)
    assert (tmp_path / "collab.jsonl.lock").exists()                  # never stolen


@pytest.mark.parametrize("content", ["{host} {pid}", "{pid}"])     # this host, or a legacy pid-only file
def test_a_lock_naming_our_own_pid_is_stale_so_a_container_restarting_as_pid_1_starts(tmp_path, monkeypatch, content):
    _no_flock(monkeypatch)
    (tmp_path / "collab.jsonl.lock").write_text(content.format(host=socket.gethostname(), pid=os.getpid()))
    log = FileEventLog(str(tmp_path / "collab.jsonl"))                 # our own pid is alive, yet the lock is stale
    assert log.lock_mode == "lockfile"
    log.close()


def test_a_live_local_holder_still_blocks_and_the_message_does_not_claim_a_repair(tmp_path, monkeypatch):
    _no_flock(monkeypatch)
    (tmp_path / "collab.jsonl.lock").write_text(f"{socket.gethostname()} {os.getppid()}")
    with pytest.raises(OSError) as exc:
        FileEventLog(str(tmp_path / "collab.jsonl"))
    assert str(os.getppid()) in str(exc.value) and "being repaired" not in str(exc.value)


def test_holders_never_counts_this_process_under_its_proc_self_pid(tmp_path, monkeypatch):
    path = tmp_path / "collab.jsonl"
    path.write_bytes(b"")
    fd = os.open(str(path), os.O_RDONLY)                               # our own descriptor on the log
    try:
        real = os.getpid()
        monkeypatch.setattr(collab_repair.os, "getpid", lambda: 1)      # a containerised repair sees itself as PID 1
        assert real not in collab_repair._holders(str(path))
        assert collab_repair._holders(str(path)) == []
    finally:
        os.close(fd)


# ======================================================================== minor: repair keeps owner and mode
def test_repair_keeps_the_logs_mode_and_owner(tmp_path, monkeypatch):
    from tests.control_plane.test_t16_security import FOREIGN, _file_log
    path = _file_log(tmp_path, FOREIGN)
    os.chmod(path, 0o640)
    st = os.stat(path)
    chowned = []
    real_stat = os.stat

    def fake_stat(p, *a, **k):                                      # pretend the log belongs to the server user
        r = real_stat(p, *a, **k)
        if str(p) == str(path):
            return os.stat_result((r.st_mode, r.st_ino, r.st_dev, r.st_nlink, 4242, 4343, r.st_size,
                                   r.st_atime, r.st_mtime, r.st_ctime))
        return r
    monkeypatch.setattr(collab_repair.os, "stat", fake_stat)
    monkeypatch.setattr(collab_repair.os, "chown", lambda p, u, g: chowned.append((os.path.basename(p), u, g)))
    out = collab_repair.repair_file(str(path), apply=True, confirmed=True)
    monkeypatch.undo()
    assert out["quarantine"]
    assert os.stat(path).st_mode & 0o777 == 0o640 and os.stat(path).st_ino != st.st_ino
    assert {u for _, u, _ in chowned} == {4242} and {g for _, _, g in chowned} == {4343}
    assert any(n.startswith("collab.jsonl.tmp-") for n, _, _ in chowned)     # the file that becomes the log
