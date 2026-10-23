"""T16 acceptance harness: REAL control-plane server processes on loopback, driven over HTTP.

Nothing here is configured through environment variables:
  * suite paths come from tests/e2e/acceptance.yaml (or acceptance.local.yaml);
  * every server gets a generated 0600 fleet-config YAML (the production configuration path) and a generated
    collaboration users file; keys and tokens are produced at run time with `secrets`, never written to the repo.

A "host" in these tests is an ingest key with its own EnvelopeStore source (the Pi side) or its own server process.
Server-to-server federation is a separate, later, gated feature (spec "Security"), so two hosts share ONE control
plane here, exactly as two machines would share one HCN room.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import site
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
E2E = Path(__file__).resolve().parent
RUN_DIR = E2E / ".run"


# ------------------------------------------------------------------------------------------------ configuration
def load_acceptance() -> dict:
    local = E2E / "acceptance.local.yaml"
    cfg = yaml.safe_load((local if local.exists() else E2E / "acceptance.yaml").read_text())
    if not isinstance(cfg, dict) or cfg.get("version") != 1:
        raise RuntimeError("acceptance config must be a mapping with version: 1")
    return cfg


def _siblings(marker: str) -> list[Path]:
    return sorted(d for d in ROOT.parent.iterdir() if d.is_dir() and d != ROOT and (d / marker).exists())


def _discover(marker: str, prefer_suffix: str | None = None) -> Path:
    """Find a sibling checkout by what it CONTAINS (not by its directory name). With several candidates the one whose
    name ends with this checkout's worktree suffix (herdr-t16 -> *-t16) wins; otherwise the choice must be unambiguous."""
    found = _siblings(marker)
    if prefer_suffix:
        same = [d for d in found if d.name.endswith(prefer_suffix)]
        if len(same) == 1:
            return same[0]
    if len(found) == 1:
        return found[0]
    built = [d for d in found if (d / "target" / "debug" / "autospec").exists()]      # AutoSpec: only a built checkout is usable
    if marker.startswith("crates/") and len(built) == 1:
        return built[0]
    raise RuntimeError(f"cannot discover a sibling checkout containing {marker}: candidates {[d.name for d in found]}; "
                       "set the path in tests/e2e/acceptance.local.yaml")


def resolve(cfg: dict, key: str) -> Path:
    """A configured path (relative to this checkout), or `auto` = discover the sibling checkout by content."""
    value = str(cfg[key])
    suffix = ROOT.name.rsplit("-", 1)[-1] if "-" in ROOT.name else None
    if value == "auto":
        if key == "pi_repo":
            return _discover("src/runtime/herdr/lineage.ts", suffix)
        if key == "autospec_repo":
            return _discover("crates/autospec-cli", suffix)
        if key == "autospec_bin":
            return resolve(cfg, "autospec_repo") / "target" / "debug" / "autospec"
        raise RuntimeError(f"{key}: `auto` is not supported")
    p = Path(value).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha256_hex(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def principal_id(api_key: str) -> str:
    """The graph principal id Herdr derives from an API key (the key itself is never stored)."""
    return "key:" + sha256_hex(api_key)[:12]


def new_secret(label: str) -> str:
    return f"e2e-{label}-{secrets.token_hex(16)}"


def sha40(seed: str) -> str:
    """A deterministic full 40-hex commit id for a fixture label (never a real object)."""
    return sha256_hex("e2e-commit:" + seed)[:40]


# ------------------------------------------------------------------------------------------------ http client
@dataclass
class Resp:
    status: int
    body: Any
    raw: bytes = b""
    headers: dict = field(default_factory=dict)

    def __getitem__(self, k):
        return self.body[k]

    def get(self, k, default=None):
        return self.body.get(k, default) if isinstance(self.body, dict) else default

    def ok(self) -> bool:
        return 200 <= self.status < 300


def http_call(base: str, method: str, path: str, body: Any = None, headers: dict | None = None,
              timeout: float = 10.0, raw_body: bytes | None = None) -> Resp:
    host, port = base.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    h = dict(headers or {})
    data = raw_body
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    try:
        conn.request(method, path, data, h)
        r = conn.getresponse()
        raw = r.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"_unparsed": raw[:200].decode("latin-1")}
        return Resp(r.status, parsed, raw, {k.lower(): v for k, v in r.getheaders()})
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------------ the server
def _resolve_refs(node: Any, keys: dict[str, str]) -> Any:
    """`@label` anywhere in a structure (value or mapping key) becomes that key's principal id."""
    if isinstance(node, str) and node.startswith("@"):
        return principal_id(keys[node[1:]])
    if isinstance(node, list):
        return [_resolve_refs(v, keys) for v in node]
    if isinstance(node, dict):
        return {_resolve_refs(k, keys): _resolve_refs(v, keys) for k, v in node.items()}
    return node


class Plane:
    """One real `herdr-eng control-plane web` process.

    keys:    {label: role} -> generated API keys (role viewer | ingest | operator)
    users:   [names]       -> generated HCN bearer tokens (static-token authenticator)
    scopes:  {label: {rooms: [...], missions: [...]}} -> control_plane.lineage_key_scopes (keys resolved at start)
    quorum:  control_plane.review_quorum; `@label` entries become that key's principal id
    collab:  extra control_plane.collab settings (merged over the defaults below)
    pg_dsn:  durable store (scenario 6); omitted = in-memory repo
    """

    FEATURES = {"delegation_enabled": True,
                "steering": {"enabled": True, "self_confirm": False},
                "contextual": {"enabled": True},
                "insights": {"enabled": True}}

    def __init__(self, workdir: Path, *, name: str = "plane", keys: dict[str, str] | None = None,
                 users: list[str] | None = None, scopes: dict | None = None, quorum: dict | None = None,
                 collab: dict | None = None, pg_dsn: str | None = None, request_timeout: float = 10.0,
                 startup_timeout: float = 30.0, extra_control_plane: dict | None = None):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.key_roles = {"viewer": "viewer", "operator": "operator", **(keys or {})}
        self.keys = {label: new_secret(label) for label in self.key_roles}
        self.users = list(users or [])
        self.tokens = {u: new_secret("tok-" + u) for u in self.users}
        self.scopes, self.quorum, self.pg_dsn = scopes or {}, quorum, pg_dsn
        self.collab = {**self.FEATURES, **(collab or {})}
        self.extra_control_plane = extra_control_plane or {}
        self.request_timeout, self.startup_timeout = request_timeout, startup_timeout
        self.port = 0
        self.proc: subprocess.Popen | None = None
        self.log_path = self.workdir / f"{name}.log"
        self._log = None

    # ---- files
    def _write_config(self) -> tuple[Path, Path]:
        cp: dict[str, Any] = {
            "api_keys": {self.keys[label]: role for label, role in self.key_roles.items()},
            "collab": self.collab, **self.extra_control_plane}
        if self.scopes:
            cp["lineage_key_scopes"] = {self.keys[label]: allow for label, allow in self.scopes.items()}
        if self.quorum is not None:
            cp["review_quorum"] = _resolve_refs(self.quorum, self.keys)
        if self.pg_dsn:
            cp["pg_dsn"] = self.pg_dsn
        fleet = self.workdir / f"{self.name}-fleet.yaml"
        fleet.write_text(yaml.safe_dump({"version": 1, "hosts": {},
                                         "sections": {"control_plane": {"roles": ["admin"], "values": cp}}}))
        fleet.chmod(0o600)
        users = self.workdir / f"{self.name}-users.json"
        users.write_text(json.dumps({u: sha256_hex(t) for u, t in self.tokens.items()}))
        users.chmod(0o600)
        return fleet, users

    # ---- lifecycle
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> Plane:
        assert not self.alive(), "already running"
        fleet, users = self._write_config()
        self.port = free_port()
        cmd = [sys.executable, "-m", "herdr_engineering.cli", "control-plane", "web",
               "--host", "127.0.0.1", "--port", str(self.port), "--fleet-config", str(fleet)]
        if self.users:
            cmd += ["--collab-users-file", str(users)]
        self._log = open(self.log_path, "ab")
        # cwd = repo root makes `herdr_engineering` importable without PYTHONPATH. The child gets an explicit, minimal
        # environment (PATH to find python/git, HOME inside the scratch dir, PYTHONUSERBASE so the user-site asyncpg is found): no config, no credentials, no proxies.
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=self._log, stderr=subprocess.STDOUT,
                                     env={"PATH": os.environ["PATH"], "HOME": str(self.workdir),
                                          "PYTHONUSERBASE": site.getuserbase()})   # lets python find user-site asyncpg
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} exited during startup:\n{self.tail()}")
            try:
                if self.call("GET", "/api/v1/ingest/sources", key="viewer").status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.15)
        else:
            self.stop()
            raise RuntimeError(f"{self.name} did not become ready:\n{self.tail()}")
        # Guard against talking to somebody else's server on a recycled port: ours alone accepts our random key
        # and refuses a wrong one.
        assert self.call("GET", "/api/v1/ingest/sources", headers={"X-API-Key": "wrong-" + secrets.token_hex(4)}).status == 401
        assert self.alive()
        return self

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        if self._log:
            self._log.close()
            self._log = None

    def kill(self) -> None:
        """SIGKILL: no shutdown path runs (a crash)."""
        if self.alive():
            self.proc.kill()
            self.proc.wait(5)

    def restart(self) -> Plane:
        self.stop()
        return self.start()

    def tail(self, n: int = 40) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    # ---- calls
    def headers(self, *, key: str | None = None, user: str | None = None, agent: tuple | None = None,
                headers: dict | None = None) -> dict:
        h: dict[str, str] = {}
        if key is not None:
            h["X-API-Key"] = self.keys.get(key, key)
        if user is not None:
            h["Authorization"] = "Bearer " + self.tokens.get(user, user)
        if agent is not None:
            h["Authorization"] = "Agent " + agent[0]
            h["X-Herdr-Device"] = agent[1]
        h.update(headers or {})
        return h

    def call(self, method: str, path: str, body: Any = None, *, key: str | None = None, user: str | None = None,
             agent: tuple | None = None, headers: dict | None = None, raw_body: bytes | None = None) -> Resp:
        return http_call(self.url, method, path, body, self.headers(key=key, user=user, agent=agent, headers=headers),
                         self.request_timeout, raw_body)

    def get(self, path, **kw) -> Resp:
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw) -> Resp:
        return self.call("POST", path, {} if body is None else body, **kw)

    # HCN (collaboration) convenience: paths relative to /api/v1/collab
    def c(self, method: str, path: str, body: Any = None, **kw) -> Resp:
        return self.call(method, "/api/v1/collab" + path, body if method == "GET" or body is not None else {}, **kw)

    def pid(self, label: str) -> str:
        return principal_id(self.keys[label])

    # ---- graph / ingest helpers (keyed API)
    def envelope(self, *, source: str, seq: int, entity_id: str, event_type: str, payload: dict,
                 scope: str = "org", event_id: str | None = None, **ids) -> dict:
        return {"schema_version": 2, "event_id": event_id or f"{source}-{seq}-{secrets.token_hex(3)}", "source": source,
                "source_sequence": seq, "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "entity_type": "lineage", "entity_id": entity_id, "event_type": event_type, "payload": payload,
                "visibility_scope": scope, "evidence_refs": [], **ids}

    def ingest(self, key: str, env: dict) -> Resp:
        return self.post("/api/v1/ingest", env, key=key)

    def node(self, nid: str, *, key: str = "operator", **q) -> Resp:
        from urllib.parse import quote, urlencode
        return self.get(f"/api/v1/graph/nodes/{quote(nid, safe='')}" + (("?" + urlencode(q)) if q else ""), key=key)

    def hood(self, nid: str, *, key: str = "operator", depth: int = 2, **q) -> Resp:
        from urllib.parse import quote, urlencode
        return self.get(f"/api/v1/graph/nodes/{quote(nid, safe='')}/neighborhood?" + urlencode({"depth": depth, **q}), key=key)

    def sources(self, key: str = "viewer") -> dict:
        return {s["source"]: s for s in self.get("/api/v1/ingest/sources", key=key).body.get("sources", [])}

    def edges(self, nid: str, *, key: str = "operator", depth: int = 1) -> list[tuple]:
        r = self.hood(nid, key=key, depth=depth)
        return sorted((e["edge_type"], e["from_id"], e["to_id"]) for e in r.body.get("edges", []) if not e.get("removed"))

    # ---- HCN scaffolding
    def make_room(self, owner: str, project: str = "p", room: str = "r") -> tuple[str, str]:
        pj = self.c("POST", "/projects", {"name": project}, user=owner)
        assert pj.status == 201, pj.body
        rm = self.c("POST", f"/projects/{pj['project_id']}/rooms", {"name": room}, user=owner)
        assert rm.status == 201, rm.body
        return pj["project_id"], rm["room_id"]

    def add_member(self, owner: str, room: str, user: str, role: str) -> Resp:
        r = self.c("POST", f"/rooms/{room}/members", {"user_id": user, "role": role}, user=owner)
        assert r.status == 201, r.body
        return r

    def verify_features(self, owner: str) -> None:
        """POSITIVE CONTROL: every optional feature the scenarios rely on must actually be ON, using the signal each
        feature really gives (an `enabled` flag, a 403 `*_disabled` code, or a validation error that is only
        reachable once the feature is on). Without this a 403/404 in a security assertion could simply mean the
        feature is switched off."""
        _, room = self.make_room(owner, "feature-probe", "feature-probe-room")
        for path in ("/xlinks", "/delegations", "/proposals", "/consultations"):
            r = self.c("GET", f"/rooms/{room}{path}", user=owner)
            assert r.status == 200 and r.get("enabled") is True, f"feature probe {path}: {r.status} {r.body} (feature off or broken)"
        for path in ("/attention", "/progress", "/analytics"):
            r = self.c("GET", f"/rooms/{room}{path}", user=owner)
            assert r.status == 200, f"feature probe {path}: {r.status} {r.body} (feature off or broken)"
        r = self.c("POST", f"/rooms/{room}/steering", {"node_id": "agent:probe", "action": "pause", "instruction": "x"}, user=owner)
        assert r.status != 403 and r.get("code") != "steering_disabled", f"feature probe steering: {r.status} {r.body} (feature off or broken)"
        r = self.c("POST", f"/rooms/{room}/comments", {"node_id": "agent:probe", "text": "x"}, user=owner)
        assert r.status != 403, f"feature probe comments: {r.status} {r.body} (feature off or broken)"
        assert self.c("GET", f"/rooms/{room}/state", user=owner).status == 200
