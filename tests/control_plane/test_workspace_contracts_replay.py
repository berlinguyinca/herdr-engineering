# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""P01 subscribe/replay cursor contract, pinned against the REAL CollabService (read_events / room_state)."""
from __future__ import annotations

import itertools
import threading

from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.workspace_contracts import ReplayCursor
from tests.control_plane.test_collab_tasks import W, code, svc, user  # noqa: F401


_N = itertools.count()


def post(w, n, who=None):
    who = who or w.alice
    out = []
    for _ in range(n):
        i = next(_N)                      # a fresh key each time: a reused key is (correctly) an idempotent no-op
        out.append(w.s.post_message(who, w.room, {"text": f"m{i}", "idempotency_key": f"k{i}"})[1])
    return out


def test_next_cursor_is_last_delivered_and_paging_covers_everything_once(svc):
    w = W(svc)
    post(w, 5)
    full = w.s.read_events(w.bob, w.room, 0, 100)
    seqs = [e["sequence"] for e in full["events"]]
    assert seqs == sorted(seqs) and full["has_more"] is False and full["next_cursor"] == seqs[-1]
    # drive the client properly: request from the current cursor, apply the response to THAT request
    rc, got = ReplayCursor(0), []
    for _ in range(20):
        asked = rc.cursor
        page = w.s.read_events(w.bob, w.room, asked, 2)
        got += rc.apply_page(asked, page)
        if not page["has_more"]:
            break
    assert [e["sequence"] for e in got] == seqs and rc.cursor == full["next_cursor"]


def test_replaying_a_cursor_is_identical_and_client_apply_is_idempotent(svc):
    w = W(svc)
    post(w, 4)
    a = w.s.read_events(w.bob, w.room, 0, 100)
    b = w.s.read_events(w.bob, w.room, 0, 100)
    assert a == b                                                        # at-least-once, no server-side consumption
    rc = ReplayCursor(0)
    first = rc.apply_page(0, a)
    assert first and rc.apply_page(0, a) == [] and rc.cursor == a["next_cursor"]       # same page twice: no-op
    assert rc.apply_page(a["next_cursor"], {"events": a["events"], "next_cursor": a["next_cursor"]}) == []   # duplicates dropped
    mid = first[1]["sequence"]
    assert rc.apply_page(mid, a) == [] and rc.cursor == a["next_cursor"]               # stale/reordered response ignored, never regresses


def test_reordered_and_repeated_pages_converge_without_duplicates(svc):
    w = W(svc)
    post(w, 6)
    pages = [w.s.read_events(w.bob, w.room, c, 2) for c in (0, 0)]
    cur = pages[0]["next_cursor"]
    pages.append(w.s.read_events(w.bob, w.room, cur, 2))
    rc = ReplayCursor(0)
    out = rc.apply_page(0, pages[0]) + rc.apply_page(0, pages[1]) + rc.apply_page(cur, pages[2]) + rc.apply_page(0, pages[0])
    seqs = [e["sequence"] for e in out]
    assert seqs == sorted(set(seqs)) and len(seqs) == len(pages[0]["events"]) + len(pages[2]["events"])


def test_member_only_sees_events_since_they_joined(svc):
    w = W(svc)
    post(w, 3)
    early = w.s.read_events(w.bob, w.room, 0, 100)["events"]
    assert len(early) >= 3                                               # positive control: bob sees the history he was present for
    w.s.add_member(w.alice, w.room, {"user_id": "mallory", "role": "viewer"})
    late = w.s.read_events(user(svc, "mallory"), w.room, 0, 100)["events"]
    assert not [e for e in late if e["event_type"] == "message"]          # none of the 3 earlier messages
    post(w, 1)
    assert [e["event_type"] for e in w.s.read_events(user(svc, "mallory"), w.room, 0, 100)["events"]].count("message") == 1


def test_room_state_unchanged_is_tiny_and_changed_carries_events(svc):
    w = W(svc)
    post(w, 2)
    st = w.s.room_state(w.bob, w.room, -1, 0, 0)
    assert st["changed"] is True and st["events"]
    same = w.s.room_state(w.bob, w.room, st["seq"], 0, 0)
    assert same == {"seq": st["seq"], "changed": False}
    post(w, 1)
    assert w.s.room_state(w.bob, w.room, st["seq"], 0, 0)["changed"] is True


def test_lost_access_is_a_404_on_read_and_on_state(svc):
    w = W(svc)
    post(w, 1)
    assert w.s.read_events(w.bob, w.room, 0, 10)["events"]               # control
    w.s.remove_member(w.alice, w.room, "bob")
    assert code(w.s.read_events, w.bob, w.room, 0, 10) == 404
    assert code(w.s.room_state, w.bob, w.room, -1, 0, 0) == 404
    assert code(w.s.read_events, w.mallory, w.room, 0, 10) == 404        # a stranger gets the same answer as a removed member


def test_revocation_mid_long_poll_ends_the_subscription(svc):
    w = W(svc)
    st = w.s.room_state(w.carol, w.room, -1, 0, 0)
    box = []

    def poll():
        try:
            box.append(w.s.room_state(w.carol, w.room, st["seq"], 0, 10))
        except CollabError as e:
            box.append(e.status)

    t = threading.Thread(target=poll)
    t.start()
    w.s.remove_member(w.alice, w.room, "carol")
    t.join(8)
    assert not t.is_alive() and box == [404]
