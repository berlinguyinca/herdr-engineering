"""T16 H1: which stored ``collab`` rows are not the collaboration service's own, and a safe way to set them aside.

The collaboration service writes exactly one stream (``collab`` / ``log``), one row per event, with contiguous
sequences from 1, a payload carrying ``org_id``, ``correlation_id`` and ``created_at`` and NO ingest envelope.
Before ingest became an allow-list a key could store other ``collab`` rows (a v1 body has no envelope at all).
``classify`` finds them. Nothing here ever deletes data: the startup path only declines to replay them, and the
operator command moves them to a quarantine file/table that keeps every column.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import time
from datetime import UTC, datetime
from typing import Any

from . import logfile_lock
from .events import V2_COLUMNS

log = logging.getLogger(__name__)

REQUIRED_PAYLOAD = ("org_id", "correlation_id", "created_at")


def _present(v: Any) -> bool:
    """A stored json ``null`` (the string 'null' from a jsonb column) is no actor."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return True
    return bool(v)


def _payload(row: dict) -> Any:
    p = row.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except ValueError:
            return None
    return p


class SchemaTooOldError(RuntimeError):
    """``fabric_events`` predates the v2 envelope columns (migration 0003)."""


class LogInUseError(RuntimeError):
    """A running server may be appending to the log; rewriting it now could silently lose acknowledged events."""


def _instant(v: Any) -> float | None:
    if isinstance(v, datetime):
        d = v
    elif isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()


def _timestamps_agree(row: dict, p: dict) -> bool:
    """The service stamps ``source_timestamp``, ``ingest_timestamp`` and ``payload.created_at`` with ONE clock reading
    (``CollabService._commit``, unchanged since the log was introduced). A forged row that copies the payload shape
    cannot also reproduce that instant, because the store sets its own ingest time."""
    a, b, c = (_instant(row.get("source_timestamp")), _instant(row.get("ingest_timestamp")),
               _instant(p.get("created_at")))
    return a is not None and a == b == c


def classify(rows: list[dict], *, cut_tail: bool = False) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split stored collab rows into ``(keep, [(row, reason), ...])``.

    Reasons: ``foreign_stream`` (not collab/log), ``ingest_envelope`` (carries source/actor), ``not_collab_schema``
    (payload is not what the service writes), ``timestamp_mismatch`` (the three timestamps the service writes from
    one clock reading disagree: a forgery), ``duplicate_sequence`` (the first row at a sequence wins) and, only
    with ``cut_tail``, ``after_gap`` (everything after the first missing sequence). A gap is otherwise left to the
    service's own contiguity check, which switches collaboration off rather than guessing which events were lost.
    """
    kept: list[dict] = []
    out: list[tuple[dict, str]] = []
    for row in rows:
        p = _payload(row)
        if row.get("entity_id") != "log":
            out.append((row, "foreign_stream"))
        elif row.get("source") is not None or _present(row.get("actor")) or row.get("source_sequence") is not None:
            out.append((row, "ingest_envelope"))
        elif (not isinstance(p, dict) or not isinstance(row.get("event_type"), str) or not row["event_type"]
              or not all(isinstance(p.get(k), str) and p.get(k) for k in REQUIRED_PAYLOAD)
              or isinstance(row.get("sequence"), bool) or not isinstance(row.get("sequence"), int)):
            out.append((row, "not_collab_schema"))
        elif not _timestamps_agree(row, p):
            out.append((row, "timestamp_mismatch"))
        else:
            kept.append(row)
    order = sorted(range(len(kept)), key=lambda i: (kept[i]["sequence"], i))
    seen: set[int] = set()
    unique = []
    for i in order:
        seq = kept[i]["sequence"]
        if seq in seen:
            out.append((kept[i], "duplicate_sequence"))
        else:
            seen.add(seq)
            unique.append(kept[i])
    if cut_tail:
        good, expected = [], 1
        for row in unique:
            if row["sequence"] == expected:
                good.append(row)
                expected += 1
            else:
                out.append((row, "after_gap"))
        unique = good
    return unique, out


def _describe(bad: list[tuple[dict, str]]) -> list[dict]:
    return [{"sequence": r.get("sequence"), "event_id": r.get("event_id"), "event_type": r.get("event_type"),
             "reason": why} for r, why in bad]


def _report(rows: list[dict], apply: bool, cut_tail: bool) -> tuple[dict, list[tuple[dict, str]]]:
    keep, bad = classify(rows, cut_tail=cut_tail)
    report: dict = {"mode": "apply" if apply else "dry-run", "rows": len(rows), "keep": len(keep),
                    "quarantine": _describe(bad)}
    if not cut_tail:
        _, cut = classify(rows, cut_tail=True)
        listed = {id(r) for r, _ in bad}
        tail = [(r, why) for r, why in cut if id(r) not in listed]
        seqs = [r["sequence"] for r in keep]
        report["contiguous"] = seqs == list(range(1, len(seqs) + 1))
        if tail:
            report["gap_tail"] = _describe(tail)
            report["note_gap"] = ("collaboration stays disabled until the log is contiguous from sequence 1; these rows "
                                  "follow the first gap (or start before sequence 1) and are NOT set aside by default: "
                                  "rerun with --include-gap-tail to quarantine them too (still never deleted)")
    return report, bad


def _chown_like(path: str, orig: os.stat_result) -> None:
    """Give ``path`` the original log's owner and group. Only root can change them; anyone else already owns what
    they create, so a refusal there changes nothing."""
    if (os.stat(path).st_uid, os.stat(path).st_gid) == (orig.st_uid, orig.st_gid):
        return
    try:
        os.chown(path, orig.st_uid, orig.st_gid)
    except PermissionError:
        log.warning("could not give %s the owner of the original log (uid %s)", path, orig.st_uid)


def _own_pids() -> set[int]:
    """This process as /proc names it. In a container whose /proc is the host's (or another pid namespace's), our
    own descriptor shows under a pid that is not ``os.getpid()``; /proc/self always resolves to the right one."""
    me = {os.getpid()}
    try:
        me.add(int(os.readlink("/proc/self")))
    except (OSError, ValueError):
        pass
    return me


def _holders(path: str) -> list[int]:
    """Other processes that have ``path`` open (Linux /proc). Best effort: a server that predates the advisory lock
    is found here; an unreadable /proc entry is skipped."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    me, found = _own_pids(), []
    try:
        pids = [int(x) for x in os.listdir("/proc") if x.isdigit()]
    except OSError:
        return []
    for pid in pids:
        if pid in me:
            continue
        fddir = f"/proc/{pid}/fd"
        try:
            names = os.listdir(fddir)
        except OSError:
            continue
        for n in names:
            try:
                f = os.stat(f"{fddir}/{n}")
            except OSError:
                continue
            if (f.st_dev, f.st_ino) == (st.st_dev, st.st_ino):
                found.append(pid)
                break
    return found


def _jsonl_load(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        return [json.loads(line) for line in fh.read().split(b"\n") if line.strip()]


def repair_file(path: str, *, apply: bool = False, confirmed: bool = False, cut_tail: bool = False) -> dict:
    """Plan (default) or perform the quarantine of a collab-only JSON-lines log. ``apply`` needs ``confirmed`` and
    refuses while any process could still be appending: the log is locked exclusively (``FileEventLog`` holds a shared
    lock for its whole life) and no other process may have the file open. Nothing is deleted: the removed rows go to
    ``<log>.quarantine.jsonl`` and the original is kept as ``<log>.bak-<time>``."""
    if not apply:
        report, _ = _report(_jsonl_load(path), False, cut_tail)
        report["log"] = path
        report["note"] = "nothing changed; rerun with --apply --confirm-quarantine to move these rows aside"
        return report
    if not confirmed:
        raise PermissionError("--apply needs --confirm-quarantine")
    try:
        held = logfile_lock.acquire(path, exclusive=True)
    except logfile_lock.Busy as exc:
        raise LogInUseError(f"refusing to rewrite {path}: a running collaboration server holds it ({exc}); stop the "
                            "server first, then repair, then start it again") from None
    except OSError as exc:
        raise LogInUseError(f"refusing to rewrite {path}: cannot lock it ({exc}); stop the server and fix the "
                            "mount before repairing") from None
    lock = held
    try:
        if held.mode == "none":
            raise LogInUseError(f"refusing to rewrite {path}: this filesystem supports no lock, so a running server "
                                "cannot be ruled out; stop the server and repair on a local disk (copy the log)")
        others = _holders(path)
        if others:
            raise LogInUseError(f"refusing to rewrite {path}: process(es) {others} have it open; stop the server "
                                "first, then repair, then start it again")
        rows = _jsonl_load(path)                 # read AFTER the lock, so the plan is of the file we rewrite
        report, bad = _report(rows, True, cut_tail)
        report["log"] = path
        if not bad:
            return report
        stamp = time.strftime("%Y%m%dT%H%M%S")
        quarantine, backup = f"{path}.quarantine.jsonl", f"{path}.bak-{stamp}"
        drop = {id(r) for r, _ in bad}
        with open(quarantine, "ab") as fh:                       # the moved rows are durable BEFORE the log changes
            for r, why in bad:
                fh.write((json.dumps({"quarantine_reason": why, "quarantined_at": stamp, "row": r}) + "\n").encode())
            fh.flush()
            os.fsync(fh.fileno())
        orig = os.stat(path)                    # run as root, the rewritten log must still be the server's to write
        os.chmod(quarantine, 0o600)
        _chown_like(quarantine, orig)
        with open(path, "rb") as src, open(backup, "xb") as dst:     # a full copy of the original stays
            dst.write(src.read())
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(backup, 0o600)
        _chown_like(backup, orig)
        tmp = f"{path}.tmp-{stamp}"
        with open(tmp, "xb") as fh:
            for r in rows:
                if id(r) not in drop:
                    fh.write((json.dumps(r, separators=(",", ":")) + "\n").encode())
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stat.S_IMODE(orig.st_mode))
        _chown_like(tmp, orig)
        os.replace(tmp, path)       # safe: the exclusive lock + no other opener prove nothing writes the old file
        report.update(quarantine_file=quarantine, backup=backup)
        return report
    finally:
        lock.release()


_PG_COLUMNS = ("ctid::text AS tid, event_id, entity_id, sequence, event_type, payload, source, source_sequence, "
               "actor, source_timestamp, ingest_timestamp, recovery_instance")
_PG_ARCHIVE_COLUMNS = ", ".join((
    "event_id", "sequence", "event_type", "entity_type", "entity_id", "mission_id", "session_id",
    "payload", "source_timestamp", "ingest_timestamp", "schema_version", *V2_COLUMNS, "recovery_instance",
))


def _tid(text: str) -> tuple[int, int]:
    """'(12,3)' -> (12, 3): asyncpg binds a ``tid`` as a (block, offset) tuple, not as text."""
    block, offset = text.strip("() ").split(",")
    return int(block), int(offset)


async def _pg_rows(conn) -> list[dict]:
    try:
        recs = await conn.fetch(f"SELECT {_PG_COLUMNS} FROM fabric_events WHERE entity_type = 'collab' "
                                "ORDER BY ingest_timestamp, ctid")
    except Exception as exc:
        if type(exc).__name__ in ("UndefinedColumnError", "UndefinedTableError"):
            raise SchemaTooOldError("fabric_events is missing columns or the table: run `herdr-engineering "
                                    "control-plane migrate` first (migration 0003 adds the envelope columns), then "
                                    "repeat the repair") from None
        raise
    return [dict(r) for r in recs]


async def repair_postgres(conn, *, apply: bool = False, confirmed: bool = False, cut_tail: bool = False) -> dict:
    """The same for ``fabric_events``. Rows MOVE (one transaction, writers blocked for its duration, the plan made
    inside it) to ``fabric_events_quarantine``, which keeps every column plus the reason. Nothing is deleted
    outside that same-transaction insert; a second run finds nothing to do."""
    if not apply:
        report, _ = _report(await _pg_rows(conn), False, cut_tail)
        report["note"] = "nothing changed; rerun with --apply --confirm-quarantine to move these rows aside"
        return report
    if not confirmed:
        raise PermissionError("--apply needs --confirm-quarantine")
    async with conn.transaction():
        await conn.execute("LOCK TABLE fabric_events IN SHARE ROW EXCLUSIVE MODE")
        report, bad = _report(await _pg_rows(conn), True, cut_tail)
        if not bad:
            return report
        await conn.execute("CREATE TABLE IF NOT EXISTS fabric_events_quarantine (LIKE fabric_events)")
        await conn.execute("ALTER TABLE fabric_events_quarantine ADD COLUMN IF NOT EXISTS quarantine_reason text, "
                           "ADD COLUMN IF NOT EXISTS quarantined_at timestamptz DEFAULT now()")
        # Archives created before migration0015 have a different physical column
        # order. Upgrade them and archive by name, never with positional moved.*.
        await conn.execute("ALTER TABLE fabric_events_quarantine ADD COLUMN IF NOT EXISTS recovery_instance uuid")
        by_reason: dict[str, list[tuple[int, int]]] = {}
        for r, why in bad:
            by_reason.setdefault(why, []).append(_tid(r["tid"]))
        for why, tids in by_reason.items():
            await conn.execute(
                "WITH moved AS (DELETE FROM fabric_events WHERE entity_type = 'collab' AND ctid = ANY($1::tid[]) "
                "RETURNING *), ins AS (INSERT INTO fabric_events_quarantine "
                f"({_PG_ARCHIVE_COLUMNS}, quarantine_reason, quarantined_at) "
                f"SELECT {_PG_ARCHIVE_COLUMNS}, $2::text, now() FROM moved "
                "RETURNING 1) SELECT count(*) FROM ins", tids, why)
    return report
