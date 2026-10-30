"""Single-operator mode (open listener acts as the operator) and the shared-credential PR poller."""
import io
import json
import urllib.error
from datetime import UTC, datetime

import pytest

from herdr_engineering import github_auth
from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import NEVER_PREAUTHORIZED, AssistantForbidden
from herdr_engineering.control_plane.intel_github import (
    MAX_PAGES,
    PER_PAGE,
    PollError,
    SharedCredentialPrLister,
)
from herdr_engineering.control_plane.project_intelligence_api import IntelligenceRoutes
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_assistant_hardening import make

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
OPEN = {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
KEY = {"kind": "operator", "id": "key:abc", "authenticated_by": "api_key"}
FULL = {"enabled": True, "ingest": True, "derive": True, "suggest": True, "default_org_id": "org1"}


# ------------------------------------------------------------------ single-operator mode
def routes(section, single):
    r = IntelligenceRoutes(pi.IntelligenceStore(clock=lambda: T0), section_fn=lambda: section)
    r.single_operator_fn = lambda: single
    return r


BODY = {"action": "pause_ingestion", "payload": {"project": "p1", "source": "gmail"}, "resource_version": 1}


def test_open_listener_still_cannot_approve_when_the_mode_is_off():
    r = routes(dict(FULL, approvers=["open-listener"]), single=False)
    p = r.post(["proposals"], BODY, OPEN, "operator")[1]
    ok = {"payload_sha256": p["payload_sha256"], "resource_version": 1}
    assert r.post(["proposals", p["id"], "approve"], ok, OPEN, "operator")[0] == 403
    assert r.post(["proposals", p["id"], "reject"], {}, OPEN, "operator")[0] == 403


def test_single_operator_can_approve_their_own_proposal_but_only_with_the_exact_payload():
    r = routes(FULL, single=True)                                   # no approvers list needed
    p = r.post(["proposals"], BODY, OPEN, "operator")[1]
    bad = {"payload_sha256": "0" * 64, "resource_version": 1}
    assert r.post(["proposals", p["id"], "approve"], bad, OPEN, "operator")[0] == 409
    ok = {"payload_sha256": p["payload_sha256"], "resource_version": 1}
    assert r.post(["proposals", p["id"], "approve"], ok, OPEN, "operator")[1]["state"] == "approved"
    q = r.post(["proposals"], BODY, OPEN, "operator")[1]
    assert r.post(["proposals", q["id"], "reject"], {}, OPEN, "operator")[1]["state"] == "refused"


def test_the_mode_never_widens_a_keyed_principal():
    r = routes(dict(FULL, approvers=["key:zzz"], viewers={"key:abc": {"org_id": "org1", "projects": "*"}}), single=True)
    p = r.post(["proposals"], BODY, KEY, "operator")[1]
    ok = {"payload_sha256": p["payload_sha256"], "resource_version": 1}
    assert r.post(["proposals", p["id"], "approve"], ok, KEY, "operator")[0] == 403      # not the open listener, not listed


def test_preauthorized_grants_follow_the_servers_setting_never_a_request_field():
    svc, a, _ = make()
    with pytest.raises(AssistantForbidden):
        svc.grant_tool(tenant_id="t", user_id="u1", assistant_id=a.assistant_id, tool_id="edit_shared", scope="rec",
                       preauthorized=True, principal=OPEN)
    g = svc.grant_tool(tenant_id="t", user_id="u1", assistant_id=a.assistant_id, tool_id="edit_shared", scope="rec",
                       preauthorized=True, principal=OPEN, single_operator=True)
    assert g.constraints["preauthorized"] is True
    for tool in NEVER_PREAUTHORIZED:                                  # still always asks
        with pytest.raises(AssistantForbidden):
            svc.grant_tool(tenant_id="t", user_id="u1", assistant_id=a.assistant_id, tool_id=tool, scope="*",
                           preauthorized=True, principal=OPEN, single_operator=True)
    with pytest.raises(AssistantForbidden):                           # an unidentified principal is not the open listener
        svc.grant_tool(tenant_id="t", user_id="u1", assistant_id=a.assistant_id, tool_id="edit_shared", scope="rec",
                       preauthorized=True, principal={"kind": "operator", "id": "x", "authenticated_by": "none"},
                       single_operator=True)


def test_the_server_refuses_single_operator_together_with_api_keys():
    with pytest.raises(ValueError, match="single_operator"):
        ControlPlaneServer(ControlPlaneRepo(), api_keys={"k" * 20: "operator"}, single_operator=True)
    ControlPlaneServer(ControlPlaneRepo(), single_operator=True)               # loopback, no keys: fine
    assert ControlPlaneServer(ControlPlaneRepo()).single_operator is False


# ------------------------------------------------------------------ the shared-credential poller
class Provider:
    mode = "app"

    def __init__(self, token="ghs_" + "a" * 30, error=None):
        self.calls, self._token, self._error = [], token, error

    def token_for(self, owner, permissions=None, repositories=None):
        self.calls.append((owner, dict(permissions or {}), list(repositories or [])))
        if self._error:
            raise github_auth.GitHubAuthError(self._error)
        return self._token


class Opener:
    def __init__(self, pages=None, status=None):
        self.pages, self.status, self.requests = list(pages or []), status, []

    def open(self, request, timeout=None):
        self.requests.append((request.full_url, request.get_header("Authorization")))
        if self.status:
            raise urllib.error.HTTPError(request.full_url, self.status, "x", {}, io.BytesIO(b""))
        body = json.dumps(self.pages.pop(0)).encode()

        class R(io.BytesIO):
            status = 200
        return R(body)


def pr(n):
    return {"number": n, "head": {"sha": f"{n:040x}"}, "base": {"ref": "main"}, "user": {"login": "u"}}


def lister(opener, provider=None):
    return SharedCredentialPrLister(credentials=lambda: provider or Provider(), opener=opener)


def test_open_prs_use_a_repo_scoped_read_only_token_and_page():
    provider = Provider()
    op = Opener([[pr(n) for n in range(PER_PAGE)], [pr(500)]])
    got = lister(op, provider).open_prs("owner/repo")
    assert [p.number for p in got][-1] == 500 and len(got) == PER_PAGE + 1
    assert provider.calls == [("owner", {"pull_requests": "read", "metadata": "read"}, ["repo"])]
    assert op.requests[0][0].startswith("https://api.github.com/repos/owner/repo/pulls?state=open")
    assert op.requests[0][1].startswith("Bearer ghs_")


def test_a_full_last_page_is_an_error_not_a_silent_truncation():
    op = Opener([[pr(n) for n in range(PER_PAGE)] for _ in range(MAX_PAGES)])
    ls = lister(op)
    with pytest.raises(PollError) as exc:
        ls.open_prs("owner/repo")
    assert exc.value.reason == "too_many_pull_requests" and ls.last_error == "too_many_pull_requests"


@pytest.mark.parametrize("status,reason", [(403, "permission"), (401, "authentication"), (404, "not_found"), (500, "unavailable")])
def test_http_failures_become_fixed_reasons_and_the_token_is_never_in_them(status, reason):
    ls = lister(Opener(status=status))
    with pytest.raises(PollError) as exc:
        ls.open_prs("owner/repo")
    assert exc.value.reason == reason and "ghs_" not in repr(exc.value) and ls.last_error == reason


def test_credential_problems_surface_as_reasons_and_success_clears_them():
    for provider, reason in ((Provider(error="unknown_owner"), "unknown_owner"), (Provider(token=None), "not_configured")):
        ls = lister(Opener(), provider)
        with pytest.raises(PollError) as exc:
            ls.open_prs("owner/repo")
        assert exc.value.reason == reason
    ls = lister(Opener([[pr(1)]]))
    ls.last_error = "permission"
    ls.open_prs("owner/repo")
    assert ls.last_error is None
    with pytest.raises(PollError):
        ls.open_prs("not a repo")


def test_the_allowlist_gained_pull_requests_read_and_nothing_writable():
    assert github_auth._permissions({"pull_requests": "read", "metadata": "read"}) == {"metadata": "read", "pull_requests": "read"}
    for bad in ({"pull_requests": "write"}, {"pull_requests": "admin"}, {"statuses": "read"}, {"contents": "write"}):
        with pytest.raises(ValueError):
            github_auth._permissions(bad)


def test_reconcile_records_the_pass_and_the_status_shows_why_it_could_not_run():
    sec = dict(FULL, webhook_projects={"o/r": ["p1"]})
    r = routes(sec, single=False)
    r.poll_status_fn = lambda: {"client": "shared", "last_error": "permission"}
    assert r.reconcile_now() is None and r.last_reconcile["ran"] is False
    class App:
        def open_prs(self, repo):
            return [type("P", (), {"number": 1, "head_sha": "c" * 40, "author_login": "u", "base_ref": "main"})()]
    r.github_app_fn = lambda: App()
    assert r.reconcile_now()["recorded"] == 1
    st, body = r.get(["status"], {}, OPEN, "operator")
    assert st == 200 and body["poller"]["recorded"] == 1 and body["poller"]["client"] == "shared"
    assert body["poller"]["last_error"] == "permission"
