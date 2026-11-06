"""The artifact library, end to end, for library.test.mjs (Playwright).

What is real: an isolated PostgreSQL (``initdb``/``pg_ctl`` in a private directory, unix socket only, every control-plane
migration applied: the very wiring of ``tests/control_plane/registry_pg.py``), the worker's intent applier and every member
intent handler, the view writer and the library-content pool, ``ShareStore`` event ingest, a ``ControlPlaneServer`` with the
real ``MemberWeb`` and ``CollabService`` (behind TLS with a throw-away certificate), the real ``share_public`` server (also
behind TLS) and the real bridge between the two, which is nothing but the mirror and outbox directories.  The browser signs in
through a fake Cognito hosted UI (PKCE S256) that both origins share.

What is not real, and why: the content-addressed object store is the in-memory ``FakeBlobs`` of the control-plane tests (no
RustFS server exists in this environment); the worker "process" is a thread of this script that runs the same ticks the
worker's housekeepers run (intent tick, ``ingest_events_file``, ``publish_member_views``) once a second; the grants and
agents feeds, which a collab-public process would write, are written by that thread.

Control server (plain loopback HTTP, used by the test, not the browser):
  GET  /info          origins, the public link of Bob's share, the ids that were seeded
  POST /user?name=    who the fake Cognito signs in next (alice | bob)
  POST /push?title=   an agent of Alice pushes a pending artifact into her library
  POST /revoke        Bob revokes the public share
  POST /wait          block until the worker has run one more full cycle (intents, ingest, views)
Stdout: ``PORT=<member origin port>``, ``SHARE=<share origin port>``, ``CONTROL=<control port>``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import asyncpg

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: E402
from herdr_engineering.control_plane.artifact_member_intents import (  # noqa: E402
    MemberRuntime,
    load_directory,
    publish_member_views,
    register_member_intents,
    take_secret,
)
from herdr_engineering.control_plane.artifact_member_service import MemberService  # noqa: E402
from herdr_engineering.control_plane.artifact_members import claim_resolver  # noqa: E402
from herdr_engineering.control_plane.artifact_share_api import share_settings_config  # noqa: E402
from herdr_engineering.control_plane.artifact_shares import ShareStore  # noqa: E402
from herdr_engineering.control_plane.collab import (  # noqa: E402
    ApprovalPolicy,
    CollabService,
    FileEventLog,
)
from herdr_engineering.control_plane.collab_artifacts import CollabArtifactBridge  # noqa: E402
from herdr_engineering.control_plane.collab_bridge_files import (  # noqa: E402
    BridgeLimits,
    atomic_write_json,
)
from herdr_engineering.control_plane.collab_cognito import CognitoAuthenticator  # noqa: E402
from herdr_engineering.control_plane.collab_mirror import GRANTS_NAME  # noqa: E402
from herdr_engineering.control_plane.db import Migrator  # noqa: E402
from herdr_engineering.control_plane.member_web import MemberWeb  # noqa: E402
from herdr_engineering.control_plane.members_config import MembersConfig  # noqa: E402
from herdr_engineering.control_plane.repo import ControlPlaneRepo  # noqa: E402
from herdr_engineering.control_plane.share_public import (  # noqa: E402
    ShareConfig,
    make_share_server,
)
from tests.control_plane.test_artifact_member_intents import (  # noqa: E402
    ALICE,
    BOB,
    HTML,
    agent,
    create,
    publish,
    send,
    user,
    write_feeds,
)
from tests.control_plane.test_collab_intents import Env, settle  # noqa: E402
from tests.control_plane.test_member_share_flow import approve, share, token_of  # noqa: E402

_spec = importlib.util.spec_from_file_location("member_artifacts_server", HERE / "member-artifacts-server.py")
mas = importlib.util.module_from_spec(_spec)
sys.modules["member_artifacts_server"] = mas
_spec.loader.exec_module(mas)

WORKER_GROUP = "lab"
DIRECTORY = {
    "alice": {"status": "approved", "email": "alice@lab.example"},
    "bob": {"status": "approved", "email": "bob@lab.example"},
}
mas.USERS["bob"] = ("bob@lab.example", [mas.GROUP])
PAGE = HTML + b"<!-- a page worth reading -->"


class LiveClock:
    def __call__(self):
        return time.time()


def start_postgres(work: Path):
    """A private PostgreSQL on a unix socket, every control-plane migration applied; returns (dsn, stop)."""
    candidates = sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"), reverse=True)
    which = shutil.which("initdb")
    if which:
        candidates.insert(0, Path(which))
    initdb = next((c for c in candidates if c.is_file()), None)
    if initdb is None:
        raise SystemExit("no PostgreSQL server binaries (initdb) found; cannot start the isolated database")
    bindir = initdb.parent
    area = work / "pg"
    area.mkdir()
    data = area / "data"
    subprocess.run([str(initdb), "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "--no-sync"],
                   check=True, capture_output=True)

    def ctl(*args):
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), *args], check=True, capture_output=True)
    ctl("-l", str(area / "server.log"), "-o", f"-c listen_addresses='' -k {area} -c dynamic_shared_memory_type=mmap", "-w", "start")
    dsn = f"postgresql:///postgres?host={area}"

    async def migrate():
        conn = await asyncpg.connect(dsn)
        try:
            await Migrator(conn).apply(str(ROOT / "deploy/control-plane/migrations"))
        finally:
            await conn.close()
    asyncio.run(migrate())
    return dsn, lambda: ctl("-m", "fast", "-w", "stop")


class Worker:
    """The worker's side on its own event loop thread: the same ticks the worker's housekeepers run, once a second."""

    def __init__(self, dsn: str, work: Path, share_origin: str):
        self.dsn, self.work, self.share_origin = dsn, work, share_origin
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.cycles = 0
        self.errors: list[str] = []
        self.stopping = False
        self.bob_share: dict = {}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(60):
            raise SystemExit("the worker did not start")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    def call(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    async def _main(self):
        clock = LiveClock()
        self.lock = asyncio.Lock()
        self.rt = MemberRuntime(outbox=self.work / "outbox", mirror_root=self.work / "mirror", group=WORKER_GROUP, enabled=True,
                                defaults={}, shares_root=self.work / "shares", clock=clock,
                                share_settings={**share_settings_config(None, public_url=self.share_origin), "member_share_limits": {}})
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=8)
        self.env = Env(self.work, self.pool, clock, context_extras={"members": self.rt})
        register_member_intents(self.env.applier)
        self.feeds()
        self.events = self.work / "share-events" / "events.jsonl"
        self.events.parent.mkdir(exist_ok=True)
        (self.work / "shares").mkdir(exist_ok=True)
        self.ready.set()
        last_feed = time.time()
        while not self.stopping:
            try:
                async with self.lock:
                    if time.time() - last_feed > 5:
                        self.feeds()
                        last_feed = time.time()
                    await self.cycle()
            except Exception as exc:                                  # noqa: BLE001 - a bad tick must not stop the loop
                self.errors.append(f"{type(exc).__name__}: {exc}")
                print(f"[worker] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self.cycles += 1
            await asyncio.sleep(0.4)
        await self.pool.close()

    def stop(self):
        self.stopping = True
        self.thread.join(20)

    def feeds(self):
        write_feeds(self.env, users=DIRECTORY, group=WORKER_GROUP, agents={"ag1": ("alice", False)})
        if getattr(self, "bridge_mirror", None) is not None:
            self.grants()

    def grants(self):
        atomic_write_json(self.bridge_mirror, GRANTS_NAME, {"generated_at": time.time(), "agents": {},
                          "limits": BridgeLimits(max_pending_intents_per_agent=50, commits_per_minute=1000).to_dict()})

    async def cycle(self):
        await settle(self.env)
        if self.events.exists():
            resolver = claim_resolver(await asyncio.to_thread(load_directory, self.rt), self.rt.group)
            async with self.pool.acquire() as conn:
                await ShareStore(conn).ingest_events_file(self.events, marker_path=self.events.with_suffix(".marker"),
                                                          members=resolver)
        async with self.pool.acquire() as conn:
            await publish_member_views(conn, dataclasses.replace(self.rt, rustfs=self.env.applier.rustfs))

    async def seed(self):
        async with self.lock:
            sql = "TRUNCATE artifacts, artifact_acl, artifact_shares, artifact_member_usage, artifact_member_events, " \
                  "artifact_blobs, artifact_collab_intents, artifact_collab_uploads, artifact_library_folders, " \
                  "artifact_share_claims CASCADE"
            async with self.pool.acquire() as conn:
                await conn.execute(sql)
            mine = await create(self.env, user("alice"), title="Plate map")
            await self._put(mine, {"index.html": PAGE}, "alice", ALICE)
            out = {"mine": mine}
            b = await create(self.env, user("bob"), title="Bob results")
            vb = await self._put(b, {"index.html": b"<html><body><h1>Bob results v1</h1></body></html>"}, "bob", BOB)
            made = await share(self.env, user("bob"), b, vb, title="Bob results")
            ticket, done = await approve(self.env, user("bob"), b, made["share_id"])
            assert done["result"] == "live", done
            url = take_secret(self.env.outbox, ticket, BOB)
            out.update(bob=b, version=vb, share_id=made["share_id"], url=url, token=token_of(url))
            lab = await create(self.env, user("bob"), title="QC report")
            vl = await self._put(lab, {"index.html": b"<html><body><h1>QC report</h1></body></html>"}, "bob", BOB)
            made = await share(self.env, user("bob"), lab, vl, "lab", allow_emails=["alice@lab.example"], title="QC report")
            ticket, done = await approve(self.env, user("bob"), lab, made["share_id"])
            assert done["result"] == "live", done
            out["assigned"] = lab
            self.bob_share = out
            return out

    async def _put(self, aid, files, who, owner):
        res = await publish(self.env, aid, files, actor=user(who), owner=owner)
        assert res["result"] == "created", res
        return res["version_id"]

    async def push(self, title):
        async with self.lock:
            res = await send(self.env, agent("ag1"), "member.artifact_create",
                             {"title": title, "filename": "push.html", "profile": "web"})
            return res

    async def revoke(self):
        async with self.lock:
            s = self.bob_share
            raw = self.env.intent(user("bob"), kind="member.share_revoke", payload={"share_id": s["share_id"]},
                                  artifact_id=s["bob"])
            await settle(self.env)
            return self.env.result(raw["ticket"])

    async def wait_cycle(self):
        target = self.cycles + 2
        while self.cycles < target:
            await asyncio.sleep(0.2)


def control_handler(cognito, worker: Worker, info: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

        def _json(self, doc, status=200):
            body = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlsplit(self.path).path == "/info":
                events = worker.events.read_text()[-2000:] if worker.events.exists() else ""
                return self._json({**info, "seed": worker.bob_share, "errors": worker.errors, "events": events})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            parts = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            if parts.path == "/user":
                cognito.user = q.get("name", "alice")
                return self._json({"user": cognito.user})
            if parts.path == "/push":
                return self._json(worker.call(worker.push(q.get("title", "Agent push"))))
            if parts.path == "/revoke":
                return self._json(worker.call(worker.revoke()))
            if parts.path == "/wait":
                worker.call(worker.wait_cycle(), timeout=60)
                return self._json({"cycles": worker.cycles})
            self._json({"error": "not found"}, 404)
    return Handler


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="library-ui-"))
    servers: list = []
    stop_pg = None
    srv = share_srv = worker = None
    try:
        tls = mas._self_signed(work)
        member_port, share_port, issuer_port = mas._free_port(), mas._free_port(), mas._free_port()
        host = f"127.0.0.1:{member_port}"
        origin = f"https://{host}"
        share_origin = f"https://127.0.0.1:{share_port}"
        issuer_origin = f"https://127.0.0.1:{issuer_port}"
        dsn, stop_pg = start_postgres(work)
        worker = Worker(dsn, work, share_origin)

        cognito = mas.FakeCognito(origin + "/my/callback", origin + "/")
        cognito.redirect_uris.add(share_origin + "/s/callback")
        auth = CognitoAuthenticator(pool_id=mas.POOL, client_ids=[mas.WEB_CLIENT], required_group="unused",
                                    jwks_fetch=lambda: cognito.jwks(), open_signup=True)
        svc = CollabService(auth, sink=FileEventLog(str(work / "log")),
                            approval=ApprovalPolicy(auto_groups=[mas.GROUP], approver_groups=["Administrators"]),
                            public_config={"client_id": mas.WEB_CLIENT, "hosted_ui_domain": issuer_origin},
                            member_group=mas.GROUP)
        bridge = CollabArtifactBridge(svc, worker.env.mirror_root, worker.env.outbox, clock=svc._now, feed_interval=3600.0)
        svc.artifact_bridge = bridge
        worker.bridge_mirror = bridge.mirror
        worker.grants()
        # the grants document goes stale in minutes: the worker thread rewrites it with the agents feed
        svc.member_service = MemberService(svc, bridge, MembersConfig(result_wait_seconds=0, session_exchanges_per_minute=200))
        mw = MemberWeb(svc, public_host=host, session_secret=secrets.token_bytes(48), session_seconds=3600,
                       web_root=ROOT / "packages")
        srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc, public_port=member_port,
                                 web_root=ROOT / "packages", member_web=mw)
        threading.Thread(target=srv.serve, daemon=True).start()
        srv.wait_until_ready()
        pub = srv._public_httpd
        pub.handle_error = lambda *_: None
        pub.socket = tls.wrap_socket(pub.socket, server_side=True, do_handshake_on_connect=False)

        sync = work / "sync"
        sync.mkdir()
        (sync / "jwks.json").write_text(json.dumps(cognito.jwks()))
        cfg = ShareConfig(shares_root=work / "shares", events_path=worker.events, sync_dir=sync, pool_id=mas.POOL,
                          client_ids=(mas.WEB_CLIENT,), web_client_id=mas.WEB_CLIENT, cognito_domain=issuer_origin,
                          public_url=share_origin + "/", signing_secret=secrets.token_bytes(48), session_seconds=3600)
        share_srv = make_share_server(cfg, host="127.0.0.1", port=share_port)
        share_srv.handle_error = lambda *_: None
        share_srv.socket = tls.wrap_socket(share_srv.socket, server_side=True, do_handshake_on_connect=False)
        threading.Thread(target=share_srv.serve_forever, daemon=True).start()

        issuer = ThreadingHTTPServer(("127.0.0.1", issuer_port), cognito.handler())
        issuer.daemon_threads = True
        issuer.handle_error = lambda *_: None
        issuer.socket = tls.wrap_socket(issuer.socket, server_side=True, do_handshake_on_connect=False)
        seeded = worker.call(worker.seed(), timeout=120)
        info = {"member_origin": origin, "share_origin": share_origin, "issuer_origin": issuer_origin,
                "public_url": seeded["url"].replace("https://share.example", share_origin) if "share.example" in seeded["url"] else seeded["url"]}
        control = ThreadingHTTPServer(("127.0.0.1", 0), control_handler(cognito, worker, info))
        control.daemon_threads = True
        servers = [issuer, control]
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        print(f"PORT={member_port}", flush=True)
        print(f"SHARE={share_port}", flush=True)
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
        if share_srv is not None:
            share_srv.shutdown()
        if srv is not None:
            srv.shutdown()
        if worker is not None:
            worker.stop()
        if stop_pg is not None:
            try:
                stop_pg()
            except Exception:                                          # noqa: BLE001
                pass
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
