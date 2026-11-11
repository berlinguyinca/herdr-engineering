"""Real Explorer + adapter contract tests; synthetic canonical CLI only, no network."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.memory_explorer import (
    MemoryAdapter,
    MemoryExplorer,
)


@pytest.fixture
def setup():
    calls = []
    principal = {"id": "open-listener"}
    policy = {
        "enabled": True,
        "viewers": {"open-listener": {"projects": ["canary"], "scopes": ["global"]}},
    }

    def build(run):
        def runner(argv, timeout):
            calls.append(argv)
            return run(argv)

        api = MemoryExplorer(
            SimpleNamespace(record_event=lambda *a, **kw: None),
            lambda: policy,
            MemoryAdapter(runner=runner),
        )

        def request(parts, query=None):
            return api.get(parts, query or {}, principal, "operator")

        return request

    return build, calls, policy


def flag(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def record(kind="fact", project="canary", **values):
    return {
        "memory_id": "fixture",
        "bank_id": project + ("-summaries" if kind == "summary" else "-facts"),
        "kind": kind,
        "status": "candidate",
        "revision": 1,
        "text": "synthetic note",
        "provenance": {},
        **values,
    }


@pytest.mark.parametrize("kind", ["fact", "summary"])
@pytest.mark.parametrize("route", ["search", "fixture"])
def test_selected_kind_scope_and_project_reach_canonical_cli(setup, kind, route):
    build, calls, _ = setup

    def run(argv):
        selected = flag(argv, "--kind", "fact")
        item = record(selected)
        return 0, json.dumps({"results": [item]} if "recall" in argv else item)

    request = build(run)
    query = {"kind": kind, "scope": "project", "project": "canary", "q": "synthetic"}
    code, body = request([route], query)
    assert code == 200
    item = body["results"][0] if route == "search" else body["memory"]
    assert item["bank_id"] == record(kind)["bank_id"]
    assert len(calls) == 1
    assert flag(calls[0], "--kind") == kind
    assert flag(calls[0], "--scope") == "project"
    assert calls[0][:3] == ["herdr-memory", "--project", "canary"]


def test_unselected_detail_global_miss_continues_only_to_authorized_project(setup):
    build, calls, _ = setup

    def run(argv):
        if flag(argv, "--scope") == "global":
            return 1, json.dumps({"error": "Memory not found"})
        return 0, json.dumps(record())

    code, body = build(run)(["fixture"])
    assert code == 200 and body["memory"]["bank_id"] == "canary-facts"
    assert [flag(argv, "--scope") for argv in calls] == ["global", "project"]
    assert flag(calls[-1], "--project") == "canary"
    assert all(flag(argv, "--kind") == "fact" for argv in calls)


def test_project_only_viewer_never_looks_up_ungranted_global_or_default_project(setup):
    build, calls, policy = setup
    policy["viewers"]["open-listener"]["scopes"] = []
    code, _ = build(lambda argv: (0, json.dumps(record())))(["fixture"])
    assert code == 200
    assert len(calls) == 1 and flag(calls[0], "--scope") == "project"
    assert flag(calls[0], "--project") == "canary"


@pytest.mark.parametrize("kind", ["decision", "personal", "bogus", "", None])
@pytest.mark.parametrize("route", ["search", "fixture"])
def test_invalid_kind_is_rejected_before_any_cli_call(setup, kind, route):
    build, calls, _ = setup
    code, _ = build(lambda argv: pytest.fail("invalid kind must not run CLI"))(
        [route], {"q": "x", "kind": kind}
    )
    assert code == 400 and calls == []


@pytest.mark.parametrize(
    "selectors",
    [
        {"project": "other"},
        {"project": "personal"},
        {"scope": "infra"},
        {"scope": "mission:release", "project": "other"},
    ],
)
@pytest.mark.parametrize("route", ["search", "fixture"])
def test_forbidden_selectors_are_refused_before_cli(setup, selectors, route):
    build, calls, _ = setup
    code, _ = build(lambda argv: pytest.fail("forbidden target must not run CLI"))(
        [route], {"q": "x", **selectors}
    )
    assert code == 403 and calls == []


@pytest.mark.parametrize(
    "error",
    [
        "authentication failed",
        "HTTP 403",
        "Memory not found: forbidden",
        "unavailable",
        "connection refused",
    ],
)
def test_non_miss_detail_errors_are_503_not_project_fallback(setup, error):
    build, calls, _ = setup
    code, body = build(lambda argv: (1, json.dumps({"error": error})))(["fixture"])
    assert code == 503 and body["state"] == "unavailable"
    assert len(calls) == 1 and flag(calls[0], "--scope") == "global"


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("missing CLI"), subprocess.TimeoutExpired("cli", 8)]
)
def test_detail_transport_errors_are_unavailable_without_fallback(setup, failure):
    build, calls, _ = setup

    def run(argv):
        raise failure

    code, _ = build(run)(["fixture"])
    assert code == 503 and len(calls) == 1


@pytest.mark.parametrize("route", ["search", "fixture"])
def test_returned_bank_and_selectors_remain_authoritative(setup, route):
    build, calls, _ = setup
    # CLI output cannot override explicit selectors, even if its different bank is granted.
    item = record(bank_id="herdr-global")
    response = {"results": [item]} if route == "search" else item
    code, body = build(lambda argv: (0, json.dumps(response)))(
        [route], {"q": "x", "scope": "project", "project": "canary"}
    )
    assert (code == 200 and body["results"] == []) if route == "search" else code == 404


@pytest.mark.parametrize("action", ["recall", "get"])
def test_adapter_rejects_unknown_kind_without_running_cli(action):
    calls = []
    adapter = MemoryAdapter(runner=lambda argv, timeout: calls.append(argv))
    with pytest.raises(ValueError):
        getattr(adapter, action)("fixture", kind="bogus")
    assert calls == []


def test_fact_bank_can_contain_semantic_decisions_not_only_literal_fact_kind(setup):
    build, _, _ = setup
    item = record(kind="fact")
    item["kind"] = "decision"
    code, body = build(lambda argv: (0, json.dumps({"results": [item]})))(
        ["search"], {"q": "x", "kind": "fact", "scope": "project"}
    )
    assert code == 200 and body["results"][0]["kind"] == "decision"


def test_detail_shared_scope_does_not_ignore_explicit_project_filter(setup):
    build, calls, _ = setup
    item = record(bank_id="herdr-global")
    code, _ = build(lambda argv: (0, json.dumps(item)))(
        ["fixture"], {"scope": "global", "project": "canary"}
    )
    assert code == 404 and len(calls) == 1


def test_unselected_detail_scan_is_bounded_and_never_uses_implicit_project(setup):
    build, calls, policy = setup
    policy["viewers"]["open-listener"] = {
        "projects": [f"p{i:02}" for i in range(20)],
        "scopes": ["global", "infra"],
    }
    code, _ = build(lambda argv: (1, json.dumps({"error": "Memory not found"})))(["fixture"])
    assert code == 404
    assert len(calls) == 10
    assert [flag(argv, "--scope") for argv in calls[:2]] == ["global", "infra"]
    assert [flag(argv, "--project") for argv in calls[2:]] == [f"p{i:02}" for i in range(8)]


@pytest.mark.parametrize("route", ["search", "fixture"])
def test_unreadable_personal_bank_is_never_returned_under_selected_kind(setup, route):
    build, _, _ = setup
    item = record("summary", project="personal")
    response = {"results": [item]} if route == "search" else item
    code, body = build(lambda argv: (0, json.dumps(response)))(
        [route], {"q": "x", "kind": "summary", "scope": "project", "project": "canary"}
    )
    assert (code == 200 and body["results"] == []) if route == "search" else code == 404


@pytest.mark.parametrize("route", ["search", "fixture"])
def test_omitted_kind_uses_canonical_fact_default(setup, route):
    build, calls, _ = setup
    response = {"results": [record()]} if route == "search" else record()
    code, _ = build(lambda argv: (0, json.dumps(response)))(
        [route], {"q": "x", "scope": "project", "project": "canary"}
    )
    assert code == 200 and flag(calls[0], "--kind") == "fact"


@pytest.mark.parametrize("route", ["search", "fixture"])
def test_selected_auth_failure_never_becomes_not_found(setup, route):
    build, calls, _ = setup
    code, body = build(lambda argv: (1, json.dumps({"error": "authentication failed"})))(
        [route], {"q": "x", "scope": "project", "project": "canary"}
    )
    assert code == 503 and body["state"] == "unavailable" and len(calls) == 1
