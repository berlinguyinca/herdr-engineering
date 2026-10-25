# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""A per-ROOM file-storage limit set in the room's properties (operator decision 2026-10-25).

`files_room_bytes` is a room property (null = the YAML default `collab.files.room_bytes`), set by the room's owner
only, stored as a `room_settings_set` event (so it survives restart and replay) and held to the YAML ceiling
`collab.files.room_bytes_max` (10 GiB). Lowering it below what the room uses deletes nothing; it only blocks new
uploads. The per-file `max_bytes` stays global. There is no per-owner limit."""
from __future__ import annotations

import threading
import time

import pytest

from herdr_engineering.control_plane.collab import CollabService, FileEventLog, StaticTokenAuthenticator
from herdr_engineering.control_plane.collab_files import DirectoryBlobStore, normalize_files
from tests.control_plane.test_collab import Clock, _digest
from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401

MiB = 1024 * 1024
GiB = 1024 * MiB


@pytest.fixture
def w(svc, tmp_path):
    svc.file_blobs = DirectoryBlobStore(str(tmp_path / "files"))
    svc.files_limits = normalize_files({"max_bytes": 60, "room_bytes": 100, "room_bytes_max": 1000})
    return W(svc)


def put(w, n, size, room=None, who=None):
    return w.s.put_file(room or w.room, who or w.bob, f"{n}.bin", bytes([n % 256]) * size)


def status(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        return getattr(exc, "status", exc)
    return 200


def test_yaml_ceiling_defaults_to_10_gib_and_must_cover_the_default():
    assert normalize_files(None) == {"max_bytes": 25 * MiB, "room_bytes": 500 * MiB, "room_bytes_max": 10 * GiB}
    for bad in ({"room_bytes_max": 0}, {"room_bytes": 200, "room_bytes_max": 100}, {"room_bytes_max": "1"}):
        with pytest.raises(ValueError):
            normalize_files(bad)


def test_the_default_applies_until_the_room_sets_its_own(w):
    s = w.s.get_room_settings(w.bob, w.room)
    assert s == {"files_room_bytes": None, "files_room_bytes_default": 100, "files_room_bytes_effective": 100,
                 "files_room_bytes_max": 1000, "files_used_bytes": 0, "files_max_bytes": 60, "can_edit": False}
    assert w.s.get_room_settings(w.alice, w.room)["can_edit"] is True
    put(w, 1, 60)
    assert status(put, w, 2, 50) == 413                                     # 110 > the 100 default


def test_a_room_override_applies_to_that_room_only(w):
    out = w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": 300})
    assert out["files_room_bytes"] == 300 and out["files_room_bytes_effective"] == 300
    for n in range(5):
        put(w, n, 60)                                                       # 300 bytes in the room
    assert status(put, w, 9, 1) == 413
    put(w, 1, 60, room=w.room2, who=w.alice)
    assert status(put, w, 2, 50, room=w.room2, who=w.alice) == 413          # room2 keeps the default
    lst = w.s.list_files(w.bob, w.room)
    assert (lst["used_bytes"], lst["room_bytes"], lst["room_bytes_max"], lst["max_bytes"]) == (300, 300, 1000, 60)
    w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": None})       # back to the default
    assert w.s.get_room_settings(w.bob, w.room)["files_room_bytes_effective"] == 100


def test_only_the_room_owner_sets_it(w):
    assert code(w.s.set_room_settings, w.bob, w.room, {"files_room_bytes": 300}) == 403      # contributor
    assert code(w.s.set_room_settings, w.carol, w.room, {"files_room_bytes": 300}) == 403    # viewer
    assert code(w.s.set_room_settings, w.dave, w.room, {"files_room_bytes": 300}) in (403, 404)  # guest
    assert code(w.s.set_room_settings, w.mallory, w.room, {"files_room_bytes": 300}) == 404  # stranger
    pi_info = w.s.connect_agent(w.alice, {"harness_type": "pi"})
    agent = w.s.authenticate(f"Agent {pi_info['credential']}", pi_info["device_id"])
    assert code(w.s.set_room_settings, agent, w.room, {"files_room_bytes": 300}) in (403, 404)  # never an agent
    assert w.s.get_room_settings(w.alice, w.room)["files_room_bytes"] is None
    assert code(w.s.get_room_settings, w.mallory, w.room) == 404
    assert code(w.s.get_room_settings, w.dave, w.room) in (403, 404)         # a guest has no shelf


def test_values_are_validated_and_held_to_the_ceiling(w):
    for bad in ({"files_room_bytes": 1001}, {"files_room_bytes": 0}, {"files_room_bytes": -5},
                {"files_room_bytes": "300"}, {"files_room_bytes": True}, {"files_room_bytes": 1.5},
                {"other": 1}, {}):
        assert code(w.s.set_room_settings, w.alice, w.room, bad) == 400, bad
    w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": 1000})
    # a ceiling lowered later in YAML wins over a stored room value
    w.s.files_limits = normalize_files({"max_bytes": 60, "room_bytes": 100, "room_bytes_max": 200})
    assert w.s.get_room_settings(w.bob, w.room)["files_room_bytes_effective"] == 200


def test_lowering_below_usage_keeps_every_file_and_blocks_new_uploads(w):
    w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": 300})
    ids = [put(w, n, 60)["id"] for n in range(4)]                           # 240 used
    w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": 100})
    assert [f["id"] for f in w.s.list_files(w.bob, w.room)["files"]] == sorted(ids)
    assert w.s.get_file(w.bob, w.room, ids[0])[1] == bytes([0]) * 60
    with pytest.raises(Exception) as e:
        put(w, 7, 1)
    assert e.value.status == 413 and "240" in e.value.message and "100 bytes" in e.value.message
    assert status(w.s.upload_precheck, w.room, w.bob, "x.bin", 1) == 413   # the HTTP pre-check agrees
    for fid in ids[:3]:
        w.s.delete_file(w.alice, w.room, fid)                               # deleting reclaims: 60 used
    put(w, 8, 40)


def test_the_change_is_an_audited_room_event_with_old_and_new(w):
    w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": 300})
    w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": 200})
    evs = [e for e in w.s._log if e["event_type"] == "room_settings_set"]
    assert [(e["payload"]["actor_user_id"], e["payload"]["old"], e["payload"]["settings"]) for e in evs] == [
        ("alice", {"files_room_bytes": None}, {"files_room_bytes": 300}),
        ("alice", {"files_room_bytes": 300}, {"files_room_bytes": 200})]
    feed = [e for e in w.s.read_events(w.bob, w.room, 0, 200)["events"] if e["event_type"] == "room_settings_set"]
    assert feed[-1]["settings"] == {"files_room_bytes": 200} and feed[-1]["old"] == {"files_room_bytes": 300}


def test_the_room_value_survives_a_restart(tmp_path):
    blobs = DirectoryBlobStore(str(tmp_path / "files"))
    auth = StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()})
    limits = normalize_files({"max_bytes": 60, "room_bytes": 100, "room_bytes_max": 1000})

    def boot():
        s = CollabService(auth, sink=FileEventLog(str(tmp_path / "log")), now=Clock())
        s.file_blobs, s.files_limits = blobs, limits
        s.hydrate(s._sink.load())
        return s
    s = boot()
    room = W(s)
    s.set_room_settings(room.alice, room.room, {"files_room_bytes": 250})
    s2 = boot()
    bob = s2.authenticate(f"Bearer {USERS['bob']}", None)
    assert s2.get_room_settings(bob, room.room)["files_room_bytes_effective"] == 250


def test_concurrent_uploads_respect_the_room_limit(w):
    w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": 150})
    inner = w.s.file_blobs

    class Slow:
        durable = True

        def put(self, data):
            time.sleep(0.2)                                                 # all pass the pre-check first
            return inner.put(data)

        def get(self, *a):
            return inner.get(*a)

        def delete(self, key):
            return inner.delete(key)
    w.s.file_blobs = Slow()
    results = []

    def go(n):
        try:
            results.append(put(w, n, 60))
        except Exception as exc:  # noqa: BLE001
            results.append(exc.status)
    ts = [threading.Thread(target=go, args=(n,)) for n in (1, 2, 3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(r if isinstance(r, int) else 201 for r in results) == [201, 201, 413]
    assert w.s.list_files(w.bob, w.room)["used_bytes"] == 120


def test_the_room_poll_carries_usage_and_limit_for_the_shelf(w):
    w.s.set_room_settings(w.alice, w.room, {"files_room_bytes": 300})
    put(w, 1, 60)
    st = w.s.room_state(w.bob, w.room, -1, 0, 0)
    assert st["files_usage"] == {"used_bytes": 60, "limit_bytes": 300}
    assert w.s.room_state(w.dave, w.room, -1, 0, 0).get("files_usage") is None      # a guest: no shelf


def test_http_routes(w):
    st, body = w.s.dispatch("GET", ["rooms", w.room, "settings"], {}, {}, f"Bearer {USERS['bob']}", None)
    assert st == 200 and body["files_room_bytes_effective"] == 100
    st, body = w.s.dispatch("POST", ["rooms", w.room, "settings"], {}, {"files_room_bytes": 400},
                            f"Bearer {USERS['alice']}", None)
    assert st == 200 and body["files_room_bytes"] == 400
    st, body = w.s.dispatch("PUT", ["rooms", w.room, "settings"], {}, {"files_room_bytes": 500},
                            f"Bearer {USERS['bob']}", None)
    assert st == 403, body

