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
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..ai_sync_feed import redact as redact_line
from ..control_plane.fleet_rollout import (
    HOME_PATH,
    LOG_TAIL_CHARS,
    LOG_TAIL_LINES,
    PR_TITLE_CHARS,
    event_id,
    leaks,
    source_stream_id,
    unsafe_text,
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
MAX_PENDING = 2000
MAX_REJECTED_BYTES = 1_000_000
SEQ_RETENTION_DAYS = 30
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BASE64ISH = re.compile(r"[A-Za-z0-9+/=]{32,}")


def clean_pr_title(title: str | None) -> str | None:
    """A one-line printable title of at most 120 characters, or None when it is empty or carries a secret."""
    if not isinstance(title, str):
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in title).split())[:PR_TITLE_CHARS]
    return None if not text or unsafe_text(text) else text


def _redact_one(line: str, home: str) -> str:
    line = _ANSI.sub("", line)
    if home and home != "/":
        line = line.replace(home, "~")
    line = HOME_PATH.sub("~", line)
    if leaks(line):
        return REDACTED_LINE
    line = _BASE64ISH.sub("[redacted]", redact_line(line, LINE_CHARS))
    # Belt and braces: nothing the control plane would refuse may leave this host.
    if unsafe_text(line) or not line.isprintable():
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


class NotAccepted(Exception):
    """The control plane answered 200 but did not accept the event; retrying cannot help."""


class Emitter:
    """Queue-then-deliver emitter. `client` is anything with `request(route, body) -> dict` (MetadataClient)."""

    def __init__(self, directory: Path | str | None = None, client=None, *, home: str | os.PathLike | None = None,
                 max_pending: int = MAX_PENDING, max_rejected_bytes: int = MAX_REJECTED_BYTES,
                 clock: Callable[[], datetime] | None = None):
        self.directory = Path(directory) if directory else state.state_dir()
        self.client = client
        self.home = home
        self.max_pending = max_pending
        self.max_rejected_bytes = max_rejected_bytes
        self.clock = clock or (lambda: datetime.now(UTC))
        self.outbox = self.directory / "outbox.jsonl"
        self.rejected = self.directory / "outbox.rejected.jsonl"
        self.seq_file = self.directory / "seq.json"
        self.seq_backup = self.directory / "seq.json.bak"
        self.dropped_file = self.directory / "outbox.dropped"

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

    def _read_seqs(self, path: Path) -> dict[str, dict]:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict) and type(v.get("next")) is int
                and isinstance(v.get("at"), str)}

    def _scan_max_seq(self, rollout_id: str) -> int:
        """Highest seq already used for `rollout_id` in the outbox and the parked files, or -1."""
        top = -1
        for path, wrapped in ((self.outbox, False), (self.rejected, True), (Path(f"{self.rejected}.1"), True)):
            try:
                text = path.read_text()
            except OSError:
                continue
            for line in text.split("\n"):
                try:
                    item = json.loads(line)
                    if wrapped:
                        item = json.loads(item["line"])
                    if item["entity_id"] == rollout_id and type(item["payload"]["seq"]) is int:
                        top = max(top, item["payload"]["seq"])
                except (ValueError, KeyError, TypeError):
                    continue
        return top

    def _next_seq(self, rollout_id: str) -> int:
        """Never lower than any copy of the counter or any event still on disk, so a lost or corrupt seq.json
        cannot make a new event reuse the seq of one the server already holds."""
        recorded = (self._read_seqs(p).get(rollout_id, {}).get("next", 0) for p in (self.seq_file, self.seq_backup))
        return max(*recorded, self._scan_max_seq(rollout_id) + 1)

    def _reserve_seq(self, rollout_id: str, seq: int) -> None:
        """Persist `seq + 1` (both copies) BEFORE the event is appended; prune rollouts idle for 30 days."""
        now = self.clock()
        merged: dict[str, dict] = {}
        for path in (self.seq_backup, self.seq_file):
            for key, entry in self._read_seqs(path).items():
                if key not in merged or entry["next"] >= merged[key]["next"]:
                    merged[key] = entry
        horizon = (now - timedelta(days=SEQ_RETENTION_DAYS)).isoformat()
        merged = {k: v for k, v in merged.items() if v["at"] >= horizon}
        merged[rollout_id] = {"next": seq + 1, "at": now.isoformat()}
        text = json.dumps(merged, sort_keys=True)
        self._write_atomic(self.seq_file, text)
        self._write_atomic(self.seq_backup, text)

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

    def dropped(self) -> int:
        """How many unflushed events were parked because the outbox was full (visible in `outbox status`)."""
        try:
            return int(self.dropped_file.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

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
            seq = self._next_seq(rollout_id)
            clean = validate_fleet_rollout(rollout_id, event_type,
                                           {"rollout_id": rollout_id, "ts": self.clock().isoformat(), "seq": seq,
                                            **payload})
            envelope = {"source": "fleet-watch", "entity_type": "fleet_rollout", "entity_id": rollout_id,
                        "event_type": event_type, "event_id": event_id(rollout_id, seq), "payload": clean}
            self._reserve_seq(rollout_id, seq)               # before the append: a crash burns a seq, never reuses one
            self._append(self.outbox, json.dumps(envelope, sort_keys=True))
            self._enforce_cap()
        if flush and self.client is not None:
            self.flush()
        return envelope

    def source_event(self, repo: str, event_type: str, **fields) -> dict:
        """A `source.*` event, filed on the watched repo's own stream (not tied to one rollout)."""
        return self.emit(source_stream_id(repo), event_type, repo=repo, **fields)

    def _enforce_cap(self) -> None:
        lines = self._raw_lines()
        overflow = len(lines) - self.max_pending
        if overflow <= 0:
            return
        for line in lines[:overflow]:
            self._park(line, "outbox full: oldest unflushed event parked")
        self._write_atomic(self.dropped_file, str(self.dropped() + overflow))
        self._write_atomic(self.outbox, "".join(f"{line}\n" for line in lines[overflow:]))

    # -- delivery ----------------------------------------------------------------------------------------

    def _post(self, envelope: dict) -> None:
        result = self.client.request("/api/v1/ingest", envelope)
        entity = result.get("entity")
        if result.get("ok") is not True or not isinstance(entity, dict) or entity.get("entity_id") != envelope["entity_id"]:
            raise NotAccepted("fleet_rollout event not accepted by the control plane")

    def flush(self) -> dict:
        """Deliver the queue oldest-first. Stops at the first transient failure (order matters); parks events the
        server will never accept. The lock is held only to read and to trim, never across a network call.
        Returns `{"sent", "pending", "rejected", "error"}`."""
        with self._locked():
            lines = self._raw_lines()
        if self.client is None:
            return {"sent": 0, "pending": len(lines), "rejected": 0, "error": "no control plane client"}
        done: set[str] = set()
        parks: list[tuple[str, str]] = []
        sent = 0
        error = None
        for line in lines:
            try:
                envelope = json.loads(line)
                if not isinstance(envelope, dict) or "payload" not in envelope:
                    raise ValueError("not an event envelope")
            except ValueError:
                parks.append((line, "unparseable"))
                done.add(line)
                continue
            try:
                self._post(envelope)
                sent += 1
                done.add(line)
            except NotAccepted as exc:
                parks.append((line, f"not accepted: {exc}"))
                done.add(line)
            except urllib.error.HTTPError as exc:
                if exc.code in PERMANENT_STATUSES:
                    parks.append((line, f"http {exc.code}: {exc.read(200).decode('utf-8', 'replace')}"))
                    done.add(line)
                    continue
                error = f"http {exc.code}"
                break
            except (OSError, ValueError) as exc:
                if isinstance(exc, ValueError) and "too large" in str(exc):
                    parks.append((line, str(exc)))
                    done.add(line)
                    continue
                error = f"{type(exc).__name__}: {exc}"[:200]
                break
        with self._locked():
            current = self._raw_lines()                      # includes anything emitted while we were on the network
            keep = [line for line in current if line not in done]
            for line, reason in parks:
                self._park(line, reason)
            if done:
                self._write_atomic(self.outbox, "".join(f"{line}\n" for line in keep))
        return {"sent": sent, "pending": len(keep), "rejected": len(parks), "error": error}

    def _park(self, line: str, reason: str) -> None:
        try:
            if self.rejected.stat().st_size > self.max_rejected_bytes:
                os.replace(self.rejected, f"{self.rejected}.1")      # one generation of history, then forgotten
        except FileNotFoundError:
            pass
        self._append(self.rejected, json.dumps({"reason": reason, "line": line}))
