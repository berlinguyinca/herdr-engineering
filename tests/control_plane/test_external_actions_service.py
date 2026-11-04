"""Slice 5 of docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md: the approval ledger and the executor.

A real ``AssistantService`` whose store is the real ``pa_store_snapshot`` table of a private Postgres, the product's own GitHub client
and Gmail draft writer over a real socket to the CONTRACT-LEVEL fake servers (``external_fakes.py``: not GitHub, not Gmail). The only
fault seam is a provider subclass that kills the thread (a BaseException) between the real provider call and the result being
written, which is what a process crash looks like to the ledger."""
from __future__ import annotations

import json
import os

import psycopg2
import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane.app_credentials import CredentialStore, SecretBox
from herdr_engineering.control_plane.assistant import (
    AssistantError,
    AssistantForbidden,
    AssistantNotFound,
    AssistantService,
    RateLimited,
    assistant_config,
)
from herdr_engineering.control_plane.assistant_briefs import TeamBriefRecord
from herdr_engineering.control_plane.external_actions import (
    DRAFT,
    ISSUE,
    GitHubIssues,
    GmailDrafts,
    action_key,
    marker_for,
)
from herdr_engineering.control_plane.google_oauth import SCOPES
from tests.control_plane import brief_fixtures as fx
from tests.control_plane.external_fakes import FakeGitHubServer, FakeGmailServer
from tests.control_plane.wg_pg import wg, wg_pg  # noqa: F401

REPO = "example-org/team-repo"
T, U = "default", "key:op"
BRIEF_KEY = "b" * 64
BRIEF_TEXT = "Email brief for the team.\nTopic: billing\nSummary: Invoice 7731 payment overdue. @someone see #12 ![x](http://a/b.png)"
REFERENCE = "bref-" + "1" * 24


class Crash(BaseException):
    """Stands for the process dying: nothing in the product catches it."""


def make_service(dsn, **ext):
    section = {"enabled": True, "persistence": {"dsn": dsn},
               "actions": {"approvers": ["key:approver"], "collab_users": {U: "alice"}},
               "external_actions": {"enabled": True, "github": {"labels": ["from-email", "bug"]}, **ext}}
    return AssistantService(config=assistant_config({"personal_assistant": section}))


class Env:
    def __init__(self, dsn, **ext):
        self.dsn, self.ext = dsn, ext
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM pa_store_snapshot")
        self.gh = FakeGitHubServer()
        self.gh.add_repo(REPO)
        self.gm = FakeGmailServer()
        self.tmp_key = None
        self.svc = None
        self.boot(first=True)

    def boot(self, first=False):
        """(Re)start the assistant on the SAME database, with the providers pointed at the fakes."""
        self.svc = make_service(self.dsn, **self.ext)
        if first:
            self.svc.create_assistant(tenant_id=T, user_id=U, name="Mail")
        self.wire()
        return self.svc

    def wire(self, github=None):
        import tempfile
        d = tempfile.mkdtemp()
        key = os.path.join(d, "key")
        open(key, "wb").write(os.urandom(32))
        store = CredentialStore(SecretBox(key))
        store.put_client("google", "cid.apps.googleusercontent.com", "client-secret")
        store.put_connection(T, U, "google", account="op@x.org", scopes=list(SCOPES), refresh_token="refresh-u1")
        self.workspace = gw.GoogleWorkspace(store, blocked_fn=lambda t, u: [], opener=self.gm.opener())
        ea = self.svc.external_actions
        token = self.gh.token                                           # the client holds the token it was configured with
        ea.github = github or GitHubIssues(self.gh.base, lambda repo: token, timeout=3)
        ea.gmail = GmailDrafts(lambda: self.workspace)

    @property
    def ea(self):
        return self.svc.external_actions

    def issue_ctx(self, **over):
        return {"brief_key": BRIEF_KEY, "repo": REPO, "default_title": "Invoice 7731 payment overdue", "brief_text": BRIEF_TEXT,
                "room_id": "room_1", "team_id": "team-" + "c" * 20, "suggestion_id": "suggestion_1", **over}

    def file_issue(self, by="agent:issue-writer", **over):
        return self.ea.file(tenant_id=T, user_id=U, kind=ISSUE, ctx=self.issue_ctx(**over), requested_by=by)

    def brief_record(self, **private):
        rec = TeamBriefRecord(reference=REFERENCE, brief_key=BRIEF_KEY, tenant_id=T, user_id=U, topic="billing", suggestion_id="suggestion_1",
                              private={"message_id": fx.MESSAGE_ID, "thread_id": "18c0ffee7a11ed02", "sender": fx.SENDER_ADDRESS,
                                       "subject": "Invoice 7731 payment overdue", "message_id_header": fx.HEADER_ID, **private})
        self.svc.store.put_team_brief_required(rec)
        return rec

    def file_draft(self, body="Thanks, we are paying on Friday.", **over):
        self.brief_record()
        ctx = {"brief_key": BRIEF_KEY, "reference": REFERENCE, "suggestion_id": "suggestion_1", "body": body,
               "room_id": "room_1", "team_id": "team-" + "c" * 20, **over}
        return self.ea.file(tenant_id=T, user_id=U, kind=DRAFT, ctx=ctx, requested_by="assistant:" + U)

    def approve(self, filed, user=U, **kw):
        return self.ea.decide(filed["action_id"], tenant_id=T, user_id=U, actor=user, decision="approved",
                              expected_sha256=kw.get("sha", filed["sha256"]))

    def close(self):
        self.gh.close()
        self.gm.close()


@pytest.fixture
def env(wg_pg):
    e = Env(wg_pg["dsn"])
    try:
        yield e
    finally:
        e.close()


def audit_actions(e):
    return [a["action"] for a in e.svc.store.audit]


# ------------------------------------------------------------------------------------------------------ flag and config
def test_with_the_flag_off_the_service_does_not_exist_and_the_old_queue_is_unchanged(wg_pg):
    for ext in ({"enabled": False}, {"kill_switch": True}):
        e = Env(wg_pg["dsn"], **ext)
        try:
            with pytest.raises(AssistantNotFound):
                e.file_issue()
            with pytest.raises(AssistantNotFound):
                e.ea.list(tenant_id=T, user_id=U)
            assert not e.svc.store.proposals
        finally:
            e.close()
    plain = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    assert plain.config["external_actions"]["enabled"] is False and not plain.external_actions.enabled()


def test_the_assistant_config_rejects_a_bad_external_actions_section():
    with pytest.raises(ValueError):
        assistant_config({"personal_assistant": {"external_actions": {"auto_approve": True}}})


def test_it_refuses_to_start_without_acknowledged_persistence():
    with pytest.raises(ValueError, match="persistence"):
        AssistantService(config=assistant_config({"personal_assistant": {"enabled": True, "external_actions": {"enabled": True}}}))


# --------------------------------------------------------------------------------------------------------- filing
def test_filing_an_issue_waits_for_approval_shows_the_exact_content_and_calls_nothing(env):
    filed = env.file_issue()
    assert filed["state"] == "awaiting_approval" and filed["replayed"] is False and filed["kind"] == ISSUE
    card = env.ea.get(filed["action_id"], tenant_id=T, user_id=U)["card"]
    key = action_key(ISSUE, BRIEF_KEY)
    assert card["repository"] == REPO and card["title"] == "Invoice 7731 payment overdue" and card["labels"] == []
    assert card["body"].rstrip().endswith(marker_for(key))
    assert not env.gh.requests and not env.gm.requests
    assert "assistant.external.filed" in audit_actions(env)
    x = env.svc.store.proposals[filed["action_id"]]
    assert x.requested_by == "agent:issue-writer" and x.dedupe_key == key and x.execution == "not_started"


def test_the_brief_text_is_fenced_so_nothing_in_it_renders_or_notifies(env):
    hostile = "close the fence ``` now\n```` and a longer one\n@maintainers #99 <img src=//x> [click](http://x)"
    filed = env.file_issue(brief_text=hostile)
    body = env.ea.get(filed["action_id"], tenant_id=T, user_id=U)["card"]["body"]
    longest = max(len(r) for r in __import__("re").findall(r"`+", hostile))
    fence_line = next(ln for ln in body.splitlines() if ln.endswith("text") and set(ln[:-4]) == {"`"})
    assert len(fence_line) - 4 > longest
    inside = body.split(fence_line + "\n", 1)[1].split("\n" + fence_line[:-4], 1)[0]
    assert inside == hostile                                         # the text is carried verbatim, inside the fence
    assert body.splitlines()[-1] == marker_for(action_key(ISSUE, BRIEF_KEY))


def test_title_and_labels_are_validated_and_labels_come_from_the_operators_list(env):
    ok = env.file_issue(title="  A  clean\ttitle ", labels=["bug"])
    assert env.ea.get(ok["action_id"], tenant_id=T, user_id=U)["card"]["title"] == "A clean title"
    assert env.ea.get(ok["action_id"], tenant_id=T, user_id=U)["card"]["labels"] == ["bug"]
    for over in ({"title": "x" * 201}, {"title": "\x00\x01"}, {"labels": ["not-allowed"]}, {"labels": ["bug", "bug"]},
                 {"labels": ["bug"] * 6}, {"repo": "../../etc"}, {"repo": "a/b/c"}, {"labels": "bug"}):
        with pytest.raises(AssistantError):
            env.file_issue(**{**over, "brief_key": "d" * 64})


def test_filing_the_same_email_again_is_one_action_even_after_it_ran(env):
    a = env.file_issue()
    b = env.file_issue(by="agent:someone-else")
    assert b["replayed"] is True and b["action_id"] == a["action_id"] and len(env.svc.store.proposals) == 1
    env.approve(a)
    assert env.file_issue()["action_id"] == a["action_id"]
    assert len(env.svc.store.proposals) == 1 and len(env.gh.issues(REPO)) == 1


def test_a_rejected_action_may_be_filed_again_but_only_one_can_ever_run(env):
    a = env.file_issue()
    r = env.ea.decide(a["action_id"], tenant_id=T, user_id=U, actor=U, decision="rejected", expected_sha256=None)
    assert r["state"] == "rejected"
    assert env.ea.decide(a["action_id"], tenant_id=T, user_id=U, actor=U, decision="rejected", expected_sha256=None)["replayed"] is True
    with pytest.raises(AssistantError):
        env.approve(a)                                               # rejected is terminal
    b = env.file_issue()
    assert b["action_id"] != a["action_id"] and b["replayed"] is False
    env.approve(b)
    assert len(env.gh.issues(REPO)) == 1


def test_an_expired_action_that_never_ran_may_be_filed_again(env):
    a = env.file_issue()
    x = env.svc.store.proposals[a["action_id"]]
    from datetime import timedelta
    x.expires_at = env.svc.now() - timedelta(seconds=1)
    assert env.ea.get(a["action_id"], tenant_id=T, user_id=U)["state"] == "expired"
    with pytest.raises(AssistantError):
        env.approve(a)
    b = env.file_issue()
    assert b["action_id"] != a["action_id"]


def test_the_hourly_filing_cap_holds(wg_pg):
    e = Env(wg_pg["dsn"], actions_per_hour=2)
    try:
        e.file_issue(brief_key="1" * 64)
        e.file_issue(brief_key="2" * 64)
        with pytest.raises(RateLimited):
            e.file_issue(brief_key="3" * 64)
        assert len(e.svc.store.proposals) == 2
    finally:
        e.close()


# ------------------------------------------------------------------------------------------- approving: who and what
def test_approval_is_bound_to_the_content_hash(env):
    a = env.file_issue()
    for sha in (None, "", "0" * 64, a["sha256"][:-1] + ("1" if a["sha256"][-1] != "1" else "2")):
        with pytest.raises(Exception) as exc:
            env.approve(a, sha=sha)
        assert getattr(exc.value, "status", None) == 409
    assert not env.gh.requests and env.ea.get(a["action_id"], tenant_id=T, user_id=U)["state"] == "awaiting_approval"


def test_approving_creates_one_issue_and_replaying_the_approval_creates_none(env):
    a = env.file_issue(labels=["from-email"])
    out = env.approve(a)
    assert out["state"] == "executed" and out["replayed"] is False and out["result"]["ref"] == f"https://github.com/{REPO}/issues/1"
    [issue] = env.gh.issues(REPO)
    assert issue["title"] == "Invoice 7731 payment overdue" and issue["labels"] == [{"name": "from-email"}]
    card = env.ea.get(a["action_id"], tenant_id=T, user_id=U)["card"]
    assert issue["body"] == card["body"]                              # what was shown is exactly what was sent
    again = env.approve(a)
    assert again["replayed"] is True and again["state"] == "executed" and len(env.gh.issues(REPO)) == 1
    with pytest.raises(AssistantError):
        env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=a["sha256"])   # an executed action is not retried
    assert len(env.gh.issues(REPO)) == 1
    acts = audit_actions(env)
    assert acts.count("assistant.external.executed") == 1 and acts.count("assistant.external.executing") == 1


def test_only_the_owner_decides_not_an_approver_nor_the_filer_nor_a_stranger(env):
    a = env.file_issue(by="agent:coord")
    for who in ("key:approver", "agent:coord", "key:stranger"):
        with pytest.raises(AssistantForbidden):
            env.ea.decide(a["action_id"], tenant_id=T, user_id=U, actor=who, decision="approved", expected_sha256=a["sha256"])
        with pytest.raises(AssistantForbidden):
            env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=who, expected_sha256=a["sha256"])
    assert audit_actions(env).count("assistant.external.denied") == 6
    assert not env.gh.requests
    # an approver of the OLD queue sees nothing of these kinds, whatever the config says
    approver_view = env.svc.list_proposals(tenant_id=T, user_id="key:approver")
    assert all(x.kind not in (ISSUE, DRAFT) for x in approver_view)
    with pytest.raises(AssistantNotFound):
        env.ea.get(a["action_id"], tenant_id=T, user_id="key:approver")
    with pytest.raises(AssistantNotFound):
        env.ea.decide(a["action_id"], tenant_id=T, user_id="key:approver", actor="key:approver", decision="approved",
                      expected_sha256=a["sha256"])


def test_the_old_proposal_queue_cannot_decide_or_execute_these_kinds(env):
    a = env.file_issue()
    with pytest.raises(AssistantError, match="external"):
        env.svc.decide_proposal(a["action_id"], tenant_id=T, user_id=U, decision="approved", actor=U)
    with pytest.raises((AssistantError, AssistantForbidden)):
        env.svc.execute_approved_proposal(a["action_id"], tenant_id=T, user_id=U, actor=U)
    with pytest.raises(AssistantError):
        env.svc.preview_proposal(a["action_id"], tenant_id=T, user_id="key:approver")
    assert not env.gh.requests


# ------------------------------------------------------------------------------- exactly once, failures, retry
def test_provider_down_is_unknown_visible_and_retried_only_by_the_operator(env):
    a = env.file_issue()
    env.gh.down = True
    out = env.approve(a)
    assert out["state"] == "unknown" and out["failure"] == {"outcome": "unknown", "reason": "provider_unavailable"}
    assert not env.gh.issues(REPO)
    env.gh.down = False
    assert env.ea.get(a["action_id"], tenant_id=T, user_id=U)["state"] == "unknown"       # nothing retries by itself
    with pytest.raises(AssistantForbidden):
        env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor="agent:x", expected_sha256=a["sha256"])
    with pytest.raises(Exception) as exc:
        env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256="0" * 64)
    assert exc.value.status == 409
    out = env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=a["sha256"])
    assert out["state"] == "executed" and len(env.gh.issues(REPO)) == 1 and out["attempts"] == 2


def test_an_answer_lost_after_the_issue_was_created_is_reconciled_not_duplicated(env):
    a = env.file_issue()
    env.gh.drop_after_create = True
    out = env.approve(a)
    assert out["state"] == "unknown" and out["failure"]["reason"] == "no_answer"
    assert len(env.gh.issues(REPO)) == 1                              # GitHub did create it; we never heard
    out = env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=a["sha256"])
    assert out["state"] == "executed" and out["result"]["ref"] == f"https://github.com/{REPO}/issues/1"
    assert len(env.gh.issues(REPO)) == 1 and env.gh.count("POST", f"/repos/{REPO}/issues") == 1
    assert "assistant.external.executed" in audit_actions(env)


@pytest.mark.parametrize("fault, reason", [(lambda g: setattr(g, "token", "rotated"), "credential_rejected"),
                                           (lambda g: g.fail_next(404), "repository_not_found"),
                                           (lambda g: g.fail_next(403), "forbidden"),
                                           (lambda g: g.fail_next(422), "invalid_issue")])
def test_a_refusal_is_failed_and_visible_and_a_retry_after_the_fix_works(env, fault, reason):
    a = env.file_issue()
    good = env.gh.token
    fault(env.gh)
    out = env.approve(a)
    assert out["state"] == "failed" and out["failure"] == {"outcome": "failed", "reason": reason}
    env.gh.token = good
    env.gh._fail.clear()
    # a definite refusal never ran a create: the retry (attempt 2) reconciles first, finds nothing, creates once
    out = env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=a["sha256"])
    assert out["state"] == "executed" and len(env.gh.issues(REPO)) == 1


def test_a_crash_after_the_create_is_healed_by_recovery_without_a_second_issue(env):
    class CrashAfterCreate(GitHubIssues):
        def create(self, *args, **kw):
            super().create(*args, **kw)
            raise Crash

    env.ea.github = CrashAfterCreate(env.gh.base, lambda r: env.gh.token, timeout=3)
    a = env.file_issue()
    with pytest.raises(Crash):
        env.approve(a)
    assert len(env.gh.issues(REPO)) == 1
    svc = env.boot()                                                  # a restart: the same database, a new process
    x = svc.store.proposals[a["action_id"]]
    assert x.execution == "executing" and x.status == "approved"      # the intent was durable
    assert svc.external_actions.get(a["action_id"], tenant_id=T, user_id=U)["state"] == "executing"
    assert svc.external_actions.recover() == {"adopted": 1, "unknown": 0}
    out = svc.external_actions.get(a["action_id"], tenant_id=T, user_id=U)
    assert out["state"] == "executed" and out["result"]["ref"].endswith("/issues/1")
    assert len(env.gh.issues(REPO)) == 1 and env.gh.count("POST", f"/repos/{REPO}/issues") == 1


def test_a_crash_before_the_create_leaves_unknown_and_the_retry_creates_exactly_one(env):
    class CrashBeforeCreate(GitHubIssues):
        def create(self, *args, **kw):
            raise Crash

    env.ea.github = CrashBeforeCreate(env.gh.base, lambda r: env.gh.token, timeout=3)
    a = env.file_issue()
    with pytest.raises(Crash):
        env.approve(a)
    svc = env.boot()
    assert svc.external_actions.recover() == {"adopted": 0, "unknown": 1}
    ea = svc.external_actions
    assert ea.get(a["action_id"], tenant_id=T, user_id=U)["failure"]["reason"] == "interrupted"
    assert not env.gh.issues(REPO)
    out = ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=a["sha256"])
    assert out["state"] == "executed" and len(env.gh.issues(REPO)) == 1


def test_a_stale_executing_action_shows_as_unknown_and_may_be_retried(env):
    class Hang(GitHubIssues):
        def create(self, *args, **kw):
            raise Crash

    env.ea.github = Hang(env.gh.base, lambda r: env.gh.token, timeout=3)
    a = env.file_issue()
    with pytest.raises(Crash):
        env.approve(a)
    from datetime import timedelta
    assert env.ea.get(a["action_id"], tenant_id=T, user_id=U)["state"] == "executing"
    with pytest.raises(AssistantError):
        env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=a["sha256"])      # still in its window
    env.ea.github = GitHubIssues(env.gh.base, lambda r: env.gh.token, timeout=3)
    now = env.svc.now()
    env.svc.now = lambda: now + timedelta(seconds=env.ea.cfg["intent_stale_seconds"] + 5)
    assert env.ea.get(a["action_id"], tenant_id=T, user_id=U)["state"] == "unknown"
    out = env.ea.retry(a["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=a["sha256"])
    assert out["state"] == "executed" and len(env.gh.issues(REPO)) == 1


def test_the_execution_cap_stops_a_second_run_in_the_hour_and_leaves_it_approved(wg_pg):
    e = Env(wg_pg["dsn"], executions_per_hour=1)
    try:
        a = e.file_issue(brief_key="1" * 64)
        b = e.file_issue(brief_key="2" * 64)
        e.approve(a)
        with pytest.raises(RateLimited):
            e.approve(b)
        assert e.ea.get(b["action_id"], tenant_id=T, user_id=U)["state"] == "approved"
        assert len(e.gh.issues(REPO)) == 1
    finally:
        e.close()


def test_the_pending_and_decided_state_survives_a_restart(env):
    a = env.file_issue()
    svc = env.boot()
    assert svc.external_actions.get(a["action_id"], tenant_id=T, user_id=U)["state"] == "awaiting_approval"
    out = svc.external_actions.decide(a["action_id"], tenant_id=T, user_id=U, actor=U, decision="approved", expected_sha256=a["sha256"])
    assert out["state"] == "executed"
    svc = env.boot()
    assert svc.external_actions.file(tenant_id=T, user_id=U, kind=ISSUE, ctx=env.issue_ctx(), requested_by="agent:x")["replayed"] is True
    assert len(env.gh.issues(REPO)) == 1


def test_no_credential_ever_reaches_the_ledger_the_audit_or_a_view(env):
    a = env.file_issue()
    env.approve(a)
    d = env.file_draft()
    env.approve(d)
    blob = json.dumps(env.svc.store.snapshot(), default=str) + json.dumps(env.svc.store.audit, default=str)
    blob += json.dumps(env.ea.list(tenant_id=T, user_id=U), default=str)
    for secret in (env.gh.token, env.gm.access_token, "refresh-u1", "client-secret"):
        assert secret not in blob
    with psycopg2.connect(env.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT data::text FROM pa_store_snapshot")
        stored = "".join(r[0] for r in cur.fetchall())
    for secret in (env.gh.token, env.gm.access_token, "refresh-u1"):
        assert secret not in stored


# ------------------------------------------------------------------------------------------------------ email drafts
def test_a_reply_draft_goes_to_the_original_sender_only_and_is_a_draft_never_a_send(env):
    d = env.file_draft()
    card = env.ea.get(d["action_id"], tenant_id=T, user_id=U)["card"]
    assert card["to"] == [fx.SENDER_ADDRESS] and card["subject"] == "Re: Invoice 7731 payment overdue" and card["in_reply_to_thread"] is True
    assert not env.gm.requests
    out = env.approve(d)
    assert out["state"] == "executed" and out["result"]["ref"] == "r-1"
    [draft] = env.gm.drafts
    assert draft["headers"]["to"] == fx.SENDER_ADDRESS and draft["headers"]["in-reply-to"] == fx.HEADER_ID
    assert draft["headers"]["x-herdr-action-key"] == action_key(DRAFT, BRIEF_KEY) and draft["thread_id"] == "18c0ffee7a11ed02"
    assert "Thanks, we are paying on Friday." in draft["body"] and action_key(DRAFT, BRIEF_KEY) not in draft["body"]
    assert env.gm.sent == [] and env.gm.count("POST", "/gmail/v1/users/me/messages/send") == 0
    assert env.approve(d)["replayed"] is True and len(env.gm.drafts) == 1
    assert env.file_draft()["action_id"] == d["action_id"]


def test_a_draft_cannot_be_addressed_by_the_filer_or_filed_from_someone_elses_brief(env):
    env.brief_record()
    base = {"brief_key": BRIEF_KEY, "reference": REFERENCE, "suggestion_id": "suggestion_1", "body": "ok", "room_id": "r", "team_id": "t"}
    for over in ({"reference": "bref-" + "9" * 24}, {"suggestion_id": "suggestion_other"}, {"brief_key": "e" * 64},
                 {"body": "x" * 801}, {"body": ""}, {"body": "my password: hunter2hunter2 ok"}, {"body": "a\x07b"}):
        with pytest.raises((AssistantError, AssistantNotFound)):
            env.ea.file(tenant_id=T, user_id=U, kind=DRAFT, ctx={**base, **over}, requested_by="assistant:" + U)
    with pytest.raises(AssistantNotFound):                              # another person's brief reference
        env.ea.file(tenant_id="default", user_id="key:other", kind=DRAFT, ctx=base, requested_by="x")
    assert not env.svc.store.proposals


def test_a_revoked_google_connection_fails_visibly_and_the_retry_after_reconnecting_adds_one_draft(env):
    d = env.file_draft()
    env.gm.revoked = True
    out = env.approve(d)
    assert out["state"] == "failed" and out["failure"]["reason"] == "connection_needs_attention" and not env.gm.drafts
    env.gm.revoked = False
    env.workspace.invalidate_all()
    env.workspace._store.set_status(T, U, "google", "connected")
    out = env.ea.retry(d["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=d["sha256"])
    assert out["state"] == "executed" and len(env.gm.drafts) == 1 and env.gm.sent == []


def test_a_draft_whose_answer_was_lost_is_found_by_its_header_and_not_duplicated(env):
    d = env.file_draft()
    env.gm.drop_after_create = True
    out = env.approve(d)
    assert out["state"] == "unknown" and len(env.gm.drafts) == 1
    out = env.ea.retry(d["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=d["sha256"])
    assert out["state"] == "executed" and out["result"]["ref"] == "r-1" and len(env.gm.drafts) == 1
    assert env.gm.count("POST", "/gmail/v1/users/me/drafts") == 1


def test_gmail_down_leaves_the_action_unknown_and_nothing_is_sent(env):
    d = env.file_draft()
    env.gm.down = True
    out = env.approve(d)
    assert out["state"] == "unknown" and out["failure"]["reason"] == "provider_unavailable"
    env.gm.down = False
    assert env.ea.retry(d["action_id"], tenant_id=T, user_id=U, actor=U, expected_sha256=d["sha256"])["state"] == "executed"
    assert len(env.gm.drafts) == 1 and env.gm.sent == []


# ---------------------------------------------------------------------------------------------------- room views
def test_a_filers_view_has_no_content_and_the_owner_view_has_the_card(env):
    a = env.file_issue(by="agent:issue-writer")
    env.file_draft()
    mine = env.ea.for_room("room_1", tenant_id=T, user_id=U, filed_by="agent:issue-writer")
    assert [r["action_id"] for r in mine] == [a["action_id"]] and set(mine[0]) == {"action_id", "kind", "state", "created_at", "team_id"}
    owner = env.ea.for_room("room_1", tenant_id=T, user_id=U)
    assert len(owner) == 2 and all("card" in r for r in owner)
    assert env.ea.for_room("other_room", tenant_id=T, user_id=U) == []
