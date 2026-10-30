"""Board proposals (project intelligence) as ask cards on the Rakazo phone, decided through the dashboard's own path.

A live ControlPlaneServer is driven over HTTP with the shapes the upstream app sends. The board, the executor and the
approver rules are the real ones; only the CI re-run hook is injected."""
import json
import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane import rakazo_events as ev
from herdr_engineering.control_plane import rakazo_proposals as rp
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_rakazo_bridge import Api, bridge_section, new_bot, read_frames

REPO = "berlinguyinca/herdr-engineering"
RERUN = {"project": "p1", "repo": REPO, "run": 302}
TOKENISH = "ghp_" + "A1b2C3d4" * 5


class Rig:
    def __init__(self, svr, api, token, bot, calls):
        self.svr, self.api, self.token, self.bot, self.calls = svr, api, token, bot, calls
        self.user = svr.rakazo.accounts[0].principal_id
        self.board = svr.intelligence.board

    def file(self, action="ci_rerun", payload=None, requester="someone-else", org="org1", **kw):
        return self.board.request(org_id=org, requester=requester, action=action,
                                  payload=dict(RERUN if payload is None else payload), resource_version=1, **kw)

    def snap(self):
        return self.api.ok("threads/get", {"botId": self.bot["id"]}, self.token)

    def cards(self, snap=None):
        return [m for m in (snap or self.snap())["messages"] if m["id"].startswith(ev.PROPOSAL_PREFIX)]

    def texts(self, prefix, snap=None):
        return [m for m in (snap or self.snap())["messages"] if m["id"].startswith(prefix)]

    def answer(self, card, answer, **over):
        body = {"botId": self.bot["id"], "runId": card["runId"], "messageId": card["id"], "answer": answer, **over}
        return self.api.rpc("threads/answer", body, self.token)


@pytest.fixture()
def make_rig(make_server_p):
    def make(*, execute=True, single=True, approvers=None, scope="default", suggest=True, **sec_over):
        svr, api = make_server_p(bridge_section())
        svr.rakazo.stream_options = {"poll_seconds": 0.02, "heartbeat_seconds": 0.3, "max_seconds": 3.0}
        svr.rakazo.desk.poll_seconds = 0.0
        svr.rakazo.desk.min_interval = 0.0
        token = api.login()
        bot = new_bot(api, token)
        user = svr.rakazo.accounts[0].principal_id
        tenant = svr.assistant.tenant_for(user)
        section = {"enabled": True, "suggest": suggest, "execute": execute, **sec_over}
        if scope == "default":
            section["assistant_viewers"] = {f"{tenant}/{user}": {"org_id": "org1", "projects": "*"}}
        elif scope is not None:
            section["assistant_viewers"] = {f"{tenant}/{user}": scope}
        if approvers is not None:
            section["approvers"] = approvers
        calls: list[tuple] = []
        svr.intelligence._section = lambda: section
        svr.intelligence._store = pi.IntelligenceStore()
        svr.intelligence.single_operator_fn = lambda: single
        svr.intelligence.ci_rerun_fn = lambda repo, run: calls.append((repo, run))
        return Rig(svr, api, token, bot, calls)
    return make


@pytest.fixture()
def make_server_p():
    started = []

    def make(bridge):
        svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
        svr = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc, rakazo_bridge=bridge)
        threading.Thread(target=svr.serve, daemon=True).start()
        svr.wait_until_ready()
        started.append(svr)
        return svr, Api(svr.port)

    yield make
    for svr in started:
        svr.shutdown()


# --------------------------------------------------------------------------------------------- appearing
def test_a_family_account_never_sees_or_decides_board_proposals_even_with_a_scope(make_server_p):
    from tests.control_plane.test_rakazo_bridge import ACCOUNTS
    svr, api = make_server_p(bridge_section(accounts=[{**ACCOUNTS[0], "role": "family"}]))
    token = api.login()
    bot = new_bot(api, token)
    user = svr.rakazo.accounts[0].principal_id
    tenant = svr.assistant.tenant_for(user)
    section = {"enabled": True, "suggest": True, "execute": True,
               "assistant_viewers": {f"{tenant}/{user}": {"org_id": "org1", "projects": "*"}}}
    calls: list[tuple] = []
    svr.intelligence._section = lambda: section
    svr.intelligence._store = pi.IntelligenceStore()
    svr.intelligence.single_operator_fn = lambda: True
    svr.intelligence.ci_rerun_fn = lambda repo, run: calls.append((repo, run))
    p = svr.intelligence.board.request(org_id="org1", requester="x", action="ci_rerun", payload=dict(RERUN), resource_version=1)
    snap = api.ok("threads/get", {"botId": bot["id"]}, token)
    assert not [m for m in snap["messages"] if m["id"].startswith(ev.PROPOSAL_PREFIX)]
    body = {"botId": bot["id"], "runId": "x", "messageId": ev.PROPOSAL_PREFIX + p.id, "answer": "allow"}
    assert api.rpc("threads/answer", body, token)[0] >= 400
    assert calls == [] and svr.intelligence.board.get(p.id, "org1").state == "awaiting_approval"


def test_a_pending_proposal_is_one_ask_card_with_a_waiting_run_and_one_note(make_rig):
    rig = make_rig()
    p = rig.file()
    for _ in range(3):                                   # however often the app reads, still one card
        snap = rig.snap()
    [card] = rig.cards(snap)
    block = card["blocks"][0]
    assert block["kind"] == "ask" and block["status"] == "pending" and block["approvalEffectId"] == p.id
    assert block["text"] == f"CI failed? Re-run {REPO} run 302"
    assert [(a["id"], a["label"]) for a in block["actions"]] == [("allow", "Approve"), ("deny", "Reject")]
    assert "Expires" in block["detail"] and "UTC" in block["detail"] and "Approving runs it now" in block["detail"]
    assert card["role"] == "bot" and card["botId"] == rig.bot["id"]
    # the app enables the buttons only while the card's run is waiting_input
    [run] = snap["activeRuns"]
    assert run["id"] == card["runId"] and run["status"] == "waiting_input" and run["userId"] == rig.user
    assert snap["run"] is None            # `run` is always a real assistant run, never a proposal's
    [note] = rig.texts(ev.PROPOSAL_NOTE_PREFIX, snap)
    assert "needs your decision" in note["blocks"][0]["text"] and REPO in note["blocks"][0]["text"]
    assert len(rig.cards()) == 1


def test_a_card_streams_to_an_open_subscription_without_a_second_scheduler(make_rig):
    rig = make_rig()
    cursor = rig.api.ok("threads/head", {"botId": rig.bot["id"]}, rig.token)["cursor"]
    conn, resp = rig.api.stream(rig.token, rig.bot["id"], cursor)
    try:
        assert read_frames(resp, 1)[0]["type"] == "heartbeat"
        p = rig.file()
        seen = read_frames(resp, 20, stop=lambda fs: any(f.get("type") == "run.waiting_input" for f in fs))
    finally:
        conn.close()
    types = [(f.get("type"), (f.get("payload") or {}).get("messageId")) for f in seen]
    assert ("thread.message.created", ev.PROPOSAL_PREFIX + p.id) in types
    assert ("thread.message.created", ev.PROPOSAL_NOTE_PREFIX + p.id) in types
    waiting = next(f for f in seen if f.get("type") == "run.waiting_input")
    assert waiting["runId"].startswith("run_proposal_" + p.id)


def test_cards_go_to_the_first_bot_only(make_rig):
    rig = make_rig()
    second = new_bot(rig.api, rig.token, "Second")
    rig.file()
    assert len(rig.cards()) == 1
    other = rig.api.ok("threads/get", {"botId": second["id"]}, rig.token)
    assert [m for m in other["messages"] if "proposal" in m["id"]] == [] and other["activeRuns"] == []


# --------------------------------------------------------------------------------------------- deciding
def test_approve_runs_the_ci_rerun_once_and_the_card_turns_answered(make_rig):
    rig = make_rig()
    p = rig.file()
    [card] = rig.cards()
    status, body, _ = rig.answer(card, "allow")
    assert status == 200 and body["json"]["ok"] is True
    assert rig.calls == [(REPO, 302)]
    assert rig.board.get(p.id, "org1").state == "executed"
    snap = rig.snap()
    [done] = rig.cards(snap)
    assert done["id"] == card["id"] and done["seq"] == card["seq"]
    assert done["blocks"][0]["status"] == "answered" and done["blocks"][0]["answer"] == "allow"
    assert done["blocks"][0]["text"].startswith("Executed: ")
    assert snap["activeRuns"] == [] and snap["run"] is None
    [outcome] = rig.texts(ev.PROPOSAL_OUTCOME_PREFIX, snap)
    assert outcome["blocks"][0]["text"].startswith("Done: ") and "CI re-run requested" in outcome["blocks"][0]["text"]
    assert len(rig.texts(ev.PROPOSAL_NOTE_PREFIX, snap)) == 1


def test_approve_with_execute_off_records_permission_only(make_rig):
    rig = make_rig(execute=False)
    p = rig.file()
    [card] = rig.cards()
    assert "Execution is off" in card["blocks"][0]["detail"]
    assert rig.answer(card, "allow")[0] == 200
    assert rig.calls == [] and rig.board.get(p.id, "org1").state == "approved"
    [outcome] = rig.texts(ev.PROPOSAL_OUTCOME_PREFIX)
    assert "Execution is off" in outcome["blocks"][0]["text"]
    assert rig.cards()[0]["blocks"][0]["status"] == "answered" and rig.snap()["activeRuns"] == []


def test_a_failed_execution_is_reported_with_the_recorded_reason(make_rig):
    rig = make_rig()
    rig.svr.intelligence.ci_rerun_fn = lambda repo, run: (_ for _ in ()).throw(RuntimeError("provider refused"))
    p = rig.file()
    assert rig.answer(rig.cards()[0], "allow")[0] == 200
    assert rig.board.get(p.id, "org1").state == "failed"
    [outcome] = rig.texts(ev.PROPOSAL_OUTCOME_PREFIX)
    text = outcome["blocks"][0]["text"]
    assert "did not run" in text and "RuntimeError" in text and "dashboard" in text
    assert "provider refused" not in json.dumps(rig.snap())
    assert rig.cards()[0]["blocks"][0]["text"].startswith("Failed: ")


def test_reject_decides_without_running_anything(make_rig):
    rig = make_rig()
    p = rig.file()
    assert rig.answer(rig.cards()[0], "deny")[0] == 200
    assert rig.calls == [] and rig.board.get(p.id, "org1").state == "refused"
    snap = rig.snap()
    block = rig.cards(snap)[0]["blocks"][0]
    assert block["status"] == "answered" and block["answer"] == "deny" and block["text"].startswith("Rejected: ")
    assert snap["activeRuns"] == []
    assert rig.texts(ev.PROPOSAL_OUTCOME_PREFIX, snap)[0]["blocks"][0]["text"].startswith("Rejected: ")


def test_an_expired_proposal_is_marked_and_cannot_be_approved(make_rig):
    rig = make_rig()
    p = rig.file()
    [card] = rig.cards()
    later = datetime.now(UTC) + timedelta(hours=1)
    rig.board._clock = lambda: later
    snap = rig.snap()
    block = rig.cards(snap)[0]["blocks"][0]
    assert block["status"] == "answered" and block["text"].startswith("Expired: ")
    assert snap["activeRuns"] == []
    assert "Expired without a decision" in rig.texts(ev.PROPOSAL_OUTCOME_PREFIX, snap)[0]["blocks"][0]["text"]
    status, body, _ = rig.answer(card, "allow")
    assert status == 409 and "settled" in body["json"]["message"] and rig.calls == []
    assert rig.board.get(p.id, "org1").state == "expired"


def test_answering_twice_is_a_noop_with_a_clear_message(make_rig):
    rig = make_rig()
    rig.file()
    [card] = rig.cards()
    assert rig.answer(card, "allow")[0] == 200
    status, body, _ = rig.answer(card, "allow")
    assert status == 200 and "Already approved" in body["json"]["message"]
    assert rig.calls == [(REPO, 302)]                                   # not run again
    status, body, _ = rig.answer(card, "deny")                          # the other answer after the fact
    assert status == 409 and "already settled" in body["json"]["message"]


def test_concurrent_answers_execute_once(make_rig):
    rig = make_rig()
    rig.file()
    [card] = rig.cards()
    results = []
    threads = [threading.Thread(target=lambda: results.append(rig.answer(card, "allow")[0])) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [200] * 4 and rig.calls == [(REPO, 302)]


# --------------------------------------------------------------------------------------------- tamper and stale
def test_a_forged_or_stale_answer_is_refused_and_changes_nothing(make_rig):
    rig = make_rig()
    p = rig.file()
    [card] = rig.cards()
    for over in ({"messageId": ev.PROPOSAL_PREFIX + "act_forged"}, {"runId": "run_proposal_" + p.id + "_000000000000"},
                 {"runId": "run_other"}, {"runId": None}):
        status, _, _ = rig.answer(card, "allow", **over)
        assert status == 404, over
    assert rig.answer(card, "maybe")[0] == 400
    assert rig.answer(card, "allow", dismiss=True)[0] == 400
    assert rig.calls == [] and rig.board.get(p.id, "org1").state == "awaiting_approval"


def test_a_payload_changed_after_the_card_was_shown_is_refused(make_rig):
    rig = make_rig()
    p = rig.file()
    [card] = rig.cards()
    p.payload_sha256 = "f" * 64                         # the board no longer holds what the card showed
    status, body, _ = rig.answer(card, "allow")
    assert status == 409 and "does not match" in body["json"]["message"]
    assert rig.calls == [] and rig.board.get(p.id, "org1").state == "awaiting_approval"


# --------------------------------------------------------------------------------------------- scope and approver rules
def test_other_orgs_and_other_projects_are_never_shown_or_decidable(make_rig):
    rig = make_rig(scope={"org_id": "org1", "projects": ["p1"]})
    other_org = rig.file(org="org2")
    other_project = rig.file(payload={**RERUN, "project": "p2"})
    no_project = rig.file(payload={"repo": REPO, "run": 5})
    mine = rig.file()
    snap = rig.snap()
    assert [c["blocks"][0]["approvalEffectId"] for c in rig.cards(snap)] == [mine.id]
    forged = {"id": ev.PROPOSAL_PREFIX + other_org.id, "runId": f"run_proposal_{other_org.id}_{other_org.payload_sha256[:12]}"}
    assert rig.answer(forged, "allow")[0] == 404
    assert rig.calls == []
    assert {other_org.id, other_project.id, no_project.id}.isdisjoint(json.dumps(snap))


def test_no_scope_configured_shows_nothing_and_logs_once(make_rig, caplog):
    rig = make_rig(scope=None)
    rig.file()
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            snap = rig.snap()
    assert rig.cards(snap) == [] and snap["activeRuns"] == []
    warnings = [r for r in caplog.records if "proposals are not shown" in r.getMessage()]
    assert len(warnings) == 1 and "assistant_viewers" in warnings[0].getMessage()
    assert "act_" not in warnings[0].getMessage()
    forged = {"id": ev.PROPOSAL_PREFIX + "act_x", "runId": "run_proposal_act_x_0"}
    assert rig.answer(forged, "allow")[0] == 404


def test_a_malformed_scope_or_a_disabled_feature_shows_nothing(make_rig):
    rig = make_rig(scope={"org_id": 7, "projects": "*"})
    rig.file()
    assert rig.cards() == []
    off = make_rig(suggest=False)
    off.file()
    assert off.cards() == []
    killed = make_rig(kill_switch=True)
    killed.file()
    assert killed.cards() == []


def test_without_single_operator_the_account_must_be_a_named_approver_and_cannot_approve_its_own(make_rig, caplog):
    rig = make_rig(single=False)                        # no approvers list: nobody decides, so nothing is shown
    rig.file()
    with caplog.at_level(logging.INFO):
        assert rig.cards() == []
    assert any("approvers" in r.getMessage() for r in caplog.records)

    rig = make_rig(single=False, approvers=["placeholder"])
    user = rig.user
    rig.svr.intelligence._section().update(approvers=[user])
    own = rig.file(requester=user)
    other = rig.file(requester="alice-the-assistant", payload={**RERUN, "run": 303})
    cards = {c["blocks"][0]["approvalEffectId"]: c for c in rig.cards()}
    status, body, _ = rig.answer(cards[own.id], "allow")
    assert status == 409 and "own proposal" in body["json"]["message"]      # the board's rule still holds
    assert rig.answer(cards[other.id], "allow")[0] == 200
    assert rig.calls == [(REPO, 303)]


def test_single_operator_mode_allows_approving_a_proposal_the_account_filed(make_rig):
    rig = make_rig(single=True)
    rig.file(requester=rig.user)
    assert rig.answer(rig.cards()[0], "allow")[0] == 200 and rig.calls == [(REPO, 302)]


# --------------------------------------------------------------------------------------------- text and secrets
def test_titles_use_only_validated_fields():
    def title(action, payload):
        return rp.proposal_title(pi_proposal(action, payload))

    assert title("ci_rerun", RERUN) == f"CI failed? Re-run {REPO} run 302"
    assert title("request_ci_repair", RERUN) == f"CI failed? Ask for a repair of {REPO} run 302"
    assert title("propose_issue", {"repo": REPO, "title": "Fix the flaky test", "body": "BODY-TEXT"}) \
        == f"Open an issue in {REPO}: Fix the flaky test"
    for bad in ({"repo": "x y; rm -rf /", "run": 3}, {"repo": "../..", "run": 3}, {"repo": REPO, "run": "3"},
                {"repo": REPO, "run": True}, {"repo": REPO}, {"repo": ["a/b"], "run": 3}):
        assert title("ci_rerun", bad) == "Approve a ci rerun request", bad
    assert title("propose_issue", {"repo": REPO, "title": "x\ny"}) == "Approve a propose issue request"


def pi_proposal(action, payload):
    from herdr_engineering.control_plane.intel_assistant import Proposal
    return Proposal("act_1", "o", "r", action, payload, "h" * 64, 1, datetime.now(UTC) + timedelta(minutes=5))


def test_no_secret_or_body_text_reaches_cards_notes_or_logs(make_rig, caplog):
    rig = make_rig()
    payload = {"project": "p1", "repo": REPO, "title": f"deploy with {TOKENISH}", "body": f"password: hunter2 {TOKENISH}",
               "api_token": TOKENISH}
    rig.file(action="propose_issue", payload=payload)
    ok = rig.file(action="propose_issue", payload={"project": "p1", "repo": REPO, "title": "Tidy docs",
                                                   "body": f"secret: {TOKENISH}"})
    with caplog.at_level(logging.DEBUG):
        snap = rig.snap()
        assert rig.answer(next(c for c in rig.cards(snap) if c["blocks"][0]["approvalEffectId"] == ok.id), "allow")[0] == 200
        wire = json.dumps(rig.snap())
    assert TOKENISH not in wire and "hunter2" not in wire and "api_token" not in wire
    assert "Tidy docs" in wire and "Approve a propose issue request" in wire
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKENISH not in logged and "hunter2" not in logged
    # no issue hook is wired in this rig: the failure shows its type only, never the exception text
    assert "no executor wired" not in wire and "IntelligenceError" in wire and "see the dashboard" in wire


# --------------------------------------------------------------------------------------------- review follow-ups
def test_a_real_running_run_is_never_hidden_behind_a_proposal_run(make_rig):
    from herdr_engineering.control_plane.assistant import AssistantRun
    rig = make_rig()
    rig.snap()
    rig.svr.assistant.store.put_run(AssistantRun(
        run_id="run_live", thread_id=rig.bot["threadId"], assistant_id=rig.bot["id"], tenant_id="default",
        user_id=rig.user, task_id="task_live", state="running"))
    rig.file()
    snap = rig.snap()
    assert snap["run"]["id"] == "run_live" and snap["run"]["status"] == "running"
    assert {r["id"] for r in snap["activeRuns"]} == {"run_live", rig.cards(snap)[0]["runId"]}


def test_losing_standing_withdraws_cards_already_shown(make_rig):
    rig = make_rig()
    rig.file()
    [card] = rig.cards()
    rig.svr.intelligence._section()["kill_switch"] = True
    snap = rig.snap()
    block = rig.cards(snap)[0]["blocks"][0]
    assert block["status"] == "answered" and block["text"].startswith("Unavailable: ")
    assert snap["activeRuns"] == []
    assert "No longer available" in rig.texts(ev.PROPOSAL_OUTCOME_PREFIX, snap)[0]["blocks"][0]["text"]
    assert rig.answer(card, "allow")[0] == 403 and rig.calls == []


def test_forced_refreshes_respect_the_minimum_interval(make_rig):
    rig = make_rig()
    desk = rig.svr.rakazo.desk
    desk.min_interval = 60.0
    scans = []
    real = rig.svr.intelligence.visible_proposals
    rig.svr.intelligence.visible_proposals = lambda v: scans.append(1) or real(v)
    for _ in range(10):
        rig.snap()
    assert len(scans) == 1                       # ten get calls, one scan of the board
    rig.file()
    assert rig.cards() == []                     # still inside the interval: not yet shown
    desk.min_interval = 0.0
    assert len(rig.cards()) == 1


def test_reject_and_approve_racing_never_execute_after_a_reject(make_rig):
    import sys
    rig = make_rig()
    routes = rig.svr.intelligence
    viewer = routes.assistant_viewer(rig.svr.assistant.tenant_for(rig.user), rig.user)
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(60):
            p = rig.file()
            before, errors = len(rig.calls), []

            def go(decision, p=p):
                try:
                    routes.decide_proposal(p.id, decision, viewer=viewer, actor=rig.user, single=True,
                                           open_listener=False, payload_sha256=p.payload_sha256, resource_version=1)
                except pi.Conflict:
                    pass
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            ts = [threading.Thread(target=go, args=(d,)) for d in ("reject", "approve")]
            [t.start() for t in ts]
            [t.join() for t in ts]
            state = rig.board.get(p.id, "org1").state
            assert not errors and state in ("refused", "executed")
            assert len(rig.calls) - before == (1 if state == "executed" else 0), state
    finally:
        sys.setswitchinterval(old)


# --------------------------------------------------------------------------------------------- async execution
def _slow_hook(rig):
    gate, started = threading.Event(), threading.Event()

    def hook(repo, run):
        started.set()
        assert gate.wait(10)
        rig.calls.append((repo, run))
    rig.svr.intelligence.ci_rerun_fn = hook
    return gate, started


def _wait(cond, timeout=5):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_answer_returns_before_a_slow_executor_finishes_and_the_outcome_follows(make_rig):
    import time
    rig = make_rig()
    gate, started = _slow_hook(rig)
    p = rig.file()
    [card] = rig.cards()
    t0 = time.monotonic()
    status, body, _ = rig.answer(card, "allow")
    took = time.monotonic() - t0
    assert status == 200 and body["json"] == {"ok": True, "state": "approved"} and took < 1.0, took
    assert started.wait(5) and rig.calls == []
    snap = rig.snap()
    block = rig.cards(snap)[0]["blocks"][0]
    assert block["status"] == "answered" and block["answer"] == "allow" and snap["activeRuns"] == []
    assert "Running now" in rig.texts(ev.PROPOSAL_OUTCOME_PREFIX, snap)[0]["blocks"][0]["text"]
    # a repeat answer and a reject while it runs change nothing
    again = rig.answer(card, "allow")
    assert again[0] == 200 and "Already approved" in again[1]["json"]["message"]
    assert rig.answer(card, "deny")[0] == 409
    gate.set()
    # nobody reads the thread: the worker itself refreshes the journal
    journal = rig.svr.rakazo.journals.get(rig.bot["threadId"], rig.bot["id"])
    out = ev.PROPOSAL_OUTCOME_PREFIX + p.id
    _wait(lambda: journal.messages[out]["blocks"][0]["text"].startswith("Done: "))
    assert journal.messages[out]["blocks"][0]["text"].startswith("Done: "), (journal.messages[out], journal.proposals[p.id]["state"], rig.board.get(p.id, "org1").history)
    assert rig.calls == [(REPO, 302)]
    assert journal.messages[ev.PROPOSAL_OUTCOME_PREFIX + p.id]["blocks"][0]["text"].startswith("Done: ")
    assert journal.messages[ev.PROPOSAL_PREFIX + p.id]["blocks"][0]["text"].startswith("Executed: ")


def test_an_async_failure_is_still_reported(make_rig):
    rig = make_rig()
    rig.svr.intelligence.ci_rerun_fn = lambda repo, run: (_ for _ in ()).throw(RuntimeError("boom"))
    p = rig.file()
    assert rig.answer(rig.cards()[0], "allow")[0] == 200
    journal = rig.svr.rakazo.journals.get(rig.bot["threadId"], rig.bot["id"])
    assert _wait(lambda: journal.proposals[p.id]["state"] == "failed")
    assert "boom" not in json.dumps(rig.snap())


def test_at_most_four_executions_run_and_a_fifth_is_refused_undecided(make_rig):
    rig = make_rig()
    gate, _ = _slow_hook(rig)
    ps = [rig.file(payload={**RERUN, "run": 400 + i}) for i in range(5)]
    cards = {c["blocks"][0]["approvalEffectId"]: c for c in rig.cards()}
    for p in ps[:4]:
        assert rig.answer(cards[p.id], "allow")[0] == 200
    status, _, hdrs = rig.answer(cards[ps[4].id], "allow")
    assert status == 429 and rig.board.get(ps[4].id, "org1").state == "awaiting_approval"
    gate.set()
    journal = rig.svr.rakazo.journals.get(rig.bot["threadId"], rig.bot["id"])
    assert _wait(lambda: all(journal.proposals[p.id]["state"] == "executed" for p in ps[:4]))
    assert rig.answer(cards[ps[4].id], "allow")[0] == 200            # a slot is free again
    assert _wait(lambda: len(rig.calls) == 5)


def test_a_reject_never_lets_an_approved_proposal_run_twice_or_after(make_rig):
    rig = make_rig()
    p = rig.file()
    [card] = rig.cards()
    assert rig.answer(card, "deny")[0] == 200
    assert rig.answer(card, "allow")[0] == 409
    assert rig.calls == [] and rig.board.get(p.id, "org1").state == "refused"


def test_close_waits_for_running_executions(make_rig):
    rig = make_rig()
    gate, started = _slow_hook(rig)
    rig.file()
    rig.answer(rig.cards()[0], "allow")
    assert started.wait(5)
    threading.Timer(0.2, gate.set).start()
    rig.svr.rakazo.desk.close(timeout=5)
    assert rig.calls == [(REPO, 302)]


# --------------------------------------------------------------------------------------------- review follow-ups (#451)
def _journal(rig):
    return rig.svr.rakazo.journals.get(rig.bot["threadId"], rig.bot["id"])


def _outcome(rig, p):
    return _journal(rig).messages[ev.PROPOSAL_OUTCOME_PREFIX + p.id]["blocks"][0]["text"]


def test_a_running_card_is_closed_out_when_the_account_loses_standing(make_rig):
    rig = make_rig()
    gate, started = _slow_hook(rig)
    p = rig.file()
    assert rig.answer(rig.cards()[0], "allow")[0] == 200 and started.wait(5)
    assert "Running now" in _outcome(rig, p)
    rig.svr.intelligence._section()["kill_switch"] = True
    rig.snap()
    assert "Running now" not in _outcome(rig, p) and "dashboard" in _outcome(rig, p)
    gate.set()


def test_an_outcome_is_reposted_when_only_its_text_changes(make_rig):
    rig = make_rig(execute=False)
    p = rig.file()
    assert rig.answer(rig.cards()[0], "allow")[0] == 200
    assert "Execution is off" in _outcome(rig, p)
    journal = _journal(rig)
    card = dict(journal.proposals[p.id], outcome="Approved: changed.")
    journal.sync_proposals([card], datetime.now(UTC))          # same state, new text
    assert _outcome(rig, p) == "Approved: changed."


def test_a_proposal_that_cannot_be_started_is_failed_and_says_so(make_rig, caplog):
    rig = make_rig()
    p = rig.file()
    [card] = rig.cards()
    rig.svr.rakazo.desk.close()                               # nothing can start any more
    with caplog.at_level(logging.WARNING):
        status, body, _ = rig.answer(card, "allow")
    assert status == 200 and body["json"]["state"] == "failed" and "could not be started" in body["json"]["message"]
    assert rig.board.get(p.id, "org1").state == "failed" and rig.calls == []
    text = _outcome(rig, p)
    assert "did not run" in text and "StartError" in text and "Running now" not in text
    assert any("could not be started" in r.getMessage() for r in caplog.records)


def test_a_repeat_answer_is_idempotent_even_when_all_workers_are_busy(make_rig):
    rig = make_rig()
    gate, started = _slow_hook(rig)
    ps = [rig.file(payload={**RERUN, "run": 500 + i}) for i in range(5)]
    cards = {c["blocks"][0]["approvalEffectId"]: c for c in rig.cards()}
    for p in ps[:4]:
        assert rig.answer(cards[p.id], "allow")[0] == 200
    status, body, hdrs = rig.answer(cards[ps[4].id], "allow")
    assert status == 429 and {k.lower(): v for k, v in hdrs.items()}["retry-after"] == "5"
    again = rig.answer(cards[ps[0].id], "allow")             # settled: no slot needed
    assert again[0] == 200 and "Already approved" in again[1]["json"]["message"]
    gate.set()
    assert _wait(lambda: len(rig.calls) == 4)


def test_a_worker_that_ends_without_a_result_leaves_no_proposal_executing(make_rig, monkeypatch):
    class Abort(BaseException):
        pass
    rig = make_rig()
    ex = rig.svr.intelligence.executor
    real = ex.execute

    def leave_executing(pid, org):
        rig.board.get(pid, org).state = "executing"
        raise Abort()
    monkeypatch.setattr(ex, "execute", leave_executing)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    p = rig.file()
    assert rig.answer(rig.cards()[0], "allow")[0] == 200
    assert _wait(lambda: rig.board.get(p.id, "org1").state == "failed")
    assert _wait(lambda: "WorkerAborted" in rig.board.get(p.id, "org1").history[-1])
    assert _wait(lambda: "did not run" in _outcome(rig, p))
    # and when execute() merely returns while still executing
    monkeypatch.setattr(ex, "execute", lambda pid, org: setattr(rig.board.get(pid, org), "state", "executing"))
    q = rig.file(payload={**RERUN, "run": 777})
    assert rig.answer([c for c in rig.cards() if c["blocks"][0]["approvalEffectId"] == q.id][0], "allow")[0] == 200
    assert _wait(lambda: rig.board.get(q.id, "org1").state == "failed")
    assert real  # the real executor is untouched for other tests


def test_concurrent_double_answers_through_the_bridge_execute_once(make_rig):
    rig = make_rig()
    gate, started = _slow_hook(rig)
    rig.file()
    [card] = rig.cards()
    results = []
    ts = [threading.Thread(target=lambda: results.append(rig.answer(card, "allow"))) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(r[0] == 200 for r in results)
    assert sum(1 for r in results if r[1]["json"].get("state") == "approved") == 1
    gate.set()
    assert _wait(lambda: rig.calls == [(REPO, 302)])
    assert _wait(lambda: rig.board.get(rig.cards()[0]["blocks"][0]["approvalEffectId"], "org1").state == "executed")
    assert rig.calls == [(REPO, 302)]
