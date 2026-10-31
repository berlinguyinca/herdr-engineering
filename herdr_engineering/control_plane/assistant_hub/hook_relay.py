"""Claude Code hook relay: forwards hook events to the tailnet hub as harness-attested observations.

Stdlib only so it runs as a plain script (``python3 -I hook_relay.py``) without importing the CLI.

Phase 1 is observe-only and STATELESS: it never prints a decision (the harness's own prompt runs), never writes
a file, never caches a decision. The assistant learns; the harness does not. Every failure is swallowed (exit 0).
"""
from __future__ import annotations

import difflib
import json
import os
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ENV_FILE = "HERDR_ASSISTANT_WORKER_FILE"
DEFAULT_FILE = "~/.config/herdr/assistant-worker.json"
TIMEOUT_S = 3
MAX_COMMAND = 300
HOOK_EVENTS = ("PermissionRequest", "PostToolUse")
_FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit")
_PATH_KEYS = ("file_path", "path", "notebook_path")


class RelayConfigError(ValueError):
    """The worker file is missing, unsafe or malformed. Never carries the credential."""


def validate_hub_url(url: object) -> str:
    """Return ``url`` without a trailing slash if it is http(s) with a host and no userinfo; else raise."""
    if not isinstance(url, str):
        raise RelayConfigError("url must be a string")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError as exc:
        raise RelayConfigError("url is malformed") from exc
    if parts.scheme not in ("http", "https") or not host or parts.username is not None or parts.password is not None:
        raise RelayConfigError("url must be http(s)://host[:port] without credentials")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise RelayConfigError("url must not carry a path, query or fragment")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class RelayConfig:
    url: str
    credential: str = ""
    device_id: str = ""

    def __repr__(self) -> str:  # never leak the credential into a traceback or log
        return f"RelayConfig(url={self.url!r}, device_id={self.device_id!r}, credential=<hidden>)"

    @classmethod
    def load(cls, path: Path | None = None) -> RelayConfig:
        target = Path(path) if path is not None else Path(os.environ.get(ENV_FILE) or DEFAULT_FILE).expanduser()
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise RelayConfigError("worker file unreadable") from exc
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise RelayConfigError("worker file is not a regular file")
            if st.st_uid != os.getuid():
                raise RelayConfigError("worker file is not owned by the current user")
            if st.st_mode & 0o077:
                raise RelayConfigError("worker file must be mode 0600 (no group/other access)")
            raw = fh.read(64 * 1024)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RelayConfigError("worker file is not valid JSON") from exc
        if not isinstance(data, dict):
            raise RelayConfigError("worker file must be a JSON object")
        cred, device = data.get("credential"), data.get("device_id")
        if not (isinstance(cred, str) and cred and isinstance(device, str) and device):
            raise RelayConfigError("worker file needs credential and device_id")
        return cls(url=validate_hub_url(data.get("url")), credential=cred, device_id=device)


# ---------------------------------------------------------------- event mapping

def _summary(tool_name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if tool_name == "Bash":
        cmd = tool_input.get("command")
        return cmd[:MAX_COMMAND] if isinstance(cmd, str) else ""
    if tool_name in _FILE_TOOLS:
        for key in _PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str):
                return value[:MAX_COMMAND]
        return ""
    return ("keys: " + ", ".join(sorted(str(k) for k in tool_input)))[:MAX_COMMAND]


def _failed(response: Any) -> bool:
    if isinstance(response, dict):
        return bool(response.get("is_error") or response.get("error") or response.get("success") is False)
    return False


def observation(event: dict) -> dict:
    """Map a Claude Code hook event to an observation payload. Never copies tool_response contents."""
    hook = str(event.get("hook_event_name") or "")
    name = str(event.get("tool_name") or "")
    payload: dict[str, Any] = {"hook_event": hook, "tool_name": name,
                               "tool_use_id": str(event.get("tool_use_id") or ""),
                               "tool_input_summary": _summary(name, event.get("tool_input")),
                               "cwd": str(event.get("cwd") or ""), "harness": "claude"}
    if hook == "PostToolUse":
        payload["outcome"] = "failed" if _failed(event.get("tool_response")) else "ran"
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # never forward the credential elsewhere
        return None


def relay(event: dict, config: RelayConfig) -> str | None:
    """POST the observation; Phase 1 ALWAYS returns None (no decision), whatever happens."""
    try:
        sid = event.get("session_id")
        body = json.dumps({"session_id": sid if isinstance(sid, str) and sid else "unknown",
                           "kind": "observation", "payload": observation(event)}).encode()
        request = urllib.request.Request(
            f"{config.url}/assistant/hook-events", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Worker {config.credential}",
                     "X-Herdr-Device": config.device_id})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        with opener.open(request, timeout=TIMEOUT_S) as response:
            response.read(4096)
    except Exception:  # noqa: BLE001 - observation must never disturb the harness
        pass
    return None


def main() -> int:
    try:
        config = RelayConfig.load()
        event = json.loads(sys.stdin.read(1024 * 1024))
        if isinstance(event, dict):
            relay(event, config)
    except Exception:  # noqa: BLE001
        pass
    return 0


# ---------------------------------------------------------------- settings installer

def _backup_path(settings: Path, *, stamp: str | None = None) -> Path:
    stamp = stamp or time.strftime("%Y%m%dT%H%M%S")
    candidate = settings.with_name(f"{settings.name}.bak-{stamp}")
    n = 1
    while os.path.lexists(candidate):
        n += 1
        candidate = settings.with_name(f"{settings.name}.bak-{stamp}-{n}")
    return candidate


def _with_hooks(data: dict, command: str) -> dict:
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings 'hooks' must be an object")
    new = dict(data)
    new_hooks = dict(hooks)
    for event in HOOK_EVENTS:
        entries = new_hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"settings hooks.{event} must be a list")
        if any(isinstance(e, dict) and isinstance(e.get("hooks"), list)
               and any(isinstance(h, dict) and h.get("command") == command for h in e["hooks"]) for e in entries):
            continue
        new_hooks[event] = [*entries, {"matcher": "*",
                                       "hooks": [{"type": "command", "command": command, "timeout": 5}]}]
    new["hooks"] = new_hooks
    return new


def install_hooks(settings_path: Path, *, command: str, confirm: Callable[[str], bool]) -> str:
    """Add the relay hooks to a Claude Code settings file. Returns the unified diff ('' when already installed)."""
    path = Path(os.path.realpath(settings_path))
    exists = path.exists()
    old_text = path.read_text() if exists else ""
    try:
        data = json.loads(old_text) if exists else {}
    except ValueError as exc:
        raise ValueError(f"{settings_path} is not valid JSON; refusing to modify it") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{settings_path} must hold a JSON object; refusing to modify it")
    new_text = json.dumps(_with_hooks(data, command), indent=2) + "\n"
    if exists and json.loads(new_text) == data:
        return ""
    diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                        fromfile=str(settings_path), tofile=f"{settings_path} (with relay hooks)"))
    if not confirm(diff):
        return diff
    mode = stat.S_IMODE(path.stat().st_mode) if exists else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        bak = _backup_path(path)
        fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(old_text)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(new_text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return diff



if __name__ == "__main__":
    sys.exit(main())
