# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""T10 review hardening: in-flight revocation, shared-pool quotas, invisible text, attr names, restart
survival of the lineage counters, and the client address on enrollment records. Credentials are built at
run time; nothing here is a secret."""
from __future__ import annotations

import threading

import pytest

from herdr_engineering.control_plane import collab_external as ext
from herdr_engineering.control_plane.collab import (
    CollabError,
    CollabService,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import _digest
from tests.control_plane.test_collab_external import emit, enroll, fab, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc, user  # noqa: F401


def own(pr, n=0):
    return {"node_type": "agent", "node_id": f"agent:{pr.id}" + (f".{n}" if n else "")}


def status_and_body(w, method, parts, body, client=None):
    return w.s.dispatch(method, parts, {}, body, "", None, client=client)


# ------------------------------------------------------------------------------ 1. in-flight revoke
def test_an_event_in_flight_when_the_credential_is_revoked_does_not_land(fab):
    w = fab
    pr, out, _ = enroll(w, actions=["read", "post"], lineage=True)
    gate, entered = threading.Event(), threading.Event()
    real = w.s.lineage_sink

    def blocked(env, principal, valid=None):
        entered.set()
        assert gate.wait(5)
        return real(env, principal, valid)
    w.s.lineage_sink = blocked
    res = []

    def go():
        try:
            res.append(emit(w, pr, "node.upsert", own(pr), 1))
        except CollabError as exc:
            res.append(exc.status)
    t = threading.Thread(target=go)
    t.start()
    assert entered.wait(5)
    w.s.revoke_external(w.alice, w.room, pr.id)
    gate.set()
    t.join(5)
    assert res == [401]                                         # refused exactly like the next request
    assert f"agent:{pr.id}" not in w.repo.graph._nodes          # nothing reached the graph
    rows = w.s.audit(w.alice, w.s._rooms[w.room]["project_id"], 0, 500)["events"]
    assert any(r["action"] == "external.lineage" and r["reason"] == "revoked_in_flight" for r in rows)
    assert w.s._usage[("agent", pr.id)] == 0                    # and it used no budget


def test_ingest_evaluates_the_validity_callback_before_admission_and_stores_nothing():
    ing = IngestService(ControlPlaneRepo(), key_scopes={"p": {"rooms": frozenset({"r"}), "missions": frozenset()}})
    env = {"schema_version": 2, "event_id": "ev1", "source": "ext-x", "source_sequence": 1,
           "entity_type": "lineage", "entity_id": "agent:a", "event_type": "node.upsert",
           "payload": {"node_type": "agent", "node_id": "agent:a"}, "visibility_scope": "room", "room_id": "r"}
    with pytest.raises(PermissionError):
        ing.ingest(env, {"kind": "external", "id": "p", "authenticated_by": "collab"}, valid=lambda: False)
    assert "agent:a" not in ing.repo.graph._nodes
    assert ing.ingest(env, {"kind": "external", "id": "p", "authenticated_by": "collab"}, valid=lambda: True)


# ------------------------------------------------------------------------------ 2. shared-pool quotas
def test_one_sponsor_cannot_exhaust_the_pool_by_enrolling_many_agents(fab, monkeypatch):
    w = fab
    monkeypatch.setattr(ext, "LINEAGE_MAX_PER_SPONSOR", 6)
    agents = [enroll(w, label=f"a{i}", lineage=True)[0] for i in range(3)]
    done = 0
    for pr in agents:
        for j in range(1, 5):
            try:
                emit(w, pr, "node.upsert", {"node_type": "attempt", "node_id": f"attempt:{pr.id}.{j}"}, j)
                done += 1
            except CollabError as exc:
                assert exc.status == 429 and exc.code == "external_lineage_quota:sponsor"
                break
    assert done == 6                                            # the sponsor's whole allowance, not 3 x 4
    status, body = status_and_body(w, "POST", ["agents", "external", "lineage"], {})  # unauthenticated
    assert status == 401


def test_the_typed_refusal_reaches_the_client_and_a_replay_costs_nothing(fab, monkeypatch):
    w = fab
    monkeypatch.setattr(ext, "LINEAGE_MAX_PER_ROOM", 2)
    pr, out, _ = enroll(w, lineage=True)
    emit(w, pr, "node.upsert", own(pr), 1)
    for _ in range(5):
        assert emit(w, pr, "node.upsert", own(pr), 1)["duplicate"] is True      # the same event again
    emit(w, pr, "node.upsert", own(pr, 2), 2)
    with pytest.raises(CollabError) as e:
        emit(w, pr, "node.upsert", own(pr, 3), 3)
    assert (e.value.status, e.value.code) == (429, "external_lineage_quota:room")
    auth = f"Agent {out['credential']}"
    status, body = w.s.dispatch("POST", ["agents", "external", "lineage"], {}, {
        "event_id": "e9", "source_sequence": 9, "event_type": "node.upsert", "payload": own(pr, 9)}, auth, out["device_id"])
    assert status == 429 and body["code"] == "external_lineage_quota:room"


def test_the_room_allowance_is_per_room_and_the_sponsor_allowance_spans_rooms(fab, monkeypatch):
    w = fab
    monkeypatch.setattr(ext, "LINEAGE_MAX_PER_ROOM", 2)
    monkeypatch.setattr(ext, "LINEAGE_MAX_PER_SPONSOR", 3)
    a1, _, _ = enroll(w, label="a1", lineage=True)
    a2, _, _ = enroll(w, label="a2", lineage=True)                 # same room, same sponsor
    b1, _, _ = enroll(w, label="b1", room=w.room2, lineage=True)   # another room, same sponsor
    emit(w, a1, "node.upsert", own(a1), 1)
    emit(w, a2, "node.upsert", own(a2), 1)
    with pytest.raises(CollabError) as e:                          # the room is full for everybody in it
        emit(w, a1, "node.upsert", own(a1, 2), 2)
    assert e.value.code == "external_lineage_quota:room"
    emit(w, b1, "node.upsert", own(b1), 1)                         # the other room still has room
    with pytest.raises(CollabError) as e:                          # but the sponsor has used all 3
        emit(w, b1, "node.upsert", own(b1, 2), 2)
    assert e.value.code == "external_lineage_quota:sponsor"


# ------------------------------------------------------------------------------ 3. invisible text
HIDDEN = {"tag": "\U000E0041", "cgj": "͏", "vs16": "️", "vs-supplement": "\U000E0100", "musical": "\U0001D173",
          "hangul filler": "ㅤ", "object replacement": "￼", "zwj": "‍", "zwnj": "‌",
          "bidi isolate": "⁦", "rlo": "‮", "interlinear": "￹", "private use": "",
          "unassigned": "\U000E0FFF", "braille blank": "⠀", "soft hyphen": "­"}


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_clean_text_removes_invisible_characters(name):
    assert ext.clean_text("a" + HIDDEN[name] + "b", 50) == "ab"
    assert ext.clean_text("a" + HIDDEN[name] + "b", 50, multiline=True) == "ab"


def test_clean_text_keeps_visible_text_and_chat_line_breaks():
    assert ext.clean_text("Dana's Claude é中\U0001F600", 50) == "Dana's Claude é中\U0001F600"
    assert ext.clean_text("a\n\n\n\nb  c\r\nd", 50) == "a b c d"
    assert ext.clean_text("a\n\n\n\nb  c\r\nd", 50, multiline=True) == "a\n\nb c\nd"


def test_an_external_senders_chat_text_is_cleaned_but_a_persons_is_not_rewritten(w):
    pr, _, _ = enroll(w, actions=["read", "post"])
    from tests.control_plane.test_collab_external import post
    ev = post(pr, w, "hi\U000E0041‮ there\n\nsecond line‍", key="c1")["event"]
    assert ev["text"] == "hi there\n\nsecond line"
    assert code(w.s.post_message, pr, w.room, {"text": "x" * 9000, "idempotency_key": "c2"}) == 400
    assert code(w.s.post_message, pr, w.room, {"text": "‍‌", "idempotency_key": "c3"}) == 400
    person = w.s.post_message(w.alice, w.room, {"text": "a‍b", "idempotency_key": "p1"})[1]["event"]
    assert person["text"] == "a‍b"                          # only untrusted external text is rewritten


def test_a_lineage_label_loses_invisible_characters(fab):
    w = fab
    pr, _, _ = enroll(w, lineage=True)
    emit(w, pr, "node.upsert", {**own(pr), "label": "hi\U000E0041͏‍ there"}, 1)
    from tests.control_plane.test_collab_external import read_node
    view = read_node(w, w.alice, f"agent:{pr.id}")["node"]
    assert view["label"] == "hi there"


# ------------------------------------------------------------------------------ 4. attribute names
@pytest.mark.parametrize("key", ["k" * 65, "a‮b", "a\nb", "a\U000E0041", "<img src=x>", "a b", "", "naïve", "a/b"])
def test_lineage_attr_names_outside_the_id_alphabet_are_refused(fab, key):
    w = fab
    pr, _, _ = enroll(w, lineage=True)
    assert code(emit, w, pr, "node.upsert", {**own(pr), "attrs": {key: 1}}, 1) == 400
    assert f"agent:{pr.id}" not in w.repo.graph._nodes


def test_ordinary_attr_names_are_accepted(fab):
    w = fab
    pr, _, _ = enroll(w, lineage=True)
    emit(w, pr, "node.upsert", {**own(pr), "attrs": {"model": "x", "tokens.used": 3, "run-id": "r_1", "k" * 64: 1}}, 1)


# ------------------------------------------------------------------------------ 5. restart survival
def restarted(w):
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), sink=None, now=w.s.clock)
    fresh.hydrate(w.s._log)
    fresh.lineage_sink, fresh.graph_source = w.s.lineage_sink, w.s.graph_source
    return fresh


def test_lineage_counters_survive_a_restart_and_never_come_back_lower(fab, monkeypatch):
    w = fab
    monkeypatch.setattr(ext, "LINEAGE_MAX_EVENTS", 30)
    monkeypatch.setattr(ext, "LINEAGE_MAX_PER_SPONSOR", 40)
    pr, out, _ = enroll(w, lineage=True)
    for n in range(1, 31):
        emit(w, pr, "node.upsert", own(pr, n), n)
    assert code(emit, w, pr, "node.upsert", own(pr, 99), 99) == 429
    fresh = restarted(w)
    assert fresh._usage[("agent", pr.id)] >= 30 and fresh._usage[("sponsor", "alice")] >= 30
    pr2 = fresh.authenticate(f"Agent {out['credential']}", out["device_id"])
    with pytest.raises(CollabError) as e:                      # the exhausted credential stays exhausted
        fresh.external_lineage(pr2, {"event_id": "z1", "source_sequence": 1, "event_type": "node.upsert",
                                     "payload": own(pr2, 98)})
    assert e.value.status == 429
    # the sponsor's allowance also survived (30 used of 40), so a second agent gets only what is left
    other, out2, _ = enroll(w, label="other", lineage=True)
    fresh2 = restarted(w)
    pr3 = fresh2.authenticate(f"Agent {out2['credential']}", out2["device_id"])
    wrote, refused = 0, False
    for n in range(1, 20):
        try:
            fresh2.external_lineage(pr3, {"event_id": f"y{n}", "source_sequence": n, "event_type": "node.upsert",
                                          "payload": own(pr3, n)})
            wrote += 1
        except CollabError as exc:
            assert exc.code == "external_lineage_quota:sponsor"
            refused = True
            break
    # counters are rounded UP to the reserve step on restart (never down), so this may be 0 here
    assert refused and wrote <= 10


def test_counters_are_written_ahead_so_the_log_does_not_grow_per_event(fab):
    w = fab
    pr, _, _ = enroll(w, lineage=True)
    before = len(w.s._log)
    for n in range(1, 11):
        emit(w, pr, "node.upsert", own(pr, n), n)
    rows = [e for e in w.s._log[before:] if e["event_type"] == "external_usage_reserved"]
    assert 3 <= len(rows) <= 4                                  # one per counter, not one per event
    assert all(ev["payload"]["reserved"] >= 10 for ev in rows) and "room_id" not in rows[0]["payload"]
    # these rows are not chat and not audit rows: a room feed and an owner's audit view never show them
    assert all(e["event_type"] == "message" for e in w.s.read_events(w.alice, w.room, 0, 500)["events"]
               if e["sequence"] > before)
    assert all(r["action"] != "external_usage_reserved"
               for r in w.s.audit(w.alice, w.s._rooms[w.room]["project_id"], 0, 500)["events"])


# ------------------------------------------------------------------------------ 6. client address
def test_failed_redemptions_are_counted_per_client_without_growing_the_log(w):
    before = len(w.s._log)
    for _ in range(5):
        assert status_and_body(w, "POST", ["agents", "join"], {"token": "hcj_" + "a" * 32}, client="203.0.113.7")[0] == 401
    status_and_body(w, "POST", ["agents", "join"], {"token": "nope"}, client="2001:db8::1")
    status_and_body(w, "POST", ["agents", "join"], {"token": "hcj_" + "b" * 32}, client="<script>")
    status_and_body(w, "POST", ["agents", "join"], {"token": "hcj_" + "b" * 32})
    assert w.s.redeem_failures["203.0.113.7"] == 5 and w.s.redeem_failures["2001:db8::1"] == 1
    assert w.s.redeem_failures["unknown"] == 2 and "<script>" not in w.s.redeem_failures
    assert len(w.s._log) == before                              # aggregate only: no row per attempt
    for i in range(300):
        status_and_body(w, "POST", ["agents", "join"], {"token": "hcj_" + "c" * 32}, client=f"10.0.{i // 250}.{i % 250}")
    assert len(w.s.redeem_failures) <= 257 and w.s.redeem_failures["other"] > 0


def test_a_successful_enrollment_records_the_client_address_in_its_audit_row(w):
    inv = w.s.create_external_invite(w.alice, w.room, {"label": "x"})
    status, out = status_and_body(w, "POST", ["agents", "join"], {"token": inv["code"]}, client="203.0.113.9")
    assert status == 201
    rows = w.s.audit(w.alice, w.s._rooms[w.room]["project_id"], 0, 500)["events"]
    enrol = [r for r in rows if r["action"] == "external.enroll"]
    assert enrol and enrol[0]["client_ip"] == "203.0.113.9"
    assert out["credential"] not in str(rows)


# ------------------------------------------------------------------------------ probes kept
def test_forty_threads_redeeming_one_invite_make_exactly_one_agent(w):
    inv = w.s.create_external_invite(w.alice, w.room, {"label": "x"})
    res = []

    def go():
        try:
            res.append(w.s.redeem_join_token({"token": inv["code"]})["agent_id"])
        except CollabError as exc:
            res.append(exc.status)
    threads = [threading.Thread(target=go) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([r for r in res if isinstance(r, str)]) == 1
    assert {r for r in res if not isinstance(r, str)} == {401}


def test_rotation_never_extends_the_hard_expiry(fab):
    w = fab
    pr, out, _ = enroll(w, credential_ttl_seconds=300)
    w.s.clock.t += 290
    rot = w.s.rotate_credential(pr)
    assert rot["expires_at"] == out["expires_at"]
    w.s.clock.t += 20
    assert code(w.s.authenticate, f"Agent {rot['credential']}", out["device_id"]) == 401
