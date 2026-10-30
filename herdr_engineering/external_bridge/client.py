"""Reference client for the external-agent protocol (docs/operations/external-agents.md).

It joins with a one-time invite code, keeps presence, reads and posts room chat, and rotates its own credential before it
expires. Everything it needs lives in one state file (mode 0600) that holds the credential: treat it like a password and
never log it. The client only ever talks outbound to the control plane."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

API = "/api/v1/collab"
MAX_TEXT = 8000
ROTATE_EVERY = 86400.0            # replace the secret daily; rotation does NOT extend the invite's hard expiry
EXPIRY_WARNING = 86400.0          # warn when less than a day of the invite's life is left


class BridgeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, Any]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https URL from the operator's config
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BridgeError(0, f"cannot reach the control plane ({type(exc).__name__})") from None


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class BridgeClient:
    def __init__(self, state_path: str | Path, *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time, timeout: float = 15.0):
        self.path = Path(state_path)
        self.state: dict[str, Any] = json.loads(self.path.read_text())
        self._transport, self._clock, self._timeout = transport or urllib_transport, clock, timeout

    # enrolment -------------------------------------------------------------------------------------------------
    @classmethod
    def join(cls, base_url: str, code: str, state_path: str | Path, *, harness_type: str = "external-bridge",
             transport: Transport | None = None, clock: Callable[[], float] = time.time) -> BridgeClient:
        base = base_url.rstrip("/")
        if not base.startswith("https://") and not base.startswith("http://127.0.0.1") and not base.startswith("http://localhost"):
            raise BridgeError(0, "the control plane URL must be https")
        status, out = (transport or urllib_transport)(
            "POST", base + API + "/agents/join", {"Content-Type": "application/json"},
            json.dumps({"token": code.strip(), "harness_type": harness_type}).encode(), 15.0)
        if status != 200 and status != 201 or "credential" not in out:
            raise BridgeError(status, "the invite was refused (expired, used or revoked)")
        state = {"base_url": base, "agent_id": out["agent_id"], "credential": out["credential"], "device_id": out["device_id"],
                 "expires_at": out["expires_at"], "room_id": out["room_id"], "actions": out.get("actions", []), "cursor": 0,
                 "rotated_at": clock()}
        _write_private(Path(state_path), state)
        return cls(state_path, transport=transport, clock=clock)

    def _save(self) -> None:
        _write_private(self.path, self.state)

    # calls -----------------------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        url = self.state["base_url"] + API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": "Agent " + self.state["credential"], "X-Herdr-Device": self.state["device_id"],
                   "Content-Type": "application/json"}
        status, out = self._transport(method, url, headers, json.dumps(body).encode() if body is not None else None,
                                      self._timeout)
        if status >= 400:
            raise BridgeError(status, str(out.get("error", "refused")) if isinstance(out, dict) else "refused")
        return out

    @property
    def agent_id(self) -> str:
        return self.state["agent_id"]

    @property
    def room_id(self) -> str:
        return self.state["room_id"]

    @property
    def can_post(self) -> bool:
        return "post" in self.state.get("actions", [])

    def heartbeat(self, status: str = "idle", activity: str = "") -> None:
        self._call("POST", "/agents/external/heartbeat", {"status": status, "activity": activity[:140]})

    def events(self, limit: int = 100) -> list[dict]:
        out = self._call("GET", f"/rooms/{self.room_id}/events", query={"cursor": self.state["cursor"], "limit": limit})
        return out.get("events", [])

    def advance(self, sequence: int) -> None:
        if sequence > self.state["cursor"]:
            self.state["cursor"] = sequence
            self._save()

    def post(self, text: str, key: str, in_reply_to: str | None = None) -> None:
        if not self.can_post:
            raise BridgeError(403, "this invite is read-only; ask the room owner for one that allows posting")
        body: dict[str, Any] = {"text": text[:MAX_TEXT], "idempotency_key": key[:100]}
        if in_reply_to:
            body["in_reply_to"] = in_reply_to
        self._call("POST", f"/rooms/{self.room_id}/messages", body)

    def expires_soon(self, margin: float = EXPIRY_WARNING) -> bool:
        """True when the invite's hard expiry is near: rotation cannot extend it, a new invite is needed."""
        return self.state["expires_at"] - self._clock() < margin

    def rotate_if_needed(self, every: float = ROTATE_EVERY) -> bool:
        """Replace the secret when it is older than ``every`` seconds (hygiene; the expiry stays the invite's)."""
        if self._clock() - self.state.get("rotated_at", 0.0) < every:
            return False
        out = self._call("POST", "/agent/rotate", {})
        self.state["credential"], self.state["expires_at"], self.state["rotated_at"] = (
            out["credential"], out["expires_at"], self._clock())
        self._save()
        return True
