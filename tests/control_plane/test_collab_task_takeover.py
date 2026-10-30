# ruff: noqa: F811
"""P05 crash takeover (rule A): an accepted task whose lease ran out goes back to pending for its target agent."""
from __future__ import annotations

from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from tests.control_plane.test_collab import Clock, _digest  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, W, code, svc, user, w  # noqa: F401

LEASE = 600.0


def _accepted(w):
    a, i = w.agent("alice")
    tid = w.s.create_task(w.bob, w.room, {"target_agent_id": i["agent_id"], "text": "x"})["task_id"]
    w.s.update_task(a, tid, {"status": "accepted"})
    return a, tid


def _status(w, tid):
    return next(t for t in w.s.list_tasks(w.bob, w.room)["tasks"] if t["task_id"] == tid)


def test_off_by_default_an_accepted_task_stays_accepted(w):
    a, tid = _accepted(w)
    w.s.clock.t += 10 * LEASE
    assert w.s.task_inbox(a)["tasks"][0]["status"] == "accepted"


def test_a_task_inside_its_lease_is_left_alone(w):
    w.s.task_lease_seconds = LEASE
    a, tid = _accepted(w)
    w.s.clock.t += LEASE - 1
    got = w.s.task_inbox(a)["tasks"][0]
    assert got["status"] == "accepted" and got["attempt"] == 1


def test_an_expired_lease_requeues_once_and_bumps_the_attempt(w):
    w.s.task_lease_seconds = LEASE
    a, tid = _accepted(w)
    w.s.clock.t += LEASE + 1
    got = w.s.task_inbox(a)["tasks"][0]
    assert got["status"] == "pending" and got["attempt"] == 2
    assert w.s.task_inbox(a)["tasks"][0]["attempt"] == 2          # polling again does not requeue again
    assert w.s.update_task(a, tid, {"status": "accepted", "attempt": 2})["attempt"] == 2


def test_the_old_run_cannot_finish_a_requeued_task(w):
    w.s.task_lease_seconds = LEASE
    a, tid = _accepted(w)
    w.s.clock.t += LEASE + 1
    w.s.task_inbox(a)
    w.s.update_task(a, tid, {"status": "accepted", "attempt": 2})
    assert code(w.s.update_task, a, tid, {"status": "done", "result": "late", "attempt": 1}) == 409
    assert _status(w, tid)["status"] == "accepted"
    assert w.s.update_task(a, tid, {"status": "done", "result": "ok", "attempt": 2})["status"] == "done"


def test_a_finished_task_is_never_requeued(w):
    w.s.task_lease_seconds = LEASE
    a, tid = _accepted(w)
    w.s.update_task(a, tid, {"status": "done", "result": "ok"})
    w.s.clock.t += 10 * LEASE
    w.s.task_inbox(a)
    assert _status(w, tid)["status"] == "done" and _status(w, tid)["attempt"] == 1


def test_the_requeue_survives_a_restart(w):
    w.s.task_lease_seconds = LEASE
    a, tid = _accepted(w)
    w.s.clock.t += LEASE + 1
    w.s.task_inbox(a)
    again = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=w.s.clock)
    again.hydrate(w.s._log)
    row = next(t for t in again.list_tasks(user(again, "bob"), w.room)["tasks"] if t["task_id"] == tid)
    assert row["status"] == "pending" and row["attempt"] == 2
