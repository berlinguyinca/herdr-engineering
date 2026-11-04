# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Slice 5 of docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md: approval-gated external actions for a team.

Real ``CollabService`` with its file event log, a real ``AssistantService`` on the real ``pa_store_snapshot`` table of a private
Postgres, real HTTP through ``ControlPlaneServer`` (the operator's API key on the assistant door, collaboration bearer and agent
credentials on the collab door), recorded Gmail dicts through the real ``intel_gmail`` normaliser, and the product's own GitHub client
and Gmail draft writer over a real socket to the CONTRACT-LEVEL fake servers in ``external_fakes.py`` (not GitHub, not Gmail).
Nothing else is mocked; a test moves only the collab service's clock."""
from __future__ import annotations

import json
import os
import threading

import psycopg2
import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.app_credentials import CredentialStore, SecretBox
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.external_actions import DRAFT, ISSUE, GitHubIssues, GmailDrafts
from herdr_engineering.control_plane.google_oauth import SCOPES
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane import brief_fixtures as fx
from tests.control_plane.external_fakes import FakeGitHubServer, FakeGmailServer
from tests.control_plane.test_assistant_team_briefs import BRIEFS, KEY, OTHER_KEY, URL, _collab, principal_id
from tests.control_plane.test_collab import BASE, Clock
from tests.control_plane.test_team_coordinators import TEAM, TEAM2, CPlane, _http, ext_team
from tests.control_plane.test_team_rooms import USERS
from tests.control_plane.wg_pg import wg_pg  # noqa: F401

REPO = "example-org/team-repo"
EXT = {"enabled": True, "github": {"labels": ["from-email"]}}
AX = "/api/v1/assistant-external-actions"
MAIL2 = "18c0ffee5ec0de99"


def _assistant(dsn, mails, ext):
    section = {"enabled": True, "actions": {"collab_users": {principal_id(KEY): "alice"}}, "persistence": {"dsn": dsn},
               "team_briefs": BRIEFS}
    if ext is not None:
        section["external_actions"] = ext
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    svc.mail_source = lambda tenant, user, message_id: mails[message_id]
    return svc


def _serve(svc, asvc):
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc, assistant_service=asvc,
                             api_keys={KEY: "operator", OTHER_KEY: "operator"})
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    svc.work_graph_handle = None
    return srv


class XWorld(CPlane):
    """alice (the operator, API key KEY) owns room r; dave and erin are guests, bob a contributor, carol a viewer."""

    def __init__(self, tmp_path, clock, dsn, ext):
        self.tmp, self.dsn, self.ext_cfg, self.clock = tmp_path, dsn, ext, clock
        self.mails = {fx.MESSAGE_ID: fx.gmail(), MAIL2: fx.gmail(MAIL2, subject="Invoice 8800 payment due", attachments=())}
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM pa_store_snapshot")
        self.gh, self.gm = FakeGitHubServer(), FakeGmailServer()
        self.gh.add_repo(REPO)
        svc = _collab(tmp_path, clock)
        asvc = _assistant(dsn, self.mails, ext)
        super().__init__(svc, _serve(svc, asvc), clock)
        self.asvc = asvc
        self.providers()
        s, out = self.op("POST", "/api/v1/assistants", {"name": "Mail"})
        assert s in (200, 201), out
        self.team = None

    def providers(self):
        """Point the product's own provider clients at the fake servers (the wiring itself is covered in the config test)."""
        d = self.tmp / "gkey"
        d.mkdir(exist_ok=True)
        (d / "key").write_bytes(b"k" * 32)
        store = CredentialStore(SecretBox(str(d / "key")))
        store.put_client("google", "cid.apps.googleusercontent.com", "client-secret")
        store.put_connection("default", principal_id(KEY), "google", account="op@x.org", scopes=list(SCOPES), refresh_token="refresh-u1")
        self.workspace = gw.GoogleWorkspace(store, blocked_fn=lambda t, u: [], opener=self.gm.opener())
        ea = self.asvc.external_actions
        token = self.gh.token
        ea.github = GitHubIssues(self.gh.base, lambda repo: token, timeout=3)
        ea.gmail = GmailDrafts(lambda: self.workspace)

    def restart(self):
        self.srv.shutdown()
        self.svc._sink.close()
        self.svc = _collab(self.tmp, self.clock)
        self.asvc = _assistant(self.dsn, self.mails, self.ext_cfg)
        self.srv = _serve(self.svc, self.asvc)
        from tests.control_plane.test_collab import Api
        self.api = Api(self.srv.port)
        self.providers()

    def close(self):
        self.srv.shutdown()
        self.gh.close()
        self.gm.close()

    # -- the operator's door (API key)
    def op(self, method, path, body=None, key=KEY):
        return _http(self.srv.port, method, path, body, {"X-API-Key": key})

    # -- the team
    def team_up(self):
        iw, impl, rev, coord = (self.agent(name=n) for n in ("writer", "impl", "rev", "coordy"))
        s, out = self.attach({"issue_writer": [iw["agent_id"]], "implementer": [impl["agent_id"]], "reviewer": [rev["agent_id"]]})
        assert s == 200, out
        s, out = self.set_coord("agent:" + coord["agent_id"])
        assert s == 200, out
        self.team = {"iw": iw, "impl": impl, "rev": rev, "coord": coord}
        return self.team

    def target(self, repo=REPO, team=TEAM, user="alice", room=None):
        return self.c(user, "POST", f"{BASE}/rooms/{room or self.room}/teams/{team}/issue-target", {"repo": repo})

    def approved_brief(self, message=fx.MESSAGE_ID, team=TEAM):
        """An email brief offered by the operator's assistant and approved by the operator: the only thing an issue can be built from."""
        s, out = self.op("POST", URL, {"room_id": self.room, "team_id": team, "message_ref": message})
        assert s in (200, 201), out
        b = out["brief"]
        [row] = [r for r in self.c("alice", "GET", f"{BASE}/rooms/{self.room}/suggestions")[1]["suggestions"]
                 if r["suggestion_id"] == b["suggestion_id"]]
        if row["state"] == "pending":
            s, ok = self.c("alice", "POST", f"{BASE}/rooms/{self.room}/suggestions/{b['suggestion_id']}/decide",
                           {"decision": "approve", "expected_digest": row["digest"],
                            "target_agent_id": self.team["impl"]["agent_id"]})
            assert s == 200, ok
        return b

    def ready(self, repo=REPO):
        self.team_up()
        assert self.target(repo)[0] == 200
        return self.approved_brief()

    # -- the new collab routes
    def file(self, who, kind=ISSUE, ref=None, room=None, **body):
        path = f"{BASE}/rooms/{room or self.room}/external-actions"
        payload = {"kind": kind, "reference": ref, **body}
        return self.a(who, "POST", path, payload) if isinstance(who, dict) else self.c(who, "POST", path, payload)

    def view(self, who, room=None):
        path = f"{BASE}/rooms/{room or self.room}/external-actions"
        return self.a(who, "GET", path) if isinstance(who, dict) else self.c(who, "GET", path)

    def decide(self, who, action_id, sha, decision="approved", room=None):
        path = f"{BASE}/rooms/{room or self.room}/external-actions/{action_id}/decide"
        body = {"decision": decision, "expected_sha256": sha}
        return self.a(who, "POST", path, body) if isinstance(who, dict) else self.c(who, "POST", path, body)

    def retry(self, who, action_id, sha, room=None):
        path = f"{BASE}/rooms/{room or self.room}/external-actions/{action_id}/retry"
        body = {"expected_sha256": sha}
        return self.a(who, "POST", path, body) if isinstance(who, dict) else self.c(who, "POST", path, body)

    def card(self, action_id):
        s, out = self.op("GET", f"{AX}/{action_id}")
        assert s == 200, out
        return out

    def issues(self):
        return self.gh.issues(REPO)


@pytest.fixture
def world(tmp_path, wg_pg):
    w = XWorld(tmp_path, Clock(), wg_pg["dsn"], EXT)
    try:
        yield w
    finally:
        w.close()


# ======================================================================================================= flags and config
def test_with_the_flag_off_every_new_route_is_a_404_and_nothing_is_written_or_changed(tmp_path, wg_pg):
    for ext in (None, {"enabled": False}, {"enabled": True, "kill_switch": True}):
        w = XWorld(tmp_path, Clock(), wg_pg["dsn"], ext)
        try:
            w.team_up()
            n = len(w.svc._log)
            routes = [("POST", f"{BASE}/rooms/{w.room}/teams/{TEAM}/issue-target", {"repo": REPO}),
                      ("GET", f"{BASE}/rooms/{w.room}/teams/{TEAM}/issue-target", None),
                      ("POST", f"{BASE}/rooms/{w.room}/external-actions", {"kind": ISSUE, "reference": "bref-" + "0" * 24}),
                      ("GET", f"{BASE}/rooms/{w.room}/external-actions", None),
                      ("POST", f"{BASE}/rooms/{w.room}/external-actions/x/decide", {"decision": "approved"}),
                      ("POST", f"{BASE}/rooms/{w.room}/external-actions/x/retry", {})]
            for method, path, body in routes:
                s, out = w.c("alice", method, path, body)
                assert s == 404 and out == {"error": "not found"}, (method, path, s, out)
            for method, path, body in (("GET", AX, None), ("GET", AX + "/x", None), ("POST", AX, {"kind": ISSUE}),
                                       ("POST", AX + "/x/decision", {}), ("POST", AX + "/x/retry", {})):
                assert w.op(method, path, body)[0] == 404, (method, path)
            assert len(w.svc._log) == n and not w.asvc.store.proposals
            assert "assistant_external_actions" not in w.op("GET", "/api/v1/health")[1].get("capabilities", [])
            # the existing Suggest flow and the existing proposal queue are what they were
            assert w.c("dave", "POST", f"{BASE}/rooms/{w.room}/suggestions", {"text": "please look at the build"})[0] == 201
            assert w.op("GET", "/api/v1/assistant-proposals")[0] == 200
            assert w.asvc.external_actions.github is None or not w.asvc.external_actions.enabled()
        finally:
            w.close()


def test_team_rooms_off_closes_the_routes_even_with_the_assistant_flag_on(world):
    w = world
    w.team_up()
    w.svc.team_rooms = {**w.svc.team_rooms, "enabled": False}
    assert w.target()[0] == 404
    assert w.file("alice", ref="bref-" + "0" * 24)[0] == 404


def test_the_capability_is_advertised_only_when_the_flag_is_on_and_the_wiring_is_real(world):
    w = world
    assert "assistant_external_actions" in w.op("GET", "/api/v1/health")[1]["capabilities"]
    # the control plane wires the providers itself from the YAML (the tests replace them with clients on the fakes)
    srv_ea = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=w.svc, assistant_service=_assistant(
        w.dsn, w.mails, {"enabled": True, "github": {"api_base": "http://127.0.0.1:9"}})).assistant.external_actions
    assert isinstance(srv_ea.github, GitHubIssues) and srv_ea.github.api_base == "http://127.0.0.1:9"
    assert isinstance(srv_ea.gmail, GmailDrafts) and w.svc.external_actions_hook is not None


# ============================================================================================================ the target
def test_the_issue_target_is_the_owners_alone_validated_idempotent_and_survives_a_restart(world):
    w = world
    w.team_up()
    assert w.c("alice", "GET", f"{BASE}/rooms/{w.room}/teams/{TEAM}/issue-target")[1] == {"room_id": w.room, "team_id": TEAM, "repo": None}
    for repo in ("../x/y", "a/b/c", "owner", "a b/c", "", None, 7, "https://evil.example/a/b", "a/..", "a/b?x=1"):
        assert w.target(repo)[0] == 400, repo
    s, out = w.target()
    assert s == 200 and out["repo"] == REPO and out["replayed"] is False
    assert w.target()[1]["replayed"] is True and len(w.events("team_issue_target_set")) == 1
    ev = w.events("team_issue_target_set")[0]["payload"]
    assert ev["repo"] == REPO and ev["team_id"] == TEAM and ev["actor_user_id"] == "alice"
    assert w.target("example-org/other")[0] == 200 and w.target("example-org/other")[1]["replayed"] is True
    w.target(REPO)
    # everyone else: agents 403 with an audit trail, members and strangers the same 404 as a missing room, a coordinator 403
    team = w.team
    for agent in (team["iw"], team["impl"], team["coord"]):
        s, out = w.a(agent, "POST", f"{BASE}/rooms/{w.room}/teams/{TEAM}/issue-target", {"repo": "x/y"})
        assert s == 403, out
    for user in ("bob", "carol", "dave", "erin", "mallory"):
        assert w.target("x/y", user=user)[0] in (403, 404), user
    assert len(w.denials("team.issue_target.set")) >= 4
    assert w.target("x/y", team=TEAM2)[0] == 404                        # not attached here
    w.restart()
    assert w.c("alice", "GET", f"{BASE}/rooms/{w.room}/teams/{TEAM}/issue-target")[1]["repo"] == REPO


def test_only_the_owner_reads_the_target_a_filer_learns_nothing_of_it(world):
    w = world
    w.team_up()
    w.target()
    for user in ("bob", "carol", "dave", "erin"):
        assert REPO not in json.dumps(w.c(user, "GET", f"{BASE}/rooms/{w.room}/teams/{TEAM}/issue-target"))
    assert REPO not in json.dumps(w.a(w.team["iw"], "GET", f"{BASE}/rooms/{w.room}/teams/{TEAM}/issue-target"))


# =========================================================================================================== filing
def test_an_issue_writer_files_an_issue_for_an_approved_brief_and_it_waits(world):
    w = world
    b = w.ready()
    n = len(w.svc._log)
    s, out = w.file(w.team["iw"], ref=b["reference"])
    assert s == 201, out
    assert set(out) == {"action_id", "kind", "state", "replayed", "team_id", "created_at"} and out["state"] == "awaiting_approval"
    assert not w.issues() and not w.gh.requests                           # nothing left the system
    ev = w.events("external_action_filed")[-1]["payload"]
    assert ev["actor_agent_id"] == w.team["iw"]["agent_id"] and ev["kind"] == ISSUE and ev["action_id"] == out["action_id"]
    assert set(ev) >= {"room_id", "team_id", "action_id", "kind", "action_key", "replayed"} and len(w.svc._log) == n + 1
    # the operator sees the exact content and where it goes
    card = w.card(out["action_id"])["card"]
    assert card["repository"] == REPO and card["body"].rstrip().endswith("-->") and "Invoice 7731" in card["body"]
    assert w.asvc.store.proposals[out["action_id"]].requested_by == "agent:" + w.team["iw"]["agent_id"]
    # filing again, by anyone allowed, is the same action
    s, again = w.file(w.team["coord"], ref=b["reference"])
    assert s == 200 and again["action_id"] == out["action_id"] and again["replayed"] is True
    assert len(w.asvc.store.proposals) == 1


def test_filing_needs_an_approved_brief_a_target_and_valid_fields(world):
    w = world
    w.team_up()
    s, out = w.op("POST", URL, {"room_id": w.room, "team_id": TEAM, "message_ref": fx.MESSAGE_ID})
    ref = out["brief"]["reference"]
    assert w.file("alice", ref=ref)[0] == 409                              # no issue target yet
    w.target()
    assert w.file("alice", ref=ref)[0] == 409                              # the brief is still pending: nothing was approved
    assert w.file("alice", ref="bref-" + "0" * 24)[0] == 404
    assert w.file("alice", ref="nonsense")[0] in (400, 404)
    assert w.file("alice", kind="send_email", ref=ref)[0] == 400
    assert w.file("alice", ref=ref, nope=1)[0] == 400
    assert w.file("alice", ref=ref, title="x" * 300)[0] == 400
    assert w.file("alice", ref=ref, labels=["not-allowed"])[0] == 400
    assert not w.asvc.store.proposals and not w.events("external_action_filed")
    w.approved_brief()
    assert w.file("alice", ref=ref, labels=["from-email"], title="Pay invoice 7731")[0] == 201
    assert w.card(next(iter(w.asvc.store.proposals)))["card"]["labels"] == ["from-email"]


def test_a_rejected_brief_can_never_become_an_issue(world):
    w = world
    w.team_up()
    w.target()
    s, out = w.op("POST", URL, {"room_id": w.room, "team_id": TEAM, "message_ref": fx.MESSAGE_ID})
    b = out["brief"]
    [row] = w.c("alice", "GET", f"{BASE}/rooms/{w.room}/suggestions")[1]["suggestions"]
    assert w.c("alice", "POST", f"{BASE}/rooms/{w.room}/suggestions/{b['suggestion_id']}/decide",
               {"decision": "reject", "expected_digest": row["digest"]})[0] == 200
    assert w.file("alice", ref=b["reference"])[0] == 409 and not w.asvc.store.proposals


def test_who_may_file_and_who_may_not(world):
    w = world
    b = w.ready()
    team = w.team
    stranger = w.agent(owner="bob", name="stranger")
    for agent in (team["impl"], team["rev"]):                               # on the team, but not the issue writer or coordinator
        s, out = w.file(agent, ref=b["reference"])
        assert s == 403, out
    for user in ("bob", "carol", "dave", "erin"):                           # contributor, viewer, guests
        assert w.file(user, ref=b["reference"])[0] == 403, user
    assert w.file("mallory", ref=b["reference"])[0] == 404                  # not in the room
    assert w.file(stranger, ref=b["reference"])[0] == 404                   # an agent with no access to the room
    assert not w.asvc.store.proposals and not w.events("external_action_filed")
    denied = {e["payload"]["actor_kind"] for e in w.denials("external.file")}
    assert denied >= {"agent", "user"}
    assert w.file(team["iw"], ref=b["reference"])[0] == 201
    assert w.file(team["coord"], ref=b["reference"])[0] == 200              # the coordinator may propose too (same action)
    assert w.file("alice", ref=b["reference"])[0] == 200                    # and so may the owner


def test_a_person_holding_the_issue_writer_role_and_an_external_coordinator_may_propose_never_decide(world):
    w = world
    iw, impl = w.agent(name="w"), w.agent(name="i")
    assert w.attach({"issue_writer": [iw["agent_id"]], "implementer": [impl["agent_id"]]}, team=TEAM)[0] == 200
    assert w.set_coord("agent:" + w.agent(name="c")["agent_id"])[0] == 200
    assert w.target()[0] == 200
    s, out = w.c("alice", "POST", f"{BASE}/rooms/{w.room}/teams/{TEAM}/roles", {"role": "issue_writer", "holder": "user:bob"})
    assert s == 200, out
    w.team = {"impl": impl}
    b = w.approved_brief()
    s, out = w.file("bob", ref=b["reference"])
    assert s == 201, out
    assert w.denials("external.file") == []
    assert w.decide("bob", out["action_id"], out["action_id"])[0] == 403    # holding the role is not approving


# ======================================================================================================= approving
def test_only_the_room_owner_can_approve_and_every_refusal_leaves_a_trail(world):
    w = world
    b = w.ready()
    ext_held, ext_rev = ext_team(w, team=TEAM2, who="dave")                   # dave: an opted-in external coordinator of TEAM2
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    sha = w.card(filed["action_id"])["sha256"]
    others = {"coordinator agent": w.team["coord"], "issue-writer agent": w.team["iw"], "team implementer": w.team["impl"],
              "team reviewer": w.team["rev"], "external coordinator": "dave", "guest": "erin", "contributor": "bob", "viewer": "carol"}
    for label, who in others.items():
        before = len(w.denials("external.decide"))
        s, out = w.decide(who, filed["action_id"], sha)
        assert s == 403, (label, s, out)
        assert len(w.denials("external.decide")) == before + 1, label
        s, out = w.retry(who, filed["action_id"], sha)
        assert s == 403, (label, s, out)
    assert w.decide("mallory", filed["action_id"], sha)[0] == 404
    assert w.decide(w.agent(owner="bob", name="outsider"), filed["action_id"], sha)[0] == 404
    who_denied = {e["payload"]["actor_user_id"] for e in w.denials("external.decide")}
    assert {"alice", "bob", "carol", "dave", "erin"} >= who_denied and {"dave", "erin", "bob", "carol"} <= who_denied
    assert any(e["payload"]["actor_kind"] == "external" or e["payload"]["actor_kind"] == "user" for e in w.denials("external.decide"))
    assert not w.issues() and not w.gh.requests
    assert w.card(filed["action_id"])["state"] == "awaiting_approval"


def test_the_assistant_door_does_not_authenticate_collab_credentials_and_another_operator_sees_nothing(world):
    w = world
    b = w.ready()
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    sha = w.card(filed["action_id"])["sha256"]
    heads = [{"Authorization": f"Agent {a['credential']}", "X-Herdr-Device": a["device_id"]} for a in w.team.values()]
    heads += [{"Authorization": f"Bearer {USERS[u]}"} for u in ("alice", "bob", "dave", "erin")] + [{}]
    for h in heads:
        for method, path, body in (("GET", AX, None), ("GET", f"{AX}/{filed['action_id']}", None),
                                   ("POST", f"{AX}/{filed['action_id']}/decision", {"decision": "approved", "expected_sha256": sha}),
                                   ("POST", f"{AX}/{filed['action_id']}/retry", {"expected_sha256": sha})):
            s, out = _http(w.srv.port, method, path, body, h)
            assert s in (401, 403, 404), (h, method, path, s)
    # a second operator key (another principal): the same 404 as a missing action, never a hint
    s_missing = w.op("GET", f"{AX}/prop_nope")
    assert w.op("GET", f"{AX}/{filed['action_id']}", key=OTHER_KEY) == s_missing
    assert w.op("POST", f"{AX}/{filed['action_id']}/decision", {"decision": "approved", "expected_sha256": sha}, key=OTHER_KEY)[0] == 404
    assert w.op("GET", AX, key=OTHER_KEY)[1] == {"actions": []}
    assert not w.issues()


def test_approving_creates_one_issue_with_exactly_the_approved_content_and_nothing_raw(world):
    w = world
    b = w.ready()
    s, filed = w.file(w.team["iw"], ref=b["reference"], labels=["from-email"])
    card = w.card(filed["action_id"])
    s, out = w.decide("alice", filed["action_id"], "0" * 64)
    assert s == 409 and not w.issues()
    s, out = w.decide("alice", filed["action_id"], card["sha256"])
    assert s == 200 and out["state"] == "executed" and out["replayed"] is False, out
    [issue] = w.issues()
    assert issue["title"] == card["card"]["title"] and issue["body"] == card["card"]["body"]
    assert issue["labels"] == [{"name": "from-email"}] and w.gh.count("POST", f"/repos/{REPO}/issues") == 1
    for sentinel in fx.SENTINELS:
        assert sentinel not in json.dumps(issue), sentinel                   # nothing of the raw email went out
    assert fx.BODY_PHRASE in issue["body"]                                   # only the derived excerpt the operator approved
    ev = w.events("external_action_decided")[-1]["payload"]
    assert ev["actor_user_id"] == "alice" and ev["decision"] == "approved" and ev["action_id"] == filed["action_id"]
    # replay of the approval, of the filing and of the whole brief: still one issue
    again = w.decide("alice", filed["action_id"], card["sha256"])
    assert again[0] == 200 and again[1]["replayed"] is True
    assert w.file(w.team["iw"], ref=b["reference"])[1]["action_id"] == filed["action_id"]
    assert w.op("POST", URL, {"room_id": w.room, "team_id": TEAM, "message_ref": fx.MESSAGE_ID})[1]["brief"]["replayed"] is True
    assert len(w.issues()) == 1
    # the filer sees the outcome, not the content
    s, mine = w.view(w.team["iw"])
    assert s == 200 and [r["state"] for r in mine["actions"]] == ["executed"] and "card" not in mine["actions"][0]
    s, owner = w.view("alice")
    assert owner["actions"][0]["result"]["ref"].endswith("/issues/1") and owner["actions"][0]["card"]["repository"] == REPO


def test_rejecting_is_terminal_and_a_second_filing_makes_a_new_action(world):
    w = world
    b = w.ready()
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    assert w.decide("alice", filed["action_id"], None, decision="rejected")[1]["state"] == "rejected"
    assert w.decide("alice", filed["action_id"], w.card(filed["action_id"])["sha256"])[0] == 400
    s, second = w.file(w.team["iw"], ref=b["reference"])
    assert s == 201 and second["action_id"] != filed["action_id"]
    assert not w.issues()


def test_a_restart_between_filing_and_approving_and_between_approving_and_executing_creates_exactly_one_issue(world):
    w = world
    b = w.ready()
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    sha = w.card(filed["action_id"])["sha256"]
    w.restart()
    assert w.card(filed["action_id"])["state"] == "awaiting_approval"
    assert w.file("alice", ref=b["reference"])[1]["action_id"] == filed["action_id"]
    # the operator approves while GitHub is down: an approved action with no issue and a visible reason
    w.gh.down = True
    s, out = w.decide("alice", filed["action_id"], sha)
    assert s == 200 and out["state"] == "unknown" and out["failure"]["reason"] == "provider_unavailable"
    w.restart()
    assert w.card(filed["action_id"])["state"] == "unknown" and not w.issues()
    w.gh.down = False
    assert w.retry(w.team["iw"], filed["action_id"], sha)[0] == 403
    s, out = w.retry("alice", filed["action_id"], sha)
    assert s == 200 and out["state"] == "executed" and len(w.issues()) == 1
    posts = w.gh.count("POST", f"/repos/{REPO}/issues")
    w.restart()
    assert w.file("alice", ref=b["reference"])[1]["action_id"] == filed["action_id"]
    assert len(w.issues()) == 1 and w.gh.count("POST", f"/repos/{REPO}/issues") == posts


def test_a_lost_answer_is_reconciled_through_the_assistant_door_too(world):
    w = world
    b = w.ready()
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    sha = w.card(filed["action_id"])["sha256"]
    w.gh.drop_after_create = True
    s, out = w.op("POST", f"{AX}/{filed['action_id']}/decision", {"decision": "approved", "expected_sha256": sha})
    assert s == 200 and out["state"] == "unknown" and out["failure"]["reason"] == "no_answer" and len(w.issues()) == 1
    s, out = w.op("POST", f"{AX}/{filed['action_id']}/retry", {"expected_sha256": sha})
    assert s == 200 and out["state"] == "executed" and len(w.issues()) == 1
    assert [e["payload"]["decision"] for e in w.events("external_action_decided")] == ["approved", "retry"]


def test_a_revoked_token_is_a_visible_failure_and_only_the_operators_retry_fixes_it(world):
    w = world
    b = w.ready()
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    sha = w.card(filed["action_id"])["sha256"]
    good = w.gh.token
    w.gh.token = "rotated-away"
    s, out = w.decide("alice", filed["action_id"], sha)
    assert out["state"] == "failed" and out["failure"] == {"outcome": "failed", "reason": "credential_rejected"}
    assert w.view(w.team["iw"])[1]["actions"][0]["state"] == "failed"
    w.gh.token = good
    assert w.retry(w.team["coord"], filed["action_id"], sha)[0] == 403
    assert not w.issues()
    assert w.retry("alice", filed["action_id"], sha)[1]["state"] == "executed" and len(w.issues()) == 1


def test_the_hourly_cap_on_filing_holds(tmp_path, wg_pg):
    w = XWorld(tmp_path, Clock(), wg_pg["dsn"], {**EXT, "actions_per_hour": 1})
    try:
        b = w.ready()
        assert w.file(w.team["iw"], ref=b["reference"])[0] == 201
        b2 = w.approved_brief(MAIL2)
        assert w.file(w.team["iw"], ref=b2["reference"])[0] == 429
        assert len(w.asvc.store.proposals) == 1
    finally:
        w.close()


# ======================================================================================================= email drafts
def test_a_reply_draft_is_proposed_approved_once_and_is_a_draft_never_a_send(world):
    w = world
    b = w.ready()
    s, filed = w.file(w.team["coord"], kind=DRAFT, ref=b["reference"], body="Thanks, payment goes out on Friday.")
    assert s == 201, filed
    assert set(filed) == {"action_id", "kind", "state", "replayed", "team_id", "created_at"}
    card = w.card(filed["action_id"])
    assert card["card"]["to"] == [fx.SENDER_ADDRESS] and card["card"]["subject"] == "Re: Invoice 7731 payment overdue"
    assert not w.gm.requests
    s, out = w.decide("alice", filed["action_id"], card["sha256"])
    assert s == 200 and out["state"] == "executed", out
    [draft] = w.gm.drafts
    assert draft["headers"]["to"] == fx.SENDER_ADDRESS and "payment goes out on Friday" in draft["body"]
    assert w.gm.sent == [] and w.gm.count("POST", "/gmail/v1/users/me/messages/send") == 0
    assert w.decide("alice", filed["action_id"], card["sha256"])[1]["replayed"] is True and len(w.gm.drafts) == 1
    # the recipient, thread and message id never appear on any surface the team can read
    surfaces = json.dumps([w.view(w.team["coord"]), w.view(w.team["iw"]), w.view("erin"), w.view("bob"),
                           w.c("dave", "GET", f"{BASE}/rooms/{w.room}/events?after=0")])
    for sentinel in fx.SENTINELS:
        assert sentinel not in surfaces, sentinel
    raw_log = open(w.tmp / "log", "rb").read().decode("utf-8", "replace")
    for sentinel in (fx.MESSAGE_ID, fx.THREAD_ID, fx.SENDER_ADDRESS, fx.HEADER_ID, fx.ATTACHMENT):
        assert sentinel not in raw_log, sentinel


def test_a_draft_body_the_filer_chose_is_checked_and_the_recipient_cannot_be_chosen(world):
    w = world
    b = w.ready()
    for body in ("", "x" * 801, "my password: hunter2hunter2 ok"):
        assert w.file("alice", kind=DRAFT, ref=b["reference"], body=body)[0] == 400, body
    assert w.file("alice", kind=DRAFT, ref=b["reference"], body="ok", to=["evil@example.com"])[0] == 400
    assert w.file("alice", kind=DRAFT, ref=b["reference"], body="ok", repo="a/b")[0] == 400
    assert not w.asvc.store.proposals


def test_the_assistant_files_and_the_operator_decides_through_the_assistant_door(world):
    w = world
    b = w.ready()
    s, out = w.op("POST", AX, {"kind": DRAFT, "room_id": w.room, "reference": b["reference"], "body": "Noted, thank you."})
    assert s == 201, out
    assert w.asvc.store.proposals[out["action_id"]].requested_by == "assistant:" + principal_id(KEY)
    assert w.events("external_action_filed")[-1]["payload"].get("via") == "assistant"
    s, listing = w.op("GET", AX + "?state=awaiting_approval")
    assert [r["action_id"] for r in listing["actions"]] == [out["action_id"]]
    card = w.card(out["action_id"])
    s, done = w.op("POST", f"{AX}/{out['action_id']}/decision", {"decision": "approved", "expected_sha256": card["sha256"]})
    assert s == 200 and done["state"] == "executed" and len(w.gm.drafts) == 1 and w.gm.sent == []
    assert w.events("external_action_decided")[-1]["payload"]["actor_user_id"] == "alice"
    assert w.op("POST", AX, {"kind": ISSUE, "room_id": "room_nope", "reference": b["reference"]})[0] == 404
    assert w.op("POST", AX, {"kind": ISSUE, "room_id": w.room, "reference": b["reference"], "bogus": 1})[0] == 400


def test_gmail_revoked_and_down_are_visible_and_fail_safe(world):
    w = world
    b = w.ready()
    s, filed = w.file("alice", kind=DRAFT, ref=b["reference"], body="ok")
    sha = w.card(filed["action_id"])["sha256"]
    w.gm.revoked = True
    s, out = w.decide("alice", filed["action_id"], sha)
    assert out["state"] == "failed" and out["failure"]["reason"] == "connection_needs_attention" and not w.gm.drafts
    w.gm.revoked = False
    w.gm.down = True
    w.workspace._store.set_status("default", principal_id(KEY), "google", "connected")
    w.workspace.invalidate_all()
    s, out = w.retry("alice", filed["action_id"], sha)
    assert out["state"] == "unknown" and out["failure"]["reason"] == "provider_unavailable"
    w.gm.down = False
    s, out = w.retry("alice", filed["action_id"], sha)
    assert out["state"] == "executed" and len(w.gm.drafts) == 1 and w.gm.sent == []


# ==================================================================================================== credentials / logs
def test_no_credential_appears_in_any_log_event_audit_response_or_the_stored_snapshot(world, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    w = world
    b = w.ready()
    bodies = []
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    bodies.append((s, filed))
    sha = w.card(filed["action_id"])["sha256"]
    w.gh.fail_next(502)
    bodies.append(w.decide("alice", filed["action_id"], sha))
    bodies.append(w.retry("alice", filed["action_id"], sha))
    s, d = w.file("alice", kind=DRAFT, ref=b["reference"], body="ok")
    bodies.append(w.decide("alice", d["action_id"], w.card(d["action_id"])["sha256"]))
    bodies += [w.view("alice"), w.op("GET", AX), w.view(w.team["iw"])]
    blob = json.dumps(bodies, default=str) + open(w.tmp / "log", "rb").read().decode("utf-8", "replace")
    blob += json.dumps(w.asvc.store.audit, default=str) + caplog.text + json.dumps(w.svc._log, default=str)
    with psycopg2.connect(w.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT data::text FROM pa_store_snapshot")
        blob += "".join(r[0] for r in cur.fetchall())
    for secret in (w.gh.token, w.gm.access_token, "refresh-u1", "client-secret", "Bearer "):
        assert secret not in blob, secret


def test_the_new_events_are_replayed_and_unknown_to_an_older_fold_harmlessly(world):
    w = world
    b = w.ready()
    s, filed = w.file(w.team["iw"], ref=b["reference"])
    w.decide("alice", filed["action_id"], w.card(filed["action_id"])["sha256"])
    kinds = {e["event_type"] for e in w.svc._log}
    assert {"team_issue_target_set", "external_action_filed", "external_action_decided"} <= kinds
    from herdr_engineering.control_plane import collab as collab_mod
    assert "external_action_filed" in collab_mod.BULK_EVENTS and "team_issue_target_set" not in collab_mod.SAFETY_EVENTS
    w.restart()
    assert w.c("alice", "GET", f"{BASE}/rooms/{w.room}/teams/{TEAM}/issue-target")[1]["repo"] == REPO
    assert os.path.getsize(w.tmp / "log") > 0
