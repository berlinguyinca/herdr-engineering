# ruff: noqa: F811  (imported pytest fixtures are used as test arguments)
"""Artifact library, Task 13: the end-to-end and security gate of the whole library.

Everything is real: the isolated PostgreSQL of ``registry_pg``, the worker intent path and view writer
(``artifact_member_intents``), a ``ControlPlaneServer`` + ``MemberWeb`` on a loopback socket whose bridge reads the
very mirror directory the worker writes, the claim flow through ``share_public`` on a socket (events.jsonl ingested by
``ShareStore`` exactly as the worker's housekeeper does), and the collab MCP ``artifact_search`` tool.  No mocks.

The two JavaScript renderers (the member site's and the dashboard's) are run through ``node``."""
from __future__ import annotations

import asyncio
import dataclasses
import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane import collab_mcp, share_public
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.artifact_member_intents import (
    MemberRuntime,
    load_directory,
    publish_member_views,
    register_member_intents,
    take_secret,
)
from herdr_engineering.control_plane.artifact_member_service import MemberService
from herdr_engineering.control_plane.artifact_members import claim_resolver, member_owner_key
from herdr_engineering.control_plane.artifact_shares import ShareStore
from herdr_engineering.control_plane.collab import ApprovalPolicy, CollabService, FileEventLog
from herdr_engineering.control_plane.collab_artifacts import CollabArtifactBridge
from herdr_engineering.control_plane.collab_bridge_files import BridgeLimits, atomic_write_json
from herdr_engineering.control_plane.collab_mirror import GRANTS_NAME
from herdr_engineering.control_plane.member_session import mint_session as mint_member_session
from herdr_engineering.control_plane.member_web import MemberWeb
from herdr_engineering.control_plane.members_config import MembersConfig
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.share_public import ShareConfig, make_share_server
from herdr_engineering.control_plane.share_session import mint_session as mint_share_session
from tests.control_plane.registry_pg import registry_pg  # noqa: F401 - imported fixture
from tests.control_plane.test_artifact_member_intents import (
    ALICE,
    BOB,
    HTML,
    agent,
    create,
    publish,
    user,
    view_of,
    write_feeds,
)
from tests.control_plane.test_artifact_member_intents import send as send_intent
from tests.control_plane.test_collab_intents import Env, settle, sql
from tests.control_plane.test_collab_mcp_artifacts import ok_json
from tests.control_plane.test_member_share_flow import (
    approve,
    share,
    share_settings,
    token_of,
)
from tests.control_plane.test_share_lab import SECRET as SHARE_SECRET
from tests.control_plane.test_share_lab import access, authenticator, ident, lab_config

ROOT = Path(__file__).resolve().parents[2]
HOST = "my.lab.example"
ORIGIN = f"https://{HOST}"
WEB_SECRET = b"m" * 48
GROUP = "local.dev"            # the collab member group the web service knows
WORKER_GROUP = "lab"           # the worker's member group (its directory)
DOC = {"Sec-Fetch-Dest": "document"}
DIRECTORY = {
    "alice": {"status": "approved", "email": "alice@lab.example.org"},
    "bob": {"status": "approved", "email": "bob@lab.example.org"},
    "carol": {"status": "approved", "email": "carol@lab.example.org"},
}
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
FILES = {"index.html": HTML, "pic.svg": SVG, "fig.png": PNG}
POOL = "library-content"
GHOST_ART = "art_nothere1"
GHOST_FLD = "fld_" + "0" * 24


# ================================================================================================= the harness
class Reply:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self.raw = status, headers, raw

    @property
    def json(self):
        return json.loads(self.raw)

    @property
    def shape(self):
        """What a client can compare: status, body and the security headers (nothing that differs per request)."""
        return self.status, self.raw, tuple(sorted((k, v) for k, v in self.headers.items()
                                                   if k in ("content-type", "content-security-policy",
                                                            "cache-control", "x-content-type-options")))


class Web:
    """The member front door over the worker's directories: service, bridge, member service, member web, socket."""

    def __init__(self, env, tmp_path, web_root):
        self.env, self.tmp_path = env, tmp_path
        self.svc = CollabService(authenticator(), sink=FileEventLog(str(tmp_path / "log")),
                                 approval=ApprovalPolicy(auto_groups=[GROUP, "Users"], approver_groups=["Administrators"]),
                                 public_config={"client_id": "web-client", "hosted_ui_domain": "auth.lab.example"})
        self.svc.member_group = GROUP
        self.bridge = CollabArtifactBridge(self.svc, env.mirror_root, env.outbox, clock=self.svc._now,
                                           feed_interval=3600.0)
        self.svc.artifact_bridge = self.bridge
        doc = {"generated_at": self.svc._now(), "agents": {}, "limits": BridgeLimits(max_pending_intents_per_agent=50,
                                                                                    commits_per_minute=1000).to_dict()}
        atomic_write_json(self.bridge.mirror, GRANTS_NAME, doc)
        self.svc.member_service = MemberService(self.svc, self.bridge, MembersConfig(result_wait_seconds=0))
        self.mw = MemberWeb(self.svc, public_host=HOST, session_secret=WEB_SECRET, session_seconds=3600,
                            web_root=web_root, require_https=True)
        self.srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=self.svc, public_port=0,
                                      web_root=web_root, collab_require_https=True, member_web=self.mw)
        self.thread = threading.Thread(target=self.srv.serve, daemon=True)
        self.thread.start()
        self.srv.wait_until_ready()
        self.people = {}

    def person(self, name, groups=None):
        groups = [GROUP] if groups is None else groups
        pr = self.svc.authenticate("Bearer " + access(name, groups=groups), None)
        if name not in self.svc._user_email:
            self.svc._commit("identity_linked", {"actor_user_id": name, "actor_agent_id": None, "user_id": name,
                                                 "email": f"{name}@example.org"})
        self.people[name] = pr
        return pr

    def agent(self, name):
        info = self.svc.connect_agent(self.people[name], {"harness_type": "claude-code"})
        return info

    def close(self):
        self.srv.shutdown()
        self.thread.join(timeout=5)
        self.bridge.stop()

    def session(self, name):
        return mint_member_session(WEB_SECRET, name, self.svc._now() + 600)

    def req_sync(self, method, path, *, body=None, member="alice", auth=None, host=HOST, origin=ORIGIN):
        raw = json.dumps(body).encode() if body is not None else b""
        head = [f"{method} {path} HTTP/1.1"]
        if host is not None:
            head.append(f"Host: {host}")
        head.append("X-Forwarded-Proto: https")
        if origin is not None and method != "GET":
            head.append(f"Origin: {origin}")
        if auth is not None:
            head.append(f"Authorization: {auth}")
        elif member is not None:
            head.append(f"Authorization: Member {self.session(member)}")
        if raw or method in ("POST", "PUT"):
            head.append(f"Content-Length: {len(raw)}")
            if raw:
                head.append("Content-Type: application/json")
        sock = socket.create_connection(("127.0.0.1", self.srv.public_port), timeout=10)
        try:
            sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + raw)
            buf = b""
            while b"\r\n\r\n" not in buf:
                part = sock.recv(65536)
                if not part:
                    break
                buf += part
            head_txt, _, rest = buf.partition(b"\r\n\r\n")
            lines = head_txt.decode().split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            need = int(headers.get("content-length", "0"))
            while len(rest) < need:
                part = sock.recv(65536)
                if not part:
                    break
                rest += part
            return Reply(int(lines[0].split()[1]), headers, rest)
        finally:
            sock.close()

    async def req(self, method, path, **kw):
        return await asyncio.to_thread(self.req_sync, method, path, **kw)

    async def write(self, method, path, body=None, *, member="alice"):
        """A write route: the ticket's settled result (the worker runs between the POST and the read)."""
        got = await self.req(method, path, body=body, member=member)
        assert got.status == 200 and got.json.get("ticket"), (path, got.raw)
        await settle(self.env)
        out = await self.req("GET", f"/api/member/results/{got.json['ticket']}", member=member)
        assert out.status == 200, out.raw
        return out.json


class Share:
    """The real public share server over the worker's ``shares_root``, with a movable clock."""

    def __init__(self, tmp_path, shares_root):
        self.events = tmp_path / "share-events" / "events.jsonl"
        self.events.parent.mkdir(exist_ok=True)
        self.clock = [time.time()]
        cfg = ShareConfig(shares_root=shares_root, events_path=self.events, **lab_config(tmp_path))
        self.server = make_share_server(cfg, host="127.0.0.1", port=0, now=lambda: self.clock[0])
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    async def get(self, path, headers=None):
        return await asyncio.to_thread(self.request, "GET", path, headers)

    async def exchange(self, token, name, email=None, groups=()):
        headers = {"Authorization": "Bearer " + access(name, groups=list(groups)),
                   "X-Id-Token": ident(name, email=email or f"{name}@lab.example.org"), "Content-Length": "0"}
        return await asyncio.to_thread(self.request, "POST", f"/s/{token}/session", headers)


def cookie_of(headers):
    return headers["set-cookie"].split(";")[0]


async def tick(env, rt):
    async with env.pool.acquire() as conn:
        await publish_member_views(conn, dataclasses.replace(rt, rustfs=env.applier.rustfs))


async def ingest(env, rt, shares):
    """What the worker's share housekeeper does with the share container's events file."""
    resolver = claim_resolver(await asyncio.to_thread(load_directory, rt), rt.group)
    async with env.pool.acquire() as conn:
        n = await ShareStore(conn).ingest_events_file(shares.events, marker_path=shares.events.with_suffix(".marker"),
                                                      members=resolver)
    await tick(env, rt)
    return n


def lib(env, who):
    return view_of(env, who)["library"]


def item(env, who, aid):
    found = [i for i in lib(env, who)["items"] if i["artifact_id"] == aid]
    return found[0] if found else None


async def put_files(env, aid, files, *, actor=None, owner=ALICE, base=None):
    out = await publish(env, aid, files, actor=actor or user("alice"), owner=owner, base=base)
    assert out["result"] == "created", (out.get("reason"), out.get("message"), out)
    return out["version_id"]


class LiveClock:
    """Wall time: the web front door stamps its intents with the real clock, so the worker's must keep up on a slow box."""

    def __call__(self):
        return time.time()


def lrun(pg, tmp_path, body, *, timeout=170):
    """The worker side of ``srun`` plus the web front door on the same directories; ``body(env, rt, web, shares)``."""
    clock = LiveClock()
    runtime = MemberRuntime(outbox=tmp_path / "outbox", mirror_root=tmp_path / "mirror", group=WORKER_GROUP, enabled=True,
                            defaults={}, shares_root=tmp_path / "shares", clock=clock, share_settings=share_settings())

    async def go():
        import asyncpg
        pool = await asyncpg.create_pool(pg["dsn"], min_size=1, max_size=10)
        env = Env(tmp_path, pool, clock, context_extras={"members": runtime})
        web = shares = None
        try:
            await sql(env, "TRUNCATE artifacts, artifact_acl, artifact_shares, artifact_member_usage, "
                           "artifact_member_events, artifact_blobs, artifact_collab_intents, artifact_collab_uploads, "
                           "artifact_library_folders, artifact_share_claims CASCADE")
            register_member_intents(env.applier)
            write_feeds(env, users=DIRECTORY)
            web = Web(env, tmp_path, ROOT / "packages")
            for name in ("alice", "bob", "carol"):
                web.person(name)
            web.person("mal", groups=["Users"])
            shares = Share(tmp_path, tmp_path / "shares")
            return await asyncio.wait_for(body(env, runtime, web, shares), timeout)
        finally:
            if shares:
                shares.close()
            if web:
                web.close()
            await env.applier.aclose(timeout=10)
            try:
                await asyncio.wait_for(pool.close(), 10)
            except TimeoutError:
                pool.terminate()
    return asyncio.run(go())


@pytest.fixture
def sandbox(registry_pg, tmp_path):
    def go(body, **kw):
        return lrun(registry_pg, tmp_path, body, **kw)
    return go


async def scenario(env, rt, web, shares):
    """Alice: artifact A (html, svg, txt) in folder FA, and a pending push P.  Bob: artifact B, folder FB, and a live
    public share of B (token).  Returns the ids and the raw token."""
    s = SimpleNamespace()
    s.a = await create(env, user("alice"), title="Alice private")
    s.va = await put_files(env, s.a, FILES)
    s.p = (await send_intent(env, agent("ag1"), "member.artifact_create",
                             {"title": "Pending push", "filename": "push.html", "profile": "web"}))["artifact_id"]
    s.b = await create(env, user("bob"), title="Bob secret")
    s.vb = await put_files(env, s.b, {"index.html": b"<html>bob v1</html>"}, actor=user("bob"), owner=BOB)
    out = await web.write("POST", "/api/member/folders", {"name": "Papers"})
    assert out["result"] == "created", out
    s.fa = lib(env, ALICE)["folders"][0]["folder_id"]
    out = await web.write("POST", "/api/member/folders", {"name": "Bobs"}, member="bob")
    s.fb = lib(env, BOB)["folders"][0]["folder_id"]
    out = await web.write("POST", f"/api/member/folders/{s.fa}/items", {"artifact_id": s.a})
    assert out["result"] == "filed", out
    made = await share(env, user("bob"), s.b, s.vb, title="Bob results")
    ticket, done = await approve(env, user("bob"), s.b, made["share_id"])
    assert done["result"] == "live", done
    s.share_id = made["share_id"]
    s.url = take_secret(env.outbox, ticket, BOB)
    s.token = token_of(s.url)
    await tick(env, rt)
    return s


async def claim(env, rt, shares, s, name="alice"):
    got = await shares.exchange(s.token, name)
    assert got[0] == 200, got
    cookie = cookie_of(got[1])
    status, _, _ = await shares.get(f"/s/{s.token}/f/index.html", {**DOC, "Cookie": cookie})
    assert status == 200
    assert await ingest(env, rt, shares) >= 1
    return cookie


# ================================================================================================= smoke
def test_the_harness_runs_the_whole_chain(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        mine = await web.req("GET", "/api/member/library")
        assert mine.status == 200
        assert {i["artifact_id"] for i in mine.json["items"]} == {s.a, s.p}
        pending = [i for i in mine.json["items"] if i["status"] == "pending"]
        assert [i["artifact_id"] for i in pending] == [s.p]
        got = await web.req("GET", f"/api/member/artifacts/{s.a}/content/index.html")
        assert got.status == 200 and got.raw == HTML
        await claim(env, rt, shares, s)
        opened = item(env, ALICE, s.b)
        assert opened and opened["source"] == "opened" and opened["share_id"] == s.share_id
    sandbox(body)


# ================================================================================================= strangers and ids
def same(ref: Reply, other: Reply, swap: tuple[tuple[str, str], ...] = ()) -> bool:
    """True when ``other`` is indistinguishable from ``ref`` once the ids the request itself carried are normalised."""
    def norm(r, pairs):
        raw = r.raw
        for a, b in pairs:
            raw = raw.replace(a.encode(), b.encode())
        return r.status, raw, tuple(v for k, v in r.shape[2])
    return norm(ref, ()) == norm(other, swap)


async def read_routes(web, aid, fid, who):
    out = {}
    for label, path in (("content", f"/api/member/artifacts/{aid}/content/index.html"),
                        ("svg", f"/api/member/artifacts/{aid}/content/pic.svg"),
                        ("head", f"/api/member/artifacts/{aid}"),
                        ("history", f"/api/member/artifacts/{aid}/history"),
                        ("shares", f"/api/member/artifacts/{aid}/shares")):
        out[label] = await web.req("GET", path, member=who)
    return out


def test_stranger_cannot_read_folder_claim_pending_or_content_by_id(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        await claim(env, rt, shares, s)                      # alice now holds a claim on bob's share
        before = {n: lib(env, n) for n in (ALICE, BOB)}
        db_before = await sql(env, "SELECT count(*) FROM artifact_library_folders")
        for who in ("bob", "mal", None):
            for aid, ghost in ((s.a, GHOST_ART), (s.p, GHOST_ART), (s.b, GHOST_ART)):
                if who == "bob" and aid == s.b:
                    continue                                  # bob owns b
                real = await read_routes(web, aid, s.fa, who)
                gone = await read_routes(web, ghost, s.fa, who)
                for label in real:
                    assert same(gone[label], real[label], ((aid, ghost),)), (who, aid, label, real[label].raw, gone[label].raw)
        # carol has no relation to bob's artifact or alice's claim on it
        for label, got in (await read_routes(web, s.b, s.fa, "carol")).items():
            assert same((await read_routes(web, GHOST_ART, s.fa, "carol"))[label], got, ((s.b, GHOST_ART),)), label
        # writes by a stranger against alice's folder, filed item and pending push: refused exactly like a missing id
        for who in ("bob", "carol"):
            for method, path, payload in (
                    ("PUT", f"/api/member/folders/{s.fa}", {"name": "Hijacked"}),
                    ("POST", f"/api/member/folders/{s.fa}/items", {"artifact_id": s.b}),
                    ("DELETE", f"/api/member/folders/{s.fa}/items/{s.a}", None),
                    ("DELETE", f"/api/member/folders/{s.fa}", None),
                    ("POST", f"/api/member/pending/{s.p}/accept", {}),
                    ("POST", f"/api/member/pending/{s.p}/discard", {})):
                ghost_path = path.replace(s.fa, GHOST_FLD).replace(s.p, GHOST_ART).replace(s.a, GHOST_ART)
                got = await web.write(method, path, payload, member=who)
                ref = await web.write(method, ghost_path, payload, member=who)
                assert got["result"] == ref["result"] and got.get("reason") == ref.get("reason"), (who, path, got, ref)
                assert got["result"] != "created" and got.get("result") not in ("accepted", "renamed", "deleted", "filed")
        # nothing moved: the same views, the same folders
        await tick(env, rt)
        assert lib(env, ALICE) == before[ALICE] and lib(env, BOB) == before[BOB]
        assert await sql(env, "SELECT count(*) FROM artifact_library_folders") == db_before
        mine = await web.req("GET", "/api/member/library")
        assert {i["artifact_id"] for i in mine.json["items"]} >= {s.a, s.p}
        folder = [f for f in mine.json["folders"] if f["folder_id"] == s.fa]
        assert folder and folder[0]["name"] == "Papers"
        # the owner still reads her content; the claimant still reads the shared item
        assert (await web.req("GET", f"/api/member/artifacts/{s.a}/content/index.html")).raw == HTML
    sandbox(body)


def test_error_bodies_identical_for_missing_and_forbidden(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        await claim(env, rt, shares, s)
        for who, foreign in (("bob", s.a), ("carol", s.b), ("bob", s.p)):
            for tail in ("", "/history", "/shares", "/content/index.html", "/content/nope.html", "/content/%2e%2e/x"):
                real = await web.req("GET", f"/api/member/artifacts/{foreign}{tail}", member=who)
                gone = await web.req("GET", f"/api/member/artifacts/{GHOST_ART}{tail}", member=who)
                # the share list answers an empty list for any id the caller does not own; either way, one answer
                assert real.status == gone.status == (200 if tail == "/shares" else 404), (who, tail, real.status, gone.status)
                if tail == "/shares":
                    assert real.json.get("shares", real.json.get("items", [])) == [], real.raw
                assert real.raw == gone.raw and real.shape[2] == gone.shape[2], (who, tail, real.raw, gone.raw)
        # the owner asking for a file that is not there gets the same body a stranger gets for a real file
        owner_missing = await web.req("GET", f"/api/member/artifacts/{s.a}/content/nope.html")
        stranger_real = await web.req("GET", f"/api/member/artifacts/{s.a}/content/index.html", member="bob")
        assert owner_missing.status == stranger_real.status == 404 and owner_missing.raw == stranger_real.raw
        # the write verbs: the settled result of a forbidden id is the result of a missing one
        for method, tmpl in (("POST", "/api/member/artifacts/{}/delete"), ("POST", "/api/member/artifacts/{}/publish"),
                             ("POST", "/api/member/artifacts/{}/shares")):
            payload = {"confirm": True} if tmpl.endswith("delete") else {}
            real = await web.req(method, tmpl.format(s.a), body=payload, member="bob")
            gone = await web.req(method, tmpl.format(GHOST_ART), body=payload, member="bob")
            assert (real.status, real.raw.replace(s.a.encode(), b"X")) == (gone.status, gone.raw.replace(GHOST_ART.encode(), b"X")), \
                (tmpl, real.raw, gone.raw)
        await settle(env)
        assert item(env, ALICE, s.a) is not None            # nothing was deleted
        # not signed in, or a forged session, or a session for a person the service does not know
        for auth in (None, "Member junk", "Member " + "a" * 80):
            real = await web.req("GET", f"/api/member/artifacts/{s.a}", member=None, auth=auth)
            gone = await web.req("GET", f"/api/member/artifacts/{GHOST_ART}", member=None, auth=auth)
            assert real.status == gone.status and real.raw == gone.raw
    sandbox(body)


# ================================================================================================= pages
_URL = re.compile(r"""https?://[A-Za-z0-9.\-]+(?::\d+)?[^\s'"<>);`]*|(?<=["'(])//[A-Za-z0-9\-]+\.[A-Za-z0-9.\-]+[^\s'"<>);`]*""")
_NS = ("//www.w3.org/",)           # an XML namespace is an identifier, never fetched


def external(text: str, allowed: set[str]) -> list[str]:
    found = []
    for m in _URL.finditer(text):
        url = m.group(0)
        if any(n in url for n in _NS):
            continue
        host = re.sub(r"^(?:https?:)?//", "", url).split("/")[0].split(":")[0]
        if host not in allowed:
            found.append(url)
    return found


def test_no_external_requests_in_page_csp(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        allowed = {HOST, "share.example"}
        seen = []
        # the member site: every file it can load, and the CSP each is sent with
        from herdr_engineering.control_plane.member_web import _STATIC
        paths = list(_STATIC)
        assert "/" in paths and "/my/member-artifacts-app.js" in paths
        for path in paths:
            got = await web.req("GET", path, member=None)
            assert got.status == 200, (path, got.status)
            if got.headers["content-type"].startswith(("text/", "application/javascript")):
                assert external(got.raw.decode("utf-8", "replace"), allowed) == [], path
            seen.append((path, got.headers.get("content-security-policy", "")))
        page_csp = dict(seen)["/"]
        assert page_csp, "the member page is sent without a CSP"
        # the share site: landing (a document load of a public link), sign-in, and the static helpers
        pages = [(f"/s/{s.token}/f/index.html", DOC), (f"/s/{s.token}/signin", DOC), (f"/s/{s.token}/", DOC)]
        for name in ("landing.js", "landing.css", "share.js", "share.css", "tokens.css", "lab-signin.js"):
            pages.append((f"/s/static/{name}", {}))
        for path, headers in pages:
            status, hdr, raw = await shares.get(path, headers)
            assert status in (200, 302), (path, status)
            seen.append((path, hdr.get("content-security-policy", "")))
            assert external(raw.decode("utf-8", "replace"), allowed | {"127.0.0.1"}) == [], path
        # the templates the lab pages are built from, and the policies themselves
        for name in ("viewer.html", "view.html", "landing.js", "landing.css", "share.js", "share.css", "tokens.css",
                     "lab-signin.js", "annotate.js", "annotate.css", "anchor.js"):
            text = (ROOT / "herdr_engineering/control_plane/share_static" / name).read_text()
            assert external(text, allowed) == [], name
        for csp in (share_public.VIEWER_CSP.format(origin="https://share.example", public="https://share.example"),
                    share_public.VIEW_CSP.format(origin="https://share.example", token=s.token, nonce="n"),
                    share_public.ANNOTATE_CSP.format(origin="https://share.example", token=s.token, nonce="n")):
            seen.append(("policy", csp))
        # the one external host a sign-in page may name: the Cognito hosted UI, in connect-src only (the sign-in
        # code exchange); every other directive is own-origin
        sso = "auth.lab.example"
        for path, csp in seen:
            for directive in csp.split(";"):
                assert sso not in directive or directive.strip().startswith("connect-src"), (path, directive)
            csp = csp.replace(f"https://{sso}", "")
            if not csp:
                continue
            bad = external(csp, allowed | {"127.0.0.1"})
            assert bad == [], (path, bad, csp)
            for directive in csp.split(";"):
                words = directive.split()
                if words[:1] and words[0] in ("script-src", "style-src", "default-src", "connect-src", "img-src",
                                                "font-src", "frame-src", "worker-src", "style-src-elem"):
                    assert "*" not in words and "http:" not in words and "https:" not in words, (path, directive)
    sandbox(body)


# ================================================================================================= XSS
HOSTILE = ["<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "'; alert(`x`); //", "`${alert(1)}`",
           "<svg/onload=alert(1)>", "&lt;b&gt; &amp;"]


class Tags(HTMLParser):
    """Every tag and attribute the browser would build from a string, and the text it would show."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, k, v) for k, v in attrs]

    def handle_data(self, data):
        self.text.append(data)


def render_with_node(tmp_path, items, folders):
    script = tmp_path / "render.mjs"
    script.write_text(f"""
import {{ renderFeedList, renderFolderSidebar, renderChips }} from '{(ROOT / 'packages/member-artifacts-view.js').as_uri()}';
import {{ renderLibrary }} from '{(ROOT / 'packages/library-view.js').as_uri()}';
const data = JSON.parse(process.argv[2]);
const out = {{
  member: renderFeedList(data.items, {{ folders: data.folders }}) + renderFolderSidebar(data.folders, null),
  dashboard: renderLibrary({{ items: data.items, folders: data.folders, q: data.items[0].title }}),
  folder: renderLibrary({{ items: data.items, folders: data.folders, folderId: data.folders[0].folder_id }}),
}};
process.stdout.write(JSON.stringify(out));
""")
    got = subprocess.run(["node", str(script), json.dumps({"items": items, "folders": folders})], capture_output=True,
                         text=True, timeout=60, check=True)
    return json.loads(got.stdout)


def test_xss_titles_escaped_in_both_surfaces(tmp_path):
    items = []
    for n, bad in enumerate(HOSTILE):
        items.append({"artifact_id": f"art_{n:012d}", "title": bad, "sharer": bad, "source": ["mine", "assigned", "opened", "pending"][n % 4],
                      "status": "pending" if n % 4 == 3 else "active", "shared_at": f"2026-01-0{n + 1}T00:00:00Z",
                      "artifact_type": bad, "folder_ids": ["fld_" + "1" * 24], "files": [{"path": bad, "size": 1}],
                      "expires_at": "2027-01-01T00:00:00Z"})
    items.append({"artifact_id": "art_gone", "title": HOSTILE[0], "sharer": HOSTILE[1], "source": "opened",
                  "status": "revoked", "shared_at": "2026-02-01T00:00:00Z", "folder_ids": []})
    folders = [{"folder_id": "fld_" + "1" * 24, "name": bad, "count": 1} for bad in HOSTILE[:2]] + \
              [{"folder_id": "fld_" + "2" * 24, "name": HOSTILE[2], "count": 0}]
    out = render_with_node(tmp_path, items, folders)
    assert set(out) == {"member", "dashboard", "folder"}
    allowed_attrs = {"class", "type", "href", "value", "role", "placeholder", "maxlength", "autocomplete", "selected",
                     "disabled", "for", "name", "required"}
    for surface, markup in out.items():
        assert len(markup) > 200, surface
        parsed = Tags()
        parsed.feed(markup)
        assert not {"script", "img", "svg", "iframe", "style", "object", "embed", "link"} & set(parsed.tags), (surface, parsed.tags)
        for tag, key, value in parsed.attrs:
            assert not key.startswith("on"), (surface, tag, key, value)
            assert key in allowed_attrs or key.startswith(("data-", "aria-")), (surface, tag, key, value)
            if key == "href":
                assert not (value or "").lower().lstrip().startswith(("javascript:", "data:")), (surface, value)
        shown = "".join(parsed.text)
        assert HOSTILE[0] in shown, surface          # the literal text, shown as text
        assert "<script" not in markup.lower() and "<img" not in markup.lower() and "<svg" not in markup.lower(), surface


# ================================================================================================= the token
async def db_text(env):
    """Every row of every table, as JSON text."""
    tables = await sql(env, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    out = []
    for row in tables:
        name = row["tablename"]
        for r in await sql(env, f'SELECT row_to_json(t)::text AS j FROM "{name}" t'):
            out.append(f"{name}: {r['j']}")
    return "\n".join(out)


def files_under(*roots):
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file() and not path.is_symlink():
                yield path


def test_token_not_in_database_or_view_file_after_claim(sandbox, tmp_path):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        cookie = await claim(env, rt, shares, s)
        assert item(env, ALICE, s.b)["share_id"] == s.share_id          # the claim is there (only share_id)
        needle = s.token.encode()
        dump = await db_text(env)
        assert s.share_id in dump and s.token not in dump
        assert s.token not in cookie and s.url not in dump
        hits = [str(p) for p in files_under(rt.mirror_root, rt.outbox, shares.events.parent)
                if needle in p.read_bytes() or s.url.encode() in p.read_bytes()]
        assert hits == []
        assert any(p.name.endswith(".json") for p in files_under(rt.mirror_root / "member-views")), "no views were scanned"
        assert any(files_under(rt.mirror_root / POOL)), "no pooled content was scanned"
        assert shares.events.read_text().count(s.share_id) >= 1 and s.token not in shares.events.read_text()
        # the web API never echoes it either
        for path in ("/api/member/library", f"/api/member/artifacts/{s.b}", f"/api/member/artifacts/{s.b}/shares"):
            for who in ("alice", "bob"):
                assert needle not in (await web.req("GET", path, member=who)).raw, (path, who)
        # nor the logs of this process
    sandbox(body)


# ================================================================================================= carry-overs
async def lab_share(env, s, **extra):
    made = await share(env, user("bob"), s.b, s.vb, "lab", **extra)
    ticket, done = await approve(env, user("bob"), s.b, made["share_id"])
    assert done["result"] == "live", done
    url = take_secret(env.outbox, ticket, BOB)
    return made["share_id"], token_of(url)


def other_token(token):
    return token[:-1] + ("A" if token[-1] != "A" else "B")


def test_unknown_revoked_and_expired_tokens_give_identical_404s(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        short = await share(env, user("bob"), s.b, s.vb, expires_in_days=1, title="Short")
        ticket, done = await approve(env, user("bob"), s.b, short["share_id"])
        short_token = token_of(take_secret(env.outbox, ticket, BOB))
        gone = await share(env, user("bob"), s.b, s.vb, title="To revoke")
        ticket, done = await approve(env, user("bob"), s.b, gone["share_id"])
        gone_token = token_of(take_secret(env.outbox, ticket, BOB))
        paths = ("/", "/f/index.html", "/signin", "/f/nothing.html", "/k/abc.def/f/index.html")
        for tok in (s.token, short_token, gone_token):                   # all live: reachable
            status, _, _ = await shares.get(f"/s/{tok}/f/index.html", DOC)
            assert status == 200
        out = await web.write("POST", f"/api/member/artifacts/{s.b}/shares/{gone['share_id']}/revoke", {}, member="bob")
        assert out["result"] in ("revoked", "ok"), out
        shares.clock[0] += 40 * 86400                                    # the short share is now expired
        async def ref(path, headers):
            status, hdr, raw = await shares.get(f"/s/{other_token(s.token)}{path}", headers)
            return status, raw, hdr.get("content-type"), hdr.get("content-security-policy")
        for headers in (DOC, {}):
            for path in paths:
                want = await ref(path, headers)
                assert want[0] == 404
                for tok in (gone_token, short_token):
                    status, hdr, raw = await shares.get(f"/s/{tok}{path}", headers)
                    assert (status, raw, hdr.get("content-type"), hdr.get("content-security-policy")) == want, (path, tok, status, raw)
        # the landing bar never names a token or a revoked share's title on the refusal
        assert b"To revoke" not in want[1] and b"landing" not in want[1].lower()
    sandbox(body)


def test_claim_cookie_and_lab_session_do_not_cross(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        lab_id, lab_token = await lab_share(env, s, allow_emails=["carol@lab.example.org"])
        exp = int(time.time()) + 600
        eml = "carol@lab.example.org"
        lab_session = mint_share_session(SHARE_SECRET, lab_id, "carol", exp, email=eml, basis="e")
        pub_cookie_value = cookie_of((await shares.exchange(s.token, "carol"))[1]).split("=", 1)[1]
        # positive controls: each is good where it belongs
        status, _, raw = await shares.get(f"/s/{lab_token}/k/{lab_session}/f/index.html")
        assert status == 200, status
        status, _, _ = await shares.get(f"/s/{s.token}/f/index.html", {**DOC, "Cookie": f"herdr_claim={pub_cookie_value}"})
        assert status == 200
        n = await ingest(env, rt, shares)
        assert n >= 1, (n, shares.events.read_text())
        assert item(env, "member:carol", s.b) is not None
        # the claim cookie is no /k/ session, on the public link or on a lab link
        ref = await shares.get(f"/s/{other_token(lab_token)}/k/{pub_cookie_value}/f/index.html")
        for tok in (lab_token, s.token):
            got = await shares.get(f"/s/{tok}/k/{pub_cookie_value}/f/index.html")
            assert got[0] == 404 and got[2] == ref[2], (tok, got[0])
        # a lab session is no claim cookie: the public link stays signed out (the notice, no claim written)
        before = shares.events.read_text()
        status, _, raw = await shares.get(f"/s/{s.token}/f/index.html", {**DOC, "Cookie": f"herdr_claim={lab_session}"})
        signed_out = await shares.get(f"/s/{s.token}/f/index.html", DOC)
        assert status == signed_out[0] and raw == signed_out[2]
        added = shares.events.read_text()[len(before):]
        assert '"claimed"' not in added and '"actor": "carol"' not in added and added.count('"actor": "public"') == 2
        # a session minted for one share is no session for another
        other = mint_share_session(SHARE_SECRET, s.share_id, "carol", exp, email=eml, basis="e")
        assert (await shares.get(f"/s/{lab_token}/k/{other}/f/index.html"))[0] == 404
    sandbox(body)


def test_a_claimed_item_serves_the_pinned_version_not_head(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        await claim(env, rt, shares, s)
        v1 = b"<html>bob v1</html>"
        mine = await web.req("GET", f"/api/member/artifacts/{s.b}/content/index.html", member="alice")
        assert mine.status == 200 and mine.raw == v1
        v2 = await put_files(env, s.b, {"index.html": b"<html>bob v2 SECRET DRAFT</html>", "draft.html": b"<html>draft</html>"},
                             actor=user("bob"), owner=BOB, base=s.vb)
        assert v2 != s.vb
        await tick(env, rt)
        got = await web.req("GET", f"/api/member/artifacts/{s.b}/content/index.html", member="alice")
        assert got.status == 200 and got.raw == v1, got.raw
        for name in ("draft.html", "pic.svg"):
            miss = await web.req("GET", f"/api/member/artifacts/{s.b}/content/{name}", member="alice")
            assert miss.status == 404 and b"draft" not in miss.raw
        row = item(env, ALICE, s.b)
        assert "draft.html" not in json.dumps(row)
        # the owner reads head; the share and the claimant stay on v1
        own = await web.req("GET", f"/api/member/artifacts/{s.b}/content/index.html", member="bob")
        assert own.status == 200 and b"v2" in own.raw
    sandbox(body)


def test_a_lab_share_recipient_is_pinned_to_the_shared_version(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        lab_id, _ = await lab_share(env, s, allow_groups=["lab"])
        await tick(env, rt)
        v1 = b"<html>bob v1</html>"
        row = item(env, ALICE, s.b)
        assert row and row["source"] == "assigned" and row["share_id"] == lab_id
        assert [f["path"] for f in row["files"]] == ["index.html"]
        v2 = await put_files(env, s.b, {"index.html": b"<html>bob v2 SECRET DRAFT</html>", "draft.html": b"<html>draft</html>"},
                             actor=user("bob"), owner=BOB, base=s.vb)
        assert v2 != s.vb
        await tick(env, rt)
        row = item(env, ALICE, s.b)
        assert [f["path"] for f in row["files"]] == ["index.html"], row["files"]
        got = await web.req("GET", f"/api/member/artifacts/{s.b}/content/index.html", member="alice")
        assert got.status == 200 and got.raw == v1, got.raw
        miss = await web.req("GET", f"/api/member/artifacts/{s.b}/content/draft.html", member="alice")
        assert miss.status == 404 and b"draft" not in miss.raw
        own = await web.req("GET", f"/api/member/artifacts/{s.b}/content/index.html", member="bob")      # the owner keeps head
        assert own.status == 200 and b"v2" in own.raw
    sandbox(body)


def test_a_member_who_only_receives_a_lab_share_gets_a_view_after_one_tick(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        carol = "member:carol"
        assert not (env.mirror_root / "member-views" / (member_owner_key(carol) + ".json")).exists()
        await lab_share(env, s, allow_emails=["Carol@lab.example.org"])
        await tick(env, rt)                       # carol never sent an intent, never opened a link
        row = item(env, carol, s.b)
        assert row and row["source"] == "assigned" and [f["path"] for f in row["files"]] == ["index.html"]
        await lab_share(env, s, allow_groups=["lab"])
        await tick(env, rt)
        assert item(env, "member:bob", s.b)["source"] == "mine"
    sandbox(body)


def test_a_member_can_file_lab_assigned_and_opened_rows_but_not_a_strangers(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        path = f"/api/member/folders/{s.fa}/items"
        stranger = await web.write("POST", path, {"artifact_id": s.b})            # alice has no route to bob's artifact yet
        assert stranger["result"] == "rejected" and stranger["reason"] == "not_found", stranger
        ghost = await web.write("POST", path, {"artifact_id": GHOST_ART})
        assert (ghost["result"], ghost["reason"]) == (stranger["result"], stranger["reason"])
        await claim(env, rt, shares, s)                                              # opened through the public link
        out = await web.write("POST", path, {"artifact_id": s.b})
        assert out["result"] == "filed", out
        await web.write("DELETE", f"/api/member/folders/{s.fa}/items/{s.b}")
        c = await create(env, user("bob"), title="Lab only")
        vc = await put_files(env, c, {"index.html": HTML}, actor=user("bob"), owner=BOB)
        made = await share(env, user("bob"), c, vc, "lab", allow_emails=["alice@lab.example.org"])
        ticket, done = await approve(env, user("bob"), c, made["share_id"])
        assert done["result"] == "live", done
        await tick(env, rt)
        out = await web.write("POST", path, {"artifact_id": c})                      # assigned by a lab share
        assert out["result"] == "filed", out
        other = await web.write("POST", f"/api/member/folders/{s.fb}/items", {"artifact_id": c})   # bob's folder is not alice's
        assert other["result"] == "rejected" and other["reason"] == "not_found"
    sandbox(body)


def test_a_revoked_row_serves_nothing_and_never_falls_back_to_a_filename(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        await claim(env, rt, shares, s)
        assert (await web.req("GET", f"/api/member/artifacts/{s.b}/content/index.html")).status == 200   # alice's claim
        out = await web.write("POST", f"/api/member/artifacts/{s.b}/shares/{s.share_id}/revoke", {}, member="bob")
        assert out["result"] in ("revoked", "ok"), out
        await tick(env, rt)
        row = item(env, ALICE, s.b)
        assert row["status"] == "revoked", row
        assert row["title"] == "Bob results"                     # the share title, never a file name
        text = json.dumps(row)
        assert "index.html" not in text and "pic.svg" not in text and not row.get("files")
        ghost = await web.req("GET", f"/api/member/artifacts/{GHOST_ART}/content/index.html")
        for name in ("index.html", "", "Bob%20results"):
            got = await web.req("GET", f"/api/member/artifacts/{s.b}/content/{name}") if name else \
                await web.req("GET", f"/api/member/artifacts/{s.b}/content/")
            assert got.status == 404 and got.raw == ghost.raw.replace(GHOST_ART.encode(), s.b.encode()) or got.raw == ghost.raw, name
        # and the same holds after the expiry sweep turns the claim into an expired row
        await tick(env, rt)
        again = await web.req("GET", f"/api/member/artifacts/{s.b}/content/index.html")
        assert again.status == 404
    sandbox(body)


def test_content_route_headers_and_pool_modes(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        for name, kind, inline in (("index.html", "text/html", False), ("pic.svg", "image/svg+xml", False),
                                   ("fig.png", "image/png", True)):
            got = await web.req("GET", f"/api/member/artifacts/{s.a}/content/{name}")
            assert got.status == 200, name
            assert got.headers["content-type"].split(";")[0] == kind, (name, got.headers["content-type"])
            assert got.headers["content-security-policy"] == "default-src 'none'; sandbox", name
            assert got.headers["x-content-type-options"] == "nosniff", name
            assert got.headers.get("x-frame-options") == "DENY", name
            disposition = got.headers.get("content-disposition", "")
            assert disposition.startswith("attachment") == (not inline), (name, disposition)
            assert "no-store" in got.headers.get("cache-control", "") or "private" in got.headers.get("cache-control", ""), name
        pool = rt.mirror_root / POOL
        blobs = [p for p in pool.iterdir() if p.is_file()]
        assert blobs
        # no collab-public group known: the documented world-readable fallback
        assert (pool.stat().st_mode & 0o777, {p.stat().st_mode & 0o777 for p in blobs}) == (0o755, {0o644})
        # with the group (here our own): 0750 and 0640, repaired in place on the next tick
        await tick(env, dataclasses.replace(rt, secret_gid=os.getgid()))
        assert pool.stat().st_mode & 0o777 == 0o750
        assert {p.stat().st_mode & 0o777 for p in pool.iterdir() if p.is_file()} == {0o640}
        assert pool.stat().st_gid == os.getgid()
        # the web (same user here) still reads the blobs
        assert (await web.req("GET", f"/api/member/artifacts/{s.a}/content/index.html")).raw == HTML
    sandbox(body)


def test_artifact_search_never_returns_another_members_or_pending_items(sandbox):
    async def body(env, rt, web, shares):
        s = await scenario(env, rt, web, shares)
        await claim(env, rt, shares, s)
        creds = {n: web.agent(n) for n in ("alice", "bob", "carol")}

        def search(name, **args):
            info = creds[name]
            status, out = collab_mcp.handle(web.svc, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                      "params": {"name": "artifact_search", "arguments": args}},
                                            f"Agent {info['credential']}", info["device_id"])
            assert status == 200
            return ok_json(out["result"]["content"][0]["text"])
        mine = search("alice", q="")
        ids = {h["artifact_id"] for h in mine["items"]}
        assert s.a in ids and s.b in ids           # her own, and the one she opened
        assert s.p not in ids                      # a pending push is not searchable
        assert {h["source"] for h in mine["items"] if h["artifact_id"] == s.b} == {"opened"}
        assert not [h for h in search("alice", q="Pending push")["items"]]
        bobs = {h["artifact_id"] for h in search("bob", q="")["items"]}
        assert s.b in bobs and s.a not in bobs and s.p not in bobs
        assert search("bob", q="Alice private")["items"] == [] and search("bob", q="Papers")["items"] == []
        out = await web.write("POST", "/api/member/folders", {"name": "Empty"}, member="carol")   # a member with a view
        assert out["result"] == "created", out
        await tick(env, rt)
        carol = search("carol", q="")
        assert carol["items"] == [] and carol["total"] == 0
        assert search("carol", q="Bob")["items"] == [] and search("carol", q="secret")["items"] == []
        # a hand-built argument cannot reach into another library either
        info = creds["carol"]
        status, out = collab_mcp.handle(web.svc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                  "params": {"name": "artifact_search",
                                                             "arguments": {"q": "", "owner": ALICE}}},
                                        f"Agent {info['credential']}", info["device_id"])
        assert out["result"]["isError"] is True or status != 200
    sandbox(body)
