"""Test-only launcher for ``scripts/share-browser-check.mjs``: the real share server over temp bundles + a fake Cognito.

Run from the repo root: ``python3 -m scripts.share_browser_server --fixtures tests/share-fixtures``. It:

* builds a temp shares root holding the fixture bundles as public shares and one lab share (allow-list
  ``alice@lab.example``), with fresh random tokens;
* starts ``make_share_server`` (the production handler, unchanged) behind TLS with a throw-away self-signed certificate
  (the lab settings and the viewer require ``https://`` origins), recording each response's method, path (token and
  session replaced by ``<token>``/``<session>``), ``Range`` header and status;
* starts a fake Cognito hosted UI (``/oauth2/authorize`` and ``/oauth2/token`` with PKCE S256) whose RS256 JWTs carry the
  real Cognito issuer for the configured pool; its JWKS goes only into the share server's ``sync_dir``;
* starts a plain-HTTP control server on 127.0.0.1 (NOT part of the share server): ``GET /log``, ``POST /revoke?share=``,
  ``POST /user?name=`` (who the fake authorize page signs in next).

It prints ONE JSON line (ports and share tokens; never the signing secret or a private key) and runs until stdin
closes or it gets SIGTERM, then removes its temp directory. Never use it outside tests.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from herdr_engineering.control_plane import share_public
from herdr_engineering.control_plane.share_bundle import (
    BundleLimits,
    build_bundle,
    remove_state,
    write_state,
)
from herdr_engineering.control_plane.share_public import ShareConfig, make_share_server

POOL = "us-west-2_BrowserCheck1"
ISSUER = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
WEB_CLIENT = "browser-check-web"
USERS = {"alice": "alice@lab.example", "eve": "eve@lab.example"}     # eve is in the pool but not on the allow-list
LAB_ALLOW = ["alice@lab.example"]
# (share id, fixture dir, audience)
SHARES = [("pub-hyparquet", "html-parquet-hyparquet", "public"),
          ("pub-duckdb", "html-parquet-duckdb", "public"),
          ("pub-pdf", "pdf-only", "public"),
          ("pub-revoke", "html-parquet-hyparquet", "public"),
          ("lab-hyparquet", "html-parquet-hyparquet", "lab")]
_TOKEN_IN_PATH = re.compile(r"^/s/[A-Za-z0-9_-]{43}/")
_SESSION_IN_PATH = re.compile(r"^/s/<token>/k/[^/]+/")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _self_signed(work: Path) -> ssl.SSLContext:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = work / "tls.crt", work / "tls.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def _tls(server, ctx: ssl.SSLContext) -> None:
    # Handshake lazily in the handler thread, so a slow client cannot stall the accept loop.
    server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_files(directory: Path):
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        data = path.read_bytes()
        yield path.relative_to(directory).as_posix(), _sha(data), len(data), [data]


def _entry_for(directory: Path) -> str:
    return "index.html" if (directory / "index.html").exists() else "report.pdf"


def build_shares(root: Path, fixtures: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Bundles + live states; returns ({share id: token}, {share id: token hash})."""
    tokens, hashes_ = {}, {}
    expires = dt.datetime.fromtimestamp(time.time() + 86400, dt.UTC).isoformat()
    for share_id, fixture, audience in SHARES:
        directory = fixtures / fixture
        index = build_bundle(root / share_id, _bundle_files(directory), entry=_entry_for(directory),
                             limits=BundleLimits(1 << 30, 1000))
        token = secrets.token_urlsafe(32)
        assert len(token) == 43
        state = {"share_id": share_id, "audience": audience, "state": "live",
                 "expires_at": expires if audience == "public" else None,
                 "allow_emails": LAB_ALLOW if audience == "lab" else [], "allow_groups": [],
                 "entry": index.entry, "title": share_id,
                 "files": [{"name": f.name, "sha256": f.sha256, "size": f.size, "content_type": f.content_type}
                           for f in index.files]}
        tokens[share_id] = token
        hashes_[share_id] = _sha(token.encode("ascii"))
        write_state(root, hashes_[share_id], state)
    return tokens, hashes_


class RequestLog:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries: list[dict] = []

    def add(self, entry: dict) -> None:
        with self.lock:
            self.entries.append(entry)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.entries)


def recording_handler(log: RequestLog):
    class Recording(share_public.ShareHandler):
        def send_response_only(self, code, message=None):
            path = _TOKEN_IN_PATH.sub("/s/<token>/", self.path or "")
            path = _SESSION_IN_PATH.sub("/s/<token>/k/<session>/", path)
            headers = self.headers
            log.add({"method": self.command, "path": path, "status": int(code),
                     "range": headers.get("Range") if headers is not None else None, "at": time.time()})
            super().send_response_only(code, message)
    return Recording


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class FakeCognito:
    """The two hosted-UI endpoints the viewer uses. Keys and codes live in memory only."""

    def __init__(self, redirect_uri: str):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.redirect_uri = redirect_uri
        self.user = "alice"
        self.codes: dict[str, dict] = {}
        self.lock = threading.Lock()

    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": "browser-check", "alg": "RS256", "use": "sig"}]}

    def tokens(self, user: str) -> dict:
        now = time.time()
        common = {"iss": ISSUER, "sub": f"sub-{user}", "exp": int(now + 3600), "iat": int(now)}
        access = {**common, "username": user, "token_use": "access", "client_id": WEB_CLIENT, "cognito:groups": []}
        ident = {**common, "cognito:username": user, "token_use": "id", "aud": WEB_CLIENT, "email": USERS[user],
                 "email_verified": True}
        sign = lambda c: jwt.encode(c, self.key, algorithm="RS256", headers={"kid": "browser-check"})  # noqa: E731
        return {"access_token": sign(access), "id_token": sign(ident), "token_type": "Bearer", "expires_in": 3600}

    def handler(self):
        cognito = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                pass

            def _send(self, status, body=b"", headers=()):
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._send(204, headers=(("Access-Control-Allow-Origin", "*"),
                                         ("Access-Control-Allow-Methods", "POST"),
                                         ("Access-Control-Allow-Headers", "Content-Type")))

            def do_GET(self):
                parts = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if (parts.path != "/oauth2/authorize" or q.get("response_type") != "code"
                        or q.get("client_id") != WEB_CLIENT or q.get("redirect_uri") != cognito.redirect_uri
                        or q.get("code_challenge_method") != "S256" or not q.get("code_challenge")
                        or not q.get("state")):
                    return self._send(400, b"bad authorize request")
                code = secrets.token_urlsafe(24)
                with cognito.lock:
                    cognito.codes[code] = {"challenge": q["code_challenge"], "user": cognito.user}
                location = cognito.redirect_uri + "?" + urlencode({"code": code, "state": q["state"]})
                self._send(302, headers=(("Location", location), ("Cache-Control", "no-store")))

            def do_POST(self):
                cors = (("Access-Control-Allow-Origin", "*"), ("Content-Type", "application/json"))
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("ascii")).items()}
                with cognito.lock:
                    grant = cognito.codes.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "")
                if (urlsplit(self.path).path != "/oauth2/token" or grant is None
                        or form.get("grant_type") != "authorization_code" or form.get("client_id") != WEB_CLIENT
                        or form.get("redirect_uri") != cognito.redirect_uri
                        or _b64url(hashlib.sha256(verifier.encode("ascii")).digest()) != grant["challenge"]):
                    return self._send(400, b'{"error":"invalid_grant"}', cors)
                self._send(200, json.dumps(cognito.tokens(grant["user"])).encode("ascii"), cors)

        return Handler


def control_handler(log: RequestLog, cognito: FakeCognito, root: Path, hashes_: dict[str, str]):
    class Control(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

        def _json(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/log":
                return self._json(200, log.snapshot())
            self._json(404, {"error": "unknown"})

        def do_POST(self):
            parts = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if parts.path == "/revoke" and q.get("share") in hashes_:
                remove_state(root, hashes_[q["share"]])
                return self._json(200, {"revoked": q["share"]})
            if parts.path == "/user" and q.get("name") in USERS:
                cognito.user = q["name"]
                return self._json(200, {"user": cognito.user})
            self._json(404, {"error": "unknown"})

    return Control


def _serve(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="share-browser-check-"))
    servers = []
    try:
        tls = _self_signed(work)
        share_port, issuer_port = _free_port(), _free_port()
        origin = f"https://127.0.0.1:{share_port}"
        cognito = FakeCognito(origin + "/s/callback")
        sync = work / "sync"
        sync.mkdir()
        (sync / "jwks.json").write_text(json.dumps(cognito.jwks()))
        root = work / "shares"
        tokens, hashes_ = build_shares(root, args.fixtures.resolve())
        (work / "events").mkdir()
        config = ShareConfig(shares_root=root, events_path=work / "events" / "events.jsonl", sync_dir=sync,
                             pool_id=POOL, client_ids=(), web_client_id=WEB_CLIENT,
                             cognito_domain=f"https://127.0.0.1:{issuer_port}", public_url=origin,
                             signing_secret=secrets.token_bytes(48), session_seconds=3600)
        share = make_share_server(config, host="127.0.0.1", port=share_port)
        if share.lab is None:
            raise SystemExit("lab settings did not validate")
        log = RequestLog()
        share.RequestHandlerClass = recording_handler(log)
        _tls(share, tls)
        issuer = ThreadingHTTPServer(("127.0.0.1", issuer_port), cognito.handler())
        issuer.daemon_threads = True
        issuer.handle_error = lambda *_: None      # a client rejecting the throw-away certificate is not news
        _tls(issuer, tls)
        control = ThreadingHTTPServer(("127.0.0.1", 0), control_handler(log, cognito, root, hashes_))
        control.daemon_threads = True
        servers = [share, issuer, control]
        for server in servers:
            _serve(server)
        print(json.dumps({"origin": origin, "issuer": f"https://127.0.0.1:{issuer_port}",
                          "control": f"http://127.0.0.1:{control.server_address[1]}", "tokens": tokens}), flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
        stop.wait()
        return 0
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
