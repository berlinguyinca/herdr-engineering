"""T16 H1: which stored ``collab`` rows are not the collaboration service's own, and a safe way to set them aside.

The collaboration service writes exactly one stream (``collab`` / ``log``), one row per event, with contiguous
sequences from 1, a payload carrying ``org_id``, ``correlation_id`` and ``created_at`` and NO ingest envelope.
Before ingest became an allow-list a key could store other ``collab`` rows (a v1 body has no envelope at all).
``classify`` finds them. Nothing here ever deletes data: the startup path only declines to replay them, and the
operator command moves them to a quarantine file/table that keeps every column.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

REQUIRED_PAYLOAD = ("org_id", "correlation_id", "created_at")


def _payload(row: dict) -> Any:
    p = row.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p)
        except ValueError:
            return None
    return p


def classify(rows: list[dict], *, cut_tail: bool = False) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split stored collab rows into ``(keep, [(row, reason), ...])``.

    Reasons: ``foreign_stream`` (not collab/log), ``ingest_envelope`` (carries source/actor), ``not_collab_schema``
    (payload is not what the service writes), ``duplicate_sequence`` (the first row at a sequence wins) and, only
    with ``cut_tail``, ``after_gap`` (everything after the first missing sequence). A gap is otherwise left to the
    service's own contiguity check, which switches collaboration off rather than guessing which events were lost.
    """
    kept: list[dict] = []
    out: list[tuple[dict, str]] = []
    for row in rows:
        p = _payload(row)
        if row.get("entity_id") != "log":
            out.append((row, "foreign_stream"))
        elif row.get("source") is not None or row.get("actor") or row.get("source_sequence") is not None:
            out.append((row, "ingest_envelope"))
        elif (not isinstance(p, dict) or not isinstance(row.get("event_type"), str) or not row["event_type"]
              or not all(isinstance(p.get(k), str) and p.get(k) for k in REQUIRED_PAYLOAD)
              or isinstance(row.get("sequence"), bool) or not isinstance(row.get("sequence"), int)):
            out.append((row, "not_collab_schema"))
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


def _jsonl_load(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        return [json.loads(line) for line in fh.read().split(b"\n") if line.strip()]


def repair_file(path: str, *, apply: bool = False, confirmed: bool = False, cut_tail: bool = False) -> dict:
    """Plan (default) or perform the quarantine of a collab-only JSON-lines log. ``apply`` needs ``confirmed``."""
    rows = _jsonl_load(path)
    keep, bad = classify(rows, cut_tail=cut_tail)
    report = {"mode": "apply" if apply else "dry-run", "log": path, "rows": len(rows), "keep": len(keep),
              "quarantine": [{"sequence": r.get("sequence"), "event_id": r.get("event_id"),
                              "event_type": r.get("event_type"), "reason": why} for r, why in bad]}
    if not apply:
        report["note"] = "nothing changed; rerun with --apply --confirm-quarantine to move these rows aside"
        return report
    if not confirmed:
        raise PermissionError("--apply needs --confirm-quarantine")
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
    os.chmod(quarantine, 0o600)
    with open(path, "rb") as src, open(backup, "xb") as dst:     # a full copy of the original stays
        dst.write(src.read())
        dst.flush()
        os.fsync(dst.fileno())
    os.chmod(backup, 0o600)
    tmp = f"{path}.tmp-{stamp}"
    with open(tmp, "xb") as fh:
        for r in rows:
            if id(r) not in drop:
                fh.write((json.dumps(r, separators=(",", ":")) + "\n").encode())
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    report.update(quarantine_file=quarantine, backup=backup)
    return report


async def repair_postgres(conn, *, apply: bool = False, confirmed: bool = False, cut_tail: bool = False) -> dict:
    """The same for ``fabric_events``: rows move (in one transaction) to ``fabric_events_quarantine``."""
    recs = await conn.fetch(
        "SELECT ctid::text AS tid, event_id, entity_id, sequence, event_type, payload, source, source_sequence, actor "
        "FROM fabric_events WHERE entity_type = 'collab' ORDER BY ingest_timestamp, ctid")
    rows = [dict(r) for r in recs]
    keep, bad = classify(rows, cut_tail=cut_tail)
    report = {"mode": "apply" if apply else "dry-run", "rows": len(rows), "keep": len(keep),
              "quarantine": [{"sequence": r.get("sequence"), "event_id": r.get("event_id"),
                              "event_type": r.get("event_type"), "reason": why} for r, why in bad]}
    if not apply:
        report["note"] = "nothing changed; rerun with --apply --confirm-quarantine to move these rows aside"
        return report
    if not confirmed:
        raise PermissionError("--apply needs --confirm-quarantine")
    if not bad:
        return report
    async with conn.transaction():
        await conn.execute("CREATE TABLE IF NOT EXISTS fabric_events_quarantine (LIKE fabric_events)")
        await conn.execute("ALTER TABLE fabric_events_quarantine ADD COLUMN IF NOT EXISTS quarantine_reason text, "
                           "ADD COLUMN IF NOT EXISTS quarantined_at timestamptz DEFAULT now()")
        by_reason: dict[str, list[str]] = {}
        for r, why in bad:
            by_reason.setdefault(why, []).append(r["tid"])
        for why, tids in by_reason.items():
            await conn.execute(
                "WITH moved AS (DELETE FROM fabric_events WHERE entity_type = 'collab' AND ctid = ANY($1::tid[]) "
                "RETURNING *) INSERT INTO fabric_events_quarantine SELECT * FROM moved", tids)
            await conn.execute("UPDATE fabric_events_quarantine SET quarantine_reason = $1 "
                               "WHERE quarantine_reason IS NULL", why)
    return report
