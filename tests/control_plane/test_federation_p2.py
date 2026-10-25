# ruff: noqa: F811, E701  (pytest fixtures are imported by name from the sibling test modules)
"""ADR-0008 phase P2: peer registry (YAML), room shares (collab events), pinned-key signing and the signed export.

Nothing here opens a network connection to a peer: the gateway is driven in process and one end-to-end class drives
the real HTTP handler on loopback. Every refusal has a positive control (the SAME request succeeds when only that guard
is out of the way), so a deleted guard fails a test instead of passing silently. Keys are generated at run time.
"""
from __future__ import annotations

import email.message
import http.client
import json
import os
import threading

import pytest

from herdr_engineering.control_plane import federation as fed
from herdr_engineering.control_plane import federation_sign as sign
from herdr_engineering.control_plane import ids
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import (
    REVOCABLE_CREATE,
    SAFETY_EVENTS,
    CollabError,
    CollabService,
    StaticTokenAuthenticator,
    _digest,
)
from herdr_engineering.control_plane.collab_federation import EXPORT_FIELDS
from herdr_engineering.control_plane.federation_export import NOT_FOUND, FederationGateway
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_external import enroll  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, W, code, svc, user  # noqa: F401

PEER_SEED = sign.seed_hex_new()
OTHER_SEED = sign.seed_hex_new()
PEER_PUB = sign.public_key_hex(PEER_SEED)
OTHER_PUB = sign.public_key_hex(OTHER_SEED)
_n = [0]


def k():
    _n[0] += 1
    return f"k{_n[0]}"


def hdrs(d: dict) -> email.message.Message:
    m = email.message.Message()
    for name, value in d.items():
        m[name] = value
    return m


# ----------------------------------------------------------------------------- the world
class Fx:
    """Alice owns the room; bob (contributor), carol (viewer), dave (guest). Peer `lab` pins PEER_PUB and lists the room."""

    def __init__(self, w, tmp_path):
        self.w, self.s, self.clock = w, w.s, w.s.clock
        self.seed = sign.seed_hex_new()
        self.key_file = str(tmp_path / "fed.key")
        with open(os.open(self.key_file, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(self.seed + "\n")
        self.pub = sign.public_key_hex(self.seed)
        self.section = self.cfg()
        w.s.federation_source = lambda: self.section
        self.gw = FederationGateway(w.s, lambda: self.section, clock=w.s.clock)

    def cfg(self, **over):
        peer = {"enabled": True, "public_key": PEER_PUB, "export_rooms": [self.w.room], **over.pop("peer", {})}
        return {"enabled": True, "server_id": "hq", "signing_key_file": self.key_file, "peers": {"lab": peer},
                **over}

    def peer(self, **over):
        self.section["peers"]["lab"].update(over)

    def share(self, who=None, peer="lab", room=None):
        return self.s.grant_share(who or self.w.alice, room or self.w.room, {"peer": peer})[1]["share"]["share_id"]

    def say(self, who, text, room=None, **extra):
        self.clock.t += 2.0
        return self.s.post_message(who, room or self.w.room, {"text": text, "idempotency_key": k(), **extra})[1]["event"]

    def get(self, target, seed=None, peer="lab", audience="hq", method="GET", body=b"", now=None, nonce=None,
            drop=(), override=None, extra=()):
        h = sign.sign_request(seed or PEER_SEED, peer, audience, method, target, body,
                              now=self.clock.t if now is None else now, nonce=nonce)
        for name in drop:
            h.pop(name)
        h.update(override or {})
        msg = hdrs(h)
        for name, value in extra:
            msg.add_header(name, value)
        return self.gw.handle(method, target, msg, body)

    def events(self, sid, query="", **kw):
        st, body, extra = self.get(f"/api/v1/federation/shares/{sid}/events{query}", **kw)
        return st, (json.loads(body) if st == 200 else None), body, extra


@pytest.fixture
def w(svc):
    return W(svc)


@pytest.fixture
def fx(w, tmp_path):
    return Fx(w, tmp_path)


LITERAL_MESSAGE_FIELDS = {"event_type", "origin_event_id", "origin_sequence", "occurred_at", "sender_kind", "sender_ref",
                          "text", "in_reply_to", "trust"}


def refused_404(r):
    return r == NOT_FOUND


# ============================================================================ YAML: registry, kill switch, keys
def test_absent_section_is_off_and_inert(fx):
    assert fed.parse_federation(None) == fed.OFF and not fed.OFF.enabled
    fx.s.federation_source = None
    assert fx.s.fed_policy() == fed.OFF
    before = len(fx.s._log)
    fx.s.maintain()
    assert len(fx.s._log) == before                                  # nothing is written while it is absent
    assert code(fx.s.grant_share, fx.w.alice, fx.w.room, {"peer": "lab"}) == 403
    assert not any(e["event_type"].startswith("federation_") for e in fx.s._log)


@pytest.mark.parametrize("bad", ["true", 1, "yes", None, 0, [], {}])
def test_kill_switch_must_be_a_real_boolean(fx, bad):
    fx.section["enabled"] = bad
    assert fx.s.fed_policy() == fed.OFF
    assert refused_404(fx.get("/api/v1/federation/shares"))
    fx.section["enabled"] = True                                      # control
    assert fx.get("/api/v1/federation/shares")[0] == 200


def test_unreadable_source_and_invalid_section_are_off_and_the_switch_is_hot(fx):
    assert fx.get("/api/v1/federation/shares")[0] == 200

    def boom():
        raise OSError("file unusable")
    fx.gw.source = boom
    assert refused_404(fx.get("/api/v1/federation/shares"))
    fx.gw.source = lambda: {**fx.section, "peers": {"lab": {"enabled": True, "public_key": "zz"}}}
    assert refused_404(fx.get("/api/v1/federation/shares"))           # one bad peer closes everything (never opens)
    fx.gw.source = lambda: fx.section
    fx.section["enabled"] = False                                     # hot: no restart
    assert refused_404(fx.get("/api/v1/federation/shares"))
    fx.section["enabled"] = True
    assert fx.get("/api/v1/federation/shares")[0] == 200


ROOMX = "room_" + "0123456789abcdefghjkmnpqrs"
MANY_ROOMS = [ids.new_id("room") for _ in range(65)]


def peercfg(**over):
    return {"enabled": True, "public_key": PEER_PUB, "export_rooms": [ROOMX], **over}


@pytest.mark.parametrize("section", [
    {"nope": 1}, {"server_id": "Has Caps"}, {"server_id": ""}, {"server_id": "a b"}, {"server_id": 5},
    {"signing_key_file": "relative/key"}, {"signing_key_file": ""}, {"signing_key_file": 5},
    {"request_window_seconds": 0}, {"request_window_seconds": 601}, {"request_window_seconds": True},
    {"export_validity_seconds": 29}, {"export_validity_seconds": 3601}, {"max_export_events": 0},
    {"max_export_events": 501}, {"max_export_events": "10"},
    {"peers": {"lab": peercfg(export_rooms=["*"])}},
    {"peers": {"lab": peercfg(export_rooms="room")}},
    {"peers": {"lab": peercfg(export_rooms={ROOMX: 1})}}, {"peers": {"lab": peercfg(export_rooms=None)}},
    {"peers": {"lab": peercfg(export_rooms=MANY_ROOMS)}},
    {"peers": {"lab": peercfg(export_rooms=[ROOMX, ROOMX])}},
    {"peers": {"lab": peercfg(export_rooms=[ROOMX + "*"])}},
    {"peers": {"lab": peercfg(export_rooms=["room_short"])}},
    {"peers": {"lab": peercfg(export_rooms=[5])}},
    {"peers": {"lab": peercfg(public_key="ab" * 31)}},
    {"peers": {"lab": peercfg(public_key="AB" * 32)}},
    {"peers": {"lab": peercfg(public_key=5)}},
    {"peers": {"lab": peercfg(key_fingerprint="ab" * 32)}},                       # not the SHA-256 of public_key
    {"peers": {"lab": peercfg(key_fingerprint=fed.key_fingerprint_of(PEER_PUB)[:12])}},       # a prefix of the right one
    {"peers": {"lab": peercfg(next_public_key=PEER_PUB)}},                         # same key is not a rotation
    {"peers": {"lab": peercfg(next_public_key="zz")}},
    {"peers": {"lab": {"enabled": True, "next_public_key": OTHER_PUB, "key_fingerprint": "ab" * 6}}},  # rotation needs a pinned key
    {"peers": {"lab": peercfg(requests_per_minute=0)}}, {"peers": {"lab": peercfg(requests_per_minute=601)}},
    {"peers": {"lab": peercfg(requests_per_minute=True)}},
    {"peers": {"lab": peercfg(max_bytes_per_hour=10)}}, {"peers": {"lab": peercfg(max_bytes_per_hour=(1 << 30) + 1)}},
    {"peers": {"lab": peercfg(key_expires_at="not a date")}}, {"peers": {"lab": peercfg(key_expires_at=True)}},
    {"peers": {"lab": peercfg(key_expires_at=float("nan"))}}, {"peers": {"lab": peercfg(rotate_after=[1])}},
    {"peers": {"lab": peercfg(secret="x")}},
])
def test_a_bad_section_is_refused_not_repaired(section):
    with pytest.raises(fed.FederationConfigError):
        fed.parse_federation({"enabled": True, **section})
    assert fed.policy_from_source(lambda: {"enabled": True, **section}) == fed.OFF


def test_a_good_section_parses_and_derives_the_full_fingerprint():
    pol = fed.parse_federation({
        "enabled": True, "server_id": "hq.example", "signing_key_file": "/etc/herdr/fed.key",
        "peers": {"lab": peercfg(key_fingerprint=fed.key_fingerprint_of(PEER_PUB), next_public_key=OTHER_PUB,
                                 key_expires_at="2030-01-01T00:00:00Z", rotate_after=1_900_000_000,
                                 requests_per_minute=5, max_bytes_per_hour=4096)}})
    p = pol.peer("lab")
    assert p.key_fingerprint == fed.key_fingerprint_of(PEER_PUB) and len(p.key_fingerprint) == 64
    assert p.key_expires_at == 1893456000.0 and p.requests_per_minute == 5 and p.max_bytes_per_hour == 4096
    assert p.export_rooms == (ROOMX,) and p.mirror_room is None        # inbound binding is a separate, optional key
    assert [fp for _, fp in p.accepted_keys(1.0)] == [p.key_fingerprint, fed.key_fingerprint_of(OTHER_PUB)]


def test_a_naive_yaml_timestamp_is_utc_whatever_the_host_zone(monkeypatch):
    import datetime
    import time
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        naive = datetime.datetime(2030, 1, 1, 0, 0, 0)
        got = fed.parse_federation({"enabled": True, "peers": {"lab": peercfg(key_expires_at=naive)}}).peer("lab")
        assert got.key_expires_at == 1893456000.0
    finally:
        monkeypatch.undo()
        time.tzset()


def test_key_expiry_and_rotation_fields():
    p = fed.parse_federation({"enabled": True, "peers": {"lab": peercfg(key_expires_at=1000, next_public_key=OTHER_PUB,
                                                                       rotate_after=500)}}).peer("lab")
    assert [pk for pk, _ in p.accepted_keys(999.0)] == [PEER_PUB, OTHER_PUB]
    assert [pk for pk, _ in p.accepted_keys(1000.0)] == [OTHER_PUB]       # the pinned key lapses AT its expiry
    assert not p.rotation_due(499.0) and p.rotation_due(500.0)
    assert not fed.parse_federation({"enabled": True, "peers": {"lab": peercfg()}}).peer("lab").rotation_due(1e12)


def test_fingerprint_match_is_the_full_key_not_a_twelve_character_prefix():
    p = fed.parse_federation({"enabled": True, "peers": {"lab": peercfg()}}).peer("lab")
    full = p.key_fingerprint
    assert p.matches(full)
    for fake in (full[:12], full[:63], full + "0", full[:12] + "0" * 52, full.upper(), "", None, 5, b"x", "\u00e9" * 64):
        assert not p.matches(fake), fake
    assert full[:12] + "0" * 52 != full                                  # (the collision case really differs)
    legacy = fed.parse_federation({"enabled": True, "peers": {"lab": {"enabled": True, "key_fingerprint": full[:12]}}})
    assert not legacy.peer("lab").matches(full[:12]) and not legacy.peer("lab").matches(full)   # no key: never matches
    keyless64 = fed.parse_federation({"enabled": True, "peers": {"lab": {"enabled": True, "key_fingerprint": full}}})
    assert not keyless64.peer("lab").matches(full)                       # even holding a full fingerprint: no pinned key


def test_the_peer_principal_carries_the_full_fingerprint():
    full = fed.key_fingerprint_of(PEER_PUB)
    pr = fed.peer_principal("lab", full)
    assert pr["fingerprint"] == full and pr["id"] == "peer:lab:" + full[:12] and fed.peer_fingerprint(pr) == full
    for bad in (full[:12], full[:63], full.upper(), "z" * 64):
        with pytest.raises(ValueError):
            fed.peer_principal("lab", bad)
    assert fed.peer_fingerprint({**pr, "fingerprint": full[:12]}) is None
    assert fed.peer_fingerprint({k_: v for k_, v in pr.items() if k_ != "fingerprint"}) is None


def test_the_lineage_scope_check_itself_refuses_a_peer_without_a_mirror_room():
    from tests.control_plane.test_federation_p1 import ROOM, World
    full = fed.key_fingerprint_of(PEER_PUB)
    peer = fed.peer_principal("acme", full)
    w = World({"enabled": True, "peers": {"acme": {"enabled": True, "public_key": PEER_PUB}}})
    env = {"actor": peer, "visibility_scope": "room", "room_id": None}
    with pytest.raises(PermissionError):
        w.svc._check_lineage_scope(env)
    w.section = {"enabled": True, "peers": {"acme": {"enabled": True, "public_key": PEER_PUB, "mirror_room": ROOM}}}
    w.svc._check_lineage_scope({**env, "room_id": ROOM})                              # control
    with pytest.raises(PermissionError):
        w.svc._check_lineage_scope({**env, "room_id": None})


def test_ingest_binds_a_peer_to_its_full_pinned_fingerprint():
    from tests.control_plane.test_federation_p1 import ROOM, World
    full = fed.key_fingerprint_of(PEER_PUB)
    section = {"enabled": True, "peers": {"acme": {"enabled": True, "public_key": PEER_PUB, "mirror_room": ROOM}}}
    w = World(section)
    good = fed.peer_principal("acme", full)

    def put(node_id, who):
        return w.svc.ingest(w.body("node.upsert", {"node_type": "peer", "node_id": node_id, "label": "x"}, "org"), who)
    assert put("peer:acme.n1", good)                                                   # control: the pinned key is admitted
    for who in (
            {**fed.peer_principal("acme", full[:12] + "0" * 52)},                   # same first 12 hex, different key
            {k_: v for k_, v in good.items() if k_ != "fingerprint"},               # a P1-shaped principal
            {**good, "fingerprint": full[:12]},                                      # a truncated claim
            {**good, "fingerprint": fed.key_fingerprint_of(OTHER_PUB)},             # right label, other key
    ):
        with pytest.raises(PermissionError):
            put("peer:acme.n2", who)
    with pytest.raises(PermissionError):                                             # the id label must be the key's prefix
        put("peer:acme.n4", {**good, "id": "peer:acme:" + "00" * 6})
    w.section = {"enabled": True, "peers": {"acme": {"enabled": True, "public_key": PEER_PUB}}}      # no mirror_room
    with pytest.raises(PermissionError):
        put("peer:acme.n3", good)


# ============================================================================ signing: requests
def verify(fx, target="/api/v1/federation/shares", method="GET", body=b"", headers=None, now=None, replay=None):
    h = headers if headers is not None else sign.sign_request(PEER_SEED, "lab", "hq", method, target, body,
                                                              now=fx.clock.t)
    pol = fed.parse_federation(fx.section)
    return sign.verify_request(pol, method=method, target=target, headers=hdrs(h), body=body,
                               now=fx.clock.t if now is None else now, replay=replay)


def test_a_valid_request_verifies_to_the_pinned_peer_and_full_fingerprint(fx):
    peer, fp = verify(fx)
    assert peer.peer_id == "lab" and fp == fed.key_fingerprint_of(PEER_PUB)
    # the GET with a query and a body-bearing method are signed over their own bytes
    assert verify(fx, "/api/v1/federation/shares/x/events?cursor=3&limit=5") is not None
    assert verify(fx, method="POST", body=b'{"a":1}') is not None


@pytest.mark.parametrize("what", [
    "wrong_key", "other_target", "other_query", "other_method", "other_body", "wrong_audience", "wrong_peer",
    "expired", "future", "dup_header", "bad_nonce", "bad_ts", "prefix_key", "wrong_tail_key", "sig_truncated",
    "sig_b64_padding", "newline_target", "non_ascii_target", "lowercase_method"])
def test_every_way_a_request_signature_can_be_wrong_is_refused(fx, what):
    t = "/api/v1/federation/shares/abc/events?cursor=1"
    good = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, b"", now=fx.clock.t)
    assert verify(fx, t, headers=good) is not None                                      # positive control
    h, target, method, body, now = dict(good), t, "GET", b"", None
    if what == "wrong_key":
        h = sign.sign_request(OTHER_SEED, "lab", "hq", "GET", t, now=fx.clock.t)
    elif what == "other_target":
        target = "/api/v1/federation/shares"
    elif what == "other_query":
        target = t + "0"
    elif what == "other_method":
        method = "POST"
    elif what == "other_body":
        body = b"x"
    elif what == "wrong_audience":
        h = sign.sign_request(PEER_SEED, "lab", "elsewhere", "GET", t, now=fx.clock.t)
    elif what == "wrong_peer":
        fx.section["peers"]["other"] = dict(fx.section["peers"]["lab"])
        h = {**good, sign.H_PEER: "other"}
    elif what == "expired":
        h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t - 121)
    elif what == "future":
        h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t + 121)
    elif what == "dup_header":
        m = hdrs(good)
        m.add_header(sign.H_NONCE, good[sign.H_NONCE])
        pol = fed.parse_federation(fx.section)
        assert sign.verify_request(pol, method="GET", target=t, headers=m, body=b"", now=fx.clock.t) is None
        return
    elif what == "bad_nonce":
        h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t, nonce="short")        # validly signed
    elif what == "bad_ts":
        h = {**good, sign.H_TS: " " + good[sign.H_TS]}
    elif what == "prefix_key":
        h = {**good, sign.H_KEY: good[sign.H_KEY][:12]}
    elif what == "wrong_tail_key":
        h = {**good, sign.H_KEY: good[sign.H_KEY][:12] + "0" * 52}
    elif what == "sig_truncated":
        h = {**good, sign.H_SIG: good[sign.H_SIG][:-2]}
    elif what == "sig_b64_padding":
        h = {**good, sign.H_SIG: good[sign.H_SIG] + "="}
    elif what == "newline_target":
        target = t + "\nX"
        h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", target, now=fx.clock.t)
    elif what == "non_ascii_target":
        target = t + "é"
        h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", target, now=fx.clock.t)
    elif what == "lowercase_method":
        method = "get"
        h = sign.sign_request(PEER_SEED, "lab", "hq", "get", t, now=fx.clock.t)
    assert verify(fx, target, method=method, body=body, headers=h, now=now) is None


@pytest.mark.parametrize("bad", ["/x\rY", "/x\x00Y", "/x\nY", "/x" + "a" * 2100, "/x\u00e9", "/x\u202e"])
def test_a_hostile_target_is_refused_even_when_signed(fx, bad):
    good = "/api/v1/federation/shares"
    assert verify(fx, good) is not None
    h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", bad, now=fx.clock.t)
    assert verify(fx, bad, headers=h) is None


@pytest.mark.parametrize("name", sign.REQUEST_HEADERS)
def test_a_missing_header_is_refused(fx, name):
    h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", "/api/v1/federation/shares", now=fx.clock.t)
    assert verify(fx, headers=h) is not None
    h.pop(name)
    assert verify(fx, headers=h) is None


def test_timestamp_window_is_inclusive_and_configurable(fx):
    t = "/api/v1/federation/shares"
    for delta, ok in ((120, True), (-120, True), (121, False), (-121, False)):
        h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t - delta)
        assert (verify(fx, headers=h) is not None) is ok, delta
    fx.section["request_window_seconds"] = 10
    h = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t - 30)
    assert verify(fx, headers=h) is None


def test_peer_state_gates_verification(fx):
    assert verify(fx) is not None
    fx.peer(enabled=False)
    assert verify(fx) is None
    fx.peer(enabled=True)
    fx.section["enabled"] = False
    assert verify(fx) is None
    fx.section["enabled"] = True
    del fx.section["peers"]["lab"]
    assert verify(fx) is None
    fx.section = fx.cfg()
    fx.section["server_id"] = None                                           # no audience to bind: nothing verifies
    assert verify(fx) is None


def test_keyless_peer_never_authenticates(fx):
    fx.section["peers"]["lab"] = {"enabled": True, "key_fingerprint": fed.key_fingerprint_of(PEER_PUB)[:12],
                                  "export_rooms": [fx.w.room]}
    assert verify(fx) is None


def test_key_expiry_and_next_key_rotation_overlap(fx):
    t = "/api/v1/federation/shares"
    fx.peer(key_expires_at=fx.clock.t + 10)
    assert verify(fx) is not None
    fx.peer(key_expires_at=fx.clock.t - 1)                                    # expired: the pinned key stops working
    assert verify(fx) is None
    h_new = sign.sign_request(OTHER_SEED, "lab", "hq", "GET", t, now=fx.clock.t)
    assert verify(fx, headers=h_new) is None                                  # and the unlisted new key never did
    fx.peer(next_public_key=OTHER_PUB)
    peer, fp = verify(fx, headers=h_new)
    assert fp == fed.key_fingerprint_of(OTHER_PUB)
    assert verify(fx) is None                                                 # the expired key is still dead
    fx.peer(key_expires_at=None)
    assert verify(fx) is not None and verify(fx, headers=h_new) is not None   # overlap: both until the operator drops one


def test_the_nonce_and_timestamp_are_covered_by_the_signature(fx):
    """Without this a captured request could be replayed by swapping its nonce, or kept alive by re-stamping it."""
    t = "/api/v1/federation/shares"
    good = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t, nonce="n" * 20)
    assert verify(fx, headers=good) is not None
    assert verify(fx, headers={**good, sign.H_NONCE: "m" * 20}) is None
    old = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t - 100, nonce="o" * 20)
    assert verify(fx, headers=old) is not None
    assert verify(fx, headers={**old, sign.H_TS: str(int(fx.clock.t) + 100)}) is None             # re-stamped to look new
    assert verify(fx, headers={**old, sign.H_TS: str(int(fx.clock.t))}) is None


def test_a_non_ascii_key_header_is_a_refusal_not_an_exception(fx):
    t = "/api/v1/federation/shares"
    good = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t)
    assert verify(fx, headers={**good, sign.H_KEY: "\u00e9" * 64}) is None
    assert verify(fx, headers={**good, sign.H_KEY: "\u00e9" * 63 + "a"}) is None


def test_a_signature_is_single_use_but_only_once_it_is_good(fx):
    replay = sign.ReplayGuard()
    t = "/api/v1/federation/shares"
    good = sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t, nonce="n" * 20)
    forged = {**good, sign.H_SIG: sign.sign_request(OTHER_SEED, "lab", "hq", "GET", t, now=fx.clock.t,
                                                    nonce="n" * 20)[sign.H_SIG]}
    assert verify(fx, headers=forged, replay=replay) is None                  # an outsider cannot burn the nonce
    assert verify(fx, headers=good, replay=replay) is not None
    assert verify(fx, headers=good, replay=replay) is None                    # the same bytes again: refused
    assert verify(fx, headers=sign.sign_request(PEER_SEED, "lab", "hq", "GET", t, now=fx.clock.t),
                  replay=replay) is not None                                  # a fresh nonce is fine


def test_replay_table_is_bounded_and_expires(fx):
    g = sign.ReplayGuard(per_peer=3)
    assert [g.first_use("lab", f"n{i}", 100.0, 10.0) for i in range(4)] == [True, True, True, False]   # full: refuse
    assert g.first_use("other", "n0", 100.0, 10.0)                            # per peer, not shared
    assert g.first_use("lab", "n0", 111.0, 10.0)                              # lapsed entries are swept and reusable
    assert not g.first_use("lab", "n0", 112.0, 10.0)


def test_domain_separation_between_request_and_export_signatures(fx):
    body = b'{"x":1}'
    s_export = sign.sign_export(fx.seed, body)
    assert sign.verify_export(fx.pub, body, s_export)
    assert not sign.verify_export(fx.pub, body + b" ", s_export)
    assert not sign.verify_export(OTHER_PUB, body, s_export)
    assert not sign.verify_export(fx.pub, body, s_export[:-1] + ("A" if s_export[-1] != "A" else "B"))
    assert not sign.verify_export(fx.pub, body, sign._sign(fx.seed, body))                 # the raw body is not an export signature
    req_sig = sign.sign_request(fx.seed, "lab", "hq", "GET", "/x", now=1.0)[sign.H_SIG]
    assert not sign.verify_export(fx.pub, body, req_sig)
    assert not sign.verify_export(fx.pub, body, "")
    assert not sign.verify_export("zz", body, s_export)


def test_signing_key_file_must_be_private_regular_and_well_formed(tmp_path):
    def mk(name, text, mode=0o600):
        p = tmp_path / name
        p.write_text(text)
        p.chmod(mode)
        return str(p)
    seed = sign.seed_hex_new()
    assert sign.load_seed(mk("ok", seed + "\n")) == seed
    assert sign.load_seed(mk("loose", seed, 0o644)) is None                   # group/world readable
    assert sign.load_seed(mk("group", seed, 0o640)) is None
    assert sign.load_seed(mk("short", seed[:-2])) is None
    assert sign.load_seed(mk("upper", seed.upper())) is None
    assert sign.load_seed(mk("big", seed + "\n" * 400)) is None
    assert sign.load_seed(str(tmp_path / "missing")) is None
    assert sign.load_seed(str(tmp_path)) is None
    assert sign.load_seed(None) is None and sign.load_seed("") is None
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "ok")
    assert sign.load_seed(str(link)) is None                                  # a symlink is not followed


# ============================================================================ room shares (collab events)
def err(fn, *a):
    with pytest.raises(CollabError) as e:
        fn(*a)
    return e.value.status, getattr(e.value, "code", None)


def test_only_a_current_owner_grants_a_share(fx):
    w = fx.w
    for who in (w.bob, w.carol, w.dave):
        assert code(fx.s.grant_share, who, w.room, {"peer": "lab"}) == 403
    assert code(fx.s.grant_share, w.mallory, w.room, {"peer": "lab"}) == 404           # a non-member: not found
    agent, _ = w.agent("alice")
    assert err(fx.s.grant_share, agent, w.room, {"peer": "lab"}) == (403, None)
    with pytest.raises(CollabError) as exc:
        fx.s.grant_share(agent, w.room, {"peer": "lab"})
    assert "agents cannot perform" in exc.value.message                         # refused as an agent, before any room check
    with pytest.raises(CollabError) as exc:
        fx.s.revoke_share(agent, w.room, "collab_x")
    assert "agents cannot perform" in exc.value.message
    assert not any(e["event_type"] == "federation_share_granted" for e in fx.s._log)
    s, out = fx.s.grant_share(w.alice, w.room, {"peer": "lab"})                         # control
    assert s == 201 and out["share"]["state"] == "active" and out["share"]["peer"] == "lab"
    assert out["share"]["granted_by"] == "alice"


def test_an_external_agent_cannot_grant_or_list(fx):
    w = fx.w
    pr, _, _ = enroll(w, actions=["read", "post"])
    assert code(fx.s.grant_share, pr, w.room, {"peer": "lab"}) == 403
    assert code(fx.s.list_shares, pr, w.room) == 403
    assert code(fx.s.revoke_share, pr, w.room, "x") == 403


def test_a_grant_needs_the_operator_yaml_to_list_this_room_for_this_peer(fx):
    w = fx.w
    cases = [
        lambda: fx.section.update(enabled=False),
        lambda: fx.peer(enabled=False),
        lambda: fx.peer(export_rooms=[w.room2]),
        lambda: fx.peer(export_rooms=[]),
        lambda: fx.section["peers"].pop("lab"),
        lambda: fx.section.update(server_id=None),
        lambda: fx.section.update(signing_key_file=None),
        lambda: fx.section["peers"]["lab"].update(public_key=None, key_fingerprint="ab" * 6),
    ]
    for mutate in cases:
        fx.section = fx.cfg()
        mutate()
        assert err(fx.s.grant_share, w.alice, w.room, {"peer": "lab"})[0] == 403
    fx.section = fx.cfg()
    assert fx.s.grant_share(w.alice, w.room, {"peer": "lab"})[0] == 201                 # control
    assert err(fx.s.grant_share, w.alice, w.room2, {"peer": "lab"}) == (403, "federation_not_listed")
    assert err(fx.s.grant_share, w.alice, w.room, {"peer": "ghost"}) == (403, "federation_not_listed")


def test_a_degraded_log_refuses_a_grant_even_for_a_repeat(fx):
    w = fx.w
    fx.share()
    fx.s.degraded = "log failed its integrity check"
    assert code(fx.s.grant_share, w.alice, w.room, {"peer": "lab"}) == 503
    fx.s.degraded = None
    assert fx.s.grant_share(w.alice, w.room, {"peer": "lab"})[0] == 200


def test_a_revoked_share_does_not_count_against_the_room_limit(fx):
    from herdr_engineering.control_plane import collab_federation as cf
    w = fx.w
    first = fx.share()
    for i in range(cf.MAX_SHARES_PER_ROOM - 1):
        fx.section["peers"][f"p{i}"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room]}
        fx.share(peer=f"p{i}")
    fx.section["peers"]["extra"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room]}
    assert code(fx.s.grant_share, w.alice, w.room, {"peer": "extra"}) == 429
    fx.s.revoke_share(w.alice, w.room, first)
    assert fx.s.grant_share(w.alice, w.room, {"peer": "extra"})[0] == 201                  # a slot was freed
    assert code(fx.s.grant_share, w.alice, w.room, {"peer": "lab"}) == 429                  # full again
    again = fx.s.grant_share(w.alice, w.room, {"peer": "p0"})                              # p0 is still active: idempotent
    assert again[0] == 200


def test_a_revoked_peer_share_can_be_granted_again_as_a_new_share(fx):
    w = fx.w
    a = fx.share()
    fx.s.revoke_share(w.alice, w.room, a)
    b = fx.share()
    assert a != b and fx.s._fed_shares[a]["state"] == "revoked" and fx.s._fed_shares[b]["state"] == "active"
    fx.say(w.bob, "x")
    assert refused_404(fx.get(f"/api/v1/federation/shares/{a}/events"))
    assert fx.events(b)[0] == 200


def test_grant_attempts_are_rate_limited_including_the_denied_ones(fx):
    w = fx.w
    codes = [code(fx.s.grant_share, w.alice, w.room, {"peer": "ghost"}) for _ in range(20)]
    assert set(codes) == {403}
    assert code(fx.s.grant_share, w.alice, w.room, {"peer": "ghost"}) == 429                # not an unbounded audit spray
    assert sum(e["event_type"] == "audit_denied" for e in fx.s._log) <= 20
    fx.clock.t += 601
    assert fx.s.grant_share(w.alice, w.room, {"peer": "lab"})[0] == 201                     # the window passes


def test_grant_body_is_strict(fx):
    w = fx.w
    for body in ({}, {"peer": ""}, {"peer": 5}, {"peer": None}, {"peer": "lab", "room_id": w.room2},
                 {"peer": "lab", "granted_by": "bob"}):
        assert code(fx.s.grant_share, w.alice, w.room, body) == 400, body
    assert not any(e["event_type"] == "federation_share_granted" for e in fx.s._log)


def test_a_repeated_grant_is_idempotent_and_shares_per_room_are_bounded(fx):
    w = fx.w
    s1, a = fx.s.grant_share(w.alice, w.room, {"peer": "lab"})
    s2, b = fx.s.grant_share(w.alice, w.room, {"peer": "lab"})
    assert (s1, s2) == (201, 200) and a["share"]["share_id"] == b["share"]["share_id"] and b["replayed"]
    assert sum(e["event_type"] == "federation_share_granted" for e in fx.s._log) == 1
    from herdr_engineering.control_plane import collab_federation as cf
    cf_max = cf.MAX_SHARES_PER_ROOM
    assert cf_max >= 2
    for i in range(cf_max - 1):
        fx.section["peers"][f"p{i}"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room]}
        assert fx.s.grant_share(w.alice, w.room, {"peer": f"p{i}"})[0] == 201
    fx.section["peers"]["one-more"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room]}
    assert code(fx.s.grant_share, w.alice, w.room, {"peer": "one-more"}) == 429


def test_members_can_see_that_the_room_is_shared_and_others_cannot(fx):
    w = fx.w
    sid = fx.share()
    for who in (w.alice, w.bob, w.carol):
        out = fx.s.list_shares(who, w.room)
        assert [s["share_id"] for s in out["shares"]] == [sid] and out["enabled"] is True
    assert code(fx.s.list_shares, w.mallory, w.room) == 404
    assert fx.s.list_shares(w.alice, w.room2)["shares"] == []


def test_revoking_is_any_current_owner_and_never_blocked_by_the_kill_switch(fx):
    w = fx.w
    sid = fx.share()
    for who in (w.bob, w.carol, w.dave):
        assert code(fx.s.revoke_share, who, w.room, sid) == 403
    assert code(fx.s.revoke_share, w.mallory, w.room, sid) == 404
    assert code(fx.s.revoke_share, w.alice, w.room, "collab_nope") == 404
    assert code(fx.s.revoke_share, w.alice, w.room2, sid) == 404                        # another room's id space
    fx.section["enabled"] = False                                                       # kill switch ON: still revocable
    out = fx.s.revoke_share(w.alice, w.room, sid)
    assert out["share"]["state"] == "revoked" and out["share"]["revoked_by"] == "alice"
    n = len(fx.s._log)
    assert fx.s.revoke_share(w.alice, w.room, sid)["share"]["state"] == "revoked" and len(fx.s._log) == n   # idempotent


def test_event_classes_are_prepaid_like_every_revocable_creation():
    assert REVOCABLE_CREATE["federation_share_granted"] == "fedshare"
    assert REVOCABLE_CREATE["federation_peer_enabled"] == "fedpeer"
    assert {"federation_share_revoked", "federation_peer_disabled"} <= SAFETY_EVENTS
    assert "federation_share_granted" not in SAFETY_EVENTS and "federation_peer_enabled" not in SAFETY_EVENTS


def test_a_full_log_cannot_make_a_share_irrevocable(tmp_path):
    clock = type("C", (), {"t": 1_800_000_000.0, "__call__": lambda self: self.t})()
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=clock,
                      max_log_events=60, message_limit=10_000)
    s.clock = clock
    w = W(s)
    fx = Fx(w, tmp_path)
    before = s._outstanding()
    sid = fx.share()
    assert s._recount()["fedshare"] == 1 and s._outstanding() == before + 1
    refused = 0
    for i in range(200):
        clock.t += 61.0
        try:
            s.post_message(w.bob, w.room, {"text": f"m{i}", "idempotency_key": k()})
        except CollabError as exc:
            assert exc.status == 503
            refused += 1
            break
    assert refused, "the log never filled"
    with pytest.raises(CollabError):                                                     # ordinary writes: refused now
        s.post_message(w.bob, w.room, {"text": "x", "idempotency_key": k()})
    assert s.revoke_share(w.alice, w.room, sid)["share"]["state"] == "revoked"           # the pre-paid slot is there
    assert s._recount()["fedshare"] == 0 and s._outstanding() == before                 # the cache was cleared too


def test_a_creation_is_admitted_only_with_room_for_its_revocation(tmp_path):
    clock = type("C", (), {"t": 1_800_000_000.0, "__call__": lambda self: self.t})()
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=clock,
                      max_log_events=60, message_limit=10_000)
    s.clock = clock
    w = W(s)
    fx = Fx(w, tmp_path)
    base = s._log_limit("federation_share_granted", {})
    assert base < s._log_limit("federation_share_revoked", {})                           # revocation tier is the full log
    assert s._log_limit("federation_share_revoked", {}) == 60
    fx.share()
    assert s._log_limit("federation_share_granted", {}) == base - 1                      # one outstanding revocation reserved


def test_share_state_survives_a_restart_and_unknown_state_is_inert(fx, tmp_path):
    w = fx.w
    a = fx.share()
    fx.section["peers"]["p2"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room]}
    b = fx.share(peer="p2")
    fx.s.revoke_share(w.alice, w.room, b)
    fx.s.maintain()
    again = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=fx.clock)
    again.hydrate(fx.s._log)
    assert {i: s["state"] for i, s in again._fed_shares.items()} == {a: "active", b: "revoked"}
    assert again._fed_peers == fx.s._fed_peers and again._recount()["fedshare"] == 1


# ---------------------------------------------------------------------------- YAML -> log reconciliation
def log_types(fx):
    return [e["event_type"] for e in fx.s._log if e["event_type"].startswith("federation_")]


def test_yaml_peer_changes_are_recorded_once_and_revoke_what_they_no_longer_allow(fx):
    fx.s.maintain()
    assert log_types(fx) == ["federation_peer_enabled"]
    fx.clock.t += 10
    fx.s.maintain()
    fx.s.maintain()
    assert log_types(fx) == ["federation_peer_enabled"]                                  # not re-recorded
    sid = fx.share()
    fx.peer(enabled=False)
    fx.clock.t += 10
    fx.s.maintain()
    assert log_types(fx)[-2:] == ["federation_share_revoked", "federation_peer_disabled"] or \
        set(log_types(fx)[-2:]) == {"federation_share_revoked", "federation_peer_disabled"}
    assert fx.s._fed_shares[sid]["state"] == "revoked" and fx.s._fed_shares[sid]["revoked_reason"] == "peer_disabled"
    assert fx.s._fed_shares[sid]["revoked_by"] == "system"
    assert fx.s._fed_peers == {"lab": False}
    fx.peer(enabled=True)                                                                # re-enabling does NOT revive it
    fx.clock.t += 10
    fx.s.maintain()
    assert fx.s._fed_shares[sid]["state"] == "revoked" and fx.s._fed_peers == {"lab": True}


def test_removing_a_room_from_the_yaml_or_the_owner_lapsing_revokes_the_share(fx):
    w = fx.w
    sid = fx.share()
    fx.peer(export_rooms=[])
    fx.clock.t += 10
    fx.s.maintain()
    assert fx.s._fed_shares[sid]["revoked_reason"] == "unlisted"
    fx.peer(export_rooms=[w.room])
    sid2 = fx.share()
    fx.s._rooms[w.room]["members"]["alice"]["role"] = "contributor"                      # the accepting owner lost the role
    fx.clock.t += 10
    fx.s.maintain()
    assert fx.s._fed_shares[sid2]["revoked_reason"] == "owner_lapsed"


def test_an_unreadable_or_invalid_yaml_never_rewrites_the_log(fx):
    fx.s.maintain()
    sid = fx.share()
    n = len(fx.s._log)

    def boom():
        raise OSError("unusable")
    fx.s.federation_source = boom
    fx.clock.t += 10
    fx.s.maintain()
    assert len(fx.s._log) == n and fx.s._fed_shares[sid]["state"] == "active"
    fx.s.federation_source = lambda: {"enabled": "yes"}
    fx.clock.t += 10
    fx.s.maintain()
    assert len(fx.s._log) == n
    fx.s.federation_source = lambda: fx.section
    assert fx.s._fed_shares[sid]["state"] == "active"


def test_a_kill_switch_alone_does_not_revoke_but_closes_the_export(fx):
    sid = fx.share()
    fx.section["enabled"] = False
    fx.clock.t += 10
    fx.s.maintain()
    assert fx.s._fed_shares[sid]["state"] == "active"
    assert refused_404(fx.get(f"/api/v1/federation/shares/{sid}/events"))
    fx.section["enabled"] = True
    assert fx.events(sid)[0] == 200


def test_a_refused_peer_record_does_not_stop_the_revocations(fx):
    """A creation can be refused on a nearly full log; the pre-paid revocations after it must still be written."""
    w = fx.w
    fx.s.maintain()
    sid = fx.share()
    fx.section["peers"]["new"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": []}      # needs a new record
    fx.peer(export_rooms=[])                                                                         # un-lists the room
    ordinary = fx.s._log_limit("federation_peer_enabled", {})
    fx.s._log.extend([{"sequence": -1}] * (ordinary - len(fx.s._log)))                               # exactly full for it
    assert fx.s._log_limit("federation_share_revoked", {}) > len(fx.s._log)
    fx.clock.t += 10
    fx.s.maintain()
    assert fx.s._fed_shares[sid]["state"] == "revoked" and fx.s._fed_shares[sid]["revoked_reason"] == "unlisted"
    assert fx.s._fed_peers.get("new") is None                                                        # refused, retried later
    fx.peer(export_rooms=[w.room])                                                                   # a re-list revives nothing
    assert refused_404(fx.get(f"/api/v1/federation/shares/{sid}/events"))


def test_one_failed_write_does_not_stop_the_other_revocations(fx):
    w = fx.w
    fx.section["peers"]["p2"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room]}
    a, b = fx.share(), fx.share(peer="p2")
    fx.section["peers"].clear()                                                       # both peers removed
    real = fx.s._sink.record
    calls = []

    def flaky(ev):
        calls.append(ev["event_type"])
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real(ev)
    fx.s._sink.record = fx.s._sink.record_required = flaky
    fx.clock.t += 10
    fx.s.maintain()
    states = sorted(s["state"] for s in fx.s._fed_shares.values())
    assert states == ["active", "revoked"], states                                    # one failed, the other went through
    fx.s._sink.record = fx.s._sink.record_required = real
    fx.clock.t += 10
    fx.s.maintain()
    assert {fx.s._fed_shares[a]["state"], fx.s._fed_shares[b]["state"]} == {"revoked"}


def test_an_unexpected_error_in_reconcile_never_escapes_maintain(fx, monkeypatch):
    def boom():
        raise RuntimeError("unexpected")
    monkeypatch.setattr(fx.s, "_fed_reconcile_locked", boom)
    fx.s.maintain()                                                                   # must not raise (nor must a graph read)


def test_a_full_log_does_not_break_maintain(fx):
    fx.s.max_log_events = len(fx.s._log)
    fx.s.maintain()                                                                      # must not raise
    fx.s.max_log_events = 200_000


def test_peer_records_are_prepaid_and_counted(fx):
    b = fx.s._outstanding()
    fx.s.maintain()
    assert fx.s._recount()["fedpeer"] == 1 and fx.s._outstanding() == b + 1
    fx.peer(enabled=False)
    fx.clock.t += 10
    fx.s.maintain()
    assert fx.s._recount()["fedpeer"] == 0 and fx.s._outstanding() == b             # the cache cleared on the disable


def test_the_accepting_owner_losing_access_revokes_and_closes(fx, monkeypatch):
    w = fx.w
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    assert fx.get(url)[0] == 200
    monkeypatch.setattr(fx.s.auth, "exists", lambda uid: uid != "alice")               # alice left the network
    assert refused_404(fx.get(url))                                                   # at once, from the export itself
    assert fx.s.federation_events("lab", sid, 0, 10, 10_000, fed.parse_federation(fx.section)) is None
    fx.clock.t += 10
    fx.s.maintain()
    assert fx.s._fed_shares[sid]["revoked_reason"] == "owner_lapsed"
    monkeypatch.undo()
    assert w


def test_a_removed_owner_is_not_an_owner_for_the_export_or_the_sweep(fx):
    w = fx.w
    sid = fx.share()
    fx.s._rooms[w.room]["members"].pop("alice")
    assert fx.s.federation_events("lab", sid, 0, 10, 10_000, fed.parse_federation(fx.section)) is None
    assert fx.s.federation_shares("lab", fed.parse_federation(fx.section))["shares"] == []
    fx.clock.t += 10
    fx.s.maintain()
    assert fx.s._fed_shares[sid]["revoked_reason"] == "owner_lapsed"


def test_dispatch_routes_for_owners(fx):
    w = fx.w
    tok = f"Bearer {USERS['alice']}"
    rid = w.room
    s, out = fx.s.dispatch("POST", ["rooms", rid, "federation", "shares"], {}, {"peer": "lab"}, tok, None)
    assert s == 201
    sid = out["share"]["share_id"]
    s, out = fx.s.dispatch("GET", ["rooms", rid, "federation", "shares"], {}, {}, tok, None)
    assert s == 200 and out["shares"][0]["share_id"] == sid
    s, out = fx.s.dispatch("POST", ["rooms", rid, "federation", "shares", sid, "revoke"], {}, {}, tok, None)
    assert s == 200 and out["share"]["state"] == "revoked"
    bob = f"Bearer {USERS['bob']}"
    assert fx.s.dispatch("POST", ["rooms", rid, "federation", "shares"], {}, {"peer": "lab"}, bob, None)[0] == 403
    s2, out2 = fx.s.dispatch("POST", ["rooms", rid, "federation", "shares"], {}, {"peer": "lab"}, tok, None)
    sid2 = out2["share"]["share_id"]
    for parts in (["rooms", rid, "federation", "shares", sid2, "revoke", "x"],
                  ["rooms", rid, "federation", "other", sid2, "revoke"],
                  ["rooms", rid, "federation", "shares", sid2, "restore"],
                  ["rooms", rid, "federation", "shares", sid2]):
        assert fx.s.dispatch("POST", parts, {}, {}, tok, None)[0] == 404, parts
    assert fx.s._fed_shares[sid2]["state"] == "active"


# ============================================================================ the signed export
SHARES = "/api/v1/federation/shares"


def test_export_is_signed_bounded_and_carries_only_the_allowlist(fx):
    w = fx.w
    w.s.post_message(w.alice, w.room, {"text": "before the grant", "idempotency_key": k()})
    sid = fx.share()
    e1 = fx.say(w.bob, "hello from bob")
    fx.say(w.alice, "an answer", in_reply_to=e1["event_id"])
    st, doc, raw, extra = fx.events(sid)
    assert st == 200
    assert sign.verify_export(fx.pub, raw, extra[sign.H_EXPORT_SIG])                      # signed over the exact bytes sent
    assert extra[sign.H_EXPORT_KEY] == fed.key_fingerprint_of(fx.pub)
    assert (doc["scheme"], doc["kind"], doc["exporter"], doc["audience"]) == (sign.SCHEME, "events", "hq", "lab")
    assert doc["share_id"] == sid and doc["expires_at"] - doc["issued_at"] == 300 and doc["issued_at"] == int(fx.clock.t)
    assert [e["text"] for e in doc["events"]] == ["hello from bob", "an answer"]            # nothing from before the grant
    for ev in doc["events"]:
        assert set(ev) == LITERAL_MESSAGE_FIELDS == set(EXPORT_FIELDS["message"])
        assert ev["trust"] == "untrusted" and ev["event_type"] == "message"
    assert doc["events"][1]["in_reply_to"] == e1["event_id"] and doc["events"][0]["in_reply_to"] is None
    assert doc["more"] is False and doc["next_cursor"] >= doc["events"][-1]["origin_sequence"]
    tampered = raw.replace(b"hello from bob", b"hello from eve")
    assert not sign.verify_export(fx.pub, tampered, extra[sign.H_EXPORT_SIG])


def test_export_validity_window_follows_the_yaml(fx):
    sid = fx.share()
    fx.section["export_validity_seconds"] = 45
    doc = fx.events(sid)[1]
    assert doc["expires_at"] - doc["issued_at"] == 45


def test_nothing_that_identifies_a_person_or_a_server_object_leaves(fx):
    w = fx.w
    sid = fx.share()
    fx.say(w.bob, "plain words")
    fx.say(w.alice, "hi there, please look")
    doc = fx.events(sid)[1]
    blob = json.dumps(doc)
    for forbidden in ("alice", "bob", "carol", "@example", w.room, fx.s._rooms[w.room]["project_id"], "sender_key",
                      "sender_id", "actor_user_id", "owner_user_id", "mentions", "context", "idempotency", "email",
                      "fingerprint", "project_id", "room_id", fx.pub, fx.seed, PEER_PUB):
        assert forbidden not in blob, forbidden
    refs = {e["sender_ref"] for e in doc["events"]}
    assert len(refs) == 2 and all(r.startswith("p-") and len(r) == 14 for r in refs)


def test_sender_pseudonym_is_stable_within_a_share_and_differs_across_shares_and_people(fx):
    w = fx.w
    fx.section["peers"]["p2"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room]}
    a, b = fx.share(), fx.share(peer="p2")
    fx.say(w.bob, "one")
    fx.say(w.bob, "two")
    fx.say(w.alice, "three")
    d1 = fx.events(a)[1]["events"]
    d2 = fx.events(b, peer="p2", seed=OTHER_SEED)[1]["events"]
    assert d1[0]["sender_ref"] == d1[1]["sender_ref"] != d1[2]["sender_ref"]
    assert d1[0]["sender_ref"] != d2[0]["sender_ref"]


def test_sender_kinds_are_coarse(fx):
    w = fx.w
    sid = fx.share()
    agent, _ = w.agent("bob")
    fx.say(w.bob, "person")
    fx.say(agent, "agent")
    pr, _, _ = enroll(w, actions=["read", "post"])
    fx.say(pr, "external")
    kinds = {e["text"]: e["sender_kind"] for e in fx.events(sid)[1]["events"]}
    assert kinds == {"person": "person", "agent": "agent", "external": "external"}


def test_redaction_runs_on_the_exporter_even_for_text_that_was_stored_raw(fx):
    w = fx.w
    sid = fx.share()
    ev = fx.say(w.bob, "marker")
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3"
    for row in fx.s._by_room[w.room]:
        if row["event_id"] == ev["event_id"]:
            row["payload"]["text"] = f"leak {secret} and password=hunter2hunter2"
    doc = fx.events(sid)[1]
    assert secret not in json.dumps(doc) and "hunter2" not in json.dumps(doc)
    assert "[REDACTED]" in doc["events"][0]["text"]


def test_only_room_scope_messages_ever_leave(fx):
    w = fx.w
    sid = fx.share()
    ev = fx.say(w.bob, "visible")
    for scope in ("org", "private", "mission", "bogus"):
        fx.say(w.bob, f"hidden-{scope}")
        for row in fx.s._by_room[w.room]:
            if row["payload"].get("text") == f"hidden-{scope}":
                row["payload"]["visibility_scope"] = scope
    doc = fx.events(sid)[1]
    assert [e["text"] for e in doc["events"]] == ["visible"] and ev


def test_non_message_events_in_the_feed_are_never_exported(fx):
    w = fx.w
    sid = fx.share()
    fx.s.add_member(w.alice, w.room, {"user_id": "mallory", "role": "viewer"})
    agent, _ = w.agent("bob")                                                            # member/agent/grant events
    fx.say(w.bob, "only this")
    feed_types = {e["event_type"] for e in fx.s._by_room[w.room]}
    assert {"member_added", "agent_granted"} <= feed_types
    doc = fx.events(sid)[1]
    assert [e["event_type"] for e in doc["events"]] == ["message"] and [e["text"] for e in doc["events"]] == ["only this"]
    assert EXPORT_FIELDS.keys() == {"message"} and agent


def test_task_text_and_other_feed_events_with_a_text_field_never_leave(fx):
    w = fx.w
    sid = fx.share()
    agent, info = w.agent("bob")
    fx.say(w.bob, "chat only")
    fx.clock.t += 2
    made = fx.s.create_task(w.bob, w.room, {"target_agent_id": info["agent_id"], "text": "SECRET TASK TEXT"})
    fx.s.update_task(agent, made["task_id"], {"status": "accepted", "result": "SECRET RESULT"})
    assert {"task_created", "task_updated"} <= {e["event_type"] for e in fx.s._by_room[w.room]}
    raw = fx.get(f"/api/v1/federation/shares/{sid}/events")[1].decode()
    assert "SECRET" not in raw and "chat only" in raw


def test_blank_text_and_overlong_text_and_cross_room_replies_are_handled_at_export(fx):
    w = fx.w
    sid = fx.share()
    fx.s.add_member(w.alice, w.room2, {"user_id": "bob", "role": "contributor"})
    other = fx.say(w.bob, "other room", room=w.room2)
    a, b, c = fx.say(w.bob, "blank"), fx.say(w.bob, "long"), fx.say(w.bob, "reply")
    for row in fx.s._by_room[w.room]:
        if row["event_id"] == a["event_id"]:
            row["payload"]["text"] = "   "
        elif row["event_id"] == b["event_id"]:
            row["payload"]["text"] = "y" * 9000
        elif row["event_id"] == c["event_id"]:
            row["payload"]["in_reply_to"] = other["event_id"]
    evs = fx.events(sid)[1]["events"]
    assert len(evs) == 2 and len(evs[0]["text"]) == 8000 and evs[1]["in_reply_to"] is None


def test_a_truncated_scan_window_says_there_is_more(fx, monkeypatch):
    from herdr_engineering.control_plane import collab_federation as cf
    w = fx.w
    sid = fx.share()
    for i in range(5):
        fx.say(w.bob, f"m{i}")
    monkeypatch.setattr(cf, "MAX_SCAN", 2)
    doc = fx.events(sid)[1]
    assert [e["text"] for e in doc["events"]] == ["m0", "m1"] and doc["more"] is True
    doc2 = fx.events(sid, f"?cursor={doc['next_cursor']}")[1]
    assert [e["text"] for e in doc2["events"]] == ["m2", "m3"] and doc2["more"] is True
    doc3 = fx.events(sid, f"?cursor={doc2['next_cursor']}")[1]
    assert [e["text"] for e in doc3["events"]] == ["m4"] and doc3["more"] is False


def test_a_reply_target_from_before_the_grant_is_not_referenced(fx):
    w = fx.w
    old = w.s.post_message(w.alice, w.room, {"text": "old", "idempotency_key": k()})[1]["event"]
    sid = fx.share()
    fx.say(w.bob, "reply to old", in_reply_to=old["event_id"])
    doc = fx.events(sid)[1]
    assert doc["events"][0]["in_reply_to"] is None


def test_cursor_pagination_limit_and_byte_budget(fx):
    w = fx.w
    sid = fx.share()
    for i in range(7):
        fx.say(w.bob, f"msg-{i}")
    st, d1, _, _ = fx.events(sid, "?limit=3")
    assert [e["text"] for e in d1["events"]] == ["msg-0", "msg-1", "msg-2"] and d1["more"] is True
    d2 = fx.events(sid, f"?cursor={d1['next_cursor']}&limit=3")[1]
    assert [e["text"] for e in d2["events"]] == ["msg-3", "msg-4", "msg-5"] and d2["more"] is True
    d3 = fx.events(sid, f"?cursor={d2['next_cursor']}&limit=3")[1]
    assert [e["text"] for e in d3["events"]] == ["msg-6"] and d3["more"] is False
    d4 = fx.events(sid, f"?cursor={d3['next_cursor']}")[1]
    assert d4["events"] == [] and d4["next_cursor"] == d3["next_cursor"]
    # a cursor below the grant cannot reach back before it
    w.s.post_message(w.alice, w.room, {"text": "x", "idempotency_key": k()})
    assert fx.events(sid, "?cursor=0")[1]["events"][0]["text"] == "msg-0"
    # a per-response byte budget stops early and says there is more
    doc = fx.s.federation_events("lab", sid, 0, 100, 1, fed.parse_federation(fx.section))
    assert len(doc["events"]) == 1 and doc["more"] is True
    fx.section["max_export_events"] = 2
    assert len(fx.events(sid)[1]["events"]) == 2


@pytest.mark.parametrize("query", [
    "?bogus=1", "?cursor=1&cursor=2", "?cursor=-1", "?cursor=1e3", "?cursor=%205", "?cursor=+5", "?cursor=1_0",
    "?cursor=", "?limit=0", "?limit=101", "?limit=x", "?limit=%D9%A3", f"?cursor={2 ** 53}", "?cursor=1&limit=1&x=1",
    "?cursor=" + "9" * 17, "?cursor", "?&"])
def test_query_parameters_are_strict(fx, query):
    sid = fx.share()
    assert fx.events(sid)[0] == 200
    st = fx.events(sid, query)[0]
    assert st == 400


def test_valid_query_forms(fx):
    sid = fx.share()
    for q in ("", "?cursor=0", "?limit=1", "?cursor=0&limit=100", "?limit=100&cursor=0"):
        assert fx.events(sid, q)[0] == 200, q


def test_shares_listing_is_per_peer_and_minimal(fx):
    w = fx.w
    a = fx.share()
    fx.section["peers"]["p2"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room, w.room2]}
    b = fx.share(peer="p2")
    c = fx.share(peer="p2", room=w.room2)
    st, raw, extra = fx.get(SHARES)
    doc = json.loads(raw)
    assert st == 200 and [s["share_id"] for s in doc["shares"]] == [a] and doc["kind"] == "shares"
    assert set(doc["shares"][0]) == {"share_id", "granted_at"} and sign.verify_export(
        fx.pub, raw, extra[sign.H_EXPORT_SIG])
    doc2 = json.loads(fx.get(SHARES, seed=OTHER_SEED, peer="p2")[1])
    assert sorted(s["share_id"] for s in doc2["shares"]) == sorted([b, c])
    assert w.room not in raw.decode() and w.room2 not in json.dumps(doc2)


def test_uniform_404_for_every_way_a_share_can_be_unavailable(fx):
    w = fx.w
    fx.say(w.bob, "x")
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    assert fx.get(url)[0] == 200                                                          # control
    fx.section["peers"]["p2"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room]}
    results = {}

    def probe(label, **kw):
        results[label] = fx.get(kw.pop("target", url), **kw)
    probe("unknown_share", target="/api/v1/federation/shares/collab_nope/events")
    probe("bad_share_chars", target="/api/v1/federation/shares/%2e%2e/events")
    probe("other_peers_share", seed=OTHER_SEED, peer="p2")
    probe("bad_signature", seed=OTHER_SEED)
    probe("post", method="POST")
    probe("wrong_path", target="/api/v1/federation/shares/" + sid)
    probe("wrong_path2", target="/api/v1/federation/other")
    probe("trailing", target=url + "/")
    probe("extra_segment", target=f"/api/v1/federation/shares/{sid}/other")
    probe("events_not_last", target=f"/api/v1/federation/shares/{sid}/events/x")
    probe("other_prefix", target="/api/v1/health")
    probe("prefix_lookalike", target="/api/v1/federationx/shares")
    probe("prefix_shifted", target="/api/v1/federationXshares")                          # same length as the prefix
    probe("double_slash", target="/api/v1/federation//shares")
    probe("shares_with_query", target=SHARES + "?x=1")
    for label, r in results.items():
        assert refused_404(r), label
    fx.section["peers"]["p2"]["enabled"] = False
    assert refused_404(fx.get(url, seed=OTHER_SEED, peer="p2"))
    # revoke -> the very next request is a 404 (revocation wins immediately)
    fx.s.revoke_share(w.alice, w.room, sid)
    assert refused_404(fx.get(url))
    assert sid not in json.loads(fx.get(SHARES)[1])["shares"].__repr__()


def test_unlisting_the_room_or_losing_the_owner_closes_the_export_at_once(fx):
    w = fx.w
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    fx.peer(export_rooms=[w.room2])
    assert refused_404(fx.get(url)) and json.loads(fx.get(SHARES)[1])["shares"] == []
    fx.peer(export_rooms=[w.room])
    assert fx.get(url)[0] == 200
    fx.s._rooms[w.room]["members"]["alice"]["role"] = "contributor"
    assert refused_404(fx.get(url))
    fx.s._rooms[w.room]["members"]["alice"]["role"] = "owner"
    assert fx.get(url)[0] == 200
    fx.s._rooms[w.room]["members"].pop("alice")
    assert refused_404(fx.get(url))
    fx.s._rooms[w.room]["members"]["alice"] = {"role": "owner", "joined_seq": 0, "expires_at": None}
    assert fx.get(url)[0] == 200
    del fx.s._rooms[w.room]
    assert refused_404(fx.get(url))


def test_a_peer_cannot_reach_another_rooms_share_even_when_both_are_listed(fx):
    w = fx.w
    fx.peer(export_rooms=[w.room, w.room2])
    fx.s.add_member(w.alice, w.room2, {"user_id": "bob", "role": "contributor"})
    a, b = fx.share(), fx.share(room=w.room2)
    fx.say(w.bob, "in room one")
    fx.say(w.bob, "in room two", room=w.room2)
    assert [e["text"] for e in fx.events(a)[1]["events"]] == ["in room one"]
    assert [e["text"] for e in fx.events(b)[1]["events"]] == ["in room two"]


def test_the_service_decides_for_itself_whatever_the_gateway_checked(fx):
    """federation_events / federation_shares are the authority on a share; they do not trust their caller."""
    w = fx.w
    sid = fx.share()
    fx.say(w.bob, "x")
    def ask(section, peer="lab", share=sid):
        return fx.s.federation_events(peer, share, 0, 10, 10_000, fed.parse_federation(section))
    assert ask(fx.section)["events"]                                                       # control
    off = {**fx.section, "enabled": False}
    assert ask(off) is None
    gone = {**fx.section, "peers": {}}
    assert ask(gone) is None
    disabled = {**fx.section, "peers": {"lab": {**fx.section["peers"]["lab"], "enabled": False}}}
    assert ask(disabled) is None
    assert ask(fx.section, share="collab_nope") is None and ask(fx.section, share=None) is None
    assert ask(fx.section, peer="other") is None
    assert fx.s.federation_shares("lab", fed.parse_federation(off))["shares"] == []
    assert fx.s.federation_shares("lab", fed.parse_federation(disabled))["shares"] == []
    assert [s["share_id"] for s in fx.s.federation_shares("lab", fed.parse_federation(fx.section))["shares"]] == [sid]
    fx.s.degraded = "x"
    assert fx.s.federation_events("lab", sid, 0, 10, 10_000, fed.parse_federation(fx.section)) is None
    assert fx.s.federation_shares("lab", fed.parse_federation(fx.section)) is None
    fx.s.degraded = None


def test_gateway_preconditions_all_answer_the_same_404(fx, tmp_path):
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    assert fx.get(url)[0] == 200
    fx.section["server_id"] = None
    assert refused_404(fx.get(url))
    fx.section = fx.cfg()
    fx.section["signing_key_file"] = str(tmp_path / "missing.key")
    assert refused_404(fx.get(url))
    os.chmod(fx.key_file, 0o644)
    fx.section = fx.cfg()
    assert refused_404(fx.get(url))                                                       # a loose key file is not used
    os.chmod(fx.key_file, 0o600)
    assert fx.get(url)[0] == 200
    fx.s.degraded = "log failed its integrity check"
    assert refused_404(fx.get(url))
    fx.s.degraded = None
    fx.gw.service = None
    assert refused_404(fx.get(url))


def test_a_missing_signing_library_means_off(fx, monkeypatch):
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    assert fx.get(url)[0] == 200
    monkeypatch.setattr(sign, "available", lambda: False)
    assert refused_404(fx.get(url))


def test_quotas_are_per_peer_per_window(fx):
    w = fx.w
    sid = fx.share()
    fx.section["peers"]["p2"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room],
                                 "requests_per_minute": 600}
    fx.peer(requests_per_minute=3)
    url = f"/api/v1/federation/shares/{sid}/events"
    assert [fx.get(SHARES, seed=OTHER_SEED, peer="p2")[0] for _ in range(5)] == [200] * 5     # p2's use is p2's own
    assert [fx.get(url)[0] for _ in range(4)] == [200, 200, 200, 429]
    st, body, extra = fx.get(url)
    assert st == 429 and extra == {"Retry-After": "60"}
    assert fx.get(SHARES, seed=OTHER_SEED, peer="p2")[0] == 200                           # another peer is unaffected
    fx.clock.t += 61
    assert fx.get(url)[0] == 200                                                          # the window slides


def test_byte_quota_is_hourly_and_counts_what_was_sent(fx):
    w = fx.w
    sid = fx.share()
    for i in range(30):
        fx.say(w.bob, "x" * 400 + str(i))
    size = len(fx.get(f"/api/v1/federation/shares/{sid}/events?limit=30")[1])
    fx.peer(max_bytes_per_hour=size * 3 + 10, requests_per_minute=600)         # the measuring call above counted once
    fx.section["peers"]["p2"] = {"enabled": True, "public_key": OTHER_PUB, "export_rooms": [w.room],
                                 "requests_per_minute": 600, "max_bytes_per_hour": 1 << 30}
    fx.share(peer="p2")
    other = [fx.get(SHARES, seed=OTHER_SEED, peer="p2")[0] for _ in range(3)]
    assert other == [200, 200, 200]                                                  # p2's bytes are not lab's bytes
    url = f"/api/v1/federation/shares/{sid}/events?limit=30"
    assert [fx.get(url)[0] for _ in range(2)] == [200, 200]
    st, body, extra = fx.get(url)
    assert st == 429 and extra == {"Retry-After": "300"}
    fx.clock.t += 3601
    assert fx.get(url)[0] == 200


def test_nonces_are_single_use_through_the_gateway(fx):
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    assert fx.get(url, nonce="r" * 20)[0] == 200
    assert refused_404(fx.get(url, nonce="r" * 20))
    assert fx.get(url, nonce="s" * 20)[0] == 200


def test_the_export_never_contains_secrets_or_keys(fx):
    w = fx.w
    sid = fx.share()
    fx.say(w.bob, "hello")
    raw = fx.get(f"/api/v1/federation/shares/{sid}/events")[1] + fx.get(SHARES)[1]
    for secret in (fx.seed, PEER_SEED, OTHER_SEED, "signing_key", fx.key_file, "public_key"):
        assert secret.encode() not in raw, secret


# ============================================================================ over HTTP (the real handler)
class Http:
    def __init__(self, port):
        self.port = port

    def req(self, method, target, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, target, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def signed(self, target, seed=PEER_SEED, peer="lab", audience="hq", method="GET", extra=None):
        h = sign.sign_request(seed, peer, audience, method, target, b"")
        return self.req(method, target, {**h, **(extra or {})})


@pytest.fixture
def served(w, tmp_path):
    fx = Fx(w, tmp_path)
    fx.clock.t = __import__("time").time()                                    # the handler uses the real clock
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=w.s, api_keys={"op": "operator",
                                                                                                 "view": "viewer"},
                             federation_source=lambda: fx.section, public_port=0)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    fx.http, fx.public, fx.srv = Http(srv.port), Http(srv.public_port), srv
    yield fx
    srv.shutdown()
    t.join(timeout=5)


def test_http_signed_export_works_without_an_api_key_and_is_signed(served):
    fx = served
    fx.w.s.post_message(fx.w.alice, fx.w.room, {"text": "old", "idempotency_key": k()})
    sid = fx.share()
    fx.say(fx.w.bob, "over the wire")
    st, body, h = fx.http.signed(f"/api/v1/federation/shares/{sid}/events")
    assert st == 200 and h["Cache-Control"] == "no-store" and h["Content-Type"] == "application/json"
    assert sign.verify_export(fx.pub, body, h[sign.H_EXPORT_SIG])
    assert [e["text"] for e in json.loads(body)["events"]] == ["over the wire"]
    st, body, h = fx.http.signed(SHARES)
    assert st == 200 and json.loads(body)["shares"][0]["share_id"] == sid


def test_http_refusals_are_uniform_and_the_surface_is_read_only(served):
    fx = served
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    want = (404, NOT_FOUND[1])
    for label, r in {
        "no_signature": fx.http.req("GET", url),
        "api_key_is_not_a_credential": fx.http.req("GET", url, {"X-API-Key": "op"}),
        "viewer_key_is_not_a_credential": fx.http.req("GET", url, {"X-API-Key": "view"}),
        "bad_key": fx.http.signed(url, seed=OTHER_SEED),
        "wrong_audience": fx.http.signed(url, audience="other"),
        "public_listener": fx.public.signed(url),
        "bare_prefix": fx.http.req("GET", "/api/v1/federation"),
        "post": fx.http.req("POST", url, {"X-API-Key": "op", "Content-Type": "application/json"}, b"{}"),
        "post_signed": fx.http.req("POST", url, {**sign.sign_request(PEER_SEED, "lab", "hq", "POST", url, b"{}"),
                                                 "Content-Type": "application/json"}, b"{}"),
        "post_shares": fx.http.req("POST", SHARES, {"X-API-Key": "op", "Content-Type": "application/json"}, b"{}"),
    }.items():
        assert (r[0], r[1]) == want, label
    st, body, h = fx.http.signed(url)
    assert st == 200                                                                       # control
    # a GET that carries a body is refused (it would otherwise desynchronise a keep-alive connection)
    c = http.client.HTTPConnection("127.0.0.1", fx.http.port, timeout=10)
    c.request("GET", url, body=b"x", headers=sign.sign_request(PEER_SEED, "lab", "hq", "GET", url, b""))
    r = c.getresponse()
    assert (r.status, r.read()) == want
    c.close()
    # no other verb is implemented at all
    for method in ("PUT", "DELETE", "PATCH"):
        assert fx.http.req(method, url)[0] in (404, 501)


def raw_http(port, text: bytes) -> bytes:
    import socket
    c = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        c.sendall(text)
        out = b""
        while True:
            chunk = c.recv(65536)
            if not chunk:
                break
            out += chunk
            if b"\r\n\r\n" in out and b"Connection: close" in out.split(b"\r\n\r\n")[0]:
                break
            if len(out) > 1_000_000 or b'"error"' in out:
                break
    finally:
        c.close()
    return out


def test_http_ambiguous_framing_and_a_signed_get_with_another_verb_are_refused(served):
    fx = served
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    sig = sign.sign_request(PEER_SEED, "lab", "hq", "GET", url, b"")
    head = "".join(f"{k}: {v}\r\n" for k, v in sig.items())
    ok = raw_http(fx.http.port, f"GET {url} HTTP/1.1\r\nHost: x\r\n{head}Connection: close\r\n\r\n".encode())
    assert ok.startswith(b"HTTP/1.0 200") or ok.startswith(b"HTTP/1.1 200")                  # control
    for extra in ("Content-Length: 0\r\nContent-Length: 0\r\n", "Transfer-Encoding: chunked\r\n"):
        sig = sign.sign_request(PEER_SEED, "lab", "hq", "GET", url, b"")
        head = "".join(f"{k}: {v}\r\n" for k, v in sig.items())
        out = raw_http(fx.http.port, f"GET {url} HTTP/1.1\r\nHost: x\r\n{head}{extra}Connection: close\r\n\r\n".encode())
        assert b" 404 " in out.split(b"\r\n")[0], extra
    # a request signed as GET but sent as POST, and one signed as POST: neither is served
    for signed_as in ("GET", "POST"):
        h = sign.sign_request(PEER_SEED, "lab", "hq", signed_as, url, b"")
        h["Content-Length"] = "0"
        assert fx.http.req("POST", url, h)[0] == 404, signed_as


def test_the_server_wires_its_federation_source_into_the_collab_service(tmp_path):
    clock = type("C", (), {"t": 1_800_000_000.0, "__call__": lambda self: self.t})()
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=clock)
    assert s.federation_source is None
    src = lambda: None                                                             # noqa: E731
    ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=s, api_keys={"op": "operator"},
                       federation_source=src)
    assert s.federation_source is src


def test_http_off_and_standalone_answer_404_and_ordinary_routes_are_unchanged(served):
    fx = served
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    assert fx.http.signed(url)[0] == 200
    fx.section["enabled"] = False
    assert (fx.http.signed(url)[0], fx.http.signed(url)[1]) == (404, NOT_FOUND[1])
    assert fx.http.req("GET", "/api/v1/health", {"X-API-Key": "view"})[0] == 200
    # a server with no federation source at all
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=fx.w.s, api_keys={"op": "operator"})
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    try:
        plain = Http(srv.port)
        assert plain.signed(url)[0] == 404 and plain.signed(SHARES)[0] == 404
        assert plain.req("GET", "/api/v1/health", {"X-API-Key": "op"})[0] == 200
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_http_requires_https_when_the_collab_proxy_rule_is_on(served):
    fx = served
    sid = fx.share()
    url = f"/api/v1/federation/shares/{sid}/events"
    assert fx.http.signed(url)[0] == 200
    fx.srv.collab_require_https = True
    assert fx.http.signed(url)[0] == 404
    assert fx.http.signed(url, extra={"X-Forwarded-Proto": "https"})[0] == 200
    fx.srv.collab_require_https = False


def test_http_off_and_on_look_the_same_to_an_unauthenticated_caller(served):
    """Status, body, headers and limiter use must not tell a prober whether federation is switched on."""
    fx = served
    sid = fx.share()
    calls = []
    real = fx.srv._limiter.allow
    fx.srv._limiter.allow = lambda who: calls.append(who) or real(who)
    url = f"/api/v1/federation/shares/{sid}/events"

    def shape(r):
        status, body, headers = r
        return status, body, {k: v for k, v in headers.items() if k not in ("Date", "Server")}
    fx.section["enabled"] = False
    off = [shape(fx.http.req("GET", url)), shape(fx.http.signed(url))]
    n_off = len(calls)
    fx.section["enabled"] = True
    on = [shape(fx.http.req("GET", url)), shape(fx.http.signed(url, seed=OTHER_SEED)),
          shape(fx.http.signed("/api/v1/federation/shares/collab_nope/events"))]
    assert n_off == 2 and len(calls) == 5                                    # the limiter was used in both states
    assert off[0] == off[1] == on[0] == on[1] == on[2]
    assert on[0][0] == 404 and on[0][2]["Connection"] == "close"


def test_http_without_a_collab_service_is_a_404(w, tmp_path):
    fx = Fx(w, tmp_path)
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=None, api_keys={"op": "operator"},
                             federation_source=lambda: fx.section)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    try:
        assert Http(srv.port).signed(SHARES)[:2] == (404, NOT_FOUND[1])
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_http_rate_limit_applies_whether_federation_is_on_or_off(served):
    from herdr_engineering.control_plane.api import _RateLimiter
    fx = served
    for on in (True, False):
        fx.section["enabled"] = on
        fx.srv._limiter = _RateLimiter(max_requests=2)
        codes = [fx.http.signed(SHARES)[0] for _ in range(4)]
        assert codes[2:] == [429, 429], (on, codes)
        assert codes[:2] == ([200, 200] if on else [404, 404])


def test_standalone_collab_without_federation_has_no_new_behaviour(w):
    s = w.s
    assert s.federation_source is None and s.fed_policy() == fed.OFF
    s.maintain()
    assert [e for e in s._log if e["event_type"].startswith("federation_")] == []
    assert s._recount()["fedshare"] == 0 and s._recount()["fedpeer"] == 0
