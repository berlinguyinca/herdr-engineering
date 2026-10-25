# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Room commands (Task C6): `/handoff`, `/decide`, `/summarize`, `/issue`, `/assign` are interpreted by the
server only when `/` starts the message; `/issue` and `/assign` write to GitHub only through a gated writer
(YAML `github.write.enabled`, a separate token reference, an explicit confirm, approvers only). No test here
ever reaches the GitHub API: every writer talks to a fake opener."""
from __future__ import annotations

import io
import json
import logging
import os
import threading

import pytest

from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.collab_commands import normalize_people, parse_command
from herdr_engineering.errors import InvalidError
from herdr_engineering.github import GitHubWriteError, GitHubWriter
from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401
from tests.test_collab_refs import gh, monitor_for  # noqa: F401

READ_TOKEN = "ghp_READ_TOKEN_0123456789abcdefghij"
WRITE_TOKEN = "ghp_WRITE_SENTINEL_0123456789abcdefgh"


def link(s, uid, email):
    s._commit("identity_linked", {"actor_user_id": uid, "actor_agent_id": None, "user_id": uid, "email": email})


class FakeResponse(io.BytesIO):
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    """Stands in for urllib's opener: records each request, answers like GitHub's REST API. Never networks."""

    def __init__(self, gate: threading.Event | None = None, fail: Exception | None = None):
        self.requests, self.gate, self.fail, self.number = [], gate, fail, 100

    def open(self, request, timeout=None):
        self.requests.append({"url": request.full_url, "method": request.get_method(),
                              "headers": dict(request.header_items()), "body": json.loads(request.data or b"{}")})
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        if request.full_url.endswith("/issues"):
            self.number += 1
            o, r = request.full_url.split("/repos/")[1].split("/")[:2]
            # GitHub never echoes a token; the writer still scrubs it, so the fake does echo it
            body = {"number": self.number, "html_url": f"https://github.com/{o}/{r}/issues/{self.number}",
                    "note": WRITE_TOKEN}
        else:
            body = {"assignees": [{"login": a} for a in json.loads(request.data)["assignees"]]}
        return FakeResponse(json.dumps(body).encode())


def token_file(tmp_path, value=WRITE_TOKEN, mode=0o600):
    path = tmp_path / "github-write-token"
    path.write_text(value + "\n")
    os.chmod(path, mode)
    return str(path)


def writer(tmp_path, *, enabled=True, opener=None, orgs=("acme", "other"), value=WRITE_TOKEN, ref=None):
    settings = {"enabled": enabled, "token_ref": ref if ref is not None else token_file(tmp_path, value)}
    return GitHubWriter(settings=lambda: settings, organizations=lambda: list(orgs),
                        read_token=lambda: READ_TOKEN, client=opener or FakeOpener())


@pytest.fixture
def w(svc, gh):
    room = W(svc)
    svc.github = gh[1]
    for uid in ("alice", "bob", "carol", "dave", "mallory"):
        link(svc, uid, f"{uid}@example.org")
    room.alice.approver = True                     # approval mode: an approver (credential-derived in production)
    svc.people = normalize_people({"bob": {"github": "bob-gh"}, "alice": {"github": "alice-gh"}})
    return room


def agent(w, owner, harness="pi", actions=("read", "post", "execute")):
    pr = getattr(w, owner)
    info = w.s.connect_agent(pr, {"harness_type": harness})
    out = w.s.grant_agent(pr, w.room, {"agent_id": info["agent_id"], "actions": list(actions)})
    if out["status"] == "pending":
        w.s.decide_agent_request(w.alice, w.room, out["request_id"], {"decision": "approve"})
    return w.s.authenticate(f"Agent {info['credential']}", info["device_id"]), info["agent_id"]


_n = [0]


def post(w, who, text, **extra):
    _n[0] += 1
    return w.s.post_message(who, w.room, {"text": text, "idempotency_key": f"c{_n[0]}", **extra})


def say(w, who, text, **extra):
    status, out = post(w, who, text, **extra)
    assert status == 201
    return out["event"]


def refused(w, who, text, **extra):
    with pytest.raises(CollabError) as e:
        post(w, who, text, **extra)
    return e.value


# ---------------------------------------------------------------- the grammar

@pytest.mark.parametrize("text, name", [
    ("/handoff @a @b take it", "handoff"), ("/decide ship it", "decide"), ("/summarize", "summarize"),
    ("/issue acme/app", "issue"), ("/assign #app-3 @bob", "assign"), ("/decide\nmulti", "decide"),
])
def test_a_command_is_recognised_only_at_the_start(text, name):
    assert parse_command(text)[0] == name


@pytest.mark.parametrize("text", [
    "see /decide later", " /decide leading space", "/handoffx @a", "/usr/bin/env", "/Decide caps",
    "@alice.pi /skill:review", "@alice.pi /decide x", "`/decide`", "/", "//decide",
])
def test_anything_else_is_plain_text(text):
    assert parse_command(text) is None


def test_slash_not_at_the_start_posts_a_plain_message(w):
    ev = say(w, w.bob, "please /decide later")
    assert ev["event_type"] == "message" and ev["text"] == "please /decide later"
    ev = say(w, w.bob, " /decide leading space is plain text")
    assert ev["event_type"] == "message"


def test_an_agent_skill_is_passed_literally_never_as_a_room_command(w):
    pi, _ = agent(w, "alice")
    ev = say(w, w.bob, "@alice.pi /decide this for me")
    assert ev["event_type"] == "message" and ev["text"] == "@alice.pi /decide this for me"


# ---------------------------------------------------------------- /handoff

def test_handoff_yields_a_handoff_event_addressed_to_b(w):
    _, a = agent(w, "alice", "pi")
    b_pr, b = agent(w, "bob", "codex")
    ev = say(w, w.bob, "/handoff @alice.pi @bob.codex finish the parser")
    assert ev["event_type"] == "handoff"
    assert ev["handoff"]["from"] == {"kind": "agent", "id": a, "name": "alice.pi"}
    assert ev["handoff"]["to"] == {"kind": "agent", "id": b, "name": "bob.codex"}
    assert ev["handoff"]["note"] == "finish the parser" and ev["handoff"]["status"] == "pending"
    seen = [e for e in w.s.read_events(b_pr, w.room, 0, 50)["events"] if e["event_type"] == "handoff"]
    assert seen and seen[-1]["mentions_me"] is True                       # the recipient is notified
    assert [e for e in w.s.read_events(w.carol, w.room, 0, 50)["events"]
            if e["event_type"] == "handoff"][-1]["mentions_me"] is False


def test_a_one_target_handoff_comes_from_the_poster(w):
    ev = say(w, w.bob, "/handoff @alice over to you")
    assert ev["handoff"]["from"] == {"kind": "person", "id": "bob", "name": "bob"}
    assert ev["handoff"]["to"] == {"kind": "person", "id": "alice", "name": "alice"}


def test_unknown_ambiguous_or_group_targets_are_refused(w):
    assert "nobody" in refused(w, w.bob, "/handoff @ghost do it").message
    _, cx = agent(w, "bob", "codex")
    w.s.rename_agent(w.bob, cx, {"name": "carol"})            # an agent's name and a person's handle collide
    err = refused(w, w.bob, "/handoff @carol take it")
    assert err.status == 400 and "More than one agent or person is called @carol" in err.message
    assert "@bob.codex" in err.message
    assert refused(w, w.bob, "/handoff @all everyone").status == 400
    assert refused(w, w.bob, "/handoff").status == 400
    assert not [e for e in w.s._log if e["event_type"] == "handoff"]


def test_a_viewer_cannot_hand_off_and_a_contributor_can(w):
    assert code(post, w, w.carol, "/handoff @bob go") == 403
    assert say(w, w.bob, "/handoff @alice go")["event_type"] == "handoff"


def test_handoff_identity_comes_from_the_credential(w):
    err = refused(w, w.bob, "/handoff @alice go", sender_id="alice")
    assert err.status == 400
    ev = say(w, w.bob, "/handoff @alice go")
    assert ev["sender"]["id"] == "bob"


def test_agent_posted_handoff_chains_cannot_exceed_the_per_sender_rate_limit(w):
    w.s.message_limit = 5
    pi, _ = agent(w, "alice", "pi")
    agent(w, "bob", "codex")
    ok = 0
    for i in range(12):
        try:
            say(w, pi, f"/handoff @bob.codex step {i}")
            ok += 1
        except CollabError as e:
            assert e.status == 429
    assert ok == 5
    assert sum(1 for e in w.s._log if e["event_type"] == "handoff") == 5


def test_a_remote_agents_slash_text_is_never_a_command(w):
    from tests.control_plane.test_collab_external import (
        enroll,  # noqa: PLC0415  invited by the owner
    )
    ext, _, _ = enroll(w, actions=["read", "post"])
    status, out = w.s.post_message(ext, w.room, {"text": "/decide take over", "idempotency_key": "x1"})
    assert out["event"]["event_type"] == "message"


# ---------------------------------------------------------------- /decide and /summarize

def test_decide_is_stored_and_listed_later(w):
    say(w, w.bob, "/decide ship on Friday")
    say(w, w.bob, "chatter")
    say(w, w.alice, "/decide keep the old API")
    listed = w.s.list_decisions(w.carol, w.room)["decisions"]
    assert [d["text"] for d in listed] == ["ship on Friday", "keep the old API"]
    assert listed[0]["sender"]["id"] == "bob"
    state = w.s.room_state(w.bob, w.room, -1, 0, 0)
    assert [d["text"] for d in state["decisions"]] == ["ship on Friday", "keep the old API"]
    assert refused(w, w.bob, "/decide").status == 400
    assert code(w.s.list_decisions, w.mallory, w.room) == 404


def test_summarize_posts_a_request_for_an_online_agent(w):
    ev = say(w, w.bob, "/summarize")
    assert ev["event_type"] == "summary_request" and ev["sender"]["id"] == "bob"


def test_command_events_survive_a_restart(w, tmp_path):
    say(w, w.bob, "/decide ship")
    ev = say(w, w.bob, "/handoff @alice go")
    from herdr_engineering.control_plane.collab import (  # noqa: PLC0415
        CollabService,
        StaticTokenAuthenticator,
    )
    from tests.control_plane.test_collab import _digest  # noqa: PLC0415
    s2 = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=w.s._now)
    s2.hydrate(list(w.s._log))
    assert [d["text"] for d in s2.list_decisions(w.bob, w.room)["decisions"]] == ["ship"]
    again = s2.post_message(w.bob, w.room, {"text": "/handoff @alice go", "idempotency_key": f"c{_n[0]}"})
    assert again[0] == 200 and again[1]["event"]["handoff"]["handoff_id"] == ev["handoff"]["handoff_id"]


# ---------------------------------------------------------------- /issue

def test_issue_drafts_only_and_never_calls_github_until_confirmed(w, tmp_path):
    opener = FakeOpener()
    w.s.github_writer = writer(tmp_path, opener=opener)
    m1 = say(w, w.bob, "the chart breaks on empty data")
    m2 = say(w, w.alice, "and on NaN")
    ev = say(w, w.alice, "/issue acme/binview Chart breaks", selection=[m1["event_id"], m2["event_id"]])
    assert ev["event_type"] == "issue_draft" and opener.requests == []
    d = ev["draft"]
    assert (d["owner"], d["repo"], d["title"], d["status"]) == ("acme", "binview", "Chart breaks", "draft")
    assert "the chart breaks on empty data" in d["body"] and "and on NaN" in d["body"]
    assert "@bob" in d["body"] and "@alice" in d["body"]                     # attribution
    out = w.s.confirm_draft(w.alice, w.room, d["draft_id"])
    assert out["status"] == "created" and out["number"] == 101
    assert len(opener.requests) == 1 and opener.requests[0]["url"] == "https://api.github.com/repos/acme/binview/issues"
    assert opener.requests[0]["body"]["title"] == "Chart breaks"
    done = [e for e in w.s.read_events(w.bob, w.room, 0, 100)["events"] if e["event_type"] == "github_written"]
    assert done[-1]["github"] == {"draft_id": d["draft_id"], "kind": "issue", "owner": "acme", "repo": "binview",
                                  "number": 101, "url": "https://github.com/acme/binview/issues/101"}


def test_issue_from_the_replied_to_message(w, tmp_path):
    m = say(w, w.bob, "login fails for SSO users")
    ev = say(w, w.alice, "/issue acme/app", in_reply_to=m["event_id"])
    assert ev["draft"]["title"] == "login fails for SSO users" and ev["draft"]["sources"] == [m["event_id"]]


def test_issue_needs_sources_a_repo_and_an_approver(w):
    m = say(w, w.bob, "x")
    assert "reply to or select" in refused(w, w.alice, "/issue acme/app").message
    assert refused(w, w.alice, "/issue", selection=[m["event_id"]]).status == 400
    err = refused(w, w.bob, "/issue acme/app", selection=[m["event_id"]])
    assert err.status == 403 and "approver" in err.message
    pi, _ = agent(w, "alice")
    assert refused(w, pi, "/issue acme/app", selection=[m["event_id"]]).status == 403
    assert refused(w, w.alice, "/issue acme/app", selection=["event-nope"]).status == 400
    assert refused(w, w.alice, "hello", selection=[m["event_id"]]).status == 400   # selection is for /issue only


def test_issue_body_is_redacted_and_capped(w):
    long = "x" * 7000
    ids_ = [say(w, w.bob, long + str(i))["event_id"] for i in range(5)]
    ids_.append(say(w, w.bob, "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 here")["event_id"])
    ev = say(w, w.alice, "/issue acme/app " + "T" * 400, selection=ids_)
    assert len(ev["draft"]["title"]) <= 256 and len(ev["draft"]["body"]) <= 20000
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(w.s._log)


def test_with_writes_disabled_confirm_returns_a_clear_disabled_result(w, tmp_path):
    opener = FakeOpener()
    w.s.github_writer = writer(tmp_path, enabled=False, opener=opener)
    m = say(w, w.bob, "x")
    d = say(w, w.alice, "/issue acme/app", selection=[m["event_id"]])["draft"]
    with pytest.raises(CollabError) as e:
        w.s.confirm_draft(w.alice, w.room, d["draft_id"])
    assert e.value.status == 409 and e.value.code == "github_write_disabled"
    assert "disabled" in e.value.message and opener.requests == []
    w.s.github_writer = None                                    # no writer wired at all (collab-only container)
    assert code(w.s.confirm_draft, w.alice, w.room, d["draft_id"]) == 409


def test_confirm_is_for_approvers_and_room_members_only(w, tmp_path):
    w.s.github_writer = writer(tmp_path)
    m = say(w, w.bob, "x")
    d = say(w, w.alice, "/issue acme/app", selection=[m["event_id"]])["draft"]
    assert code(w.s.confirm_draft, w.bob, w.room, d["draft_id"]) == 403
    assert code(w.s.confirm_draft, w.mallory, w.room, d["draft_id"]) == 404
    w.mallory.approver = True
    assert code(w.s.confirm_draft, w.mallory, w.room, d["draft_id"]) == 404     # an approver outside the room
    assert code(w.s.confirm_draft, w.alice, w.room2, d["draft_id"]) == 404      # the draft lives in another room


def test_a_double_confirm_creates_one_issue(w, tmp_path):
    gate = threading.Event()
    opener = FakeOpener(gate=gate)
    w.s.github_writer = writer(tmp_path, opener=opener)
    m = say(w, w.bob, "x")
    d = say(w, w.alice, "/issue acme/app", selection=[m["event_id"]])["draft"]
    results = []

    def go():
        try:
            results.append(w.s.confirm_draft(w.alice, w.room, d["draft_id"]))
        except CollabError as e:
            results.append(e.status)
    t1 = threading.Thread(target=go)
    t1.start()
    for _ in range(200):
        if opener.requests:
            break
        threading.Event().wait(0.01)
    go()                                                    # the second click while the first is in flight
    gate.set()
    t1.join(5)
    assert len(opener.requests) == 1
    assert 409 in results and any(isinstance(r, dict) and r["number"] == 101 for r in results)
    again = w.s.confirm_draft(w.alice, w.room, d["draft_id"])                 # after: the same number, no call
    assert again["number"] == 101 and again["replayed"] is True and len(opener.requests) == 1


def test_a_confirmed_issue_is_remembered_across_a_restart(w, tmp_path):
    opener = FakeOpener()
    w.s.github_writer = writer(tmp_path, opener=opener)
    m = say(w, w.bob, "x")
    d = say(w, w.alice, "/issue acme/app", selection=[m["event_id"]])["draft"]
    w.s.confirm_draft(w.alice, w.room, d["draft_id"])
    from herdr_engineering.control_plane.collab import (  # noqa: PLC0415
        CollabService,
        StaticTokenAuthenticator,
    )
    from tests.control_plane.test_collab import _digest  # noqa: PLC0415
    s2 = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=w.s._now)
    s2.hydrate(list(w.s._log))
    s2.github_writer = w.s.github_writer
    assert s2.confirm_draft(w.alice, w.room, d["draft_id"])["number"] == 101 and len(opener.requests) == 1


def test_writes_are_limited_to_in_scope_owners(w, tmp_path):
    opener = FakeOpener()
    w.s.github_writer = writer(tmp_path, opener=opener)
    m = say(w, w.bob, "x")
    d = say(w, w.alice, "/issue excluded/tool", selection=[m["event_id"]])["draft"]
    with pytest.raises(CollabError) as e:
        w.s.confirm_draft(w.alice, w.room, d["draft_id"])
    assert e.value.status == 403 and "github.organizations" in e.value.message and opener.requests == []


def test_writes_are_rate_limited_per_principal(w, tmp_path):
    w.s.github_writer = writer(tmp_path)
    statuses = []
    for i in range(14):
        m = say(w, w.bob, f"x{i}")
        d = say(w, w.alice, "/issue acme/app", selection=[m["event_id"]])["draft"]
        try:
            w.s.confirm_draft(w.alice, w.room, d["draft_id"])
            statuses.append(201)
        except CollabError as e:
            statuses.append(e.status)
    assert statuses.count(201) == 10 and statuses[-1] == 429


def test_a_github_failure_is_reported_without_the_token_and_can_be_retried(w, tmp_path, caplog):
    import urllib.error  # noqa: PLC0415
    err = urllib.error.HTTPError("https://api.github.com/x", 401, f"bad {WRITE_TOKEN}", {}, io.BytesIO(WRITE_TOKEN.encode()))
    opener = FakeOpener(fail=err)
    w.s.github_writer = writer(tmp_path, opener=opener)
    m = say(w, w.bob, "x")
    d = say(w, w.alice, "/issue acme/app", selection=[m["event_id"]])["draft"]
    caplog.set_level(logging.DEBUG)
    # the operator listener's principal (an approver by construction); a bearer non-approver gets 403
    assert w.s.dispatch("POST", ["rooms", w.room, "drafts", d["draft_id"], "confirm"], {}, {},
                        f"Bearer {USERS['alice']}", None)[0] == 403
    status, body = w.s.dispatch("POST", ["rooms", w.room, "drafts", d["draft_id"], "confirm"], {}, {},
                                "", None, operator="alice")
    assert status == 502 and WRITE_TOKEN not in json.dumps(body) and "credential" in body["error"]
    opener.fail = None
    out = w.s.confirm_draft(w.alice, w.room, d["draft_id"])
    assert out["status"] == "created"
    blob = json.dumps(w.s._log) + caplog.text + json.dumps(w.s.audit(w.alice, w.s._rooms[w.room]["project_id"], 0, 500))
    assert WRITE_TOKEN not in blob and READ_TOKEN not in blob


# ---------------------------------------------------------------- /assign

def test_assign_maps_a_person_to_their_github_login_and_waits_for_confirm(w, tmp_path):
    opener = FakeOpener()
    w.s.github_writer = writer(tmp_path, opener=opener)
    ev = say(w, w.alice, "/assign #binview-44 @bob")
    assert ev["event_type"] == "assignment" and opener.requests == []
    d = ev["draft"]
    assert (d["owner"], d["repo"], d["number"], d["login"], d["person"]["name"]) == ("acme", "binview", 44, "bob-gh", "bob")
    out = w.s.confirm_draft(w.alice, w.room, d["draft_id"])
    assert out["status"] == "assigned"
    assert opener.requests[0]["url"] == "https://api.github.com/repos/acme/binview/issues/44/assignees"
    assert opener.requests[0]["body"] == {"assignees": ["bob-gh"]}


def test_assign_refuses_unmapped_people_unknown_issues_and_non_approvers(w, tmp_path):
    opener = FakeOpener()
    w.s.github_writer = writer(tmp_path, opener=opener)
    err = refused(w, w.alice, "/assign #binview-44 @carol")
    assert err.status == 400 and "collab.people.carol.github" in err.message
    assert refused(w, w.alice, "/assign #nothing-9 @bob").status == 400
    assert refused(w, w.alice, "/assign #binview-44 @alice.pi").status == 400
    assert refused(w, w.alice, "/assign #binview-44").status == 400
    assert refused(w, w.bob, "/assign #binview-44 @bob").status == 403
    assert opener.requests == [] and not [e for e in w.s._log if e["event_type"] == "assignment"]


def test_a_guest_never_sees_the_github_login(w):
    say(w, w.alice, "/assign #binview-44 @bob")
    seen = [e for e in w.s.read_events(w.dave, w.room, 0, 100)["events"] if e["event_type"] == "assignment"]
    assert seen and "login" not in seen[-1]["draft"]


# ---------------------------------------------------------------- the writer itself

def test_the_writer_refuses_unless_enabled(tmp_path):
    opener = FakeOpener()
    wr = writer(tmp_path, enabled=False, opener=opener)
    with pytest.raises(GitHubWriteError) as e:
        wr.create_issue("acme", "app", "t", "b")
    assert e.value.reason == "disabled"
    with pytest.raises(GitHubWriteError):
        wr.assign("acme", "app", 1, "bob")
    assert opener.requests == []
    assert GitHubWriter(settings=lambda: None, organizations=lambda: ["acme"], read_token=lambda: READ_TOKEN,
                        client=opener).status()["enabled"] is False


def test_the_write_token_is_never_the_read_token(tmp_path):
    opener = FakeOpener()
    wr = writer(tmp_path, opener=opener, value=READ_TOKEN)
    with pytest.raises(GitHubWriteError) as e:
        wr.create_issue("acme", "app", "t", "b")
    assert e.value.reason == "same_token" and opener.requests == []
    assert READ_TOKEN not in str(e.value)


def test_the_token_reference_must_be_a_private_file(tmp_path):
    opener = FakeOpener()
    for ref in ("relative/path", str(tmp_path / "missing")):
        with pytest.raises(GitHubWriteError) as e:
            writer(tmp_path, opener=opener, ref=ref).create_issue("acme", "app", "t", "b")
        assert e.value.reason == "not_configured"
    loose = token_file(tmp_path, mode=0o644)
    with pytest.raises(GitHubWriteError):
        writer(tmp_path, opener=opener, ref=loose).create_issue("acme", "app", "t", "b")
    assert opener.requests == []


def test_the_writer_sends_the_write_token_and_scrubs_it_from_replies(tmp_path):
    opener = FakeOpener()
    out = writer(tmp_path, opener=opener).create_issue("acme", "app", "t", "b")
    assert opener.requests[0]["headers"]["Authorization"] == f"Bearer {WRITE_TOKEN}"
    assert out == {"number": 101, "url": "https://github.com/acme/app/issues/101"}
    assert WRITE_TOKEN not in json.dumps(out)


def test_the_writer_validates_names_and_scope(tmp_path):
    opener = FakeOpener()
    wr = writer(tmp_path, opener=opener)
    for args in (("acme/../x", "app"), ("acme", "a b"), ("excluded", "app")):
        with pytest.raises(GitHubWriteError):
            wr.create_issue(*args, "t", "b")
    with pytest.raises(GitHubWriteError):
        wr.assign("acme", "app", 1, "bad login!")
    with pytest.raises(GitHubWriteError) as e:
        writer(tmp_path, opener=opener, orgs=()).create_issue("acme", "app", "t", "b")
    assert e.value.reason == "out_of_scope"                 # an empty allowlist permits no writes
    assert opener.requests == []


# ---------------------------------------------------------------- configuration

def test_github_write_config_is_validated():
    from herdr_engineering.config import validate_github  # noqa: PLC0415
    validate_github({"write": {"enabled": False}})
    validate_github({"write": {"enabled": True, "token_ref": "/etc/herdr/github-write-token"}})
    for bad in ({"enabled": "yes"}, {"enabled": True}, {"enabled": True, "token_ref": "relative"},
                {"enabled": True, "token_ref": "/x", "token": "ghp_secret"}, "on"):
        with pytest.raises(InvalidError):
            validate_github({"write": bad})


def test_people_mapping_is_validated():
    assert normalize_people({"Gert": {"github": "Gert-W"}}) == {"gert": {"github": "Gert-W"}}
    assert normalize_people(None) == {}
    for bad in ({"gert": {"github": "bad login"}}, {"gert": "gert"}, {"gert": {"github": "x", "token": "y"}},
                {"a" * 40: {"github": "x"}}, ["gert"]):
        with pytest.raises(ValueError):
            normalize_people(bad)


def test_people_is_an_allowed_collab_key():
    from herdr_engineering.cli import COLLAB_CONFIG_KEYS  # noqa: PLC0415
    from herdr_engineering.fleet_config_cli import COLLAB_KEYS  # noqa: PLC0415
    assert "people" in COLLAB_KEYS and "people" in COLLAB_CONFIG_KEYS
