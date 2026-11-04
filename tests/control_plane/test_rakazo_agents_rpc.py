"""agents/* bridge procedures: read-only, viewer-role, answered from the repo's in-memory projections."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane.repo import ControlPlaneRepo

BridgeError = rb.BridgeError


def _iso(delta_days=0):
    return (datetime.now(timezone.utc) - timedelta(days=delta_days)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def repo():
    return ControlPlaneRepo()


@pytest.fixture
def bridge(repo):
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": [
        {"email": "a@example.com", "password_scrypt": rb.hash_password("pw pw pw pw pw", n=2 ** 14), "role": "operator"}]})
    b = rb.RakazoBridge(cfg, assistant=None, version="x")
    b.agents.repo = repo
    return b


def call(bridge, proc, **inp):
    return bridge.agents.procs()[f"agents/{proc}"](None, inp)


def session(repo, sid, status="working", **kw):
    repo.upsert_session(sid, status=status, **kw)
    return sid


def feed(repo, sid, *events):
    return repo.inspector.ingest(sid, {"events": list(events)})


def test_list_sorts_and_hides_older_by_default(bridge, repo):
    session(repo, "s-work", "working")
    session(repo, "s-block", "blocked")
    session(repo, "s-closed", "closed")
    out = call(bridge, "list")
    assert [a["id"] for a in out["agents"]] == ["s-block", "s-work"]
    assert out["hiddenCount"] == 1


def test_list_show_older_reveals_hidden_and_reports_hidden_count(bridge, repo):
    session(repo, "s-work", "working")
    session(repo, "s-closed", "closed")
    out = call(bridge, "list", showOlder=True)
    assert [a["id"] for a in out["agents"]] == ["s-work", "s-closed"]
    assert out["hiddenCount"] == 0


def test_list_summary_order_and_has_conversation(bridge, repo):
    session(repo, "s-own", summary="did the thing")
    session(repo, "s-msg")
    session(repo, "s-none", "idle")
    feed(repo, "s-msg", {"kind": "message", "role": "assistant", "text": "from the log"})
    rows = {a["id"]: a for a in call(bridge, "list")["agents"]}
    assert rows["s-own"]["summary"] == "did the thing" and rows["s-own"]["has_conversation"] is False
    assert rows["s-msg"]["summary"] == "from the log" and rows["s-msg"]["has_conversation"] is True
    assert rows["s-none"]["summary"] == "Idle"


def test_get_unknown_agent_is_404(bridge):
    with pytest.raises(BridgeError) as e:
        call(bridge, "get", id="nope")
    assert (e.value.status, e.value.code) == (404, "NOT_FOUND")


def test_get_returns_row(bridge, repo):
    session(repo, "s1")
    feed(repo, "s1", {"kind": "message", "role": "user", "text": "hi"})
    out = call(bridge, "get", id="s1")
    assert out["agent"]["id"] == "s1" and out["hasConversation"] is True


def test_messages_pages_by_before_seq_and_caps_limit_at_200(bridge, repo):
    session(repo, "s1")
    evs = [{"kind": "message", "role": "assistant", "text": f"m{i}"} for i in range(250)]
    for i in range(0, 250, 50):
        feed(repo, "s1", *evs[i:i + 50])
    first = call(bridge, "messages", id="s1", limit=1000)
    assert len(first["events"]) == 200 and first["hasMore"] is True and first["newestSeq"] == 249
    assert first["events"][0]["seq"] == 249
    rest = call(bridge, "messages", id="s1", beforeSeq=first["events"][-1]["seq"], limit=200)
    assert [e["seq"] for e in rest["events"]][0] == 49 and len(rest["events"]) == 50 and rest["hasMore"] is False
    assert len(call(bridge, "messages", id="s1")["events"]) == 50
    with pytest.raises(BridgeError) as e:
        call(bridge, "messages", id="nope")
    assert e.value.status == 404


def test_event_returns_full_text_beyond_2000_chars(bridge, repo):
    session(repo, "s1")
    feed(repo, "s1", {"kind": "message", "role": "assistant", "text": "y" * 5000})
    listed = call(bridge, "messages", id="s1")["events"][0]
    assert len(listed["data"]["text"]) == 2000
    full = call(bridge, "event", id="s1", seq=listed["seq"])
    assert len(full["data"]["text"]) == 5000
    with pytest.raises(BridgeError) as e:
        call(bridge, "event", id="s1", seq=99)
    assert e.value.status == 404


def test_summary_never_contains_withheld_text(bridge, repo):
    session(repo, "s1")
    feed(repo, "s1", {"kind": "message", "role": "assistant", "text": "public"},
         {"kind": "message", "role": "assistant", "text": "TOP-SECRET", "sensitive": True})
    out = call(bridge, "list")
    assert out["agents"][0]["summary"] == "public"
    assert "TOP-SECRET" not in json.dumps(out) and "TOP-SECRET" not in json.dumps(call(bridge, "get", id="s1"))
    assert "TOP-SECRET" not in json.dumps(call(bridge, "messages", id="s1"))


def test_procedures_503_without_repo(bridge):
    bridge.agents.repo = None
    for proc, inp in (("list", {}), ("get", {"id": "x"}), ("messages", {"id": "x"}), ("event", {"id": "x", "seq": 0})):
        with pytest.raises(BridgeError) as e:
            call(bridge, proc, **inp)
        assert (e.value.status, e.value.code, e.value.message) == (503, "UNAVAILABLE", "The Agents view is not ready.")


def test_requires_owner_token(bridge):
    for proc in ("list", "get", "messages", "event"):
        res = bridge.handle("POST", f"/rpc/agents/{proc}", {"origin": "rakazo://"}, b'{"json": {}}', "127.0.0.1")
        assert res.status == 401


def test_list_returns_the_change_cursor_and_messages_returns_rev(bridge, repo):
    session(repo, "s1")
    feed(repo, "s1", {"kind": "message", "role": "assistant", "text": "hi"})
    out = call(bridge, "list")
    assert isinstance(out["cursor"], int) and out["cursor"] > 0
    assert call(bridge, "list")["cursor"] == out["cursor"]
    assert call(bridge, "messages", id="s1")["rev"] == repo.inspector.query("s1")["rev"]


def test_secret_values_never_leave_through_list_get_or_messages(bridge, repo):
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    session(repo, "s-sum", summary=f"deployed with {secret}")
    session(repo, "s-msg")
    feed(repo, "s-msg", {"kind": "message", "role": "assistant", "text": f"token is {secret}"})
    blob = json.dumps([call(bridge, "list"), call(bridge, "get", id="s-sum"), call(bridge, "get", id="s-msg"),
                       call(bridge, "messages", id="s-msg")])
    assert secret not in blob


def test_list_takes_the_inspector_lock_a_constant_number_of_times(bridge, repo):
    for i in range(60):
        session(repo, f"s{i}")
        feed(repo, f"s{i}", {"kind": "message", "role": "assistant", "text": "x"})

    class Counting:
        def __init__(self, inner):
            self.inner, self.n = inner, 0

        def __enter__(self):
            self.n += 1
            return self.inner.__enter__()

        def __exit__(self, *a):
            return self.inner.__exit__(*a)

    counting = Counting(repo.inspector._lock)
    repo.inspector._lock = counting
    call(bridge, "list")
    assert counting.n <= 5


def test_live_shaped_record_renders_name_machine_and_status(bridge, repo):
    session(repo, "s-live", "active", host_id="bender", name="Fix the build",
            summary="looking at CI", agent_role="engineer")
    row = call(bridge, "list")["agents"][0]
    assert (row["id"], row["name"], row["machine"], row["status"]) == (
        "s-live", "Fix the build", "bender", "idle")
    assert row["summary"] == "looking at CI"
