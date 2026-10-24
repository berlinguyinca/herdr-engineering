"""Sandboxed CI fix agent: config, request, state machine, claim caps, cancel, replay and the API."""
import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ci_fix import CIFixService, ci_fix_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo

SHA = "b" * 40
CONFIG = {"enabled": True, "hosts": ["bender", "beast"], "max_concurrent": {"global": 2, "per_host": 1}}


class Provider:
    available = True
    token = "private-ci-token"
    base_url = "https://ci.example"

    def __init__(self):
        self.reads = 0

    def repos(self):
        return [{"id": 17, "repository": "org/frontend", "forge_url": "https://github.com/org/frontend",
                 "clone_url": "https://github.com/org/frontend.git"},
                {"id": 18, "repository": "org/backend", "forge_url": "https://github.com/org/backend",
                 "clone_url": "https://github.com/org/backend.git"}]

    def debug_with_pi(self, repository, number):
        self.reads += 1
        return {"ok": True, "view_in_web_ui": f"https://ci.example/repos/{repository}/pipeline/{number}",
                "handoff": {"pipeline_id": str(number), "revision": SHA, "failing_step": "build",
                            "log_summary": "FAILED token private-ci-token", "branch": "fix/parser"}}


def make(config=None):
    repo = ControlPlaneRepo()
    service = CIFixService(repo, ci_fix_config(config or CONFIG))
    monitor = SimpleNamespace(_factory=Provider)
    return repo, service, monitor


def ask(service, monitor, number=12, op="op-1", repository="17", **kw):
    return service.request(monitor, repository, number, op, **kw)


# ------------------------------------------------------------------ config
def test_config_defaults_are_disabled_and_closed():
    cfg = ci_fix_config(None)
    assert cfg["enabled"] is False and cfg["hosts"] == []
    assert cfg["pull_request"] == {"draft": True, "label": "herdr-ci-fix"}
    assert cfg["egress"]["default"] == ["gateway"]
    assert ci_fix_config({})["enabled"] is False


@pytest.mark.parametrize("raw", [
    {"enabled": "yes"}, {"enabled": True},                       # enabled needs hosts
    {"hosts": ["bender; rm -rf /"]}, {"hosts": "bender"}, {"bogus": 1},
    {"max_concurrent": {"global": 0}}, {"max_concurrent": {"per_host": True}},
    {"deadline_minutes": 100000}, {"limits": {"max_files": "50"}}, {"models": ["a b"]},
    {"egress": {"default": ["evil host"]}}, {"egress": {"per_project": {"nonsense": ["pypi.org"]}}},
    {"paths": {"flag": ["/etc/passwd"]}}, {"paths": {"block": ["../x"]}},
    {"pull_request": {"draft": "no"}}, {"pull_request": {"label": "x\ny"}},
    {"github_token": "ghp_x"}, "text",
])
def test_config_rejects_bad_values(raw):
    with pytest.raises(ValueError, match="control_plane.ci_fix"):
        ci_fix_config(raw)


def test_config_accepts_the_documented_block():
    cfg = ci_fix_config({
        "enabled": True, "hosts": ["bender"], "models": ["qwen3.8-27b"], "deadline_minutes": 30,
        "egress": {"default": ["gateway"], "per_project": {"org/app": ["pypi.org"]}},
        "limits": {"max_files": 10}, "paths": {"flag": [".woodpecker/**"], "block": [".git*/hooks/**"]},
        "pull_request": {"draft": True, "label": "herdr-ci-fix"}})
    assert cfg["limits"]["max_files"] == 10 and cfg["limits"]["max_commits"] == 10
    assert cfg["egress"]["per_project"] == {"org/app": ["pypi.org"]}


# ------------------------------------------------------------------ request
def test_request_validates_the_run_like_send_to_pi_and_stores_bounded_context():
    repo, service, monitor = make()
    view, created = ask(service, monitor, repository="17")
    assert created and view["state"] == "queued" and view["revision"] == SHA
    assert view["run"] == "org/frontend:12" and view["id"].startswith("cifix_")
    record = service._fold(view["id"])
    assert "private-ci-token" not in json.dumps(record)
    assert record["context"]["branch"] == "fix/parser"
    assert "context" not in view                            # the dashboard view omits the log summary
    assert [e["event_type"] for e in repo.stream("ci_fix", view["id"])] == ["CIFixRequested"]


@pytest.mark.parametrize("repository,number,op", [
    ("", 12, "op"), (None, 12, "op"), ("17", 0, "op"), ("17", True, "op"), ("17", "1x", "op"),
    ("17", 12, ""), ("17", 12, None), ("999", 12, "op")])
def test_request_rejects_bad_input(repository, number, op):
    _, service, monitor = make()
    with pytest.raises(ValueError):
        service.request(monitor, repository, number, op)


def test_request_refuses_a_run_without_an_exact_commit():
    _, service, _ = make()

    class Short(Provider):
        def debug_with_pi(self, repository, number):
            out = super().debug_with_pi(repository, number)
            out["handoff"]["revision"] = "abc123"
            return out
    with pytest.raises(ValueError, match="exact full commit"):
        service.request(SimpleNamespace(_factory=Short), "17", 12, "op")


def test_request_is_idempotent_and_rejects_operation_reuse():
    repo, service, monitor = make()
    first, created = ask(service, monitor)
    again, created_again = ask(service, monitor)
    assert created and not created_again and again["id"] == first["id"]
    assert len(repo.entity_ids("ci_fix")) == 1
    with pytest.raises(ValueError, match="different request"):
        ask(service, monitor, number=13)                    # same operation_id, other run


def test_one_active_fix_per_run_until_it_ends():
    _, service, monitor = make()
    first, _ = ask(service, monitor, op="a")
    second, created = ask(service, monitor, op="b")
    assert not created and second["id"] == first["id"]
    other, created = ask(service, monitor, number=13, op="c")
    assert created and other["id"] != first["id"]
    service.cancel(first["id"])
    third, created = ask(service, monitor, op="d")          # the first ended, so a new one may start
    assert created and third["id"] not in {first["id"], other["id"]}


def test_a_run_is_identified_by_its_canonical_repository_not_the_spelling():
    _, service, monitor = make()
    first, _ = ask(service, monitor, repository="17", op="a")
    second, created = ask(service, monitor, repository="org/frontend", op="b")
    assert not created and second["id"] == first["id"]


# ------------------------------------------------------------------ claim and caps
def test_claim_hands_out_the_oldest_once_and_only_to_configured_hosts():
    _, service, monitor = make()
    first, _ = ask(service, monitor, number=1, op="a")
    ask(service, monitor, number=2, op="b")
    with pytest.raises(ValueError, match="not enabled"):
        service.claim("elsewhere")
    with pytest.raises(ValueError):
        service.claim("bender; id")
    claimed = service.claim("bender")
    assert claimed["request"]["id"] == first["id"] and claimed["request"]["state"] == "claimed"
    assert claimed["request"]["host"] == "bender"
    assert claimed["request"]["context"]["log_summary"]    # the runner gets the bounded context
    nxt = service.claim("beast")
    assert nxt["request"]["id"] != first["id"]


def test_caps_keep_a_request_queued_with_a_visible_reason():
    repo, service, monitor = make()
    ids = [ask(service, monitor, number=n, op=f"o{n}")[0]["id"] for n in (1, 2, 3)]
    assert service.claim("bender")["request"]["id"] == ids[0]
    capped = service.claim("bender")                         # per_host is 1
    assert capped["request"] is None and "bender" in capped["reason"]
    assert service.get(ids[1])["state"] == "queued" and "bender" in service.get(ids[1])["reason"]
    assert service.claim("beast")["request"]["id"] == ids[1]
    full = service.claim("beast")                            # global is 2
    assert full["request"] is None and "global" in full["reason"]
    events = len(repo.stream("ci_fix", ids[2]))
    service.claim("beast")
    assert len(repo.stream("ci_fix", ids[2])) == events      # the same reason is not re-recorded
    service.set_state(ids[0], "preparing")
    service.set_state(ids[0], "failed", reason="no model available")
    assert service.claim("bender")["request"]["id"] == ids[2]   # a slot freed up


def test_claim_with_nothing_queued():
    _, service, _ = make()
    assert service.claim("bender") == {"request": None, "reason": "nothing queued"}


# ------------------------------------------------------------------ state machine
def claimed(service, monitor, host="bender"):
    fix, _ = ask(service, monitor)
    assert service.claim(host)["request"]["id"] == fix["id"]
    return fix["id"]


def test_the_happy_path_records_every_transition_and_its_fields():
    repo, service, monitor = make()
    fix = claimed(service, monitor)
    service.set_state(fix, "preparing")
    running = service.set_state(fix, "running", model="qwen3.8-27b", session_id="herdr:bender:eng:p1",
                                branch="herdr-ci-fix/12-bbbbbbb")
    assert running["state"] == "running" and running["started_at"] and running["host"] == "bender"
    service.set_state(fix, "finishing")
    done = service.record_result(fix, {
        "pr_url": "https://github.com/org/frontend/pull/9", "pr_number": 9, "commits": 1, "files": 2,
        "insertions": 5, "deletions": 1, "diff_kb": 1, "flags": [".woodpecker/ci.yaml changed"],
        "summary": "Fixed the flaky timing assertion."})
    assert done["pr_url"].endswith("/pull/9") and done["result"]["flags"]
    final = service.set_state(fix, "succeeded")
    assert final["state"] == "succeeded" and final["ended_at"] and final["session_id"] == "herdr:bender:eng:p1"
    kinds = [e["event_type"] for e in repo.stream("ci_fix", fix)]
    assert kinds == ["CIFixRequested"] + ["CIFixStateChanged"] * 4 + ["CIFixResult", "CIFixStateChanged"]


def test_illegal_transitions_are_rejected():
    _, service, monitor = make()
    queued, _ = ask(service, monitor, number=5, op="q")
    for state in ("running", "succeeded", "claimed", "queued"):
        with pytest.raises(ValueError):
            service.set_state(queued["id"], state)           # the runner cannot skip the claim
    service.cancel(queued["id"])
    fix = claimed(service, monitor)
    for state in ("succeeded", "no_change"):
        with pytest.raises(ValueError):
            service.set_state(fix, state)                    # not before finishing
    service.set_state(fix, "running")
    with pytest.raises(ValueError):
        service.set_state(fix, "preparing")                  # no going back
    with pytest.raises(ValueError, match="not one the runner"):
        service.set_state(fix, "bogus")
    service.set_state(fix, "finishing")
    with pytest.raises(ValueError, match="requires a recorded result"):
        service.set_state(fix, "succeeded")                  # succeeded means a PR exists
    service.set_state(fix, "no_change")
    with pytest.raises(ValueError, match="already no_change"):
        service.set_state(fix, "failed")


def test_state_reports_are_replay_safe():
    repo, service, monitor = make()
    fix = claimed(service, monitor)
    service.set_state(fix, "running", model="m1")
    events = len(repo.stream("ci_fix", fix))
    service.set_state(fix, "running", model="m1")
    assert len(repo.stream("ci_fix", fix)) == events         # a retried POST adds nothing
    service.set_state(fix, "running", session_id="s")        # but new information is recorded
    assert len(repo.stream("ci_fix", fix)) == events + 1
    service.set_state(fix, "failed", reason="x")
    service.set_state(fix, "failed", reason="x")
    assert service.get(fix)["state"] == "failed"


@pytest.mark.parametrize("field,value", [
    ("branch", "main"), ("branch", "herdr-ci-fix/"), ("branch", "herdr-ci-fix/../main"),
    ("branch", "herdr-ci-fix/a b"), ("branch", "herdr-ci-fix/x.lock"), ("host", "a;b"),
    ("model", "a b"), ("reason", "x\x00y"), ("session_id", "x" * 1001)])
def test_state_fields_are_validated(field, value):
    _, service, monitor = make()
    fix = claimed(service, monitor)
    with pytest.raises(ValueError):
        service.set_state(fix, "running", **{field: value})


def test_reasons_are_redacted_and_bounded():
    _, service, monitor = make()
    fix = claimed(service, monitor)
    out = service.set_state(fix, "failed", reason="boom " + "x" * 2000)
    assert len(out["reason"]) <= 500


@pytest.mark.parametrize("body", [
    {"pr_url": "http://github.com/o/r/pull/1"}, {"pr_url": "https://u:p@github.com/o/r/pull/1"},
    {"pr_url": "javascript:alert(1)"}, {"branch": "main"}, {"commits": -1}, {"commits": True},
    {"flags": ["x"] * 21}, {"flags": [3]}, {"summary": ""}, {"token": "x"}, [], "text"])
def test_result_is_bounded_and_validated(body):
    _, service, monitor = make()
    fix = claimed(service, monitor)
    service.set_state(fix, "finishing")
    with pytest.raises(ValueError):
        service.record_result(fix, body)


def test_result_only_while_finishing_and_only_once():
    _, service, monitor = make()
    fix = claimed(service, monitor)
    with pytest.raises(ValueError, match="finishing"):
        service.record_result(fix, {"commits": 0})
    service.set_state(fix, "finishing")
    service.record_result(fix, {"commits": 0, "summary": "nothing"})
    with pytest.raises(ValueError, match="already recorded"):
        service.record_result(fix, {"commits": 1})


def test_unknown_ids_are_not_found():
    _, service, _ = make()
    for call in (lambda i: service.get(i), lambda i: service.cancel(i), lambda i: service.set_state(i, "failed"),
                 lambda i: service.record_result(i, {})):
        for bad in ("nope", "cifix_" + "0" * 26, "", None, "mission_" + "0" * 26):
            with pytest.raises(KeyError):
                call(bad)


# ------------------------------------------------------------------ cancel
def test_cancel_a_queued_fix_ends_it_at_once():
    _, service, monitor = make()
    fix, _ = ask(service, monitor)
    out = service.cancel(fix["id"], actor={"role": "operator"})
    assert out["state"] == "cancelled" and out["ended_at"] and not out["cancel_requested"]
    assert service.claim("bender")["request"] is None
    assert service.cancel(fix["id"])["state"] == "cancelled"  # idempotent


def test_cancel_a_running_fix_flags_it_for_the_runner_to_stop_and_clean_up():
    repo, service, monitor = make()
    fix = claimed(service, monitor)
    service.set_state(fix, "running")
    flagged = service.cancel(fix)
    assert flagged["state"] == "running" and flagged["cancel_requested"] is True
    events = len(repo.stream("ci_fix", fix))
    service.cancel(fix)
    assert len(repo.stream("ci_fix", fix)) == events
    assert service.set_state(fix, "cancelled", reason="stopped and cleaned up")["state"] == "cancelled"
    assert service.cancel(fix)["state"] == "cancelled"


# ------------------------------------------------------------------ durability
def test_state_is_rebuilt_from_the_events_after_a_restart():
    repo, service, monitor = make()
    fix = claimed(service, monitor)
    service.set_state(fix, "running", session_id="s1")
    restarted = CIFixService(repo, ci_fix_config(CONFIG))
    assert restarted.get(fix)["state"] == "running" and restarted.get(fix)["session_id"] == "s1"
    assert restarted.list(run="org/frontend:12")["fixes"][0]["id"] == fix
    assert restarted.list(run="org/frontend:99")["fixes"] == []


def test_list_is_newest_first_and_bounded():
    _, service, monitor = make()
    ids = [ask(service, monitor, number=n, op=f"o{n}")[0]["id"] for n in (1, 2, 3)]
    assert [row["id"] for row in service.list()["fixes"]] == ids[::-1]
    assert len(service.list(limit=2)["fixes"]) == 2


# ------------------------------------------------------------------ the API
@pytest.fixture
def api():
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"read": "viewer", "write": "operator",
                                                              "runner": "agent", "feed": "ingest"},
                                ci_fix=CONFIG)
    server.ci_monitor._factory = Provider
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield server
    server.shutdown()


def call(server, path, body=None, key="read", method=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/api/v1/{path}", method=method or ("POST" if body is not None else "GET"),
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json", **(headers or {})})
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def test_api_full_flow_with_the_runner_role(api):
    status, created = call(api, "ci/fix", {"repository": "17", "number": 12, "operation_id": "op"}, key="write")
    assert status == 202 and created["state"] == "queued"
    status, again = call(api, "ci/fix", {"repository": "17", "number": 12, "operation_id": "op"}, key="write")
    assert status == 200 and again["id"] == created["id"]
    fix = created["id"]
    assert call(api, f"ci/fix/{fix}")[1]["state"] == "queued"                     # viewers may read
    assert call(api, "ci/fix?run=org/frontend:12")[1]["fixes"][0]["id"] == fix
    status, claimed_ = call(api, "ci/fix/claim", {"host": "bender"}, key="runner")
    assert status == 200 and claimed_["request"]["id"] == fix and claimed_["request"]["context"]["revision"] == SHA
    assert call(api, "ci/fix/claim", {"host": "bender"}, key="runner")[1]["request"] is None
    assert call(api, f"ci/fix/{fix}/state", {"state": "running", "model": "m", "session_id": "s"}, key="runner")[0] == 200
    assert call(api, f"ci/fix/{fix}/state", {"state": "finishing"}, key="runner")[0] == 200
    assert call(api, f"ci/fix/{fix}/result", {"pr_url": "https://github.com/o/r/pull/3", "summary": "ok"},
                key="runner")[0] == 200
    status, final = call(api, f"ci/fix/{fix}/state", {"state": "succeeded"}, key="runner")
    assert status == 200 and final["pr_url"].endswith("/pull/3")
    kinds = {e["event_type"] for e in api.repo.stream("ci_fix", fix)}
    assert "CIFixResult" in kinds


def test_api_roles_are_split_between_the_browser_and_the_runner(api):
    body = {"repository": "17", "number": 12, "operation_id": "op"}
    assert call(api, "ci/fix", body, key="read")[0] == 403                        # a viewer cannot start one
    assert call(api, "ci/fix", body, key="runner")[0] == 403                      # nor can the runner
    assert call(api, "ci/fix", body, key="feed")[0] == 403
    assert call(api, "ci/fix", body, key="bad")[0] == 401
    fix = call(api, "ci/fix", body, key="write")[1]["id"]
    assert call(api, "ci/fix/claim", {"host": "bender"}, key="read")[0] == 403
    assert call(api, f"ci/fix/{fix}/state", {"state": "running"}, key="read")[0] == 403
    assert call(api, f"ci/fix/{fix}/cancel", {}, key="runner")[0] == 403          # the runner cannot cancel
    assert call(api, f"ci/fix/{fix}/cancel", {}, key="read")[0] == 403
    assert call(api, f"ci/fix/{fix}/cancel", {}, key="write")[1]["state"] == "cancelled"
    # the runner may only report on what it was given: a bad state is a 400, an unknown fix a 404
    assert call(api, f"ci/fix/{fix}/state", {"state": "bogus"}, key="runner")[0] == 400
    assert call(api, "ci/fix/cifix_" + "0" * 26 + "/state", {"state": "failed"}, key="runner")[0] == 404
    assert call(api, "ci/fix/cifix_" + "0" * 26)[0] == 404


def test_api_rejects_cross_origin_and_non_json_mutations(api):
    body = {"repository": "17", "number": 12, "operation_id": "op"}
    assert call(api, "ci/fix", body, key="write", headers={"Origin": "https://evil.example"})[0] == 403
    assert call(api, "ci/fix", body, key="write", headers={"Content-Type": "text/plain"})[0] == 415


def test_api_validation_errors_are_400(api):
    assert call(api, "ci/fix", {"repository": "17", "number": 0, "operation_id": "op"}, key="write")[0] == 400
    assert call(api, "ci/fix", {"repository": "17", "number": 1}, key="write")[0] == 400
    assert call(api, "ci/fix/claim", {"host": "unlisted"}, key="runner")[0] == 400
    assert call(api, "ci/fix/claim", {}, key="runner")[0] == 400


def test_disabled_by_default_every_route_is_404():
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"write": "operator", "runner": "agent"})
    server.ci_monitor._factory = Provider
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        assert server.ci_fix is None
        for path, body, key in [
                ("ci/fix", {"repository": "17", "number": 12, "operation_id": "op"}, "write"),
                ("ci/fix/claim", {"host": "bender"}, "runner"),
                ("ci/fix/cifix_" + "0" * 26 + "/state", {"state": "failed"}, "runner"),
                ("ci/fix/cifix_" + "0" * 26 + "/cancel", {}, "write"),
                ("ci/fix", None, "write"), ("ci/fix/cifix_" + "0" * 26, None, "write"),
                ("ci/fix/config", None, "runner"), ("ci/fix?host=bender&active=1", None, "runner")]:
            assert call(server, path, body, key=key)[0] == 404, path
        assert call(server, "ci", key="write")[0] == 200                          # the rest of CI is untouched
    finally:
        server.shutdown()


def test_public_listener_has_no_ci_fix_routes(acknowledged_sink, tmp_path):
    from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
    from tests.control_plane.test_collab import TOKENS, Api, _digest
    web = tmp_path / "web"
    web.mkdir()
    for name in ("collab.html", "collab-app.js", "collab.css", "app.js", "index.html"):
        (web / name).write_text(f"// {name}")
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()}),
                        sink=acknowledged_sink, public_config={"client_id": "c"})
    server = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc,
                                api_keys={"op": "operator", "runner": "agent"}, public_port=0, web_root=web,
                                collab_require_https=True, ci_fix=CONFIG)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        public = Api(server.public_port)
        headers = {"X-Forwarded-Proto": "https", "X-API-Key": "op"}
        fix = "cifix_" + "0" * 26
        for method, path, body in [
                ("POST", "/api/v1/ci/fix", {"repository": "17", "number": 1, "operation_id": "o"}),
                ("POST", "/api/v1/ci/fix/claim", {"host": "bender"}),
                ("POST", f"/api/v1/ci/fix/{fix}/state", {"state": "failed"}),
                ("POST", f"/api/v1/ci/fix/{fix}/result", {}),
                ("POST", f"/api/v1/ci/fix/{fix}/cancel", {}),
                ("GET", "/api/v1/ci/fix", None), ("GET", f"/api/v1/ci/fix/{fix}", None),
                ("GET", "/api/v1/ci/fix/config", None)]:
            assert public.call(method, path, body, headers=headers)[0] == 404, path
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ------------------------------------------------------------------ runner support
def test_runner_config_is_served_and_has_the_queue_timeout(api):
    status, cfg = call(api, "ci/fix/config", key="runner")
    assert status == 200 and cfg["hosts"] == ["bender", "beast"] and cfg["queue_timeout_minutes"] == 30
    assert cfg["pull_request"]["draft"] is True
    assert "token" not in json.dumps(cfg).lower()


def test_defer_shows_a_reason_and_times_out_a_request_waiting_for_a_model():
    clock = [1_000_000.0]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    service = CIFixService(repo, ci_fix_config({**CONFIG, "queue_timeout_minutes": 10}))
    monitor = SimpleNamespace(_factory=Provider)
    fix, _ = service.request(monitor, "17", 12, "op")
    out = service.claim("bender", {"kind": "no_model", "reason": "no model available: none listed"})
    assert out["request"] is None and service.get(fix["id"])["state"] == "queued"
    assert "none listed" in service.get(fix["id"])["reason"]
    events = len(repo.stream("ci_fix", fix["id"]))
    service.claim("beast", {"kind": "no_model", "reason": "no model available: none listed"})
    assert len(repo.stream("ci_fix", fix["id"])) == events           # the same reason is not re-recorded
    clock[0] += 11 * 60
    service.claim("bender", {"kind": "no_model", "reason": "no model available: none listed"})
    gone = service.get(fix["id"])
    assert gone["state"] == "failed" and "gave up after 10 min" in gone["reason"]
    assert service.claim("bender")["request"] is None                # and it cannot be claimed any more


@pytest.mark.parametrize("defer", [{"kind": "other"}, {"kind": "no_model", "extra": 1}, "text", []])
def test_defer_is_validated(defer):
    _, service, monitor = make()
    ask(service, monitor)
    with pytest.raises(ValueError):
        service.claim("bender", defer)


def test_list_filters_by_host_and_active():
    _, service, monitor = make()
    first, _ = ask(service, monitor, number=1, op="a")
    ask(service, monitor, number=2, op="b")
    service.claim("bender")
    assert [row["id"] for row in service.list(host="bender")["fixes"]] == [first["id"]]
    assert [row["id"] for row in service.list(active=True)["fixes"]] == [first["id"]]
    assert service.list(host="beast")["fixes"] == []
    service.set_state(first["id"], "failed", reason="x")
    assert service.list(active=True)["fixes"] == []


def test_api_serves_the_runner_filters_and_defer(api):
    fix = call(api, "ci/fix", {"repository": "17", "number": 12, "operation_id": "op"}, key="write")[1]["id"]
    out = call(api, "ci/fix/claim", {"host": "bender", "defer": {"kind": "no_model", "reason": "none"}}, key="runner")[1]
    assert out["request"] is None and out["reason"] == "none"
    assert call(api, "ci/fix/claim", {"host": "bender"}, key="runner")[1]["request"]["id"] == fix
    assert call(api, "ci/fix?host=bender&active=1", key="runner")[1]["fixes"][0]["id"] == fix
    assert call(api, "ci/fix?host=beast&active=1", key="runner")[1]["fixes"] == []
