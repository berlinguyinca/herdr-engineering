"""Slice 5 of docs/specs/herdr-unified-workspace-20261028/design/TEAM_COLLABORATION.md, the provider layer.

The product's GitHub client and its Gmail draft writer run over a real socket against the CONTRACT-LEVEL fake servers in
``external_fakes.py`` (real HTTP, the real GitHub / Gmail REST shapes, not the real services)."""
from __future__ import annotations

import os

import pytest

from herdr_engineering.control_plane import google_workspace as gw
from herdr_engineering.control_plane.app_credentials import CredentialStore, SecretBox
from herdr_engineering.control_plane.external_actions import (
    GitHubIssues,
    GmailDrafts,
    ProviderError,
    external_actions_config,
    marker_for,
)
from herdr_engineering.control_plane.google_oauth import SCOPES
from tests.control_plane.external_fakes import FakeGitHubServer, FakeGmailServer, hours_ago

KEY = "a" * 64
REPO = "example-org/team-repo"


# ----------------------------------------------------------------------------------------------------------- config
def test_config_is_off_by_default_and_strict():
    cfg = external_actions_config(None)
    assert cfg["enabled"] is False and cfg["kill_switch"] is False
    assert cfg["github"]["api_base"] == "https://api.github.com"
    for bad in ({"typo": 1}, {"enabled": "yes"}, {"actions_per_hour": 0}, {"actions_per_hour": 999}, {"github": {"api_base": "http://evil.example"}},
                {"github": {"api_base": "ftp://x"}}, {"github": {"labels": "bug"}}, {"github": {"labels": ["a\nb"]}},
                {"github": {"nope": 1}}, {"github": {"timeout_seconds": 0}}, "str"):
        with pytest.raises(ValueError):
            external_actions_config(bad)
    ok = external_actions_config({"enabled": True, "actions_per_hour": 3, "github": {"api_base": "http://127.0.0.1:9/", "labels": ["bug", "from-email"]}})
    assert ok["enabled"] is True and ok["github"]["api_base"] == "http://127.0.0.1:9" and ok["github"]["labels"] == ["bug", "from-email"]
    assert external_actions_config({"github": {"api_base": "https://ghe.example/api/v3"}})["github"]["api_base"] == "https://ghe.example/api/v3"


# ------------------------------------------------------------------------------------------------------------ GitHub
@pytest.fixture
def gh():
    srv = FakeGitHubServer()
    srv.add_repo(REPO)
    try:
        yield srv
    finally:
        srv.close()


def client(srv, token=None):
    return GitHubIssues(srv.base, lambda repo: srv.token if token is None else token, timeout=3)


def body_for(key=KEY, text="the approved brief"):
    return f"{text}\n\n{marker_for(key)}"


def test_create_posts_the_exact_issue_with_the_token_and_returns_the_number(gh):
    out = client(gh).create(REPO, "Billing: invoice overdue", body_for(), ["bug"])
    assert out == {"ref": f"https://github.com/{REPO}/issues/1", "number": 1}
    [issue] = gh.issues(REPO)
    assert issue["title"] == "Billing: invoice overdue" and issue["body"].endswith(marker_for(KEY)) and issue["labels"] == [{"name": "bug"}]
    assert gh.count("POST", f"/repos/{REPO}/issues") == 1


def test_find_matches_the_marker_on_the_last_line_only_and_pages(gh):
    c = client(gh)
    for i in range(7):
        c.create(REPO, f"other {i}", body_for(key=f"{i:064x}"), [])
    c.create(REPO, "mine", body_for(KEY), [])
    # the key quoted INSIDE an issue body (an attacker pasting the marker mid-text) is not a match
    c.create(REPO, "decoy", f"{marker_for('b' * 64)}\n\nsomething after it", [])
    c.create(REPO, "pr-like", "x", [])
    found = c.find(REPO, KEY, since=hours_ago(1))
    assert found == {"ref": f"https://github.com/{REPO}/issues/8", "number": 8}
    assert c.find(REPO, "b" * 64, since=hours_ago(1)) is None
    assert c.find(REPO, "c" * 64, since=hours_ago(1)) is None
    # ...a window in the future finds nothing at all
    assert c.find(REPO, KEY, since="2999-01-01T00:00:00Z") is None
    # ...and a small page size makes it walk several pages (the Link header) to the same answer
    paged = GitHubIssues(gh.base, lambda r: gh.token, timeout=3, per_page=2, max_pages=10)
    gets = gh.count("GET", f"/repos/{REPO}/issues")
    assert paged.find(REPO, KEY, since=hours_ago(1)) == found
    assert gh.count("GET", f"/repos/{REPO}/issues") - gets >= 2


def test_find_fails_closed_when_the_window_is_too_large_to_scan(gh):
    c = GitHubIssues(gh.base, lambda r: gh.token, timeout=3, max_pages=1, per_page=2)
    for i in range(5):
        c.create(REPO, f"t{i}", body_for(key=f"{i:064x}"), [])
    with pytest.raises(ProviderError) as exc:
        c.find(REPO, KEY, since=hours_ago(1))
    assert exc.value.outcome == "unknown" and exc.value.reason == "reconcile_incomplete"


@pytest.mark.parametrize("setup, outcome, reason", [
    (lambda s: setattr(s, "token", "something-else"), "failed", "credential_rejected"),
    (lambda s: s.fail_next(404), "failed", "repository_not_found"),
    (lambda s: s.fail_next(403), "failed", "forbidden"),
    (lambda s: s.fail_next(422), "failed", "invalid_issue"),
    (lambda s: s.fail_next(502), "unknown", "provider_unavailable"),
    (lambda s: s.fail_next(500), "unknown", "provider_unavailable"),
    (lambda s: setattr(s, "down", True), "unknown", "provider_unavailable"),
    (lambda s: setattr(s, "drop_after_create", True), "unknown", "no_answer"),
])
def test_create_failures_are_classified_without_leaking_anything(gh, setup, outcome, reason):
    c = client(gh, token=gh.token)
    setup(gh)
    with pytest.raises(ProviderError) as exc:
        c.create(REPO, "t", body_for(), [])
    assert (exc.value.outcome, exc.value.reason) == (outcome, reason)
    text = repr(exc.value) + str(exc.value)
    assert gh.token not in text and "Bearer" not in text


def test_a_missing_credential_is_a_definite_failure_before_any_call(gh):
    def none(repo):
        raise ProviderError("failed", "no_credential")
    with pytest.raises(ProviderError) as exc:
        GitHubIssues(gh.base, none, timeout=3).create(REPO, "t", "b", [])
    assert exc.value.reason == "no_credential" and not gh.requests


@pytest.mark.parametrize("repo", ["../etc/passwd", "a/..", "a/b/c", "a b/c", "", "owner", "https://evil.example/x/y", "a/b?x=1"])
def test_the_repository_name_is_validated_so_nothing_can_steer_the_url(gh, repo):
    with pytest.raises(ProviderError) as exc:
        client(gh).create(repo, "t", "b", [])
    assert exc.value.reason == "invalid_repository" and not gh.requests


def test_redirects_are_never_followed(gh):
    class Redirecting(FakeGitHubServer):
        def route(self, method, path, query, raw, headers):
            return 302, {}, {"Location": "http://127.0.0.1:1/steal"}, False
    srv = Redirecting()
    try:
        with pytest.raises(ProviderError) as exc:
            GitHubIssues(srv.base, lambda r: "tok", timeout=3).create(REPO, "t", "b", [])
        assert exc.value.outcome == "unknown"
        assert len(srv.requests) == 1
    finally:
        srv.close()


# ------------------------------------------------------------------------------------------------------------ Gmail
@pytest.fixture
def gm(tmp_path):
    srv = FakeGmailServer()
    key = tmp_path / "key"
    key.write_bytes(os.urandom(32))
    store = CredentialStore(SecretBox(str(key)))
    store.put_client("google", "cid.apps.googleusercontent.com", "client-secret")
    store.put_connection("t", "key:u1", "google", account="u1@x.org", scopes=list(SCOPES), refresh_token="refresh-u1")
    workspace = gw.GoogleWorkspace(store, blocked_fn=lambda t, u: [], opener=srv.opener())
    try:
        yield srv, GmailDrafts(lambda: workspace), workspace
    finally:
        srv.close()


DRAFT = {"to": ["vendor@example.com"], "subject": "Re: Invoice", "body": "Thanks, handled.", "thread_id": "th-origin",
         "in_reply_to": "<orig@mail.example>", "action_key": KEY}


def test_a_draft_is_created_with_the_key_header_the_thread_and_nothing_is_sent(gm):
    srv, drafts, _ = gm
    out = drafts.create("t", "key:u1", DRAFT)
    assert out["ref"] == "r-1"
    [d] = srv.drafts
    assert d["headers"]["to"] == "vendor@example.com" and d["headers"]["subject"] == "Re: Invoice"
    assert d["headers"]["x-herdr-action-key"] == KEY and d["headers"]["in-reply-to"] == "<orig@mail.example>"
    assert d["thread_id"] == "th-origin" and "Thanks, handled." in d["body"] and KEY not in d["body"]
    assert not srv.sent and srv.count("POST", "/gmail/v1/users/me/messages/send") == 0


def test_find_locates_a_draft_by_its_key_among_recent_drafts(gm):
    srv, drafts, _ = gm
    for i in range(3):
        drafts.create("t", "key:u1", {**DRAFT, "action_key": f"{i:064x}"})
    drafts.create("t", "key:u1", DRAFT)
    drafts.create("t", "key:u1", {**DRAFT, "action_key": "f" * 64})
    assert drafts.find("t", "key:u1", KEY) == {"ref": "r-4"}
    assert drafts.find("t", "key:u1", "9" * 64) is None


@pytest.mark.parametrize("setup, outcome, reason", [
    (lambda s: setattr(s, "revoked", True), "failed", "connection_needs_attention"),
    (lambda s: setattr(s, "down", True), "unknown", "provider_unavailable"),
    (lambda s: s.fail_next(401), "failed", "credential_rejected"),
    (lambda s: setattr(s, "drop_after_create", True), "unknown", "provider_unavailable"),
])
def test_draft_failures_are_classified_and_leak_nothing(gm, setup, outcome, reason):
    srv, drafts, workspace = gm
    setup(srv)
    with pytest.raises(ProviderError) as exc:
        drafts.create("t", "key:u1", DRAFT)
    assert (exc.value.outcome, exc.value.reason) == (outcome, reason)
    assert srv.access_token not in repr(exc.value) and "refresh-u1" not in repr(exc.value)


def test_an_unconnected_person_is_a_definite_failure(gm):
    srv, drafts, _ = gm
    with pytest.raises(ProviderError) as exc:
        drafts.create("t", "key:nobody", DRAFT)
    assert exc.value.outcome == "failed" and exc.value.reason == "not_connected"


def test_a_missing_workspace_is_a_definite_failure():
    with pytest.raises(ProviderError) as exc:
        GmailDrafts(lambda: None).create("t", "u", DRAFT)
    assert exc.value.outcome == "failed" and exc.value.reason == "not_connected"
