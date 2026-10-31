"""Authenticated CLI client for Herdr assistant discovery and task contracts."""
from __future__ import annotations

import argparse
import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .assistant_packages import agent_package_record

SCHEMA_VERSION = 1
MAX_RESPONSE = 2_000_000
MAX_INPUT = 64 * 1024


class ConfigError(ValueError):
    pass


class AssistantAPIError(RuntimeError):
    def __init__(self, status: int, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason


@dataclass(frozen=True)
class AssistantConfig:
    base_url: str
    token: str = field(repr=False)
    auth_kind: str
    device_id: str | None = None

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        try:
            port = parsed.port
        except ValueError:
            raise ConfigError("assistant API URL has an invalid port") from None
        host = parsed.hostname
        loopback = host in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and not loopback):
            raise ConfigError("assistant API requires HTTPS, except for loopback HTTP")
        if (parsed.username is not None or parsed.password is not None
                or "?" in self.base_url or "#" in self.base_url
                or not parsed.netloc or not host or parsed.netloc.endswith(":")
                or parsed.path not in {"", "/"}
                or any(ord(ch) <= 32 or ord(ch) == 127 for ch in host)):
            raise ConfigError("assistant API URL must be an origin without credentials, query, or fragment")
        if not self.token or any(ch in self.token for ch in "\r\n"):
            raise ConfigError("assistant credential is missing or invalid")
        if self.auth_kind not in {"user", "agent"}:
            raise ConfigError("HERDR_ASSISTANT_AUTH_KIND must be user or agent")
        if self.auth_kind == "agent" and not self.device_id:
            raise ConfigError("HERDR_ASSISTANT_DEVICE is required for agent authentication")
        if self.device_id and any(ch in self.device_id for ch in "\r\n"):
            raise ConfigError("assistant device id is invalid")
        canonical_host = f"[{host}]" if ":" in host else host
        canonical_port = f":{port}" if port is not None else ""
        object.__setattr__(self, "base_url", f"{parsed.scheme}://{canonical_host}{canonical_port}")

    @classmethod
    def from_env(cls) -> AssistantConfig:
        token = os.environ.get("HERDR_ASSISTANT_TOKEN")
        token_file = os.environ.get("HERDR_ASSISTANT_TOKEN_FILE")
        if token and token_file:
            raise ConfigError("set only one of HERDR_ASSISTANT_TOKEN or HERDR_ASSISTANT_TOKEN_FILE")
        if token_file:
            token = _read_token_file(Path(token_file).expanduser())
        return cls(
            base_url=os.environ.get("HERDR_ASSISTANT_URL", "http://127.0.0.1:8080"),
            token=token or "",
            auth_kind=os.environ.get("HERDR_ASSISTANT_AUTH_KIND", "user"),
            device_id=os.environ.get("HERDR_ASSISTANT_DEVICE"),
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class AssistantClient:
    def __init__(self, config: AssistantConfig):
        self.config = config
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if not path.startswith("/api/v1/collab/"):
            raise ValueError("assistant API path must remain inside /api/v1/collab")
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        if data is not None and len(data) > MAX_INPUT:
            raise ConfigError("request body exceeds 64 KiB")
        kind = "Bearer" if self.config.auth_kind == "user" else "Agent"
        headers = {"Authorization": f"{kind} {self.config.token}", "Accept": "application/json"}
        if self.config.auth_kind == "agent":
            headers["X-Herdr-Device"] = self.config.device_id or ""
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.config.base_url + path, data=data,
                                         headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE + 1)
            status = exc.code
        except urllib.error.URLError:
            raise AssistantAPIError(0, "assistant API unavailable") from None
        if len(raw) > MAX_RESPONSE:
            raise AssistantAPIError(status, "assistant API response exceeds 2 MB")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise AssistantAPIError(status, "assistant API returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise AssistantAPIError(status, "assistant API returned a non-object response")
        if not 200 <= status < 300:
            message = payload.get("error") if isinstance(payload.get("error"), str) else "request failed"
            raise AssistantAPIError(status, message, reason=payload.get("reason"))
        return payload

    def agents_list(self, room_id: str | None = None) -> dict:
        path = f"/api/v1/collab/rooms/{_segment(room_id)}/agents" if room_id else "/api/v1/collab/agents"
        return self._request("GET", path)

    def agent_get(self, agent_id: str, room_id: str | None = None) -> dict:
        rows = self.agents_list(room_id).get("agents", [])
        for row in rows:
            if row.get("agent_id") == agent_id:
                return row
        raise AssistantAPIError(404, "agent not found in visible scope")

    def capabilities(self, room_id: str) -> dict:
        agents = self.agents_list(room_id).get("agents", [])
        normalized = [{**row, "actions": sorted(row.get("actions", []))} for row in agents]
        return {
            "room_id": room_id,
            "role_registry_available": False,
            "source": "current room grants",
            "agents": normalized,
            "contracts": [
                {"name": "task.receive", "requires_action": "execute"},
                {"name": "task.delegate", "requires_action": "instruct"},
                {"name": "room.read", "requires_action": "read"},
            ],
        }

    def tasks_list(self, room_id: str) -> dict:
        return self._request("GET", f"/api/v1/collab/rooms/{_segment(room_id)}/tasks")

    def task_get(self, room_id: str, task_id: str) -> dict:
        payload = self._request(
            "GET", f"/api/v1/collab/rooms/{_segment(room_id)}/tasks/{_segment(task_id)}")
        task = payload.get("task")
        if not isinstance(task, dict):
            raise AssistantAPIError(502, "assistant API returned an invalid task response")
        return task

    def task_create(self, room_id: str, target_agent_id: str, text: str,
                    idempotency_key: str, parent_task_id: str | None = None) -> dict:
        body = {"target_agent_id": target_agent_id, "text": text,
                "idempotency_key": idempotency_key}
        if parent_task_id is not None:
            body["parent_task_id"] = parent_task_id
        return self._request("POST", f"/api/v1/collab/rooms/{_segment(room_id)}/tasks", body)

    def task_inbox(self) -> dict:
        return self._request("GET", "/api/v1/collab/tasks/inbox")

    def task_update(self, task_id: str, status: str, result: str | None = None) -> dict:
        body = {"status": status}
        if result is not None:
            body["result"] = result
        return self._request("POST", f"/api/v1/collab/tasks/{_segment(task_id)}/update", body)

    def approval_request(self, room_id: str, proposal: dict) -> dict:
        return self._request("POST", f"/api/v1/collab/rooms/{_segment(room_id)}/approval-proposals",
                             proposal)

    def approvals_list(self, room_id: str) -> dict:
        return self._request("GET", f"/api/v1/collab/rooms/{_segment(room_id)}/approval-proposals")

    def package_register(self, room_id: str, manifest: dict) -> dict:
        normalized = agent_package_record(manifest)["manifest"]
        return self._request(
            "POST", f"/api/v1/collab/rooms/{_segment(room_id)}/assistant-packages",
            {"manifest": normalized},
        )

    def packages_list(self, room_id: str) -> dict:
        return self._request("GET", f"/api/v1/collab/rooms/{_segment(room_id)}/assistant-packages")

    def package_get(self, room_id: str, package_id: str) -> dict:
        payload = self._request(
            "GET", f"/api/v1/collab/rooms/{_segment(room_id)}/assistant-packages/"
            f"{_segment(package_id)}")
        package = payload.get("package")
        if not isinstance(package, dict):
            raise AssistantAPIError(502, "assistant API returned an invalid package response")
        return package


def _segment(value: str | None) -> str:
    if not isinstance(value, str) or not value or "/" in value or value in {".", ".."}:
        raise ConfigError("invalid resource identifier")
    return urllib.parse.quote(value, safe="")


def _read_object(path: str) -> dict:
    source = Path(path)
    if source.stat().st_size > MAX_INPUT:
        raise ConfigError("input file exceeds 64 KiB")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except RecursionError:
        raise ConfigError("input JSON is nested too deeply") from None
    if not isinstance(value, dict):
        raise ConfigError("input file must contain one JSON object")
    return value


def _read_token_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError("cannot securely open assistant token file") from exc
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ConfigError("assistant token file must be a regular file owned by this user")
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError("assistant token file permissions must be owner-only")
        if info.st_size > 8192:
            raise ConfigError("assistant token file is too large")
        raw = os.read(fd, 8193)
    finally:
        os.close(fd)
    if len(raw) > 8192:
        raise ConfigError("assistant token file is too large")
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ConfigError("assistant token file must be UTF-8") from None


def _success(result: Any) -> dict:
    return {"schema_version": SCHEMA_VERSION, "ok": True, "result": result}


def _failure(exc: Exception) -> dict:
    if isinstance(exc, AssistantAPIError):
        error = {"code": "api_error", "message": exc.message, "status": exc.status}
        if exc.reason:
            error["reason"] = exc.reason
    else:
        error = {"code": "invalid_request", "message": str(exc)}
    return {"schema_version": SCHEMA_VERSION, "ok": False, "error": error}


def _run(args: argparse.Namespace) -> int:
    try:
        op = args.assistant_operation
        if op == "packages-validate":
            result = agent_package_record(_read_object(args.file))
            print(json.dumps(_success(result), indent=2, sort_keys=True))
            return 0
        client = AssistantClient(AssistantConfig.from_env())
        if op == "agents-list":
            result = client.agents_list(args.room)
        elif op == "agents-inspect":
            result = client.agent_get(args.agent_id, args.room)
        elif op == "capabilities-list":
            result = client.capabilities(args.room)
        elif op == "tasks-list":
            result = client.tasks_list(args.room)
        elif op == "tasks-inspect":
            result = client.task_get(args.room, args.task_id)
        elif op == "tasks-inbox":
            result = client.task_inbox()
        elif op == "tasks-create":
            body = _read_object(args.file)
            _fields(body, {"room_id", "target_agent_id", "text", "idempotency_key",
                           "parent_task_id"}, required={"room_id", "target_agent_id", "text",
                                                        "idempotency_key"})
            result = client.task_create(body.get("room_id"), body.get("target_agent_id"),
                                        body.get("text"), body.get("idempotency_key"),
                                        body.get("parent_task_id"))
        elif op == "tasks-update":
            body = _read_object(args.file) if args.file else {}
            if args.file:
                _fields(body, {"result"})
            result = client.task_update(args.task_id, args.status,
                                        body.get("result") if args.file else args.result)
        elif op == "approvals-request":
            body = _read_object(args.file)
            _fields(body, {"task_id", "kind", "summary", "details", "idempotency_key"},
                    required={"task_id", "kind", "summary", "details", "idempotency_key"})
            result = client.approval_request(args.room, body)
        elif op == "approvals-list":
            result = client.approvals_list(args.room)
        elif op == "packages-register":
            result = client.package_register(args.room, _read_object(args.file))
        elif op == "packages-list":
            result = client.packages_list(args.room)
        elif op == "packages-inspect":
            result = client.package_get(args.room, args.package_id)
        else:  # pragma: no cover - argparse constrains this
            raise ConfigError("unknown assistant operation")
        print(json.dumps(_success(result), indent=2, sort_keys=True))
        return 0
    except (AssistantAPIError, ConfigError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps(_failure(exc), indent=2, sort_keys=True))
        return 1


def register(subparsers: argparse._SubParsersAction) -> None:
    assistant = subparsers.add_parser("assistant", help="authenticated personal-agent control tools")
    groups = assistant.add_subparsers(dest="assistant_group", required=True)

    agents = groups.add_parser("agents", help="discover visible agents")
    agent_ops = agents.add_subparsers(required=True)
    cmd = agent_ops.add_parser("list")
    cmd.add_argument("--room")
    _finish(cmd, "agents-list")
    cmd = agent_ops.add_parser("inspect")
    cmd.add_argument("agent_id")
    cmd.add_argument("--room")
    _finish(cmd, "agents-inspect")

    capabilities = groups.add_parser("capabilities", help="show current room grant capabilities")
    capability_ops = capabilities.add_subparsers(required=True)
    cmd = capability_ops.add_parser("list")
    cmd.add_argument("--room", required=True)
    _finish(cmd, "capabilities-list")

    tasks = groups.add_parser("tasks", help="delegate and inspect work")
    task_ops = tasks.add_subparsers(required=True)
    cmd = task_ops.add_parser("list")
    cmd.add_argument("--room", required=True)
    _finish(cmd, "tasks-list")
    cmd = task_ops.add_parser("inspect")
    cmd.add_argument("task_id")
    cmd.add_argument("--room", required=True)
    _finish(cmd, "tasks-inspect")
    cmd = task_ops.add_parser("inbox")
    _finish(cmd, "tasks-inbox")
    cmd = task_ops.add_parser("create")
    cmd.add_argument("--file", required=True)
    _finish(cmd, "tasks-create")
    cmd = task_ops.add_parser("update")
    cmd.add_argument("task_id")
    cmd.add_argument("--status", choices=["accepted", "done", "failed"], required=True)
    output = cmd.add_mutually_exclusive_group()
    output.add_argument("--result")
    output.add_argument("--file")
    _finish(cmd, "tasks-update")

    approvals = groups.add_parser("approvals", help="request or list pending approval proposals")
    approval_ops = approvals.add_subparsers(required=True)
    cmd = approval_ops.add_parser("request")
    cmd.add_argument("--room", required=True)
    cmd.add_argument("--file", required=True)
    _finish(cmd, "approvals-request")
    cmd = approval_ops.add_parser("list")
    cmd.add_argument("--room", required=True)
    _finish(cmd, "approvals-list")

    packages = groups.add_parser("packages", help="validate and register assistant definitions")
    package_ops = packages.add_subparsers(required=True)
    cmd = package_ops.add_parser("validate")
    cmd.add_argument("--file", required=True)
    _finish(cmd, "packages-validate")
    cmd = package_ops.add_parser("register")
    cmd.add_argument("--room", required=True)
    cmd.add_argument("--file", required=True)
    _finish(cmd, "packages-register")
    cmd = package_ops.add_parser("list")
    cmd.add_argument("--room", required=True)
    _finish(cmd, "packages-list")
    cmd = package_ops.add_parser("inspect")
    cmd.add_argument("package_id")
    cmd.add_argument("--room", required=True)
    _finish(cmd, "packages-inspect")


def _finish(parser: argparse.ArgumentParser, operation: str) -> None:
    parser.add_argument("--json", action="store_true", help="versioned JSON output (default)")
    parser.set_defaults(func=_run, assistant_operation=operation)


def _fields(value: dict, allowed: set[str], *, required: set[str] | None = None) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted((required or set()) - set(value))
    if unknown:
        raise ConfigError("unknown field(s): " + ", ".join(unknown))
    if missing:
        raise ConfigError("missing field(s): " + ", ".join(missing))
