"""Per-project LLM key injection for sessions the fabric starts.

``herdr-eng session run --project P -- <command>`` mints (or reuses) a gateway key
for ``P`` through the control-plane API and starts ``<command>`` with
``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` in *its environment only*, so the token
usage of everything the session does is attributed to the project without anyone
sourcing a file by hand.

Key handling rules (see SECURITY.md):

* the secret travels only in the child's environment or a 0600 file, never in argv,
  logs, stdout or exception text;
* the control-plane client never follows redirects (it carries ``X-API-Key``);
* a per-session key is revoked when the session ends; a project key is reused.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .host_config import control_plane_url_or

DEFAULT_CONTROL_PLANE = "http://dev.lan"
DEFAULT_GATEWAY = "http://dev.lan:8088/v1"
KEY_FILE = "~/.config/herdr-engineering/control-plane-key"
SECRET_ENV = ("OPENAI_API_KEY",)


class LlmEnvError(RuntimeError):
    """Raised with a message that never contains a key."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _control_plane_token() -> str | None:
    from .host_config import host_value
    tok = host_value("agent.api_key")
    if tok:
        return str(tok).strip()
    try:
        path = os.path.expanduser(KEY_FILE)
        if os.stat(path).st_mode & 0o077:
            return None  # group/world readable: refuse to use it
        with open(path) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _post(base: str, path: str, body: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    tok = _control_plane_token()
    if tok:
        headers["X-API-Key"] = tok
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise LlmEnvError(f"control plane refused {path}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LlmEnvError(f"control plane unreachable for {path}: {type(exc).__name__}") from None


def _get(base: str, path: str) -> dict:
    headers = {}
    tok = _control_plane_token()
    if tok:
        headers["X-API-Key"] = tok
    req = urllib.request.Request(base.rstrip("/") + path, headers=headers)
    try:
        with _OPENER.open(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LlmEnvError(f"control plane unreachable for {path}: {type(exc).__name__}") from None


def mint_key(control_plane: str, project: str, label: str = "") -> dict:
    minted = _post(control_plane, "/api/v1/llm/keys", {"project": project, "label": label})
    if not minted.get("key") or not minted.get("key_id"):
        raise LlmEnvError("control plane returned no key")
    return minted


def revoke_key(control_plane: str, key_id: str) -> None:
    _post(control_plane, f"/api/v1/llm/keys/{key_id}/revoke", {})


def key_is_active(control_plane: str, key_id: str) -> bool:
    keys = _get(control_plane, "/api/v1/llm/keys").get("keys", [])
    return any(k.get("key_id") == key_id and not k.get("revoked") for k in keys)


def _clean(value: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        raise LlmEnvError("refusing env value with control characters")
    return value


def write_env_file(path: str, values: dict[str, str]) -> None:
    """Write ``KEY=value`` lines atomically into a file that is 0600 from the first byte."""
    path = os.path.expanduser(path)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".llm-", dir=parent)  # mkstemp creates 0600
    try:
        with os.fdopen(fd, "w") as fh:
            for k, v in values.items():
                fh.write(f"{k}={_clean(v)}\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        path = os.path.expanduser(path)
        if os.stat(path).st_mode & 0o077:
            return out  # loose permissions: do not trust or use
        with open(path) as fh:
            for line in fh:
                k, sep, v = line.rstrip("\n").partition("=")
                if sep:
                    out[k] = v
    except OSError:
        pass
    return out


def project_cache_path(project: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in project)
    return os.path.expanduser(f"~/.config/herdr-engineering/llm-{safe}.env")


@dataclass
class SessionKey:
    env: dict[str, str] = field(default_factory=dict)
    key_id: str | None = None
    revoke_on_end: bool = False
    control_plane: str = DEFAULT_CONTROL_PLANE

    def revoke(self) -> bool:
        if not (self.revoke_on_end and self.key_id):
            return True
        try:
            revoke_key(self.control_plane, self.key_id)
            return True
        except LlmEnvError:
            return False


def llm_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    s = dict(cfg.get("llm") or {})
    s.setdefault("inject", True)
    s.setdefault("control_plane_url", "")
    s.setdefault("gateway_url", DEFAULT_GATEWAY)
    s.setdefault("key_mode", "session")
    s["control_plane_url"] = (s["control_plane_url"]
                              or (cfg.get("fabric") or {}).get("control_plane_url")
                              or control_plane_url_or(DEFAULT_CONTROL_PLANE))
    return s


def injection_enabled(cfg: dict[str, Any], *, cli_disable: bool = False) -> bool:
    if cli_disable:
        return False
    return str(llm_settings(cfg).get("inject", True)).lower() not in ("false", "0", "no", "off")


def service_hints(cfg: dict[str, Any], *, project: str, session: str | None,
                  mission: str | None) -> dict[str, str]:
    """Non-secret context so tools in the session can use the rewriting helpers
    (gateway ``/resolve-loopback`` and ``/rewrite`` with ``from_machine``)."""
    from .fabric import gateway_url_from_config
    s = llm_settings(cfg)
    hints = {"HERDR_ENGINEERING_CONTROL_PLANE_URL": s["control_plane_url"],
             "HERDR_FABRIC_MACHINE": socket.gethostname(),
             "HERDR_PROJECT": project}
    gw = gateway_url_from_config(cfg)
    if gw:
        hints["HERDR_ENGINEERING_FABRIC_URL"] = gw
    if session:
        hints["HERDR_SESSION_ID"] = session
    if mission:
        hints["HERDR_MISSION_ID"] = mission
    return {k: _clean(v) for k, v in hints.items() if v}


def acquire(cfg: dict[str, Any], *, project: str, session: str | None = None,
            mission: str | None = None, key_mode: str | None = None) -> SessionKey:
    """Mint (session mode) or reuse (project mode) a key; return the env to inject."""
    s = llm_settings(cfg)
    mode = key_mode or s["key_mode"]
    cp = s["control_plane_url"]
    base = {"OPENAI_BASE_URL": _clean(s["gateway_url"])}
    if mode == "project":
        cache = project_cache_path(project)
        cached = read_env_file(cache)
        kid, key = cached.get("HERDR_LLM_KEY_ID"), cached.get("OPENAI_API_KEY")
        if kid and key and key_is_active(cp, kid):
            return SessionKey({**base, "OPENAI_API_KEY": key}, kid, False, cp)
        minted = mint_key(cp, project, "project")
        write_env_file(cache, {"OPENAI_BASE_URL": s["gateway_url"],
                               "OPENAI_API_KEY": minted["key"],
                               "HERDR_LLM_KEY_ID": minted["key_id"]})
        return SessionKey({**base, "OPENAI_API_KEY": minted["key"]}, minted["key_id"], False, cp)
    label = "session:" + (session or mission or f"pid{os.getpid()}")
    minted = mint_key(cp, project, label)
    return SessionKey({**base, "OPENAI_API_KEY": minted["key"]}, minted["key_id"], True, cp)


def run_session(cfg: dict[str, Any], *, project: str, command: list[str],
                session: str | None = None, mission: str | None = None,
                disable: bool = False, require: bool = False,
                key_mode: str | None = None) -> int:
    """Run ``command`` with the project's LLM env; revoke a per-session key at the end."""
    env = dict(os.environ)
    env.update(service_hints(cfg, project=project, session=session, mission=mission))
    sk: SessionKey | None = None
    if injection_enabled(cfg, cli_disable=disable):
        try:
            sk = acquire(cfg, project=project, session=session, mission=mission,
                         key_mode=key_mode)
            env.update(sk.env)
        except LlmEnvError as exc:
            print(f"herdr-eng: LLM env not injected: {exc}", file=sys.stderr)
            if require:
                return 3
    child: subprocess.Popen | None = None

    def _forward(signum, _frame):
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    # SIGINT from the terminal already reaches the child (same process group), so the
    # wrapper only needs to survive it long enough to revoke; TERM/HUP are forwarded.
    old = {signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN)}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        old[sig] = signal.signal(sig, _forward)
    try:
        child = subprocess.Popen(command, env=env)  # noqa: S603 - argv carries no secret
        return child.wait()
    except OSError as exc:
        print(f"herdr-eng: cannot start {command[0]!r}: {type(exc).__name__}", file=sys.stderr)
        return 127
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        if sk is not None and not sk.revoke():
            print(f"herdr-eng: could not revoke session LLM key {sk.key_id}; revoke it with "
                  f"POST /api/v1/llm/keys/{sk.key_id}/revoke", file=sys.stderr)
