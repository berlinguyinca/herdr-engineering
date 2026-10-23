import io
import json
import traceback
import urllib.error

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import herdr_engineering.github as gh
from herdr_engineering.review_gate.config import AppConfig
from herdr_engineering.review_gate.github_app import (
    GitHubAppClient,
    GitHubAppError,
)

REPO = "github.com/acme/widget"
TOKEN = "ghs_SECRETINSTALLATIONTOKEN1234567890"


class Resp:
    def __init__(self, status, body):
        self.status = status
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self, n=-1):
        return self._raw if n < 0 else self._raw[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, routes):
        self.routes = routes  # (method, path-prefix) -> response | callable | Exception
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        path = request.full_url.split("api.github.com", 1)[1]
        for (method, prefix), result in self.routes.items():
            if request.get_method() == method and path.startswith(prefix):
                if callable(result):
                    result = result(request)
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unrouted {request.get_method()} {path}")


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.github.com/x", code, "x", headers or {}, io.BytesIO(b"{}"))


@pytest.fixture
def keypair(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    path = tmp_path / "app.pem"
    path.write_bytes(pem)
    path.chmod(0o600)
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return AppConfig(app_id=4242, installation_id=99, private_key_file=str(path)), pub


def token_route(expires="2026-10-22T13:00:00Z"):
    return Resp(201, {"token": TOKEN, "expires_at": expires})


def client_for(cfg, routes, now=lambda: 1792670400.0):  # 2026-10-22T12:00:00Z
    opener = FakeOpener({("POST", "/app/installations/99/access_tokens"): token_route(), **routes})
    return GitHubAppClient(cfg, client=opener, now=now), opener


def test_jwt_is_rs256_with_app_id_issuer(keypair):
    cfg, pub = keypair
    c, opener = client_for(cfg, {})
    assert c.installation_token() == TOKEN
    req = opener.requests[0]
    bearer = req.get_header("Authorization")
    assert bearer.startswith("Bearer ")
    encoded = bearer.split(" ", 1)[1]
    assert jwt.get_unverified_header(encoded)["alg"] == "RS256"
    claims = jwt.decode(encoded, pub, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "4242"
    assert claims["iat"] == 1792670400 - 60
    assert claims["exp"] == 1792670400 + 540


def test_installation_token_cached_until_near_expiry(keypair):
    cfg, _ = keypair
    clock = [1792670400.0]
    c, opener = client_for(cfg, {}, now=lambda: clock[0])
    c.installation_token()
    c.installation_token()
    assert len(opener.requests) == 1
    clock[0] += 3600 - 61  # one second before the refresh window
    c.installation_token()
    assert len(opener.requests) == 1
    clock[0] += 2  # inside the last 60s
    c.installation_token()
    assert len(opener.requests) == 2


def test_set_status_posts_context_and_state(keypair):
    cfg, _ = keypair
    c, opener = client_for(cfg, {("POST", "/repos/acme/widget/statuses/" + "a" * 40): Resp(201, {})})
    c.set_status(REPO, "a" * 40, "success", "x" * 300, target_url="https://example.test/r")
    req = opener.requests[-1]
    body = json.loads(req.data)
    assert body["state"] == "success"
    assert body["context"] == "herdr/review-gate"
    assert len(body["description"]) == 140
    assert body["target_url"] == "https://example.test/r"
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    with pytest.raises(ValueError):
        c.set_status(REPO, "a" * 40, "bogus", "d")


def test_merge_sends_sha_and_returns_false_on_409(keypair):
    cfg, _ = keypair
    results = [Resp(200, {"merged": True}), http_error(409), http_error(405), http_error(403)]
    c, opener = client_for(cfg, {("PUT", "/repos/acme/widget/pulls/7/merge"): lambda r: results.pop(0)})
    assert c.merge_pr(REPO, 7, "b" * 40) is True
    body = json.loads(opener.requests[-1].data)
    assert body == {"sha": "b" * 40, "merge_method": "squash"}
    assert c.merge_pr(REPO, 7, "b" * 40) is False
    assert c.merge_pr(REPO, 7, "b" * 40) is False
    with pytest.raises(GitHubAppError) as ei:
        c.merge_pr(REPO, 7, "b" * 40)
    assert ei.value.reason == "permission"


def pr(n, sha, draft=False):
    return {"number": n, "draft": draft, "head": {"sha": sha, "ref": f"feat/{n}"},
            "base": {"ref": "main"}, "user": {"login": "alice"}}


def test_open_prs_maps_fields_and_pages(keypair):
    cfg, _ = keypair
    page1 = [pr(i, "c" * 40) for i in range(1, 101)]
    page2 = [pr(101, "d" * 40, draft=True)]

    def route(req):
        return Resp(200, page1 if req.full_url.endswith("&page=1") else page2)

    c, opener = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): route})
    prs = c.open_prs(REPO)
    assert len(prs) == 101
    assert prs[0].number == 1 and prs[0].head_sha == "c" * 40 and prs[0].head_ref == "feat/1"
    assert prs[0].base_ref == "main" and prs[0].author_login == "alice" and prs[0].draft is False
    assert prs[-1].draft is True
    assert "per_page=100" in opener.requests[-1].full_url and "state=open" in opener.requests[-1].full_url


def compare(patch_a, patch_b="@@ -1,1 +1,1 @@\n-x\n+y"):
    return Resp(200, {"files": [{"filename": "z.py", "status": "modified", "patch": patch_b},
                                {"filename": "a.py", "status": "modified", "patch": patch_a}]})


def patch_id_for(cfg, resp):
    c, _ = client_for(cfg, {("GET", "/repos/acme/widget/compare/"): resp})
    return c.patch_id(REPO, "1" * 40, "2" * 40)


def test_patch_id_ignores_hunk_offsets(keypair):
    cfg, _ = keypair
    a = patch_id_for(cfg, compare("@@ -10,3 +10,4 @@ def f():\n ctx\n+new\n ctx"))
    b = patch_id_for(cfg, compare("@@ -250,3 +251,4 @@ def g():\n ctx\n+new\n ctx"))
    d = patch_id_for(cfg, compare("@@ -10,3 +10,4 @@ def f():\n ctx\n+other\n ctx"))
    assert a == b
    assert a != d
    assert len(a) == 64


def _assert_clean(exc, secrets):
    blobs = [repr(exc), str(exc), repr(exc.args)]
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        if frame.f_globals.get("__name__") == "herdr_engineering.review_gate.github_app":
            blobs.append(repr(frame.f_locals))
    for blob in blobs:
        for secret in secrets:
            assert secret not in blob
    assert exc.__context__ is None and exc.__cause__ is None and exc.__suppress_context__


def test_errors_never_contain_token(keypair, monkeypatch):
    cfg, _ = keypair
    pem_body = open(cfg.private_key_file).read().splitlines()[1]
    # HTTP 500 path (token + JWT were both in flight)
    c, opener = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): http_error(500)})
    with pytest.raises(GitHubAppError) as ei:
        c.open_prs(REPO)
    the_jwt = opener.requests[0].get_header("Authorization").split(" ", 1)[1]
    _assert_clean(ei.value, [TOKEN, the_jwt, pem_body])
    # non-JSON body echoing the token
    c, _ = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): Resp(200, f"{TOKEN} not json".encode())})
    with pytest.raises(GitHubAppError) as ei:
        c.open_prs(REPO)
    assert ei.value.reason == "invalid_response"
    _assert_clean(ei.value, [TOKEN, pem_body])
    # failing jwt.encode that echoes the key
    def boom(payload, key, algorithm=None):
        raise RuntimeError(key.decode())
    monkeypatch.setattr(jwt, "encode", boom)
    c2 = GitHubAppClient(cfg, client=FakeOpener({}))
    with pytest.raises(GitHubAppError) as ei:
        c2.installation_token()
    assert ei.value.reason == "authentication"
    _assert_clean(ei.value, [pem_body, "BEGIN"])


def test_garbage_key_is_authentication_error(keypair):
    cfg, _ = keypair
    open(cfg.private_key_file, "w").write("-----BEGIN PRIVATE KEY-----\nSECRETBODY\n-----END PRIVATE KEY-----\n")
    c = GitHubAppClient(cfg, client=FakeOpener({}))
    with pytest.raises(GitHubAppError) as ei:
        c.installation_token()
    _assert_clean(ei.value, ["SECRETBODY", "BEGIN"])


def test_401_maps_to_authentication(keypair):
    cfg, _ = keypair
    c, _ = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): http_error(401)})
    with pytest.raises(GitHubAppError) as ei:
        c.open_prs(REPO)
    assert ei.value.reason == "authentication"


def test_redirect_is_refused(keypair):
    cfg, _ = keypair
    default = GitHubAppClient(cfg)
    assert any(isinstance(h, gh._NoRedirect) for h in default._client.handlers)
    c, _ = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): http_error(302, {"Location": "https://evil.test/"})})
    with pytest.raises(GitHubAppError) as ei:
        c.open_prs(REPO)
    assert ei.value.reason == "unavailable"


def test_repo_path_injection_rejected(keypair):
    cfg, _ = keypair
    c, opener = client_for(cfg, {})
    for bad in ("github.com/acme/../x", "gitlab.com/acme/widget", "github.com/acme/wid get", "nonsense"):
        with pytest.raises(ValueError):
            c.open_prs(bad)
    assert opener.requests == []


def files_resp(files):
    return Resp(200, {"files": files})


def f(name, patch=None, sha=None, status="modified", prev=None):
    d = {"filename": name, "status": status}
    if patch is not None:
        d["patch"] = patch
    if sha is not None:
        d["sha"] = sha
    if prev is not None:
        d["previous_filename"] = prev
    return d


def test_patch_id_single_unpaged_request(keypair):
    cfg, _ = keypair
    c, opener = client_for(cfg, {("GET", "/repos/acme/widget/compare/"): files_resp([f("a", "@@ -1 +1 @@\n-a\n+b")])})
    c.patch_id(REPO, "1" * 40, "2" * 40)
    compares = [r for r in opener.requests if "/compare/" in r.full_url]
    assert len(compares) == 1 and "?" not in compares[0].full_url


def test_patch_id_fails_closed_at_300_files(keypair):
    cfg, _ = keypair
    many = [f(f"f{i}", "@@ -1 +1 @@\n-a\n+b") for i in range(300)]
    with pytest.raises(GitHubAppError) as ei:
        patch_id_for(cfg, files_resp(many))
    assert ei.value.reason == "invalid_response"
    patch_id_for(cfg, files_resp(many[:299]))


def test_patch_id_patchless_needs_blob_sha(keypair):
    cfg, _ = keypair
    for bad in (f("img.png"), f("img.png", sha="short")):
        with pytest.raises(GitHubAppError) as ei:
            patch_id_for(cfg, files_resp([bad]))
        assert ei.value.reason == "invalid_response"
    one = patch_id_for(cfg, files_resp([f("img.png", sha="a" * 40)]))
    two = patch_id_for(cfg, files_resp([f("img.png", sha="b" * 40)]))
    assert one != two


def test_patch_id_renames_from_different_sources_differ(keypair):
    cfg, _ = keypair
    p = "@@ -1 +1 @@\n-a\n+b"
    one = patch_id_for(cfg, files_resp([f("new", p, status="renamed", prev="old1")]))
    two = patch_id_for(cfg, files_resp([f("new", p, status="renamed", prev="old2")]))
    assert one != two


@pytest.mark.parametrize("bad", ["abc1234", "a" * 39, "a" * 41, "g" * 40, ""])
def test_short_or_malformed_sha_rejected_without_request(keypair, bad):
    cfg, _ = keypair
    c, opener = client_for(cfg, {})
    good = "a" * 40
    for call in (lambda: c.set_status(REPO, bad, "success", "d"),
                 lambda: c.merge_pr(REPO, 1, bad),
                 lambda: c.update_branch(REPO, 1, bad),
                 lambda: c.patch_id(REPO, bad, good),
                 lambda: c.patch_id(REPO, good, bad)):
        with pytest.raises(ValueError):
            call()
    assert opener.requests == []


def test_sha_is_lowercased_and_64_hex_accepted(keypair):
    cfg, _ = keypair
    c, opener = client_for(cfg, {("PUT", "/repos/acme/widget/pulls/1/merge"): Resp(200, {})})
    assert c.merge_pr(REPO, 1, "AB" * 20) is True
    assert json.loads(opener.requests[-1].data)["sha"] == "ab" * 20
    assert c.merge_pr(REPO, 1, "c" * 64) is True


def test_401_on_rest_call_clears_cached_token(keypair):
    cfg, _ = keypair
    results = [http_error(401), Resp(200, [])]
    c, opener = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): lambda r: results.pop(0)})
    with pytest.raises(GitHubAppError):
        c.open_prs(REPO)
    c.open_prs(REPO)
    assert sum("access_tokens" in r.full_url for r in opener.requests) == 2


def test_rate_limit_retry_at_uses_injected_clock(keypair):
    cfg, _ = keypair
    c, _ = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): http_error(403, {"Retry-After": "30"})})
    with pytest.raises(GitHubAppError) as ei:
        c.open_prs(REPO)
    assert ei.value.reason == "rate_limited" and ei.value.retry_at == 1792670400.0 + 30


def test_set_status_redacts_description_and_rejects_secret_url(keypair):
    cfg, _ = keypair
    c, opener = client_for(cfg, {("POST", "/repos/acme/widget/statuses/"): Resp(201, {})})
    c.set_status(REPO, "a" * 40, "failure", "leaked Bearer abcdef123456 here")
    assert "abcdef123456" not in json.loads(opener.requests[-1].data)["description"]
    n = len(opener.requests)
    with pytest.raises(ValueError):
        c.set_status(REPO, "a" * 40, "failure", "d", target_url="https://x.test/?k=sk-abcdefgh123")
    assert len(opener.requests) == n


def test_open_prs_cap_is_not_silent_and_null_user_tolerated(keypair):
    cfg, _ = keypair
    full = [pr(i, "c" * 40) for i in range(100)]
    c, _ = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): Resp(200, full)})
    with pytest.raises(GitHubAppError) as ei:
        c.open_prs(REPO)
    assert ei.value.reason == "invalid_response"
    ghost = pr(1, "c" * 40)
    ghost["user"] = None
    c, _ = client_for(cfg, {("GET", "/repos/acme/widget/pulls"): Resp(200, [ghost])})
    assert c.open_prs(REPO)[0].author_login == ""
