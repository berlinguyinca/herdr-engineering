"""Observe native agent prompts without owning or replaying their delivery."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import selectors
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from .control_plane.redact import redact_payload

MAX_TEXT = 16 * 1024
MAX_RECORDS = 10_000
MAX_BYTES = 64 * 1024 * 1024
MAX_AGE = 14 * 86400
STATUSES = frozenset({"queued", "delivered", "failed", "unknown"})


def normalize_party(value: object) -> dict[str, object]:
    """Keep usable identity hints; nullable/new native fields must not poison audit."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {
        key: item
        for key in ("pane_id", "terminal_id", "agent", "name", "session_id")
        if isinstance(item := value.get(key), str) and 0 < len(item) <= 1000
    }
    session = value.get("agent_session")
    if isinstance(session, dict):
        clean = {
            key: item
            for key in ("source", "agent", "kind", "value")
            if isinstance(item := session.get(key), str) and 0 < len(item) <= 1000
        }
        if len(clean) == 4 and clean["kind"] in {"id", "path"}:
            result["agent_session"] = clean
    return result


def operation_identity(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) or redact_payload(value) != value:
        return "command_" + hashlib.sha256(value.encode()).hexdigest()
    return value


def parse_prompt(args: list[str]) -> tuple[list[str], str, str] | None:
    """Recognize only native agent prompt; never reinterpret other commands."""
    i = 0
    routing = {"--session", "--machine", "--remote", "--remote-keybindings"}
    while i < len(args) and args[i].startswith("-"):
        if args[i] in routing:
            if i + 1 >= len(args):
                return None
            i += 2
        elif args[i].split("=", 1)[0] in routing and "=" in args[i]:
            i += 1
        elif args[i] == "--handoff":
            i += 1
        else:
            return None
    if args[i : i + 2] != ["agent", "prompt"]:
        return None
    tail = args[i + 2 :]
    positions: list[str] = []
    literal = False
    while tail:
        value, *tail = tail
        # Native 0.9.1 consumes the text immediately after the target literally,
        # including a leading hyphen. Do not silently bypass capture for it.
        if len(positions) == 1:
            positions.append(value)
        elif value == "--" and not literal:
            literal = True
        elif not literal and value == "--wait":
            continue
        elif not literal and value in {"--until", "--timeout"}:
            if not tail:
                return None
            tail = tail[1:]
        elif not literal and value.startswith(("--until=", "--timeout=")):
            continue
        elif not literal and value.startswith("-"):
            return None
        else:
            positions.append(value)
    if len(positions) != 2:
        return None
    return args[:i], positions[0], positions[1]


def delivery_status(code: int, output: bytes, errors: bytes = b"") -> str:
    """Report transport evidence, not merely the exit status of --wait."""
    refusal = False
    for raw in (output, errors):
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(body, dict):
            continue
        result = body.get("result")
        if (
            body.get("ok") is not False
            and isinstance(result, dict)
            and (
                result.get("type") == "agent_prompted"
                and isinstance(result.get("agent"), dict)
                or body.get("ok") is True
                and result.get("submitted") is True
            )
        ):
            return "delivered"
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") in {
            "agent_not_found",
            "agent_blocked",
            "pane_not_found",
            "invalid_request",
            "unauthorized",
            "permission_denied",
            "agent_not_ready",
        }:
            refusal = True
    if refusal:  # Never infer nondelivery from an exit status alone.
        return "failed"
    return "unknown"


def receipt_recipient(output: bytes) -> dict[str, object]:
    """Recipient from the acknowledged delivery, not a possibly stale preflight."""
    try:
        body = json.loads(output)
        result = body.get("result", {}) if isinstance(body, dict) else {}
        agent = result.get("agent", {}) if isinstance(result, dict) else {}
        if isinstance(agent, dict):
            return normalize_party(agent)
    except (ValueError, UnicodeError):
        pass
    return {}


class Outbox:
    """Private, transaction-bounded observations. Replay never sends a prompt."""

    def __init__(
        self, path: Path, *, max_records: int = MAX_RECORDS, max_bytes: int = MAX_BYTES
    ) -> None:
        self.path, self.max_records, self.max_bytes = path, max_records, max_bytes
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise PermissionError("audit database has another owner")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._connect() as db:
            db.execute("PRAGMA auto_vacuum=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, created REAL NOT NULL, body TEXT NOT NULL, bytes INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS rejected (id TEXT PRIMARY KEY, created REAL NOT NULL, body TEXT NOT NULL, bytes INTEGER NOT NULL, reason TEXT NOT NULL)"
            )
            db.execute("DELETE FROM observations WHERE created < ?", (time.time() - MAX_AGE,))
            db.execute("DELETE FROM rejected WHERE created < ?", (time.time() - MAX_AGE,))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={MAX_BYTES // page_size}")
            with db:
                yield db
        finally:
            db.close()

    def put(self, operation_id: str, status: str, data: dict[str, object]) -> None:
        if status not in STATUSES:
            raise ValueError("invalid communication status")
        operation_id = operation_identity(operation_id)
        prepared = dict(data)
        for name in ("sender", "recipient"):
            if name in prepared:
                prepared[name] = normalize_party(prepared[name])
        record = cast(dict[str, object], redact_payload(prepared))
        text = str(record.get("text", "")).encode("utf-8")
        record["text"] = text[:MAX_TEXT].decode("utf-8", errors="ignore")
        record["truncated"] = len(text) > MAX_TEXT
        identity = hashlib.sha256((operation_id + ":" + status).encode()).hexdigest()
        record.update(
            operation_id=operation_id, status=status, record_id=identity, reported_at=time.time()
        )
        raw = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        size = len(raw.encode())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM observations WHERE created < ?", (time.time() - MAX_AGE,))
            if db.execute("SELECT 1 FROM observations WHERE id=?", (identity,)).fetchone():
                return
            count, used = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0) FROM (SELECT bytes FROM observations UNION ALL SELECT bytes FROM rejected)"
            ).fetchone()
            if count >= self.max_records or used + size > self.max_bytes:
                raise BufferError("communication audit outbox full; native delivery is unaffected")
            db.execute(
                "INSERT INTO observations VALUES (?,?,?,?)", (identity, time.time(), raw, size)
            )

    def pending(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM observations ORDER BY created,id LIMIT ?", (limit,)
                )
            ]

    def acknowledge(self, record_id: object) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM observations WHERE id=?", (str(record_id),))

    def reject(self, record_id: object, reason: str) -> None:
        """Retain at most 100 redacted bad records without blocking healthy uploads."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,created,body,bytes FROM observations WHERE id=?", (str(record_id),)
            ).fetchone()
            if row is None:
                return
            db.execute("DELETE FROM observations WHERE id=?", (str(record_id),))
            db.execute("INSERT OR IGNORE INTO rejected VALUES (?,?,?,?,?)", (*row, reason[:100]))
            db.execute(
                "DELETE FROM rejected WHERE id NOT IN (SELECT id FROM rejected ORDER BY created DESC,id DESC LIMIT 100)"
            )

    def rejected(self) -> list[dict[str, object]]:
        with self._connect() as db:
            return [
                {"record_id": row[0], "reason": row[1]}
                for row in db.execute("SELECT id,reason FROM rejected ORDER BY created,id")
            ]


def _agent(native: str, route: list[str], target: str) -> dict[str, object]:
    try:
        proc = subprocess.run(
            [native, *route, "agent", "get", target],
            capture_output=True,
            timeout=5,
            check=False,
        )
        body = json.loads(proc.stdout[: 256 * 1024])
        result = body.get("result", {})
        agent = result.get("agent", {})
        if proc.returncode == 0 and isinstance(agent, dict):
            return normalize_party(agent)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        pass
    return {}


def run_native(native: str, args: list[str], box: Outbox, *, host_id: str | None = None) -> int:
    parsed = parse_prompt(args)
    if parsed is None:
        return subprocess.call([native, *args])
    route, target, text = parsed
    operation_id = (
        os.environ.get("HERDR_COMMUNICATION_OPERATION_ID") or "native_" + uuid.uuid4().hex
    )
    operation_id = operation_identity(operation_id)
    sender_pane = os.environ.get("HERDR_PANE_ID", "")
    sender = _agent(native, [], sender_pane) if sender_pane else {}
    data: dict[str, object] = {
        "text": text,
        "host_id": host_id or socket.gethostname(),
        "sender": sender,
        "recipient": _agent(native, route, target) or {"name": target},
        "route": route,
        "sender_origin": "caller_environment" if sender else "unknown",
        "channel": "herdr.agent.prompt",
        "evidence_kind": "host_observation",
    }

    def record(status: str) -> None:
        try:
            box.put(operation_id, status, data)
        except (OSError, sqlite3.Error, BufferError, ValueError):
            print(
                "HerdR audit warning: observation could not be saved; delivery is unaffected.",
                file=sys.stderr,
            )

    record("queued")
    captured, errors = bytearray(), bytearray()
    child = None
    try:
        child = subprocess.Popen([native, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert child.stdout is not None and child.stderr is not None
        with child.stdout, child.stderr, selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ, (sys.stdout.buffer, captured))
            ready.register(child.stderr, selectors.EVENT_READ, (sys.stderr.buffer, errors))
            while ready.get_map():
                for key, _ in ready.select():
                    stream = cast(io.BufferedReader, key.fileobj)
                    chunk = stream.read1(8192)
                    if not chunk:
                        ready.unregister(stream)
                        continue
                    output, retained = key.data
                    output.write(chunk)
                    output.flush()
                    retained.extend(chunk[: max(0, 256 * 1024 - len(retained))])
        code = child.wait()
    except OSError:
        record("failed" if child is None else "unknown")
        raise
    status = delivery_status(code, bytes(captured), bytes(errors))
    actual_recipient = receipt_recipient(bytes(captured)) if status == "delivered" else {}
    if actual_recipient:
        data["recipient"] = actual_recipient
    record(status)
    return code


def permanent_rejection(status: int, body: object) -> bool:
    return (
        status in {400, 422}
        and isinstance(body, dict)
        and body.get("code") == "invalid_communication_observation"
    )


def sync(box: Outbox, url: str, key_file: Path, *, limit: int = 100, max_seconds: float = 5) -> int:
    """Upload observations only. An unavailable server leaves them pending."""
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("a credential-free HTTP(S) control-plane URL is required")
    key = key_file.read_text().strip()
    count = 0
    deadline = time.monotonic() + max_seconds
    for record in box.pending(limit):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        from .control_plane.communications import validate_observation

        try:
            record = validate_observation(
                str(record.get("operation_id", "")),
                "communication." + str(record.get("status", "")),
                record,
            )
        except ValueError:
            box.reject(record["record_id"], "client_validation")
            print(
                "HerdR audit warning: invalid observation isolated; later records can upload.",
                file=sys.stderr,
            )
            continue
        body = {
            "source": "herdr-cli",
            "entity_type": "communication",
            "entity_id": record["operation_id"],
            "event_type": "communication." + str(record["status"]),
            "payload": record,
        }
        request = urllib.request.Request(
            url.rstrip("/") + "/api/v1/ingest",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": key},
        )

        # Do not send credentials to a redirect destination.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(
                self,
                req: urllib.request.Request,
                fp: object,
                code: int,
                msg: str,
                headers: object,
                newurl: str,
            ) -> None:
                return None

        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=min(3, remaining)
            ) as response:
                if response.status != 200:
                    break
                result = json.loads(response.read(64 * 1024))
                entity = result.get("entity") if isinstance(result, dict) else None
                if (
                    not isinstance(entity, dict)
                    or result.get("ok") is not True
                    or entity.get("entity_id") != record["operation_id"]
                ):
                    raise ValueError("control plane did not acknowledge the observation")
        except urllib.error.HTTPError as error:
            try:
                error_body: object = json.loads(error.read(64 * 1024))
            except (ValueError, UnicodeError):
                error_body = None
            if not permanent_rejection(error.code, error_body):
                raise
            box.reject(record["record_id"], "server_validation")
            print(
                "HerdR audit warning: rejected observation isolated; later records can upload.",
                file=sys.stderr,
            )
            continue
        box.acknowledge(record["record_id"])
        count += 1
    return count


def install(
    native: Path, config: Path, *, url: str = "", key_file: str = "", host_id: str = ""
) -> None:
    """Opt-in local shim; original executable retained beside it for rollback."""
    if os.name != "posix":
        raise ValueError("native audit installation currently requires macOS or Linux")
    native = native.absolute()
    if native.is_symlink() or not native.is_file():
        raise ValueError("select the actual native executable, not a symlink")
    backup = native.with_name(native.name + ".native-untracked")
    if os.path.lexists(backup) or os.path.lexists(config):
        raise ValueError("audit shim or configuration already exists; refusing to overwrite")
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "native": str(backup),
        "host_id": host_id,
        "url": url,
        "key_file": key_file,
        "outbox": str(Path.home() / ".local/state/herdr-engineering/communications.sqlite"),
    }
    with config.open("x") as stream:
        os.chmod(config, 0o600)
        json.dump(payload, stream)
    launcher = native.with_name(native.name + ".audit-install-" + uuid.uuid4().hex)
    program = (
        "import os,sys\n"
        "try:\n from herdr_engineering.communications import main\n"
        "except Exception:\n"
        " print('HerdR audit warning: module unavailable; using native delivery.',file=sys.stderr)\n"
        f" os.execv({str(backup)!r}, [{str(backup)!r}, *sys.argv[1:]])\n"
        f"raise SystemExit(main(['native','--native',{str(backup)!r},'--config',{str(config)!r},'--',*sys.argv[1:]]))\n"
    )
    python, retained = shlex.quote(sys.executable), shlex.quote(str(backup))
    launcher.write_text(
        "#!/bin/sh\n"
        f"if [ ! -x {python} ]; then\n"
        "  printf '%s\\n' 'HerdR audit warning: runtime unavailable; using native delivery.' >&2\n"
        f'  exec {retained} "$@"\n'
        "fi\n"
        f'exec {python} -I -c {shlex.quote(program)} "$@"\n'
    )
    launcher.chmod(0o755)
    try:
        native.rename(backup)
        try:
            launcher.replace(native)
        except BaseException:
            backup.rename(native)
            raise
    finally:
        launcher.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("native", "sync", "install", "status"))
    parser.add_argument(
        "--config", type=Path, default=Path.home() / ".config/herdr-engineering/communications.json"
    )
    parser.add_argument("--native", type=Path)
    parser.add_argument("--url", default="")
    parser.add_argument("--key-file", default="")
    parser.add_argument(
        "--host-id", default="", help="existing fleet host id for session correlation"
    )
    arguments = sys.argv[1:] if argv is None else argv
    boundary = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser.parse_args(arguments[:boundary])
    if args.action == "install":
        install(
            args.native or Path(shutil.which("herdr") or "herdr"),
            args.config,
            url=args.url,
            key_file=args.key_file,
            host_id=args.host_id,
        )
        return 0
    native_args = arguments[boundary + 1 :]
    try:
        cfg = json.loads(args.config.read_text())
    except (OSError, ValueError):
        if args.action == "native" and args.native:
            print(
                "HerdR audit warning: configuration unavailable; using native delivery.",
                file=sys.stderr,
            )
            os.execv(args.native, [str(args.native), *native_args])
        raise
    if args.action == "native" and parse_prompt(native_args) is None:
        os.execv(cfg["native"], [cfg["native"], *native_args])
    try:
        box = Outbox(Path(cfg["outbox"]))
    except (OSError, sqlite3.Error):
        if args.action != "native":
            raise
        print("HerdR audit warning: storage unavailable; using native delivery.", file=sys.stderr)
        os.execv(cfg["native"], [cfg["native"], *native_args])
    if args.action == "status":
        print(
            json.dumps(
                {
                    "pending": len(box.pending(MAX_RECORDS)),
                    "rejected": len(box.rejected()),
                    "database": cfg["outbox"],
                }
            )
        )
        return 0
    code = (
        run_native(cfg["native"], native_args, box, host_id=cfg.get("host_id"))
        if args.action == "native"
        else 0
    )
    if args.action == "sync" and not (cfg.get("url") and cfg.get("key_file")):
        print(
            "HerdR audit: no dashboard URL/key file configured; observations remain local.",
            file=sys.stderr,
        )
        return 2
    if cfg.get("url") and cfg.get("key_file"):
        try:
            count = sync(box, cfg["url"], Path(cfg["key_file"]))
            if args.action == "sync":
                print(json.dumps({"synced": count}))
        except (OSError, ValueError, sqlite3.Error):
            print(
                "HerdR audit: dashboard unavailable; observations remain queued locally.",
                file=sys.stderr,
            )
            if args.action == "sync":
                return 1
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())
