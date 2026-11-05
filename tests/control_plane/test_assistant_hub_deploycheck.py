"""Independent deploy outcome check: the hub itself polls ``/api/v1/health`` after it authorized a deploy.

Real stores, a real local health listener whose reported commit changes during the test (and that can go down and come
back on the same port), the real ``HealthClient``, the real Slack listener and a genuinely restarted runtime over a
restored snapshot.  No fakes.  Time is passed in as ``now`` so no test sleeps against a guessed duration."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering.control_plane.assistant import AssistantStore, assistant_config
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction
from herdr_engineering.control_plane.assistant_hub.deploy import DeployMatch, match_deploy
from herdr_engineering.control_plane.assistant_hub.deploycheck import (
    DeployCheck,
    describe_status,
    open_check,
    record_exit,
    verify_deploy,
)
from herdr_engineering.control_plane.assistant_hub.policy import record_owner_decision
from herdr_engineering.control_plane.assistant_hub.preconditions import (
    HealthClient,
    PreconditionClients,
)
from herdr_engineering.control_plane.assistant_hub.slack import SlackRuntime
from tests.control_plane.test_assistant_hub_action_request import NOW as HUB_NOW
from tests.control_plane.test_assistant_hub_deploy_grants import CMD, REPO, SHA, SHA2
from tests.control_plane.test_assistant_hub_deploy_grants import worker as mk_worker
from tests.control_plane.test_assistant_hub_preconditions import OLD, DeployWorld, Stack
from tests.control_plane.test_assistant_hub_preconditions import deploy_cfg as stack_deploy_cfg
from tests.control_plane.test_assistant_hub_slack_summary import SumWorld

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
T, U = "acme", "owner"


class HealthListener:
    """A real HTTP server answering ``GET /api/v1/health?probe=1``; ``commit`` is what it reports.  ``down`` closes the
    socket (the hub's container is being replaced), ``up`` listens again on the very same port."""

    def __init__(self) -> None:
        self.commit: object = OLD
        self.hits = 0
        self.srv: ThreadingHTTPServer | None = None
        self.port = 0
        self.up()

    def up(self) -> None:
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits += 1
                body = json.dumps({"status": "ok", "version": "1", "commit": outer.commit}).encode()
                self.send_response(200 if self.path == "/api/v1/health?probe=1" else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: object) -> None:
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def down(self) -> None:
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1/health"


@pytest.fixture
def hl():
    listener = HealthListener()
    try:
        yield listener
    finally:
        listener.down()


def cfg_for(url: str, **extra) -> dict:
    return assistant_config({"personal_assistant": {"deploy": {**extra, "projects": {"herdr-engineering": {
        "repo": REPO, "scopes": {"control-plane": {"command": CMD, "mode": "auto", "health_url": url,
                                                   "require": ["on_main"]}}}}}}})["deploy"]


def match_for(cfg: dict, sha: str = SHA) -> DeployMatch:
    m = match_deploy(RawAction(tool="Bash", command=f"{CMD} --ref {sha}", repo=REPO), cfg)
    assert m is not None
    return m


def opened(store: AssistantStore, cfg: dict, *, sha: str = SHA, who=None, task="t1", now: datetime = T0) -> DeployCheck:
    return open_check(store, cfg, who or mk_worker(), match_for(cfg, sha), task, "", now)


# ------------------------------------------------------------------------------------------------ verify_deploy
def test_healthy_when_health_reports_the_target_commit(hl):
    store, cfg = AssistantStore(), cfg_for(hl.url)
    check = opened(store, cfg)
    assert check.status == "awaiting" and check.deadline == T0 + timedelta(minutes=20)
    assert check.health_url == hl.url and check.window_ends == T0 + timedelta(minutes=10)
    health = HealthClient(timeout=5)
    still = verify_deploy(store, check, health, T0 + timedelta(minutes=1))
    assert still.status == "awaiting" and still.seen_commit == OLD[:12]            # the old version is still serving
    hl.commit = SHA
    done = verify_deploy(store, check, health, T0 + timedelta(minutes=2))
    assert done.status == "healthy" and done.finished_at == T0 + timedelta(minutes=2)
    assert store.get_deploy_check(check.check_id, tenant_id=T, user_id=U).status == "healthy"
    (audit,) = [e for e in store.audit if e["action"] == "assistant.deploy.outcome"]
    assert "status=healthy" in audit["detail"] and SHA not in audit["detail"]
    hl.commit = "9" * 40                                        # a final check never changes again
    assert verify_deploy(store, check, health, T0 + timedelta(minutes=3)).status == "healthy"


def test_a_prefix_of_at_least_twelve_characters_counts_but_a_short_one_does_not(hl):
    store, cfg = AssistantStore(), cfg_for(hl.url)
    check = opened(store, cfg)
    health = HealthClient(timeout=5)
    hl.commit = SHA[:8]
    assert verify_deploy(store, check, health, T0 + timedelta(minutes=1)).status == "awaiting"
    hl.commit = SHA[:12]
    assert verify_deploy(store, check, health, T0 + timedelta(minutes=2)).status == "healthy"


def test_rolled_back_only_after_the_deploy_window(hl):
    store, cfg = AssistantStore(), cfg_for(hl.url)
    check = opened(store, cfg)
    health = HealthClient(timeout=5)
    hl.commit = OLD                                             # the previous version is (still or again) serving
    assert verify_deploy(store, check, health, T0 + timedelta(minutes=9)).status == "awaiting"
    done = verify_deploy(store, check, health, T0 + timedelta(minutes=11))
    assert done.status == "rolled_back" and done.seen_commit == OLD[:12]
    assert describe_status(done).startswith("rolled back")


def test_deadline_without_a_confirmed_result_is_unconfirmed(hl):
    store, cfg = AssistantStore(), cfg_for(hl.url)
    health = HealthClient(timeout=5)
    # reachable but nonsense: not a commit at all
    check = opened(store, cfg)
    hl.commit = "no-such-thing"
    assert verify_deploy(store, check, health, T0 + timedelta(minutes=19)).status == "awaiting"
    done = verify_deploy(store, check, health, T0 + timedelta(minutes=20))
    assert done.status == "unconfirmed" and done.last_error == "other_commit"
    assert "unconfirmed" in describe_status(done) and "success" not in describe_status(done)
    # unreachable the whole time
    other = opened(store, cfg, sha=SHA2)
    hl.down()
    assert verify_deploy(store, other, health, T0 + timedelta(minutes=5)).last_error == "unreachable"
    gone = verify_deploy(store, other, health, T0 + timedelta(minutes=21))
    assert gone.status == "unconfirmed" and gone.last_error == "unreachable"


def test_listener_down_for_a_while_then_up_is_still_healthy(hl):
    store, cfg = AssistantStore(), cfg_for(hl.url)
    check = opened(store, cfg)
    health = HealthClient(timeout=5)
    hl.down()
    for minute in (1, 2, 3):                                    # the hub's own container is being replaced
        step = verify_deploy(store, check, health, T0 + timedelta(minutes=minute))
        assert step.status == "awaiting" and step.last_error == "unreachable"
    hl.commit = SHA
    hl.up()                                                     # same port, new version
    done = verify_deploy(store, check, health, T0 + timedelta(minutes=4))
    assert done.status == "healthy" and done.last_error == ""


def test_no_health_url_can_never_be_healthy(hl):
    store = AssistantStore()
    cfg = assistant_config({"personal_assistant": {"deploy": {"projects": {"herdr-engineering": {
        "repo": REPO, "scopes": {"control-plane": {"command": CMD, "require": ["on_main"]}}}}}}})["deploy"]
    check = opened(store, cfg)
    assert check.health_url == "" and check.last_error == "no_health_url"
    done = verify_deploy(store, check, HealthClient(timeout=5), T0 + timedelta(minutes=21))
    assert done.status == "unconfirmed" and done.last_error == "no_health_url"


# ------------------------------------------------------------------------------------------------ config
def test_deadline_comes_from_deploy_check_minutes(hl):
    store = AssistantStore()
    check = opened(store, cfg_for(hl.url, deploy_check_minutes=5, deploy_window_minutes=2))
    assert check.deadline == T0 + timedelta(minutes=5) and check.window_ends == T0 + timedelta(minutes=2)
    assert cfg_for(hl.url)["projects"]
    assert "deploy_check_minutes" not in cfg_for(hl.url)               # default 20 lives in the checker


@pytest.mark.parametrize("name,bad", [("deploy_check_minutes", 0), ("deploy_check_minutes", 241),
                                      ("deploy_check_minutes", True), ("deploy_check_minutes", "20"),
                                      ("deploy_window_minutes", 0), ("deploy_window_minutes", 1.5)])
def test_check_minutes_are_validated(hl, name, bad):
    with pytest.raises(ValueError, match=name):
        cfg_for(hl.url, **{name: bad})


def test_window_may_not_exceed_the_check_time(hl):
    with pytest.raises(ValueError, match="deploy_window_minutes"):
        cfg_for(hl.url, deploy_check_minutes=5, deploy_window_minutes=6)


# ------------------------------------------------------------------------------------------------ tenant scoping
def test_checks_are_tenant_and_user_scoped(hl):
    store, cfg = AssistantStore(), cfg_for(hl.url)
    mine = opened(store, cfg)
    theirs = opened(store, cfg, who=mk_worker("w2", tenant="other", user="u9"))
    assert store.get_deploy_check(mine.check_id, tenant_id="other", user_id="u9") is None
    assert store.get_deploy_check(mine.check_id, tenant_id=T, user_id="u9") is None
    assert [c.check_id for c in store.list_deploy_checks(T, U)] == [mine.check_id]
    assert [c.check_id for c in store.list_deploy_checks("other", "u9")] == [theirs.check_id]
    assert [c.check_id for c in store.list_deploy_checks(T, U, worker_id="w2")] == []
    # one tenant's exit-3 report can never touch another tenant's check
    assert record_exit(store, mk_worker("w1", tenant="third", user="u3"), "t1", 3, T0) is None
    assert store.get_deploy_check(mine.check_id, tenant_id=T, user_id=U).status == "awaiting"
    assert record_exit(store, mk_worker("w2", tenant=T, user=U), "t1", 3, T0) is None      # same tenant, other worker


def test_asking_again_for_the_same_open_deploy_keeps_one_check(hl):
    store, cfg = AssistantStore(), cfg_for(hl.url)
    first = opened(store, cfg)
    again = opened(store, cfg, now=T0 + timedelta(minutes=1))
    assert again.check_id == first.check_id and len(store.list_deploy_checks(T, U)) == 1
    assert opened(store, cfg, sha=SHA2).check_id != first.check_id


def test_old_snapshots_without_checks_restore(hl):
    store = AssistantStore()
    snap = store.snapshot()
    snap.pop("deploy_checks")
    AssistantStore().restore(snap)
    opened(store, cfg_for(hl.url))
    fresh = AssistantStore()
    fresh.restore(json.loads(json.dumps(store.snapshot(), default=str)))
    (c,) = fresh.list_deploy_checks(T, U)
    assert c.status == "awaiting" and c.deadline == T0 + timedelta(minutes=20)


# ------------------------------------------------------------------------------------------------ authorization hooks
@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def dwrld(tmp_path):
    w = DeployWorld(tmp_path)
    w.gate()
    w.stand()
    try:
        yield w
    finally:
        w.listener.close()


def test_an_authorized_deploy_opens_a_check_from_the_live_config(dwrld, stack):
    cfg = stack_deploy_cfg(stack)
    d = dwrld.go(stack, cfg)
    assert d.outcome == "authorized"
    (c,) = dwrld.store.list_deploy_checks("acme", "owner")
    assert (c.status, c.project, c.scope, c.sha, c.worker_id, c.task_id) == (
        "awaiting", "herdr-engineering", "control-plane", SHA, "w1", "t1")
    assert c.health_url == stack.health_url and c.request_id == "" and c.repo == REPO
    assert c.deadline == c.started_at + timedelta(minutes=20)
    assert dwrld.go(stack, cfg).outcome == "authorized"                 # the same deploy again: still one open check
    assert len(dwrld.store.list_deploy_checks("acme", "owner")) == 1


def test_a_one_shot_approved_deploy_opens_a_check_naming_the_request(tmp_path, stack):
    w = DeployWorld(tmp_path)
    try:
        cfg = stack_deploy_cfg(stack)
        first = w.go(stack, cfg)
        assert first.outcome == "pending" and w.store.list_deploy_checks("acme", "owner") == []   # nothing yet
        record_owner_decision(w.store, w.cfg, w.clock.now, "acme", "owner", first.request_id, True, "owner")
        again = w.go(stack, cfg)
        assert again.outcome == "authorized" and again.source == "owner"
        (c,) = w.store.list_deploy_checks("acme", "owner")
        assert c.request_id == first.request_id and c.status == "awaiting" and c.sha == SHA
    finally:
        w.listener.close()


def test_a_pending_or_denied_deploy_opens_no_check(dwrld, stack):
    stack.r.status_state = "failure"
    assert dwrld.go(stack, stack_deploy_cfg(stack)).outcome == "pending"
    assert dwrld.store.list_deploy_checks("acme", "owner") == []


# ------------------------------------------------------------------------------------------------ Slack side
def runtime_for(w: SumWorld, store: AssistantStore, **kw) -> SlackRuntime:
    return SlackRuntime(store, w.hub.service, lambda: w.cfg, w.clock, min_backoff=0.05, max_backoff=0.2,
                        ping_interval=0.3, reoffer_backoff=0.1, summary_interval=0.1, check_interval=0.05, **kw)


@pytest.fixture
def sw(tmp_path):
    w = SumWorld(tmp_path)
    w.worker("a")
    w.runtime.stop()
    w.runtime = runtime_for(w, w.store)
    w.hub = replace(w.hub, slack=w.runtime)
    w.runtime.start()
    try:
        yield w
    finally:
        w.close()


def outcome_posts(w: SumWorld) -> list[str]:
    return [str(p.get("text", "")) + " " + json.dumps(p.get("blocks", [])) for p in w.posts()
            if "eploy" in str(p.get("text", "")) or "URGENT" in str(p.get("text", ""))]


def test_healthy_outcome_is_posted_once_to_slack(sw, hl):
    cfg = cfg_for(hl.url)
    hl.commit = SHA
    check = open_check(sw.store, cfg, sw.auths["a"][0], match_for(cfg), "t1", "", HUB_NOW)
    assert sw.listener.wait_for(lambda: len(outcome_posts(sw)) == 1)
    assert sw.store.get_deploy_check(check.check_id, tenant_id="t1", user_id="u1").status == "healthy"
    time.sleep(0.3)
    (text,) = outcome_posts(sw)
    assert "Deploy healthy" in text and SHA[:12] in text and SHA not in text
    assert "Main could not be re-checked" in text                      # no GitHub client: said so, not assumed
    assert sw.store.get_deploy_check(check.check_id, tenant_id="t1", user_id="u1").notified is True
    assert [e for e in sw.store.audit if e["action"] == "assistant.slack.deploy_outcome_posted"]


def test_unconfirmed_and_rolled_back_are_worded_as_what_they_are(sw, hl):
    cfg = cfg_for(hl.url, deploy_check_minutes=2, deploy_window_minutes=1)
    hl.commit = "not-a-commit"
    c1 = open_check(sw.store, cfg, sw.auths["a"][0], match_for(cfg), "t1", "", HUB_NOW)
    sw.clock.now = HUB_NOW + timedelta(minutes=3)
    assert sw.listener.wait_for(lambda: len(outcome_posts(sw)) == 1)
    assert sw.store.get_deploy_check(c1.check_id, tenant_id="t1", user_id="u1").status == "unconfirmed"
    (text,) = outcome_posts(sw)
    assert "Deploy unconfirmed" in text and "Do not assume it worked" in text
    assert "success" not in text.lower() and "healthy" not in text.lower()
    hl.commit = OLD
    c2 = open_check(sw.store, cfg, sw.auths["a"][0], match_for(cfg, SHA2), "t1", "", sw.clock.now)
    sw.clock.now += timedelta(minutes=1, seconds=30)
    assert sw.listener.wait_for(lambda: len(outcome_posts(sw)) == 2)
    assert sw.store.get_deploy_check(c2.check_id, tenant_id="t1", user_id="u1").status == "rolled_back"
    assert "Deploy rolled back" in outcome_posts(sw)[1]


def test_main_is_re_verified_after_a_healthy_deploy(tmp_path, hl):
    stack = Stack(tmp_path)
    w = SumWorld(tmp_path)
    w.worker("a")
    w.runtime.stop()
    w.runtime = runtime_for(w, w.store, health=HealthClient(timeout=5),
                            preconditions=PreconditionClients(gh=stack.gh))
    w.runtime.start()
    try:
        cfg = cfg_for(hl.url)
        hl.commit = SHA
        stack.r.tip = "c" * 40
        stack.r.compare = "diverged"                                   # the deployed commit fell off main's history
        open_check(w.store, cfg, w.auths["a"][0], match_for(cfg), "t1", "", HUB_NOW)
        assert w.listener.wait_for(lambda: len(outcome_posts(w)) == 1)
        assert "no longer on main's history" in outcome_posts(w)[0]
        stack.r.compare = "ahead"                                      # still an ancestor of main's tip
        open_check(w.store, cfg, w.auths["a"][0], match_for(cfg, SHA2), "t1", "", HUB_NOW)
        hl.commit = SHA2
        assert w.listener.wait_for(lambda: len(outcome_posts(w)) == 2)
        assert "no longer" not in outcome_posts(w)[1] and "could not be re-checked" not in outcome_posts(w)[1]
        assert {c.reverified for c in w.store.list_deploy_checks("t1", "u1")} == {"off_main", "on_main"}
    finally:
        w.close()
        stack.close()


def test_a_restarted_runtime_resumes_an_awaiting_check(sw, hl):
    cfg = cfg_for(hl.url)
    check = open_check(sw.store, cfg, sw.auths["a"][0], match_for(cfg), "t1", "", HUB_NOW)
    assert sw.listener.wait_for(lambda: hl.hits >= 1)                  # the first runtime has been polling
    snap = json.loads(json.dumps(sw.store.snapshot(), default=str))
    sw.runtime.stop()
    hl.down()                                                          # the hub's container is replaced ...
    restored = AssistantStore()
    restored.restore(snap)                                             # ... and the new process loads its state
    (still,) = restored.list_deploy_checks("t1", "u1")
    assert still.status == "awaiting"
    hl.commit = SHA
    hl.up()
    rt2 = runtime_for(sw, restored)
    rt2.start_checks()                                                 # Slack off: checks still resume
    try:
        assert sw.listener.wait_for(lambda: restored.get_deploy_check(check.check_id, tenant_id="t1",
                                                                      user_id="u1").status == "healthy")
    finally:
        rt2.stop()


def test_exit_three_posts_an_urgent_ask_that_is_never_rate_limited(tmp_path, hl):
    w = SumWorld(tmp_path, max_asks_per_task_per_hour=1)
    w.worker("a")
    w.runtime.stop()
    w.runtime = runtime_for(w, w.store)
    w.hub = replace(w.hub, slack=w.runtime)
    w.runtime.start()
    try:
        cfg = cfg_for(hl.url)
        check = open_check(w.store, cfg, w.auths["a"][0], match_for(cfg), "t1", "", HUB_NOW)
        assert w.act("a", command="git push origin one")["outcome"] == "pending"   # uses up the only ask for the task
        assert w.listener.wait_for(lambda: any("Approval needed" in str(p.get("text")) for p in w.posts()))
        out = w.report("a", text="deploy failed", kind="outcome", payload={"deploy_exit": 3})
        assert out["isError"] is False
        assert w.listener.wait_for(lambda: any("URGENT" in t for t in outcome_posts(w)))
        stored = w.store.get_deploy_check(check.check_id, tenant_id="t1", user_id="u1")
        assert stored.status == "failed_rollback" and stored.exit_code == 3
        for _ in range(2):                                              # repeated claims never repeat the message
            w.report("a", text="again", kind="outcome", payload={"deploy_exit": 3})
        time.sleep(0.4)
        urgent = [t for t in outcome_posts(w) if "URGENT" in t]
        assert len(urgent) == 1 and "failed to roll back" in urgent[0] and "exit 3" in urgent[0]
        # the later health poll cannot undo or repeat it
        hl.commit = SHA
        w.runtime.run_deploy_checks()
        assert w.store.get_deploy_check(check.check_id, tenant_id="t1", user_id="u1").status == "failed_rollback"
        assert len([t for t in outcome_posts(w) if "URGENT" in t]) == 1
    finally:
        w.close()


def test_other_exit_codes_are_only_stored_and_never_urgent(sw, hl):
    cfg = cfg_for(hl.url)
    check = open_check(sw.store, cfg, sw.auths["a"][0], match_for(cfg), "t1", "", HUB_NOW)
    for code in (0, 1, 2):
        assert sw.report("a", text="exit", kind="outcome", payload={"deploy_exit": code})["isError"] is False
    assert sw.report("a", text="exit", kind="decision", payload={"deploy_exit": 3})["isError"] is False   # wrong kind
    assert sw.report("a", text="exit", kind="outcome", payload={"deploy_exit": "3"})["isError"] is False  # not an int
    stored = sw.store.get_deploy_check(check.check_id, tenant_id="t1", user_id="u1")
    assert stored.status in ("awaiting", "unconfirmed", "rolled_back") and stored.exit_code in (None, 0, 1, 2)
    assert not any("URGENT" in t for t in outcome_posts(sw))


def test_a_healthy_check_is_not_undone_by_a_workers_exit_three(sw, hl):
    cfg = cfg_for(hl.url)
    hl.commit = SHA
    check = open_check(sw.store, cfg, sw.auths["a"][0], match_for(cfg), "t1", "", HUB_NOW)
    assert sw.listener.wait_for(lambda: sw.store.get_deploy_check(check.check_id, tenant_id="t1",
                                                                  user_id="u1").status == "healthy")
    sw.report("a", text="claim", kind="outcome", payload={"deploy_exit": 3})
    assert sw.store.get_deploy_check(check.check_id, tenant_id="t1", user_id="u1").status == "healthy"


def test_the_summary_includes_the_deploy_status_and_never_says_success(sw, hl):
    cfg = cfg_for(hl.url)
    open_check(sw.store, cfg, sw.auths["a"][0], match_for(cfg), "t1", "", HUB_NOW)        # not confirmed yet
    hl.commit = "not-a-commit"
    sw.report("a", text="done", final=True)
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)
    body = sw.summary_body()
    assert "Deploy herdr-engineering/control-plane" in body and SHA[:12] in body and SHA not in body
    assert "still being checked" in body or "unconfirmed" in body
    assert "success" not in body.lower() and "healthy" not in body.lower()


def test_summary_counts_say_at_least(sw):
    sw.report("a", text="done", final=True)
    assert sw.listener.wait_for(lambda: len(sw.summaries()) == 1)
    body = sw.summary_body()
    for label in ("Authorized under grants", "One-shot approvals used", "Denied by you", "Vetoed"):
        assert f"{label}: at least 0" in body


def test_checks_are_the_only_place_a_deploy_is_called_healthy(hl):
    store, cfg = AssistantStore(), cfg_for(hl.url)
    check = opened(store, cfg)
    assert describe_status(check) == "still being checked"
    assert "success" not in " ".join(describe_status(replace(check, status=s))
                                     for s in ("awaiting", "healthy", "rolled_back", "failed_rollback", "unconfirmed"))
