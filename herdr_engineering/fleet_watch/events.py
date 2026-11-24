"""Rollout event emitter with a durable outbox (spec sections 5 and 8). Off unless something calls it.

The event model has no free text (decision 2026-11-05): every field is an enum, a host name, hex, a number or a UTC
timestamp, so nothing secret can ride along. Log text stays on the host (`logs.LogStore`) and events carry only a
reference to it (`Emitter.record_log`). `Emitter.emit` validates with the SAME allow-list the control plane applies,
assigns the next `seq` of the rollout's stream and appends the envelope to `outbox.jsonl` (fsynced) before it tries
`POST /api/v1/ingest`. A control plane that is down costs nothing: the event stays queued and `flush()` delivers it
later, oldest first, stopping at the first transient failure so the stream order is kept. Delivery is idempotent on
the server (`<rollout_id>:<seq>`), so a crash between "delivered" and "trimmed" just replays harmlessly.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import urllib.error
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..control_plane.fleet_rollout import event_id, source_stream_id, validate_fleet_rollout
from . import logs, state

#: HTTP statuses that mean "this event will never be accepted": parked, the queue moves on.
PERMANENT_STATUSES = frozenset({400, 409, 413, 422})
#: Optional payload fields whose value may legitimately be null; any other None is dropped before validation.
NULLABLE = frozenset({"before_sha", "after_sha", "halted_at_host"})
MAX_PENDING = 2000
MAX_REJECTED_BYTES = 1_000_000


class NotAccepted(Exception):
    """The control plane answered 200 but did not accept the event; retrying cannot help."""


class Emitter:
    """Queue-then-deliver emitter. `client` is anything with `request(route, body) -> dict` (MetadataClient)."""

    def __init__(self, directory: Path | str | None = None, client=None, *,
                 max_pending: int = MAX_PENDING, max_rejected_bytes: int = MAX_REJECTED_BYTES,
                 clock: Callable[[], datetime] | None = None):
        self.directory = Path(directory) if directory else state.state_dir()
        self.client = client
        self.max_pending = max_pending
        self.max_rejected_bytes = max_rejected_bytes
        self.clock = clock or (lambda: datetime.now(UTC))
        self.outbox = self.directory / "outbox.jsonl"
        self.rejected = self.directory / "outbox.rejected.jsonl"
        self.seq_file = self.directory / "seq.json"
        self.seq_backup = self.directory / "seq.json.bak"
        self.seq_marker = self.directory / "seq.init"
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
        recorded = [self._read_seqs(p).get(rollout_id, {}).get("next", 0) for p in (self.seq_file, self.seq_backup)]
        floor = max(*recorded, self._scan_max_seq(rollout_id) + 1)
        if floor == 0 and self.seq_marker.exists() and not any(self._readable(p) for p in (self.seq_file, self.seq_backup)):
            # Both counters are gone although this emitter has run before, and nothing on disk remembers the
            # stream: start from the clock (seconds, so it stays below the validator's 2**31) rather than at 0,
            # which could collide with seqs the server already holds.
            floor = min(int(self.clock().timestamp()), 2**31 - 10_000)
        return floor

    @staticmethod
    def _readable(path: Path) -> bool:
        try:
            return isinstance(json.loads(path.read_text()), dict)
        except (OSError, ValueError):
            return False

    def _reserve_seq(self, rollout_id: str, seq: int) -> None:
        """Persist `seq + 1` (both copies) BEFORE the event is appended. Counters are never pruned: they are a few
        bytes each, and a dropped counter would restart at seq 0 and collide with what the server already holds."""
        now = self.clock()
        merged: dict[str, dict] = {}
        for path in (self.seq_backup, self.seq_file):
            for key, entry in self._read_seqs(path).items():
                if key not in merged or entry["next"] >= merged[key]["next"]:
                    merged[key] = entry
        merged[rollout_id] = {"next": seq + 1, "at": now.isoformat()}
        text = json.dumps(merged, sort_keys=True)
        self._write_atomic(self.seq_file, text)
        self._write_atomic(self.seq_backup, text)
        if not self.seq_marker.exists():
            self._write_atomic(self.seq_marker, now.isoformat())

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

    def record_log(self, rollout_id: str, host: str, path_id: str, text: str) -> dict:
        """Keep `text` in this host's private log store and return the reference to put in `log=` of an event."""
        return logs.LogStore(self.directory / "logs").write(rollout_id, host, path_id, text, now=self.clock())

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
