"""Assistant action adapters (review, proposals, comments, drafts), their lineage, CI-failure watching and
dashboard notifications. Every adapter is exercised through the same gate the model uses
(`_execute_gated`), plus the negative cases: no grant, no approval, replay, other principal."""
import json
import socket
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import (
    AssistantForbidden,
    AssistantNotFound,
    AssistantService,
    AssistantStore,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_adapters import comment_hook
from herdr_engineering.control_plane.assistant_lineage import ActionLineageEmitter, LineageReader
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.project_intelligence_api import IntelligenceRoutes
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_graph import Feed, _AnyKey

OPERATOR = {"kind": "operator", "id": "key:op", "authenticated_by": "api_key"}
U1 = {"kind": "agent", "id": "key:u1", "authenticated_by": "api_key"}
U2 = {"kind": "agent", "id": "key:u2", "authenticated_by": "api_key"}
PRINCIPALS = {"key:u1": U1, "key:u2": U2}
PR = "pr:github.com/o/r#7"
ANCHOR = {"kind": "node", "node_id": "agent:a1"}


def build(section=None, now=None, repo=None):
    base = {"enabled": True, "actions": {"collab_users": {"key:u1": "alice"}}}
    for k, v in (section or {}).items():
        base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) else v
    clock = now or (lambda: datetime(2026, 10, 29, 12, 0, tzinfo=UTC))
    svc = AssistantService(config=assistant_config({"personal_assistant": base}), now=clock)
    out = {}
    for user in ("key:u1", "key:u2"):
        a = svc.create_assistant(tenant_id="t", user_id=user, name="A")
        th = svc.create_thread(tenant_id="t", user_id=user, assistant_id=a.assistant_id)
        out[user] = svc.send_message(tenant_id="t", user_id=user, assistant_id=a.assistant_id,
                                     thread_id=th.thread_id, content="hi")
    return svc, out["key:u1"], out["key:u2"]


def grant(svc, run, tool, scope="*", **kw):
    return svc.grant_tool(tenant_id=run.tenant_id, user_id=run.user_id, assistant_id=run.assistant_id,
                          tool_id=tool, scope=scope, **kw)


def approve_pending(svc, run, user="key:u1"):
    appr = [a for a in svc.store.approvals.values() if a.decision == "pending"][-1]
    svc.decide_approval(appr.approval_id, tenant_id="t", user_id=user, decision="approved", actor=user)


def run_tool(svc, run, tool, args):
    return svc._execute_gated(run, tool, args, 4000)


# ---- nothing works without a grant ---------------------------------------------------------------------------

NO_GRANT_CASES = [
    ("request_review", {"task": "x", "pr_id": PR}),
    ("propose_branch", {"title": "T", "repo": "o/r"}),
    ("request_mission", {"title": "T", "task": "x"}),
    ("comment_on_node", {"room_id": "room1", "anchor": ANCHOR, "text": "hi"}),
    ("draft_email", {"to": "bob@example.com", "subject": "s", "body": "b"}),
    ("send_message", {"to": "bob@example.com", "body": "b"}),
    ("run_shell", {"command": "ls"}),
]


@pytest.mark.parametrize("tool,args", NO_GRANT_CASES)
def test_no_grant_means_denied_and_no_side_effect(tool, args):
    svc, r1, _ = build()
    for hook in ("review_rereview", "comment_poster", "artifact_writer", "lineage_emit"):
        setattr(svc, hook, Mock())
    svc._shell_runner, svc._transport = Mock(), Mock()
    assert "denied" in run_tool(svc, r1, tool, args)
    assert not (svc.store.delegations or svc.store.proposals)
    for hook in ("review_rereview", "comment_poster", "artifact_writer", "lineage_emit"):
        getattr(svc, hook).assert_not_called()
    svc._shell_runner.assert_not_called()
    svc._transport.assert_not_called()


@pytest.mark.parametrize("tool,args", [c for c in NO_GRANT_CASES if c[0] in
                                       ("request_review", "comment_on_node", "send_message", "run_shell")])
def test_a_grant_alone_is_not_enough_for_sensitive_tools(tool, args):
    svc, r1, _ = build()
    svc._shell_runner, svc.comment_poster = Mock(), Mock()
    grant(svc, r1, tool)
    assert "approval required" in run_tool(svc, r1, tool, args)
    assert not svc.store.delegations
    svc._shell_runner.assert_not_called()
    svc.comment_poster.assert_not_called()


# ---- request_review ----------------------------------------------------------------------------------------

def test_request_review_needs_grant_then_approval_then_records_once():
    svc, r1, _ = build()
    svc.review_rereview = Mock(return_value="re-review recorded for head abc")
    args = {"target_system": "claude", "pr_id": PR, "head": "a" * 40, "task": "review please", "budget_seconds": 99999}
    grant(svc, r1, "request_review", "claude")
    assert "approval required" in run_tool(svc, r1, "request_review", args)  # sensitive: a person decides
    assert not svc.store.delegations
    approve_pending(svc, r1)
    out = run_tool(svc, r1, "request_review", args)
    assert '"kind": "review"' in out
    (d,) = svc.store.delegations.values()
    assert d.status == "queued" and d.kind == "review" and d.target_system == "claude"
    assert d.budget_seconds == 3600                                          # clamped to the YAML cap
    assert "re-review recorded" in d.note
    svc.review_rereview.assert_called_once_with("t", "key:u1", PR, "a" * 40)
    assert "already ran" in run_tool(svc, r1, "request_review", args)        # replay
    assert len(svc.store.delegations) == 1 and svc.review_rereview.call_count == 1


def test_request_review_rejects_an_unknown_reviewer_and_survives_a_hidden_pr():
    svc, r1, _ = build()
    grant(svc, r1, "request_review", preauthorized=True, principal=OPERATOR)
    assert "target_system must be pi or claude" in run_tool(svc, r1, "request_review",
                                                            {"target_system": "bash", "task": "x"})
    svc.review_rereview = Mock(side_effect=LookupError("not found"))
    run_tool(svc, r1, "request_review", {"pr_id": PR, "task": "x"})
    (d,) = svc.store.delegations.values()
    assert "not visible" in d.note and d.status == "queued"


def test_other_user_cannot_see_or_cancel_a_review_delegation():
    svc, r1, _ = build()
    grant(svc, r1, "request_review", preauthorized=True, principal=OPERATOR)
    run_tool(svc, r1, "request_review", {"task": "x"})
    (d,) = svc.store.delegations.values()
    assert svc.list_delegations(tenant_id="t", user_id="key:u2") == []
    with pytest.raises(KeyError):
        svc.get_delegation(d.delegation_id, tenant_id="t", user_id="key:u2")


# ---- proposals: propose_branch / request_mission ---------------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    ("propose_branch", {"title": "Fix flaky test", "repo": "o/r", "base": "main"}),
    ("request_mission", {"title": "Migrate", "task": "do the migration", "target_system": "pi"}),
])
def test_proposal_tools_create_a_record_and_start_nothing(tool, args):
    svc, r1, _ = build()
    svc._shell_runner, svc._transport = Mock(), Mock()
    grant(svc, r1, tool)
    out = run_tool(svc, r1, tool, args)
    (p,) = svc.store.proposals.values()
    assert p.kind == tool and p.status == "awaiting_approval" and p.requested_by.startswith("assistant:")
    assert json.loads(out)["proposal"]["execution"] == "not_started"
    # approving is a record of permission only
    svc.decide_proposal(p.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    assert p.status == "approved"
    assert not svc.store.delegations
    svc._shell_runner.assert_not_called()
    svc._transport.assert_not_called()


def test_same_proposal_is_not_duplicated_and_bad_input_is_refused():
    svc, r1, _ = build()
    grant(svc, r1, "propose_branch")
    a = {"title": "T", "repo": "o/r"}
    run_tool(svc, r1, "propose_branch", a)
    svc.create_proposal(tenant_id="t", user_id="key:u1", assistant_id=r1.assistant_id, kind="propose_branch",
                        title="T", payload={"title": "T", "repo": "o/r", "base": "", "rationale": ""},
                        requested_by="x")
    assert len(svc.store.proposals) == 1
    assert "owner/name" in run_tool(svc, r1, "propose_branch", {"title": "T", "repo": "../../etc"})


def test_proposal_decision_rules():
    svc, r1, _ = build()
    grant(svc, r1, "propose_branch")
    run_tool(svc, r1, "propose_branch", {"title": "T", "repo": "o/r"})
    (p,) = svc.store.proposals.values()
    assert svc.list_proposals(tenant_id="t", user_id="key:u2") == []
    with pytest.raises(AssistantNotFound):
        svc.decide_proposal(p.proposal_id, tenant_id="t", user_id="key:u2", decision="approved", actor="key:u2")
    with pytest.raises(AssistantForbidden):                               # requester approving itself
        svc.decide_proposal(p.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor=p.requested_by)
    p.expires_at = svc.now() - timedelta(seconds=1)
    with pytest.raises(Exception, match="expired"):
        svc.decide_proposal(p.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    assert p.status == "expired"


# ---- comment_on_node ---------------------------------------------------------------------------------------

def test_comment_posts_as_the_mapped_user_after_grant_and_approval_and_replay_reuses_the_key():
    svc, r1, _ = build()
    svc.comment_poster = Mock(return_value=(201, {"comment": {"comment_id": "c1"}}))
    args = {"room_id": "room1", "anchor": ANCHOR, "text": "why did B branch?"}
    grant(svc, r1, "comment_on_node", "room1")
    assert "approval required" in run_tool(svc, r1, "comment_on_node", args)
    svc.comment_poster.assert_not_called()
    approve_pending(svc, r1)
    assert '"ok": true' in run_tool(svc, r1, "comment_on_node", args)
    collab_user, room, body = svc.comment_poster.call_args.args
    assert (collab_user, room) == ("alice", "room1")
    assert set(body) == {"anchor", "text", "idempotency_key"}
    assert "already ran" in run_tool(svc, r1, "comment_on_node", args)
    assert svc.comment_poster.call_count == 1


def test_comment_is_refused_for_a_principal_with_no_collaboration_identity():
    svc, _, r2 = build()                                                 # key:u2 has no collab_users entry
    svc.comment_poster = Mock()
    grant(svc, r2, "comment_on_node", "room1", preauthorized=True, principal=OPERATOR)
    out = run_tool(svc, r2, "comment_on_node", {"room_id": "room1", "anchor": ANCHOR, "text": "hi"})
    assert "no collaboration identity" in out
    svc.comment_poster.assert_not_called()


class _FakeCollab:
    def __init__(self, users=("alice",), approval=None, statuses=None):
        self.auth = SimpleNamespace(exists=lambda u: u in users)
        self.approval, self._users, self.degraded = approval, statuses or {}, False
        self.seen = []

    def add_comment(self, pr, room_id, body):
        self.seen.append(pr)
        return 201, {"comment": {"comment_id": "c9"}}


def test_comment_hook_acts_as_an_ordinary_user_never_admin_or_approver():
    collab = _FakeCollab()
    status, _ = comment_hook(SimpleNamespace(collab=collab))("alice", "room1", {})
    pr = collab.seen[0]
    assert status == 201 and (pr.kind, pr.id) == ("user", "alice")
    assert pr.admin is False and pr.approver is False and pr.is_agent is False


def test_comment_hook_refuses_unknown_and_unapproved_users():
    from herdr_engineering.control_plane.collab import CollabError
    with pytest.raises(CollabError):
        comment_hook(SimpleNamespace(collab=_FakeCollab()))("mallory", "room1", {})
    gated = _FakeCollab(approval=object(), statuses={"alice": {"status": "pending"}})
    with pytest.raises(CollabError, match="waiting"):
        comment_hook(SimpleNamespace(collab=gated))("alice", "room1", {})
    assert not gated.seen


# ---- draft_email / send stays gated -------------------------------------------------------------------------

def test_draft_email_saves_a_draft_artifact_and_never_sends():
    svc, r1, _ = build()
    svc._transport, svc._shell_runner = Mock(), Mock()
    svc.artifact_writer = Mock(return_value={"artifact_id": "artifact_1", "version_id": "v1", "version_number": 1})
    args = {"to": "bob@example.com", "subject": "Hi", "body": "hello"}
    grant(svc, r1, "draft_email", "bob@example.com")
    out = json.loads(run_tool(svc, r1, "draft_email", args))
    assert out["sent"] is False and out["artifact_id"] == "artifact_1"
    user, draft, _ = svc.artifact_writer.call_args.args
    assert user == "key:u1" and draft["sent"] is False and draft["status"] == "draft"
    assert draft["drafted_by"] == f"assistant:{r1.assistant_id}"
    svc._transport.assert_not_called()
    svc._shell_runner.assert_not_called()
    assert "already ran" in run_tool(svc, r1, "draft_email", args)
    assert svc.artifact_writer.call_count == 1


def test_draft_email_rejects_a_bad_recipient_and_a_missing_registry():
    svc, r1, _ = build()
    grant(svc, r1, "draft_email")
    assert "registry is not available" in run_tool(svc, r1, "draft_email",
                                                   {"to": "bob@example.com", "subject": "s", "body": "b"})
    svc.artifact_writer = Mock()
    assert "one email address" in run_tool(svc, r1, "draft_email",
                                           {"to": "a@b.c, d@e.f", "subject": "s", "body": "b"})
    svc.artifact_writer.assert_not_called()


def test_draft_writer_commits_a_versioned_private_artifact_and_a_new_version_on_edit():
    from herdr_engineering.control_plane.assistant_adapters import draft_writer_hook
    calls = []

    def store(method, *args, **kw):
        calls.append((method, args, kw))
        return {"register_logical": {"artifact_id": "artifact_9"},
                "get_record": {"current_version_id": "ver_1"},
                "report_permission": True,
                "commit_version": {"artifact_id": "artifact_9", "version_id": "ver_2", "version_number": 2}}[method]

    acl = SimpleNamespace(grant=Mock(), can=lambda *a: True)
    rustfs = SimpleNamespace(put=lambda data: ("sha256/aa/bb/" + "a" * 64, "a" * 64))
    server = SimpleNamespace(_keys={"k-1": "operator"}, artifact_store=store, rustfs=rustfs, artifact_acl=acl,
                             ingest=Mock(ingest=Mock()))
    import hashlib
    uid = f"key:{hashlib.sha256(b'k-1').hexdigest()[:12]}"
    out = draft_writer_hook(server)(uid, {"to": "b@x.io", "subject": "s", "body": "b", "artifact_id": ""}, "idem")
    assert out["version_id"] == "ver_2"
    commit = [c for c in calls if c[0] == "commit_version"][0]
    assert commit[2]["base_version_id"] is None and commit[2]["provenance"]["sent"] is False
    acl.grant.assert_called_once()
    again = draft_writer_hook(server)(uid, {"to": "b@x.io", "subject": "s", "body": "b2", "artifact_id": "artifact_9"}, "i2")
    assert again["version_id"] == "ver_2"
    assert [c for c in calls if c[0] == "commit_version"][1][2]["base_version_id"] == "ver_1"


# ---- lineage ---------------------------------------------------------------------------------------------------

def _graph_world():
    repo = ControlPlaneRepo()
    ingest = IngestService(repo, key_scopes=_AnyKey())
    return repo, ingest


def test_action_is_linked_into_the_graph_with_correlation_ids_visible_only_to_its_owner():
    repo, ingest = _graph_world()
    svc, r1, _ = build()
    svc.lineage_emit = ActionLineageEmitter(ingest, PRINCIPALS.get).emit
    grant(svc, r1, "request_review", preauthorized=True, principal=OPERATOR)
    run_tool(svc, r1, "request_review", {"task": "look", "pr_id": PR})
    action = next(iter(svc.store.actions.values()))
    node = f"decision:assistant.{action.action_id}"
    attrs = repo.graph.get_node(U1, node)["node"]["attrs"]
    assert attrs["assistant_id"] == r1.assistant_id and attrs["thread_id"] == r1.thread_id
    assert attrs["run_id"] == r1.run_id and attrs["action_id"] == action.action_id
    assert attrs["request_hash"] == action.request_hash and attrs["idempotency_key"] == action.idempotency_key
    assert attrs["delegation_id"].startswith("dlg_")
    nb = repo.graph.neighborhood(U1, f"thread:assistant.{r1.thread_id}", depth=1)
    assert {e["edge_type"] for e in nb["edges"]} == {"delegation"}
    for other in (U2, OPERATOR):
        with pytest.raises(KeyError):
            repo.graph.get_node(other, node)


def test_lineage_emission_replay_is_idempotent_and_never_breaks_the_action():
    repo, ingest = _graph_world()
    emitter = ActionLineageEmitter(ingest, PRINCIPALS.get)
    c = {"action_id": "act_1", "thread_id": "thr_1", "assistant_id": "a", "tool_id": "propose_branch"}
    assert emitter.emit("key:u1", c)["node"] == "decision:assistant.act_1"
    before = repo.graph.snapshot()
    assert emitter.emit("key:u1", c)["error"] is None
    assert repo.graph.snapshot() == before
    assert emitter.emit("nobody", c)["error"]                          # no principal: nothing emitted, no raise
    svc, r1, _ = build()
    svc.lineage_emit = Mock(side_effect=RuntimeError("graph down"))
    grant(svc, r1, "propose_branch")
    assert '"ok": true' in run_tool(svc, r1, "propose_branch", {"title": "T", "repo": "o/r"})


def test_draft_email_links_a_produced_edge_to_the_artifact():
    repo, ingest = _graph_world()
    svc, r1, _ = build()
    svc.lineage_emit = ActionLineageEmitter(ingest, PRINCIPALS.get).emit
    svc.artifact_writer = Mock(return_value={"artifact_id": "artifact_1", "version_id": "v1", "version_number": 1})
    grant(svc, r1, "draft_email")
    from herdr_engineering.control_plane.artifact_lineage import emit_version_lineage
    emit_version_lineage(ingest, {"artifact_id": "artifact_1", "version_id": "v1", "version_number": 1}, U1)
    run_tool(svc, r1, "draft_email", {"to": "bob@example.com", "subject": "s", "body": "b"})
    action = next(iter(svc.store.actions.values()))
    nb = repo.graph.neighborhood(U1, f"decision:assistant.{action.action_id}", depth=1)
    assert ("produced", "artifact:artifact_1") in {(e["edge_type"], e["to_id"]) for e in nb["edges"]}


# ---- HTTP: the whole path through a real server and its key scopes --------------------------------------------

class _Script:
    supports_tools = True

    def __init__(self, calls):
        self.calls, self.i = calls, 0

    def execute(self, run, messages):
        return "done"

    def step(self, run, messages, tools):
        resp = self.calls[min(self.i, len(self.calls) - 1)]
        self.i += 1
        return resp


KEY1, KEY2 = "k-assistant-0001", "k-assistant-0002"


def _call(port, key, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


def test_http_action_lands_in_the_graph_through_real_key_scopes(acknowledged_sink):
    script = _Script([{"content": "", "tool_calls": [{"id": "c1", "name": "propose_branch",
                                                      "arguments": json.dumps({"title": "Fix", "repo": "o/r"})}]},
                      {"content": "proposed", "tool_calls": []}])
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "inference": {"endpoint": "http://x.invalid"}, "tools": {"enabled": True}}}),
        runner=script)
    server = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc,
                                api_keys={KEY1: "operator", KEY2: "operator"})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        port = server.port
        _, a = _call(port, KEY1, "POST", "/api/v1/assistants", {"name": "A"})
        aid = a["assistant_id"]
        assert _call(port, KEY1, "POST", f"/api/v1/assistants/{aid}/permissions",
                     {"tool_id": "propose_branch", "scope": "o/r"})[0] == 201
        _, t = _call(port, KEY1, "POST", f"/api/v1/assistants/{aid}/threads", {})
        _, r = _call(port, KEY1, "POST", f"/api/v1/assistants/{aid}/threads/{t['thread_id']}/messages",
                     {"content": "propose a fix"})
        assert r["run"]["state"] in ("completed", "running", "queued", "submitted")
        status, body = _call(port, KEY1, "GET", "/api/v1/assistant-proposals")
        (prop,) = body["proposals"]
        assert prop["kind"] == "propose_branch" and prop["execution"] == "not_started"
        action = next(iter(svc.store.actions.values()))
        node = f"/api/v1/graph/nodes/decision:assistant.{action.action_id}"
        status, node_body = _call(port, KEY1, "GET", node)
        assert status == 200
        assert node_body["node"]["attrs"]["thread_id"] == t["thread_id"]
        assert _call(port, KEY2, "GET", node)[0] == 404                   # another key never sees it
        assert _call(port, KEY2, "GET", "/api/v1/assistant-proposals")[1]["proposals"] == []
        assert _call(port, KEY2, "POST", f"/api/v1/assistant-proposals/{prop['proposal_id']}/decision",
                     {"decision": "approved"})[0] == 404
        status, body = _call(port, KEY1, "POST", f"/api/v1/assistant-proposals/{prop['proposal_id']}/decision",
                             {"decision": "approved"})
        assert status == 200 and body["status"] == "approved"
    finally:
        server.shutdown()


# ---- CI-failure watching ------------------------------------------------------------------------------------------

def _ci_world(*, board_scope=True, execute=False):
    repo = ControlPlaneRepo()
    ingest = IngestService(repo, key_scopes=_AnyKey())
    feed = Feed(ingest)
    feed.principal = U1
    section = {"enabled": True, "execute": execute,
               "assistant_viewers": ({"t/key:u1": {"org_id": "org1", "projects": "*"}} if board_scope else {})}
    routes = IntelligenceRoutes(section_fn=lambda: section)
    svc, r1, r2 = build()
    svc.lineage_read = LineageReader(repo.graph, PRINCIPALS.get).read
    svc.ci_board_request = routes.assistant_ci_repair
    return repo, feed, routes, svc, r1, r2


def _fail(feed, run=12, head="a" * 40, node="ci:r1", state="failed"):
    feed.node(node, label="ci", state=state, attrs={"repo": "o/r", "run": run, "head": head})


def test_a_new_ci_failure_becomes_one_proposal_and_one_notification_and_is_not_realerted():
    _, feed, routes, svc, r1, _ = _ci_world()
    _fail(feed)
    kw = dict(tenant_id="t", user_id="key:u1", assistant_id=r1.assistant_id, actor="key:u1")
    first = svc.watch_ci(**kw)
    assert len(first["created"]) == 1 and first["duplicates"] == 0
    (p,) = svc.store.proposals.values()
    assert p.kind == "ci_repair" and p.payload["auto_merge"] is False and p.link == "#/attention"
    (board,) = routes.board.pending("org1")
    assert board.action == "request_ci_repair" and board.payload == {"repo": "o/r", "run": 12}
    assert board.requester == "key:u1" and p.external_ref == board.id
    (n,) = svc.store.notifications.values()
    assert n.channel == "dashboard" and n.external is False and "o/r" in n.text
    again = svc.watch_ci(**kw)
    assert again["created"] == [] and again["duplicates"] == 1
    assert len(svc.store.proposals) == 1 and len(svc.store.notifications) == 1 and len(routes.board.pending("org1")) == 1


def test_dedupe_survives_a_restart_and_a_new_head_is_a_new_failure():
    _, feed, _, svc, r1, _ = _ci_world()
    _fail(feed)
    kw = dict(tenant_id="t", user_id="key:u1", assistant_id=r1.assistant_id, actor="key:u1")
    svc.watch_ci(**kw)
    fresh = AssistantStore()
    fresh.restore(svc.store.snapshot())
    svc.store = fresh
    assert svc.watch_ci(**kw)["duplicates"] == 1
    _fail(feed, head="b" * 40)
    assert len(svc.watch_ci(**kw)["created"]) == 1


def test_passing_runs_and_nodes_without_a_repo_are_ignored():
    _, feed, _, svc, r1, _ = _ci_world()
    _fail(feed, state="passed", node="ci:ok")
    feed.node("ci:bare", state="failed", attrs={})
    out = svc.watch_ci(tenant_id="t", user_id="key:u1", assistant_id=r1.assistant_id, actor="key:u1")
    assert out["created"] == [] and len(out["skipped"]) == 1


def test_no_scope_means_no_proposal_and_another_user_sees_nothing():
    _, feed, _, svc, r1, r2 = _ci_world(board_scope=False)
    _fail(feed)
    out = svc.watch_ci(tenant_id="t", user_id="key:u1", assistant_id=r1.assistant_id, actor="key:u1")
    assert out["created"] == [] and "scope" in out["skipped"][0]["reason"]
    out2 = svc.watch_ci(tenant_id="t", user_id="key:u2", assistant_id=r2.assistant_id, actor="key:u2")
    assert out2["scanned"] == 0 or out2["created"] == []                       # org-visible node, but no board scope
    assert not svc.store.proposals


def test_ci_proposals_cannot_be_decided_by_the_assistant_and_approval_executes_nothing_by_default():
    _, feed, routes, svc, r1, _ = _ci_world()
    _fail(feed)
    svc.watch_ci(tenant_id="t", user_id="key:u1", assistant_id=r1.assistant_id, actor="key:u1")
    (p,) = svc.store.proposals.values()
    with pytest.raises(Exception, match="attention queue"):
        svc.decide_proposal(p.proposal_id, tenant_id="t", user_id="key:u1", decision="approved", actor="key:u1")
    (board,) = routes.board.pending("org1")
    with pytest.raises(Exception, match="own proposal"):                      # the requester cannot approve
        routes.board.approve(board.id, approver="key:u1", org_id="org1",
                             payload_sha256=board.payload_sha256, resource_version=board.resource_version)
    routes.board.approve(board.id, approver="key:boss", org_id="org1",
                         payload_sha256=board.payload_sha256, resource_version=board.resource_version)
    repair = Mock()
    routes.ci_repair_fn = repair
    routes.executor.execute(board.id, "org1")
    repair.assert_not_called()                                                # execute flag is off: permission only


# ---- notifications -----------------------------------------------------------------------------------------------

def _notifier(hour, tz="UTC", day=29):
    return build({"notifications": {"timezone": tz}}, now=lambda: datetime(2026, 10, day, hour, 0, tzinfo=UTC))[0]


def test_notice_outside_quiet_hours_is_ready_and_a_repeat_is_a_duplicate():
    svc = _notifier(12)
    rec, verdict = svc.notify(tenant_id="t", user_id="key:u1", key="k", priority="normal", text="hi", link="#/attention")
    assert verdict == "send_now" and rec.state == "ready" and rec.external is False and rec.channel == "dashboard"
    again, verdict2 = svc.notify(tenant_id="t", user_id="key:u1", key="k", priority="normal", text="hi")
    assert verdict2 == "duplicate" and again.notification_id == rec.notification_id


def test_quiet_hours_hold_normal_notices_for_the_digest_but_not_urgent_ones():
    svc = _notifier(23)
    held, v1 = svc.notify(tenant_id="t", user_id="key:u1", key="a", priority="normal", text="later")
    urgent, v2 = svc.notify(tenant_id="t", user_id="key:u1", key="b", priority="urgent", text="now")
    assert (v1, held.state) == ("held_for_digest", "held_for_digest") and (v2, urgent.state) == ("send_now", "ready")
    assert svc.digest(tenant_id="t", user_id="key:u1")["items"] == []          # still quiet: digest waits
    svc.now = lambda: datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    items = svc.digest(tenant_id="t", user_id="key:u1")["items"]
    assert [i["key"] for i in items] == ["a"]
    assert svc.digest(tenant_id="t", user_id="key:u1")["items"] == []          # collected once


def test_quiet_hours_use_the_users_timezone():
    # 06:00 UTC is 23:00 the previous evening in Los Angeles (quiet); 12:00 UTC is 05:00 there (also quiet);
    # 18:00 UTC is 11:00 there (not quiet).
    quiet = _notifier(6, "America/Los_Angeles")
    assert quiet.notify(tenant_id="t", user_id="key:u1", key="x", priority="normal", text="t")[1] == "held_for_digest"
    loud = _notifier(18, "America/Los_Angeles")
    assert loud.notify(tenant_id="t", user_id="key:u1", key="x", priority="normal", text="t")[1] == "send_now"


def test_dedupe_is_per_user_and_survives_a_restart():
    svc = _notifier(12)
    svc.notify(tenant_id="t", user_id="key:u1", key="same", priority="normal", text="a")
    assert svc.notify(tenant_id="t", user_id="key:u2", key="same", priority="normal", text="a")[1] == "send_now"
    fresh = AssistantStore()
    fresh.restore(svc.store.snapshot())
    svc.store = fresh
    assert svc.notify(tenant_id="t", user_id="key:u1", key="same", priority="normal", text="a")[1] == "duplicate"
    assert svc.list_notifications(tenant_id="t", user_id="key:u2")[0].user_id == "key:u2"


def test_notifications_never_leave_the_process(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an external connection was attempted")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    svc = _notifier(12)
    svc._transport, svc._shell_runner = Mock(), Mock()
    svc.notify(tenant_id="t", user_id="key:u1", key="k", priority="urgent", text="CI failed", link="#/attention")
    svc.digest(tenant_id="t", user_id="key:u1")
    svc._transport.assert_not_called()
    svc._shell_runner.assert_not_called()
    with pytest.raises(Exception, match="dashboard links"):
        svc.notify(tenant_id="t", user_id="key:u1", key="k2", priority="normal", text="x", link="https://evil.example")


def test_one_user_cannot_read_or_acknowledge_anothers_notifications():
    svc = _notifier(12)
    rec, _ = svc.notify(tenant_id="t", user_id="key:u1", key="k", priority="normal", text="private")
    assert svc.list_notifications(tenant_id="t", user_id="key:u2") == []
    with pytest.raises(AssistantNotFound):
        svc.ack_notification(rec.notification_id, tenant_id="t", user_id="key:u2")
    assert svc.ack_notification(rec.notification_id, tenant_id="t", user_id="key:u1").state == "acknowledged"
