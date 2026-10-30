"""The review gate client on the fleet's shared GitHub App: least-privilege tokens per call, write capability gate."""

import pytest

from herdr_engineering import github_auth
from herdr_engineering.review_gate import config as gate_config
from herdr_engineering.review_gate.github_app import GitHubAppClient, GitHubAppError
from tests.review_gate.test_github_app import FakeOpener, Resp, http_error

REPO = "github.com/acme/widget"
SHA = "a" * 40


class Provider:
    """Records the permissions each call asked for. ``write`` lists the enabled capabilities."""

    mode = "app"

    def __init__(self, write=("review_gate",), error=None, reason=None):
        self.write, self.error, self.reason = write, error, reason
        self.reads, self.writes, self.invalidated = [], [], []

    def token_for(self, owner, permissions=None, repositories=None):
        self.reads.append((owner, dict(permissions or {}), list(repositories or [])))
        return None if self.error == "no_token" else "ghs_READ" + "r" * 20

    def token_or_raise(self, owner, permissions=None, repositories=None):
        self.reads.append((owner, dict(permissions or {}), list(repositories or [])))
        if self.error in ("rate_limited", "unknown_owner"):
            raise github_auth.GitHubAuthError(self.error, retry_s=30 if self.error == "rate_limited" else None)
        return None if self.error == "no_token" else "ghs_READ" + "r" * 20

    def capability_token_or_raise(self, owner, capability, repositories=None):
        self.writes.append((owner, capability, list(repositories or [])))
        if capability not in self.write:
            raise github_auth.GitHubAuthError("write_disabled")
        if self.error:
            raise github_auth.GitHubAuthError(self.error, retry_s=30 if self.error == "rate_limited" else None)
        return "ghs_WRITE" + "w" * 20

    def invalidate(self, owner):
        self.invalidated.append(owner)


def client(provider, routes):
    opener = FakeOpener(routes)
    return GitHubAppClient(None, client=opener, credentials=lambda: provider, now=lambda: 1000.0), opener


def pr_item():
    return {"number": 7, "head": {"sha": SHA, "ref": "f"}, "base": {"ref": "main"}, "user": {"login": "u"}, "draft": False}


def test_reads_use_a_repo_scoped_read_only_token_and_writes_the_review_gate_capability():
    p = Provider()
    c, opener = client(p, {("GET", "/repos/acme/widget/pulls"): Resp(200, [pr_item()]),
                           ("POST", "/repos/acme/widget/statuses/"): Resp(201, {})})
    assert [x.number for x in c.open_prs(REPO)] == [7]
    c.set_status(REPO, SHA, "success", "ok")
    assert p.writes == [("acme", "review_gate", ["widget"])]
    assert opener.requests[1].get_header("Authorization") == "Bearer ghs_WRITE" + "w" * 20
    assert p.reads == [("acme", {"pull_requests": "read", "contents": "read", "metadata": "read"}, ["widget"])]
    assert opener.requests[0].get_header("Authorization") == "Bearer ghs_READ" + "r" * 20


def test_a_write_without_the_capability_is_a_permission_error_and_never_reaches_github():
    p = Provider(write=())
    c, opener = client(p, {("PUT", "/repos/acme/widget/pulls/7/merge"): Resp(200, {"merged": True, "sha": SHA})})
    with pytest.raises(GitHubAppError) as exc:
        c.merge_pr(REPO, 7, SHA)
    assert exc.value.reason == "permission" and opener.requests == []
    assert p.writes == [("acme", "review_gate", ["widget"])]


def test_a_write_uses_the_capability_token_and_a_401_drops_the_owners_cached_tokens():
    p = Provider()
    c, opener = client(p, {("PUT", "/repos/acme/widget/pulls/7/merge"): http_error(401)})
    with pytest.raises(GitHubAppError) as exc:
        c.merge_pr(REPO, 7, SHA)
    assert exc.value.reason == "authentication" and p.invalidated == ["acme"]
    assert opener.requests[0].get_header("Authorization") == "Bearer ghs_WRITE" + "w" * 20


@pytest.mark.parametrize("error,reason", [("rate_limited", "rate_limited"), ("unknown_owner", "permission"),
                                          ("backoff", "unavailable"), ("timeout", "unavailable")])
def test_credential_failures_become_the_gates_fixed_reasons(error, reason):
    c, opener = client(Provider(error=error), {})
    with pytest.raises(GitHubAppError) as exc:
        c.merge_pr(REPO, 7, SHA)
    assert exc.value.reason == reason and opener.requests == []
    assert "ghs_" not in repr(exc.value)


def test_no_read_token_is_an_authentication_error():
    c, opener = client(Provider(error="no_token"), {})
    with pytest.raises(GitHubAppError) as exc:
        c.open_prs(REPO)
    assert exc.value.reason == "authentication" and opener.requests == []


def test_the_client_needs_one_credential_source():
    with pytest.raises(ValueError):
        GitHubAppClient(None)


def test_the_gate_config_accepts_app_shared_and_still_validates_everything_else():
    section = {"enabled": True, "app": "shared", "repos": [{"repo": "github.com/acme/widget", "review_cap": 3, "ci_fix_cap": 3}]}
    cfg = gate_config.parse_review_gate_section(section, check_key_file=False)
    assert cfg.shared_app is True and cfg.app is None and cfg.enabled is True
    with pytest.raises(ValueError, match="enabled requires app"):
        gate_config.parse_review_gate_section({"enabled": True, "repos": []}, check_key_file=False)
    with pytest.raises(ValueError):
        gate_config.parse_review_gate_section({"enabled": True, "app": "elsewhere", "repos": []}, check_key_file=False)
    assert gate_config.parse_review_gate_section({"enabled": False, "repos": []}).shared_app is False


def test_a_rate_limited_read_keeps_its_reason_and_retry_time():
    p = Provider(error="rate_limited")
    c, _ = client(p, {})
    with pytest.raises(GitHubAppError) as exc:
        c.open_prs(REPO)
    assert exc.value.reason == "rate_limited" and exc.value.retry_at == 1030.0


def test_an_unknown_owner_read_is_a_permission_error():
    p = Provider(error="unknown_owner")
    c, _ = client(p, {})
    with pytest.raises(GitHubAppError) as exc:
        c.open_prs(REPO)
    assert exc.value.reason == "permission"
