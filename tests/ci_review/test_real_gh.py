"""The reviewer's gh-facing code against the REAL `gh` CLI. No fake gh binary, no stubs.

The real gh is pointed at a local HTTPS server that really implements the few GitHub endpoints the reviewer uses
(PR view/list via GraphQL, PR diff, user, commit statuses, check runs, issue comments, raw file contents, and
POST statuses/comments). gh talks to it through GH_HOST (an enterprise-style host:port) and GH_ENTERPRISE_TOKEN, and
trusts the server's throwaway certificate through SSL_CERT_FILE. GH_CONFIG_DIR is a temp dir and GH_TOKEN/GITHUB_TOKEN
are removed, so the operator's real credentials are never read and nothing can reach github.com.

Why this is safe and sufficient: github.com itself cannot be redirected, but gh treats any other GH_HOST as GitHub
Enterprise Server (REST under /api/v3, GraphQL at /api/graphql), which is the same wire protocol.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import re
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPO = "acme/widget"
SHA = "c" * 40
DIFF = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,2 @@\n x = 1\n+y = eval(x)\n")

pytestmark = pytest.mark.skipif(not (shutil.which("gh") and shutil.which("openssl")), reason="needs gh and openssl")


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy/ci-review" / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class GitHubApi:
    """A real HTTPS server speaking the GitHub endpoints the reviewer calls. Every request is recorded."""

    def __init__(self, certdir: Path):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(certdir / "k.pem"),
                        "-out", str(certdir / "c.pem"), "-days", "2", "-subj", "/CN=localhost", "-addext",
                        "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
        self.cert = certdir / "c.pem"
        self.requests: list[dict] = []
        self.statuses: list[dict] = []
        self.comments: list[dict] = []
        self.check_runs: list[dict] = []
        self.head = SHA
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code: int, body: bytes, ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def read_body(self) -> bytes:
                """gh streams `--input -` bodies chunked (no content-length)."""
                if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
                    out = b""
                    while size := int(self.rfile.readline().strip() or b"0", 16):
                        out += self.rfile.read(size)
                        self.rfile.readline()
                    self.rfile.readline()
                    return out
                n = int(self.headers.get("content-length") or 0)
                return self.rfile.read(n) if n else b""

            def handle_any(self):
                raw = self.read_body()
                u = urlparse(self.path)
                rec = {"method": self.command, "path": u.path, "query": parse_qs(u.query), "body": raw,
                       "auth": self.headers.get("authorization"), "accept": self.headers.get("accept")}
                api.requests.append(rec)
                code, body, ctype = api.route(rec)
                self.reply(code, body if isinstance(body, bytes) else json.dumps(body).encode(), ctype)

            do_GET = do_POST = do_PATCH = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certdir / "c.pem", certdir / "k.pem")
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.host = f"localhost:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def pr_node(self) -> dict:
        return {"id": "PR_1", "number": 7, "headRefOid": self.head, "state": "OPEN", "isDraft": False,
                "title": "Add y", "body": "body text", "baseRefName": "main",
                "commits": {"totalCount": 1, "nodes": [{"commit": {
                    "oid": self.head, "messageHeadline": "add y", "messageBody": "Co-Authored-By: Claude <a@b.c>",
                    "authoredDate": "2026-01-01T00:00:00Z", "committedDate": "2026-01-01T00:00:00Z",
                    "authors": {"nodes": []}}}]}}

    def route(self, r: dict) -> tuple[int, object, str]:
        p, m = r["path"], r["method"]
        if p == "/api/graphql":
            q = json.loads(r["body"])["query"]
            if "PullRequestList" in q:
                return 200, {"data": {"repository": {"pullRequests": {
                    "totalCount": 2, "pageInfo": {"hasNextPage": False, "endCursor": ""},
                    "nodes": [self.pr_node(), {**self.pr_node(), "number": 8, "isDraft": True}]}}}}, "application/json"
            return 200, {"data": {"repository": {"pullRequest": self.pr_node()}}}, "application/json"
        p = p.removeprefix("/api/v3")
        if m == "GET" and p == f"/repos/{REPO}/pulls/7":
            return 200, DIFF.encode(), "text/plain"
        if m == "GET" and p == "/user":
            return 200, {"login": "review-bot"}, "application/json"
        if m == "GET" and re.fullmatch(rf"/repos/{REPO}/commits/{SHA}/status", p):
            page = int(r["query"].get("page", ["1"])[0])
            return 200, {"statuses": self.statuses if page == 1 else []}, "application/json"
        if m == "GET" and re.fullmatch(rf"/repos/{REPO}/commits/{SHA}/check-runs", p):
            page = int(r["query"].get("page", ["1"])[0])
            return 200, {"check_runs": self.check_runs if page == 1 else []}, "application/json"
        if m == "GET" and p == f"/repos/{REPO}/issues/7/comments":
            page = int(r["query"].get("page", ["1"])[0])
            return 200, self.comments if page == 1 else [], "application/json"
        if m == "GET" and p == f"/repos/{REPO}/contents/src/a%20b.py".replace("%20", " ") or unquote(p) == f"/repos/{REPO}/contents/src/a b.py":
            return 200, b"x = 1\ny = eval(x)\n", "text/plain"
        if m == "POST" and p == f"/repos/{REPO}/statuses/{SHA}":
            s = json.loads(r["body"])
            self.statuses.append(s)
            return 201, {"id": len(self.statuses), **s}, "application/json"
        if m == "POST" and p == f"/repos/{REPO}/issues/7/comments":
            c = {"id": 100 + len(self.comments), "user": {"login": "review-bot"}, **json.loads(r["body"]),
                 "html_url": f"https://example.test/c/{len(self.comments)}"}
            self.comments.append(c)
            return 201, c, "application/json"
        if m == "PATCH" and (mm := re.fullmatch(rf"/repos/{REPO}/issues/comments/(\d+)", p)):
            for c in self.comments:
                if c["id"] == int(mm.group(1)):
                    c.update(json.loads(r["body"]))
                    return 200, c, "application/json"
        return 404, {"message": "Not Found"}, "application/json"


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    a = GitHubApi(tmp_path_factory.mktemp("ghcert"))
    yield a
    a.server.shutdown()


@pytest.fixture()
def env(api, tmp_path, monkeypatch):
    """Point the real gh at the local server; hide every real credential."""
    for k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GH_HOST", api.host)
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "local-test-token")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "ghcfg"))
    monkeypatch.setenv("SSL_CERT_FILE", str(api.cert))
    monkeypatch.setenv("GH_PROMPT_DISABLED", "1")
    api.requests.clear()
    api.statuses.clear()
    api.comments.clear()
    api.check_runs.clear()
    api.head = SHA
    return api


@pytest.fixture(scope="module")
def ai():
    return _load("ai_review_real_gh", "ai-review.py")


@pytest.fixture(scope="module")
def always():
    return _load("always_on_real_gh", "always-on-review.py")


def test_real_gh_really_hits_the_local_server_only(env, ai):
    """Guard against the test silently talking to the real GitHub: the server must see the request and its token."""
    assert ai.Gh().viewer() == "review-bot"
    assert env.requests[-1]["auth"] == "token local-test-token" or env.requests[-1]["auth"] == "Bearer local-test-token"


def test_pr_view_list_and_diff(env, ai):
    gh = ai.Gh()
    pr = gh.pr(REPO, 7)
    assert pr["headRefOid"] == SHA and pr["title"] == "Add y" and pr["baseRefName"] == "main"
    assert pr["commits"][0]["messageHeadline"] == "add y"
    assert gh.head(REPO, 7) == SHA
    assert gh.diff(REPO, 7) == DIFF
    assert [(p["number"], p["isDraft"]) for p in gh.open_prs(REPO)] == [(7, False), (8, True)]


def test_statuses_check_runs_and_gates(env, ai, always):
    env.statuses.extend([{"context": "lint", "state": "success", "created_at": "2026-01-01T00:00:00Z"},
                         {"context": "Independent AI review", "state": "pending"}])
    env.check_runs.extend([{"id": 1, "name": "build", "status": "completed", "conclusion": "success",
                            "started_at": "2026-01-01T00:00:00Z"}])
    gh = ai.Gh()
    assert {s["context"] for s in gh.statuses(REPO, SHA)} == {"lint", "Independent AI review"}
    assert [c["name"] for c in gh.check_runs(REPO, SHA)] == ["build"]
    green, missing = always.gates_green(gh, REPO, SHA, ["lint", "build"])
    assert green and not missing
    green, missing = always.gates_green(gh, REPO, SHA, ["lint", "e2e"])
    assert not green and "e2e" in " ".join(map(str, missing))


def test_file_text_sends_raw_media_type(env, ai):
    assert ai.Gh().file_text(REPO, SHA, "src/a b.py") == "x = 1\ny = eval(x)\n"
    assert "application/vnd.github.raw" in env.requests[-1]["accept"]


def test_post_status_reaches_server_with_body_on_stdin(env, ai, always):
    gh = ai.Gh(dry_run=False)
    d = always.Decision("failure", "one blocking finding", (), (), (), (), "x", None)
    always.post_status(gh, REPO, SHA, "Opus agent review", d, "https://example.test/run/1")
    assert env.statuses == [{"state": "failure", "context": "Opus agent review",
                             "description": "one blocking finding", "target_url": "https://example.test/run/1"}]


def test_dry_run_sends_no_write_to_the_server(env, ai, always):
    gh = ai.Gh(dry_run=True)
    always.post_status(gh, REPO, SHA, "Opus agent review", always.Decision("failure", "x", (), (), (), (), "x", None))
    assert env.statuses == [] and [r for r in env.requests if r["method"] != "GET"] == []
    assert gh.posted and gh.posted[0]["dry_run"]


def test_register_gh_secret_reads_real_gh_auth_token_and_redacts_the_posted_status(env, ai, always, tmp_path,
                                                                                    monkeypatch):
    """The operator's `gh auth token` (from a throwaway gh config under a temp HOME) joins the redaction set, and the
    real `gh api` POST then carries `[redacted]` instead of the token."""
    token = "gho_REALGHTOKEN0123456789abcdefABCDEF"
    cfg = tmp_path / "home" / ".config" / "gh"
    cfg.mkdir(parents=True)
    (cfg / "hosts.yml").write_text(f"github.com:\n    oauth_token: {token}\n    user: operator\n"
                                   "    git_protocol: https\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)
    saved = set(always.ai.REDACT)
    try:
        always.register_gh_secret()
        assert token in always.ai.REDACT
        monkeypatch.setenv("GH_HOST", env.host)
        monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "local-test-token")
        monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "ghcfg2"))
        always.post_status(always.ai.Gh(dry_run=False), REPO, SHA, "Opus agent review",
                           always.Decision("failure", f"leaked {token} here", (), (), (), (), "x", None))
    finally:
        always.ai.REDACT.clear()
        always.ai.REDACT.update(saved)
    assert token not in json.dumps(env.statuses) and "[redacted]" in env.statuses[0]["description"]


def test_upsert_comment_creates_then_updates_one_comment(env, ai, always):
    gh = ai.Gh(dry_run=False)
    ctx = "Opus agent review"
    first = always.upsert_comment(gh, REPO, 7, ctx, always.marker(ctx) + "\nfirst")
    second = always.upsert_comment(gh, REPO, 7, ctx, always.marker(ctx) + "\nsecond")
    assert len(env.comments) == 1 and "second" in env.comments[0]["body"]
    assert first and second


def test_failure_from_server_surfaces_as_review_failed(env, ai):
    with pytest.raises(ai.ReviewFailed):
        ai.Gh().run(["api", f"repos/{REPO}/nope"])


# ---- #792 follow-ups, exercised through the real gh ----

def test_check_runs_sort_by_id_alone_even_with_missing_or_odd_started_at(env, ai):
    env.check_runs.extend([{"id": 5, "name": "old-id-late-start", "started_at": "2030-01-01T00:00:00Z"},
                           {"id": 9, "name": "newest-id", "started_at": None},
                           {"id": 7, "name": "mid", "started_at": "2020-01-01T00:00:00Z"}])
    assert [c["name"] for c in ai.Gh().check_runs(REPO, SHA)] == ["newest-id", "mid", "old-id-late-start"]


def test_commit_subjects_lose_control_characters(env, ai):
    node = env.pr_node
    env.pr_node = lambda: {**node(), "commits": {"totalCount": 1, "nodes": [{"commit": {
        "oid": SHA, "messageHeadline": "fix\u001b[31m red\u0007 bell", "messageBody": "b\u0000ody",
        "authoredDate": "2026-01-01T00:00:00Z", "committedDate": "2026-01-01T00:00:00Z", "authors": {"nodes": []}}}]}}
    try:
        c = ai.Gh().pr(REPO, 7)["commits"][0]
    finally:
        del env.pr_node
    assert "\x1b" not in c["messageHeadline"] and "\x07" not in c["messageHeadline"]
    assert "\x00" not in c["messageBody"]


def test_gh_token_env_values_are_registered_for_redaction(env, always, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_ENVTOKEN0123456789abcdefABCDEF")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_GHACTIONS0123456789abcdefABCDEF")
    saved = set(always.ai.REDACT)
    try:
        always.register_gh_secret()
        assert {"ghp_ENVTOKEN0123456789abcdefABCDEF", "ghs_GHACTIONS0123456789abcdefABCDEF"} <= always.ai.REDACT
    finally:
        always.ai.REDACT.clear()
        always.ai.REDACT.update(saved)
