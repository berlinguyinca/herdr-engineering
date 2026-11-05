"""Rollout event emitter with a durable outbox (spec sections 5 and 8). Off unless something calls it.

`Emitter.emit` redacts, validates with the SAME allow-list the control plane applies, assigns the next `seq` of
the rollout's stream and appends the envelope to `outbox.jsonl` (fsynced) before it tries `POST /api/v1/ingest`.
A control plane that is down costs nothing: the event stays queued and `flush()` delivers it later, oldest first,
stopping at the first transient failure so the stream order is kept. Delivery is idempotent on the server
(`<rollout_id>:<seq>`), so a crash between "delivered" and "trimmed" just replays harmlessly.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import urllib.error
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..ai_sync_feed import redact as redact_line
from ..control_plane.fleet_rollout import (
    HOME_PATH,
    LOG_TAIL_CHARS,
    LOG_TAIL_LINES,
    PR_TITLE_CHARS,
    TOKENISH,
    event_id,
    leaks,
    source_stream_id,
    validate_fleet_rollout,
)
from . import state

#: Sent in place of a whole line that carries a credential marker.
REDACTED_LINE = "[redacted line]"
#: HTTP statuses that mean "this event will never be accepted": parked, the queue moves on.
PERMANENT_STATUSES = frozenset({400, 409, 413, 422})
#: Optional payload fields whose value may legitimately be null; any other None is dropped before validation.
NULLABLE = frozenset({"before_sha", "after_sha", "halted_at_host"})
LINE_CHARS = 200
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BASE64ISH = re.compile(r"[A-Za-z0-9+/=]{32,}")


def clean_pr_title(title: str | None) -> str | None:
    """A one-line printable title of at most 120 characters, or None when it is empty or carries a secret."""
    if not isinstance(title, str):
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in title).split())[:PR_TITLE_CHARS]
    return None if not text or leaks(text) else text


def _redact_one(line: str, home: str) -> str:
    line = _ANSI.sub("", line)
    if home and home != "/":
        line = line.replace(home, "~")
    line = HOME_PATH.sub("~", line)
    if leaks(line):
        return REDACTED_LINE
    line = _BASE64ISH.sub("[redacted]", redact_line(line, LINE_CHARS))
    # Belt and braces: nothing the control plane would refuse may leave this host.
    if leaks(line) or TOKENISH.search(line) or HOME_PATH.search(line) or not line.isprintable():
        return REDACTED_LINE
    return line


def redact_log_tail(text: str, home: str | os.PathLike | None = None) -> str:
    """The last <=40 non-empty lines (<=2048 chars) of `text`, with credentials, token-like runs, control
    characters and the operator's home directory removed. Never raises; the result always validates."""
    home_text = str(home if home is not None else Path.home()).rstrip("/")
    lines = [_redact_one(line, home_text) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")[-200:]]
    lines = [line for line in lines if line.strip()][-LOG_TAIL_LINES:]
    while lines and len("\n".join(lines)) > LOG_TAIL_CHARS:
        lines.pop(0)
    return "\n".join(lines)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Emitter:
    """Queue-then-deliver emitter. `client` is anything with `request(route, body) -> dict` (MetadataClient)."""

    def __init__(self, directory: Path | str | None = None, client=None, *, home: str | os.PathLike | None = None):
        self.directory = Path(directory) if directory else state.state_dir()
        self.client = client
        self.home = home
        self.outbox = self.directory / "outbox.jsonl"
        self.rejected = self.directory / "outbox.rejected.jsonl"
        self.seq_file = self.directory / "seq.json"

    # -- storage -----------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        state.private_dir(self.directory)
        fd = os.open(self.directory / "outbox.lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _write_atomic(self, path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _append(self, path: Path, line: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as handle:
            if handle.tell() and path.read_bytes()[-1:] != b"\n":
                handle.write("\n")                           # isolate a torn previous line
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _seqs(self) -> dict[str, int]:
        try:
            data = json.loads(self.seq_file.read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}

    def _raw_lines(self) -> list[str]:
        try:
            return [line for line in self.outbox.read_text().split("\n") if line.strip()]
        except FileNotFoundError:
            return []

    def pending(self) -> list[dict]:
        """The queued envelopes, oldest first (unparseable lines are skipped; `flush` parks them)."""
        out = []
        for line in self._raw_lines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    # -- emitting ----------------------------------------------------------------------------------------

    def emit(self, rollout_id: str, event_type: str, *, flush: bool = True, **fields) -> dict:
        """Validate, queue and (best effort) deliver one event; returns the queued envelope.

        Raises ValueError for an event the control plane would refuse - nothing is queued and no seq is used.
        Delivery problems never raise: the event simply stays in the outbox.
        """
        payload = {k: copy.deepcopy(v) for k, v in fields.items() if v is not None or k in NULLABLE}
        if isinstance(payload.get("log_tail"), str):
            payload["log_tail"] = redact_log_tail(payload["log_tail"], self.home)
        if isinstance(payload.get("trigger"), dict) and "pr_title" in payload["trigger"]:
            payload["trigger"]["pr_title"] = clean_pr_title(payload["trigger"]["pr_title"])
        with self._locked():
            seqs = self._seqs()
            seq = seqs.get(rollout_id, 0)
            clean = validate_fleet_rollout(rollout_id, event_type,
                                           {"rollout_id": rollout_id, "ts": _now(), "seq": seq, **payload})
            envelope = {"source": "fleet-watch", "entity_type": "fleet_rollout", "entity_id": rollout_id,
                        "event_type": event_type, "event_id": event_id(rollout_id, seq), "payload": clean}
            self._append(self.outbox, json.dumps(envelope, sort_keys=True))
            seqs[rollout_id] = seq + 1
            self._write_atomic(self.seq_file, json.dumps(seqs, sort_keys=True))
        if flush and self.client is not None:
            self.flush()
        return envelope

    def source_event(self, repo: str, event_type: str, **fields) -> dict:
        """A `source.*` event, filed on the watched repo's own stream (not tied to one rollout)."""
        return self.emit(source_stream_id(repo), event_type, repo=repo, **fields)

    # -- delivery ----------------------------------------------------------------------------------------

    def _post(self, envelope: dict) -> None:
        result = self.client.request("/api/v1/ingest", envelope)
        entity = result.get("entity")
        if result.get("ok") is not True or not isinstance(entity, dict) or entity.get("entity_id") != envelope["entity_id"]:
            raise ValueError("fleet_rollout event rejected")

    def flush(self) -> dict:
        """Deliver the queue oldest-first. Stops at the first transient failure (order matters); parks events the
        server will never accept. Returns `{"sent", "pending", "rejected", "error"}`."""
        sent = rejected = 0
        error = None
        with self._locked():
            lines = self._raw_lines()
            keep: list[str] = []
            for index, line in enumerate(lines):
                try:
                    envelope = json.loads(line)
                    if not isinstance(envelope, dict) or "payload" not in envelope:
                        raise ValueError("not an event envelope")
                except ValueError:
                    self._park(line, "unparseable")
                    rejected += 1
                    continue
                if self.client is None:
                    error, keep = "no control plane client", lines[index:]
                    break
                try:
                    self._post(envelope)
                    sent += 1
                except urllib.error.HTTPError as exc:
                    if exc.code in PERMANENT_STATUSES:
                        self._park(line, f"http {exc.code}: {exc.read(200).decode('utf-8', 'replace')}")
                        rejected += 1
                        continue
                    error, keep = f"http {exc.code}", lines[index:]
                    break
                except (OSError, ValueError) as exc:
                    if isinstance(exc, ValueError) and "too large" in str(exc):
                        self._park(line, str(exc))
                        rejected += 1
                        continue
                    error, keep = f"{type(exc).__name__}: {exc}"[:200], lines[index:]
                    break
            if sent or rejected:
                self._write_atomic(self.outbox, "".join(f"{line}\n" for line in keep))
        return {"sent": sent, "pending": len(keep), "rejected": rejected, "error": error}

    def _park(self, line: str, reason: str) -> None:
        self._append(self.rejected, json.dumps({"reason": reason, "line": line}))
