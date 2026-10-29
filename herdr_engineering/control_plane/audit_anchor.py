"""Anchor the assistant audit head OFF the event store, and verify the stored chain against it.

The audit chain (``assistant.AssistantStore``) is tamper-evident inside itself, but anyone who can rewrite the event
store can drop the newest entries and the shortened chain still verifies. An *anchor* is a copy of the head
(``tenant_id``, ``seq``, ``sha256``, time) written somewhere the event store's writers cannot reach: an operator-chosen
append-only file (YAML ``personal_assistant.audit.anchor_file``; ideally a different device or an ``chattr +a`` file).

The anchor file is JSON lines. Each line carries ``line_sha256`` = sha256(previous line's ``line_sha256`` + this line's
canonical body), so editing or deleting an old line is detected too (it does not protect against deleting the TAIL of the
anchor file: keep copies, or put the file on append-only storage).

``verify`` asks the server (``GET /api/v1/assistant-audit/check``) whether the entry at the last anchored ``seq`` still
has the anchored ``sha256``. Statuses: ``ok`` | ``rolled_back`` (the chain is now shorter) | ``diverged`` (same position,
different entry: history was rewritten) | ``chain_broken`` | ``anchor_tampered`` | ``no_anchor``.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FIELDS = ("v", "tenant_id", "seq", "sha256", "keyed", "anchored_at", "prev_line_sha256")


class AnchorError(Exception):
    pass


def _canon(body: dict[str, Any]) -> str:
    return json.dumps({k: body[k] for k in FIELDS}, sort_keys=True, separators=(",", ":"))


def _line_digest(prev: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev + _canon(body)).encode()).hexdigest()


def read_anchors(path: str | os.PathLike) -> list[dict[str, Any]]:
    """All anchor lines; raises AnchorError if the file's own chain does not verify. A missing file is empty."""
    p = Path(path)
    if not p.exists():
        return []
    rows, prev = [], ""
    for n, raw in enumerate(p.read_text().splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            ok = (row.get("prev_line_sha256") == prev and row.get("line_sha256") == _line_digest(prev, row))
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            raise AnchorError(f"anchor file line {n} does not verify (edited, removed or reordered)")
        rows.append(row)
        prev = row["line_sha256"]
    return rows


def _check_head(head: Any) -> dict[str, Any]:
    if (not isinstance(head, dict) or not isinstance(head.get("tenant_id"), str) or not head["tenant_id"]
            or not isinstance(head.get("head"), dict) or not isinstance(head["head"].get("seq"), int)
            or not isinstance(head["head"].get("sha256"), str) or len(head["head"]["sha256"]) != 64):
        raise AnchorError("the server returned no audit head to anchor (the audit log is empty or unavailable)")
    return head


def anchor_head(path: str | os.PathLike, head_response: dict[str, Any], *,
                now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> dict[str, Any]:
    """Append the head from ``GET /api/v1/assistant-audit/head`` to the anchor file. Refuses (appends nothing) when the
    head is BEHIND the last anchor for that tenant: that is a rollback, not something to record over."""
    if not str(path):
        raise AnchorError("personal_assistant.audit.anchor_file is not set in the fleet YAML")
    resp = _check_head(head_response)
    tenant, h = resp["tenant_id"], resp["head"]
    p = Path(path)
    if p.is_symlink():
        raise AnchorError("the anchor file must not be a symlink")
    rows = read_anchors(p)
    mine = [r for r in rows if r["tenant_id"] == tenant]
    if mine:
        last = mine[-1]
        if h["seq"] < last["seq"]:
            raise AnchorError(f"refusing to anchor: the head (seq {h['seq']}) is behind the last anchor "
                              f"(seq {last['seq']}); the audit chain was rolled back or truncated")
        if h["seq"] == last["seq"]:
            if h["sha256"] != last["sha256"]:
                raise AnchorError("refusing to anchor: same seq as the last anchor but a different digest")
            return {**last, "unchanged": True}
    prev = rows[-1]["line_sha256"] if rows else ""
    body = {"v": 1, "tenant_id": tenant, "seq": h["seq"], "sha256": h["sha256"], "keyed": bool(h.get("keyed")),
            "anchored_at": now().strftime("%Y-%m-%dT%H:%M:%SZ"), "prev_line_sha256": prev}
    line = {**body, "line_sha256": _line_digest(prev, body)}
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AnchorError("the anchor file must be a regular file")
        os.write(fd, (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return line


def verify_against_anchor(path: str | os.PathLike, tenant_id: str,
                          check: Callable[[int, str], dict[str, Any]]) -> dict[str, Any]:
    """Compare the stored chain with the LAST anchor for ``tenant_id``. ``check(seq, sha256)`` is the server call."""
    try:
        rows = read_anchors(path)
    except AnchorError as exc:
        return {"status": "anchor_tampered", "ok": False, "detail": str(exc)}
    mine = [r for r in rows if r["tenant_id"] == tenant_id]
    if not mine:
        return {"status": "no_anchor", "ok": False, "detail": f"no anchor recorded for tenant {tenant_id}"}
    last = mine[-1]
    got = check(last["seq"], last["sha256"])
    status = got.get("status")
    if status not in ("ok", "rolled_back", "diverged", "chain_broken"):
        return {"status": "chain_broken", "ok": False, "detail": "unexpected answer from the server"}
    return {"status": status, "ok": status == "ok", "anchored": {k: last[k] for k in ("seq", "sha256", "anchored_at")},
            "head": got.get("head") or {}, "detail": got.get("detail", "")}


def check_chain(store, tenant_id: str, seq: int, sha256: str) -> dict[str, Any]:
    """Server side of ``/assistant-audit/check``: is the anchored entry still where it was?"""
    head = store.audit_head(tenant_id)
    if not store.verify_audit(tenant_id):
        return {"status": "chain_broken", "head": head, "detail": "the stored chain does not verify internally"}
    if not head or head["seq"] < seq:
        return {"status": "rolled_back", "head": head,
                "detail": f"the chain ends at seq {head.get('seq', 0)}, before the anchored seq {seq}"}
    with store._lock:
        entry = next((e for e in store._audit if e.get("tenant_id") == tenant_id and e.get("seq") == seq), None)
    if entry is None or entry.get("sha256") != sha256:
        return {"status": "diverged", "head": head, "detail": f"the entry at seq {seq} is not the one anchored"}
    return {"status": "ok", "head": head, "detail": ""}
