"""Loopback fixture for the member "My artifacts" page (Playwright).

The browser talks to the production code: a real ``ControlPlaneServer`` public listener (behind TLS with a throw-away
certificate) mounting the real ``MemberWeb`` routes, the real ``CollabService`` with the real ``CognitoAuthenticator``
verifying RS256 tokens from a fake Cognito hosted UI (PKCE S256), the real session scheme, Origin/Host checks, header and
body validation, and the real upload-id derivation. Only persistence is in memory: ``FakeMemberService`` implements the
``MemberService`` methods the routes call (artifacts, versions, staged chunks, share links, tickets) with a short,
configurable processing delay so the page's ticket polling is exercised.

Control server (plain loopback HTTP, used by the test, not the browser): ``POST /user?name=alice|mal`` picks who the fake
Cognito signs in next, ``POST /rotate`` invalidates every issued member session, ``GET /stats`` reports upload counters.
Environment: ``MEMBER_RUN_DELAY_MS`` (sleep per run PUT, default 0), ``MEMBER_RESULT_DELAY_MS`` (ticket latency, default 300),
``MEMBER_CHUNKS_PER_REQUEST`` (default 2, so a few MiB makes several runs). Stdout carries ``PORT=n`` and ``CONTROL=n``.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.artifact_member_service import MemberServiceError  # noqa: E402
from herdr_engineering.control_plane.artifact_members import member_owner_key  # noqa: E402
from herdr_engineering.control_plane.collab import ApprovalPolicy, CollabService, FileEventLog  # noqa: E402
from herdr_engineering.control_plane.collab_bridge_files import CHUNK_BYTES, upload_id_for  # noqa: E402
from herdr_engineering.control_plane.collab_cognito import CognitoAuthenticator  # noqa: E402
from herdr_engineering.control_plane.member_web import MemberWeb  # noqa: E402
from herdr_engineering.control_plane.members_config import MembersConfig  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402

POOL = "us-west-2_MemberUi1"
ISSUER = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
WEB_CLIENT = "member-ui-web"
GROUP = "local.dev"
USERS = {"alice": ("alice@lab.example", [GROUP]), "mal": ("mal@lab.example", ["Users"])}
HOSTILE = '"><img src=x onerror=window.__x=1>'
_LOCK = threading.RLock()


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
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = work / "tls.crt", work / "tls.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    os.chmod(key_path, 0o600)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class FakeCognito:
    """The hosted-UI endpoints the page uses: authorize (PKCE S256), token, logout. Keys and codes live in memory only."""

    def __init__(self, redirect_uri: str, logout_uri: str):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.redirect_uri, self.logout_uri = redirect_uri, logout_uri
        self.user = "alice"
        self.codes: dict[str, dict] = {}
        self.logouts = 0
        self.lock = threading.Lock()

    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": "member-ui", "alg": "RS256", "use": "sig"}]}

    def tokens(self, user: str) -> dict:
        email, groups = USERS[user]
        now = time.time()
        common = {"iss": ISSUER, "sub": f"sub-{user}", "exp": int(now + 3600), "iat": int(now)}
        access = {**common, "username": user, "token_use": "access", "client_id": WEB_CLIENT, "cognito:groups": groups}
        ident = {**common, "cognito:username": user, "token_use": "id", "aud": WEB_CLIENT, "email": email,
                 "email_verified": True}

        def sign(claims):
            return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "member-ui"})
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
                self._send(204, headers=(("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Methods", "POST"),
                                         ("Access-Control-Allow-Headers", "Content-Type")))

            def do_GET(self):
                parts = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path == "/logout":
                    if q.get("client_id") != WEB_CLIENT or q.get("logout_uri") != cognito.logout_uri:
                        return self._send(400, b"bad logout request")
                    cognito.logouts += 1
                    return self._send(302, headers=(("Location", cognito.logout_uri),))
                if (parts.path != "/oauth2/authorize" or q.get("response_type") != "code"
                        or q.get("client_id") != WEB_CLIENT or q.get("redirect_uri") != cognito.redirect_uri
                        or q.get("code_challenge_method") != "S256" or not q.get("code_challenge") or not q.get("state")):
                    return self._send(400, b"bad authorize request")
                code = secrets.token_urlsafe(24)
                with cognito.lock:
                    cognito.codes[code] = {"challenge": q["code_challenge"], "user": cognito.user}
                location = cognito.redirect_uri + "?" + urlencode({"code": code, "state": q["state"]})
                return self._send(302, headers=(("Location", location), ("Cache-Control", "no-store")))

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
                return self._send(200, json.dumps(cognito.tokens(grant["user"])).encode("ascii"), cors)

        return Handler


def _iso(dt_: dt.datetime) -> str:
    return dt_.astimezone(dt.UTC).isoformat()


class FakeMemberService:
    """In-memory stand-in for ``MemberService``: same method names, arguments and answer shapes."""

    def __init__(self, origin: str, config: MembersConfig, result_delay: float, run_delay: float):
        self.origin, self.config = origin, config
        self.result_delay, self.run_delay = result_delay, run_delay
        self.users: dict[str, dict] = {}
        self.tickets: dict[str, dict] = {}
        self.stats = {"run_puts": 0, "duplicate_runs": 0, "max_in_flight": 0, "status_calls": 0, "approve_calls": 0,
                      "approve_keys": [], "revokes": 0}
        self.fault_next_approve: str | None = None
        self._in_flight = 0

    # ------------------------------------------------------------------ state
    def _u(self, actor) -> dict:
        uid = actor["id"]
        with _LOCK:
            if uid not in self.users:
                self.users[uid] = {"principal": f"member:{uid}", "artifacts": {}, "links": {}, "uploads": {},
                                   "shared": [
                                       {"artifact_id": "sh_read", "title": f"Team notes {HOSTILE}", "head_version_id": "sv1",
                                        "head_version_number": 4, "permissions": ["read"]},
                                       {"artifact_id": "sh_share", "title": "Team plan", "head_version_id": "sv2",
                                        "head_version_number": 2, "permissions": ["read", "share"]}]}
            return self.users[uid]

    def _stage_bytes(self, u) -> int:
        return sum(len(p) for up in u["uploads"].values() for p in up["parts"].values())

    def _submit(self, u, doc: dict, secret: str | None = None) -> dict:
        ticket = "t_" + secrets.token_hex(8)
        with _LOCK:
            self.tickets[ticket] = {"ready": time.time() + self.result_delay, "doc": doc, "secret": secret, "owner": u["principal"],
                                    "secret_read": False}
        return self.result(None, ticket, 0, _owner=u["principal"]) if self.result_delay <= 0 else {"ticket": ticket, "status": "pending"}

    def result(self, actor, ticket, seconds=0, _owner=None):
        with _LOCK:
            t = self.tickets.get(ticket)
            owner = _owner or (f"member:{actor['id']}" if actor else None)
            if t is None or t["owner"] != owner:
                raise MemberServiceError(404, "not_found", "not found")
            if time.time() < t["ready"]:
                return {"ticket": ticket, "status": "pending"}
            out = {"ticket": ticket, "status": "done", **t["doc"]}
            if t["secret"] is not None:
                if t["secret_read"]:
                    out["secret"] = {"status": "already_read"}
                else:
                    t["secret_read"] = True
                    out["secret"] = {"url": t["secret"], "notice": "shown once; generate a new link to re-share"}
            return out

    def _own(self, u, artifact_id, active=True) -> dict:
        a = u["artifacts"].get(artifact_id)
        if a is None or (active and a["lifecycle"] != "active"):
            raise MemberServiceError(404, "not_found", "not found")
        return a

    # ------------------------------------------------------------------ reads
    def _row(self, a) -> dict:
        head = a["versions"][0] if a["versions"] else None
        return {"artifact_id": a["artifact_id"], "title": a["title"], "lifecycle": a["lifecycle"],
                "head_version_id": head["version_id"] if head else None,
                "head_version_number": head["version_number"] if head else None,
                "bytes": sum(f["size_bytes"] for f in (a["files"] if head else [])), "versions": list(a["versions"])}

    def _link_rows(self, u) -> list[dict]:
        return [{k: v for k, v in link.items() if not k.startswith("_")} for link in u["links"].values() if link["state"] != "pending"]

    def mine(self, actor):
        u = self._u(actor)
        arts = [self._row(a) for a in u["artifacts"].values()]
        used = sum(r["bytes"] for r in arts if r["lifecycle"] == "active")
        gone = sum(r["bytes"] for r in arts if r["lifecycle"] != "active")
        usage = {"bytes_used": used, "bytes_retracted": gone, "artifact_count": len(arts),
                 "version_count": sum(len(r["versions"]) for r in arts),
                 "limits": {"max_artifact_bytes": None, "max_member_bytes": None, "max_artifacts": None},
                 "sharing_disabled": False}
        return {"available": True, "updated": time.time(), "usage": usage, "artifacts": arts,
                "shared_with_me": list(u["shared"]), "links": self._link_rows(u)}

    def share_list(self, actor, artifact_id=None):
        links = [link for link in self._link_rows(self._u(actor))]
        if artifact_id is not None:
            links = [link for link in links if link["artifact_id"] == artifact_id]
        return {"links": links}

    def _readable(self, u, artifact_id):
        a = u["artifacts"].get(artifact_id)
        if a is not None:
            return self._row(a), True
        for s in u["shared"]:
            if s["artifact_id"] == artifact_id:
                return s, False
        raise MemberServiceError(404, "not_found", "not found")

    def head(self, actor, artifact_id):
        row, own = self._readable(self._u(actor), artifact_id)
        out = {"artifact_id": row["artifact_id"], "title": row["title"], "head_version_id": row["head_version_id"],
               "head_version_number": row["head_version_number"], "permission": "owner" if own else row["permissions"]}
        if own:
            out.update(lifecycle=row["lifecycle"], bytes=row["bytes"])
        return out

    def history(self, actor, artifact_id, limit=50):
        row, own = self._readable(self._u(actor), artifact_id)
        if own:
            versions = row["versions"]
            return {"artifact_id": artifact_id, "head_version_id": row["head_version_id"], "versions": versions[:limit],
                    "truncated": len(versions) > limit}
        head = {"version_id": row["head_version_id"], "version_number": row["head_version_number"]}
        return {"artifact_id": artifact_id, "head_version_id": row["head_version_id"], "versions": [head], "truncated": True}

    # ------------------------------------------------------------------ writes
    def create(self, actor, title, filename, profile, idempotency_key=None):
        u = self._u(actor)
        aid = "art_" + secrets.token_hex(6)
        with _LOCK:
            u["artifacts"][aid] = {"artifact_id": aid, "title": title, "lifecycle": "active", "versions": [], "files": []}
        return self._submit(u, {"result": "created", "artifact_id": aid})

    def publish(self, actor, artifact_id, *, base_version_id=None, files, message, entry=None, idempotency_key=None):
        u = self._u(actor)
        a = self._own(u, artifact_id)
        listed = []
        for f in files:
            up = u["uploads"].get(f.get("upload_id"))
            if up is None or len(up["parts"]) != up["total"]:
                return self._submit(u, {"result": "rejected", "reason": "upload not found"})
            data = b"".join(up["parts"][i] for i in range(up["total"]))
            if hashlib.sha256(data).hexdigest() != up["sha"]:
                return self._submit(u, {"result": "rejected", "reason": "upload failed verification"})
            listed.append({"name": up["name"], "size_bytes": up["size"]})
        head = a["versions"][0] if a["versions"] else None
        if (head["version_id"] if head else None) != base_version_id:
            return self._submit(u, {"result": "conflict", "head_version_id": head["version_id"] if head else None})
        number = (head["version_number"] if head else 0) + 1
        version = {"version_id": f"v_{artifact_id}_{number}", "version_number": number, "created_at": _iso(dt.datetime.now(dt.UTC)),
                   "message": message, "parent_version_ids": [head["version_id"]] if head else []}
        with _LOCK:
            a["versions"].insert(0, version)
            a["files"] = listed
            for f in files:
                u["uploads"].pop(f["upload_id"], None)
        return self._submit(u, {"result": "created", "version_id": version["version_id"], "version_number": number})

    def delete(self, actor, artifact_id, confirm):
        u = self._u(actor)
        a = self._own(u, artifact_id, active=False)
        with _LOCK:
            a["lifecycle"] = "tombstoned"
            for link in u["links"].values():
                if link["artifact_id"] == artifact_id and link["state"] in ("live", "pending"):
                    link["state"] = "revoked"
        return self._submit(u, {"result": "deleted"})

    def share_create(self, actor, artifact_id, version_id, audience, allow_groups=None, allow_emails=None,
                     expires_in_days=None, entry=None, title=None, idempotency_key=None):
        u = self._u(actor)
        owned = u["artifacts"].get(artifact_id)
        if owned is None and not any(s["artifact_id"] == artifact_id and ("share" in s["permissions"])
                                     for s in u["shared"]):
            raise MemberServiceError(404, "not_found", "not found")
        if audience == "public" and (not isinstance(expires_in_days, int) or not 1 <= expires_in_days <= 90):
            return self._submit(u, {"result": "rejected", "reason": "expiry_too_long"})
        sid = "shr_" + secrets.token_hex(6)
        days = expires_in_days
        expires = _iso(dt.datetime.now(dt.UTC) + dt.timedelta(days=days)) if days else None
        files = owned["files"] if owned else [{"name": "index.html", "size_bytes": 100}]
        summary = {"entry": files[0]["name"] if files else "index.html", "file_count": len(files),
                   "total_bytes": sum(f["size_bytes"] for f in files), "files": files, "audience": audience,
                   "expires_at": expires, "warnings": []}
        with _LOCK:
            u["links"][sid] = {"share_id": sid, "artifact_id": artifact_id, "version_id": version_id, "audience": audience,
                               "state": "pending", "expires_at": expires,
                               "recipients": [*(allow_emails or []), *(allow_groups or [])],
                               "created_at": _iso(dt.datetime.now(dt.UTC)), "mine": True, "_summary": summary}
        return self._submit(u, {"result": "created", "share_id": sid, "audience": audience, "summary": summary})

    def share_approve(self, actor, artifact_id, share_id, confirm, idempotency_key=None):
        u = self._u(actor)
        link = u["links"].get(share_id)
        if link is None or link["artifact_id"] != artifact_id:
            raise MemberServiceError(404, "not_found", "not found")
        with _LOCK:
            self.stats["approve_calls"] += 1
            self.stats["approve_keys"].append(idempotency_key)
            fault, self.fault_next_approve = self.fault_next_approve, None
        if fault:
            return self._submit(u, {"result": "rejected", "reason": fault})
        if link["state"] != "pending":
            return self._submit(u, {"result": "already_approved", "share_id": share_id, "audience": link["audience"]})
        link["state"] = "live"
        url = f"{self.origin}/s/{secrets.token_urlsafe(32)}/"
        return self._submit(u, {"result": "live", "share_id": share_id, "audience": link["audience"]}, secret=url)

    def share_revoke(self, actor, artifact_id, share_id):
        u = self._u(actor)
        link = u["links"].get(share_id)
        if link is None or link["artifact_id"] != artifact_id:
            raise MemberServiceError(404, "not_found", "not found")
        with _LOCK:
            self.stats["revokes"] += 1
        link["state"] = "revoked"
        return self._submit(u, {"result": "revoked", "share_id": share_id, "audience": link["audience"]})

    def share_events(self, actor, artifact_id, share_id):
        u = self._u(actor)
        if share_id not in u["links"]:
            raise MemberServiceError(404, "not_found", "not found")
        return self._submit(u, {"result": "listed", "share_id": share_id, "events": []})

    # ------------------------------------------------------------------ uploads
    def _key(self, u, artifact_id, name, size, sha):
        return upload_id_for(member_owner_key(u["principal"]), artifact_id, name, sha, size)

    def upload_status(self, actor, artifact_id, name, size_bytes, file_sha256):
        u = self._u(actor)
        self._own(u, artifact_id)
        uid = self._key(u, artifact_id, name, size_bytes, file_sha256)
        total = -(-size_bytes // CHUNK_BYTES)
        with _LOCK:
            self.stats["status_calls"] += 1
            have = set(u["uploads"].get(uid, {}).get("parts", {}))
        ranges, start = [], None
        for i in range(total + 1):
            absent = i < total and i not in have
            if absent and start is None:
                start = i
            elif not absent and start is not None:
                ranges.append([start, i - 1])
                start = None
        return {"upload_id": uid, "total": total, "missing_ranges": ranges, "complete": not ranges, "verified": False,
                "truncated": False}

    def upload_abort(self, actor, artifact_id, upload_id):
        u = self._u(actor)
        u["uploads"].pop(upload_id, None)
        return {"aborted": True}

    def precheck_run(self, actor, content_length):
        return None

    def upload_run(self, actor, artifact_id, name, size_bytes, file_sha256, first_index, data, run_sha256):
        u = self._u(actor)
        self._own(u, artifact_id)
        if hashlib.sha256(data).hexdigest() != run_sha256:
            raise MemberServiceError(400, "bad_request", "run sha256 does not match its data")
        with _LOCK:
            self._in_flight += 1
            self.stats["max_in_flight"] = max(self.stats["max_in_flight"], self._in_flight)
        try:
            if self.run_delay:
                time.sleep(self.run_delay)
            uid = self._key(u, artifact_id, name, size_bytes, file_sha256)
            total = -(-size_bytes // CHUNK_BYTES)
            pieces = [data[i:i + CHUNK_BYTES] for i in range(0, len(data), CHUNK_BYTES)]
            with _LOCK:
                self.stats["run_puts"] += 1
                up = u["uploads"].setdefault(uid, {"name": name, "size": size_bytes, "sha": file_sha256, "total": total, "parts": {}})
                replayed = all(first_index + k in up["parts"] for k in range(len(pieces)))
                if replayed:
                    self.stats["duplicate_runs"] += 1
                for k, piece in enumerate(pieces):
                    up["parts"][first_index + k] = piece
            return {"upload_id": uid, "replayed": replayed, "chunks": len(pieces)}
        finally:
            with _LOCK:
                self._in_flight -= 1


def control_handler(cognito: FakeCognito, mw: MemberWeb, fake: FakeMemberService):
    class Handler(BaseHTTPRequestHandler):
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
            if self.path == "/stats":
                with _LOCK:
                    return self._json(200, {**fake.stats, "logouts": cognito.logouts})
            return self._json(404, {})

        def do_POST(self):
            parts = urlsplit(self.path)
            if parts.path == "/user":
                name = parse_qs(parts.query).get("name", [""])[0]
                if name not in USERS:
                    return self._json(400, {})
                cognito.user = name
                return self._json(200, {"user": name})
            if parts.path == "/fault":
                fake.fault_next_approve = parse_qs(parts.query).get("approve", [""])[0] or None
                return self._json(200, {"fault": fake.fault_next_approve})
            if parts.path == "/rotate":
                mw.secret = secrets.token_bytes(48)
                return self._json(200, {"rotated": True})
            return self._json(404, {})
    return Handler


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="member-ui-"))
    servers: list = []
    srv = None
    try:
        tls = _self_signed(work)
        public_port, issuer_port = _free_port(), _free_port()
        host = f"127.0.0.1:{public_port}"
        origin = f"https://{host}"
        cognito = FakeCognito(origin + "/my/callback", origin + "/")
        auth = CognitoAuthenticator(pool_id=POOL, client_ids=[WEB_CLIENT], required_group="unused",
                                    jwks_fetch=lambda: cognito.jwks(), open_signup=True)
        svc = CollabService(auth, sink=FileEventLog(str(work / "log")),
                            approval=ApprovalPolicy(auto_groups=[GROUP], approver_groups=["Administrators"]),
                            public_config={"client_id": WEB_CLIENT, "hosted_ui_domain": f"https://127.0.0.1:{issuer_port}"},
                            member_group=GROUP)
        cfg = MembersConfig(http_chunks_per_request=int(os.environ.get("MEMBER_CHUNKS_PER_REQUEST", "2")),
                            session_exchanges_per_minute=200)
        fake = FakeMemberService(origin, cfg, int(os.environ.get("MEMBER_RESULT_DELAY_MS", "300")) / 1000,
                                 int(os.environ.get("MEMBER_RUN_DELAY_MS", "0")) / 1000)
        svc.member_service = fake
        mw = MemberWeb(svc, public_host=host, session_secret=secrets.token_bytes(48), session_seconds=3600,
                       web_root=ROOT / "packages")
        srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc, public_port=public_port,
                                 web_root=ROOT / "packages", member_web=mw)
        threading.Thread(target=srv.serve, daemon=True).start()
        srv.wait_until_ready()
        pub = srv._public_httpd
        pub.handle_error = lambda *_: None             # a browser rejecting the throw-away certificate is not news
        pub.socket = tls.wrap_socket(pub.socket, server_side=True, do_handshake_on_connect=False)
        issuer = ThreadingHTTPServer(("127.0.0.1", issuer_port), cognito.handler())
        issuer.daemon_threads = True
        issuer.handle_error = lambda *_: None          # a client rejecting the throw-away certificate is not news
        issuer.socket = tls.wrap_socket(issuer.socket, server_side=True, do_handshake_on_connect=False)
        control = ThreadingHTTPServer(("127.0.0.1", 0), control_handler(cognito, mw, fake))
        control.daemon_threads = True
        servers = [issuer, control]
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        print(f"PORT={public_port}", flush=True)
        print(f"CONTROL={control.server_address[1]}", flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
        stop.wait()
        return 0
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
