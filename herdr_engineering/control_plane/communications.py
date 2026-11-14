"""Read-only communication audit. Host reports never confer command authority."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import TYPE_CHECKING, cast

from .redact import redact_payload

if TYPE_CHECKING:
    from .repo import ControlPlaneRepo

STATUSES = frozenset({"queued", "delivered", "failed", "unknown"})
FIELDS = frozenset(
    {
        "operation_id",
        "status",
        "record_id",
        "reported_at",
        "text",
        "truncated",
        "host_id",
        "sender",
        "recipient",
        "route",
        "sender_origin",
        "channel",
        "evidence_kind",
    }
)


def validate_observation(
    identity: str, event_type: str, payload: dict[str, object], *, trusted: bool = False
) -> dict[str, object]:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", identity)
        or payload.get("operation_id") != identity
        or set(payload) - FIELDS
    ):
        raise ValueError("invalid communication operation")
    status = payload.get("status")
    if (
        not isinstance(status, str)
        or status not in STATUSES
        or event_type != "communication." + status
    ):
        raise ValueError("invalid communication status")
    if payload.get("evidence_kind") != ("server_receipt" if trusted else "host_observation"):
        raise ValueError("host observations cannot assert server authority")
    text = payload.get("text")
    if not isinstance(text, str) or len(text.encode()) > 16 * 1024:
        raise ValueError("communication text exceeds 16 KiB")
    for key in ("record_id", "host_id", "sender_origin", "channel"):
        if not isinstance(payload.get(key), str) or len(str(payload[key])) > 300:
            raise ValueError("invalid communication metadata")
    stamp = payload.get("reported_at")
    if (
        type(stamp) not in (int, float)
        or not math.isfinite(cast(float, stamp))
        or not 0 <= cast(float, stamp) <= 253402300799
    ):
        raise ValueError("invalid observation time")
    if type(payload.get("truncated")) is not bool:
        raise ValueError("invalid truncation flag")
    route = payload.get("route")
    if (
        not isinstance(route, list)
        or len(route) > 16
        or any(not isinstance(v, str) or len(v) > 1000 for v in route)
    ):
        raise ValueError("invalid native route")
    for name in ("sender", "recipient"):
        value = payload.get(name)
        if not isinstance(value, dict) or set(value) - {
            "pane_id",
            "terminal_id",
            "agent_session",
            "agent",
            "name",
            "session_id",
        }:
            raise ValueError("invalid communication identity")
        if len(json.dumps(value).encode()) > 4096:
            raise ValueError("communication identity exceeds its bound")
        for key, item in value.items():
            if key == "agent_session":
                if (
                    not isinstance(item, dict)
                    or set(item) - {"source", "agent", "kind", "value"}
                    or any(not isinstance(v, str) for v in item.values())
                ):
                    raise ValueError("invalid native conversation identity")
            elif not isinstance(item, str) or len(item) > 1000:
                raise ValueError("invalid communication identity field")
    return cast(dict[str, object], redact_payload(payload))


def record_observation(
    repo: ControlPlaneRepo, payload: dict[str, object], *, trusted: bool = False
) -> bool:
    identity, status = str(payload.get("operation_id", "")), str(payload.get("status", ""))
    clean = validate_observation(identity, "communication." + status, payload, trusted=trusted)
    with repo._command_lock:
        for event in repo.stream("communication", identity):
            previous = event.get("payload", {})
            if (previous.get("evidence_kind"), previous.get("status"), previous.get("host_id")) == (
                clean["evidence_kind"],
                status,
                clean["host_id"],
            ):
                # Replay has no transport side effect and does not append a second message.
                return False
        if repo._sink is not None and not callable(getattr(repo._sink, "record_required", None)):
            raise RuntimeError("communication audit requires acknowledged persistence")
        repo.record_event(
            "communication",
            identity,
            "communication." + status,
            clean,
            require_durable=repo._sink is not None,
        )
    return True


def command_failure_status(error: Exception) -> str:
    from ..errors import TimeoutError_Engineering
    from ..herdr_adapter import NativeDeliveryUnconfirmed

    return (
        "unknown"
        if isinstance(error, (TimeoutError_Engineering, NativeDeliveryUnconfirmed))
        else "failed"
    )


def record_command(
    repo: ControlPlaneRepo, session: dict[str, object], receipt: dict[str, object]
) -> None:
    """Mirror an authoritative server command without invoking its transport."""
    text = str(redact_payload(receipt.get("text", ""))).encode()
    operation = str(receipt["operation_id"])
    from ..communications import operation_identity, receipt_recipient

    operation = operation_identity(operation)
    status = str(receipt["status"])
    recipient = {
        key: value
        for key in ("session_id", "pane_id", "terminal_id", "agent_session")
        if (value := session.get(key)) is not None
    }
    delivery = receipt.get("delivery")
    if isinstance(delivery, dict):
        actual = receipt_recipient(json.dumps(delivery.get("receipt", {})).encode())
        if actual:
            recipient = actual
    record_observation(
        repo,
        {
            "operation_id": operation,
            "status": status,
            "record_id": hashlib.sha256((operation + status + "server").encode()).hexdigest(),
            "reported_at": repo._now(),
            "text": text[: 16 * 1024].decode(errors="ignore"),
            "truncated": len(text) > 16 * 1024,
            "host_id": str(session.get("host_id") or ""),
            "sender": {"agent": "control-plane"},
            "recipient": recipient,
            "route": [],
            "channel": "herdr.agent.prompt",
            "sender_origin": "control_plane",
            "evidence_kind": "server_receipt",
        },
        trusted=True,
    )


def decode_before(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    try:
        if len(value) > 512:
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (
            not isinstance(data, list)
            or len(data) != 2
            or not all(isinstance(v, str) for v in data)
        ):
            raise ValueError()
        from datetime import datetime

        if datetime.fromisoformat(data[0]).tzinfo is None or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,200}", data[1]
        ):
            raise ValueError()
        return data[0], data[1]
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValueError("invalid communication history cursor") from exc


def _rank(event: dict[str, object]) -> tuple[bool, bool, str, int]:
    p = cast(dict[str, object], event["payload"])
    return (
        p.get("evidence_kind") == "server_receipt",
        p.get("status") != "queued",
        str(event.get("ingest_timestamp", "")),
        int(cast(int, event.get("sequence", 0))),
    )


def _link(
    repo: ControlPlaneRepo, party: object, host: str, *, remote: bool = False
) -> dict[str, object]:
    identity = dict(party) if isinstance(party, dict) else {}
    sessions = repo.list_sessions()
    # A remote route is not proof that the recipient lives on the reporting host.
    if remote:
        identity.pop("session_id", None)
        return identity
    matches = []
    for session in sessions:
        same_host = repo._canonical_host(session.get("host_id")) == repo._canonical_host(host)
        direct = identity.get("session_id") == session["session_id"]
        native = identity.get("agent_session")
        conversation = bool(
            isinstance(native, dict)
            and native.get("value")
            and native == session.get("agent_session")
        )
        terminal = identity.get("terminal_id")
        pane = identity.get("pane_id")
        location = bool(
            terminal
            and terminal == session.get("terminal_id")
            or pane
            and pane == session.get("pane_id")
        )
        # Conversation identity survives a pane being reused by another agent.
        matched = (
            conversation if isinstance(native, dict) and native.get("value") else direct or location
        )
        if same_host and matched:
            matches.append(session["session_id"])
    # Ambiguous mappings remain explicit; never link the focused or arbitrary pane.
    identity.pop("session_id", None)
    if len(matches) == 1:
        identity["session_id"] = matches[0]
    return identity


def communication_page(
    repo: ControlPlaneRepo, *, limit: int = 50, before: str | None = None
) -> dict[str, object]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    boundary = decode_before(before)
    reader = getattr(repo._durable_history, "communications", None)
    if callable(reader):
        candidates = reader(limit + 1, boundary)
    else:
        candidates = []
        for identity in repo.entity_ids("communication"):
            events = repo.stream("communication", identity)
            if not events:
                continue
            event = max(events, key=_rank)
            start = min(str(e["ingest_timestamp"]) for e in events)
            if boundary is None or (start, identity) < boundary:
                candidates.append({**event, "communication_started": start})
        candidates.sort(key=lambda e: (e["communication_started"], e["entity_id"]), reverse=True)
        candidates = candidates[: limit + 1]
    rows = []
    for event in candidates[:limit]:
        payload = cast(dict[str, object], redact_payload(event["payload"]))
        host = str(payload.get("host_id", ""))
        route = cast(list[str], payload.get("route", []))
        rows.append(
            {
                **payload,
                "created_at": str(event["communication_started"]),
                "recorded_at": str(event["ingest_timestamp"]),
                "sender": _link(repo, payload.get("sender"), host),
                "recipient": _link(
                    repo,
                    payload.get("recipient"),
                    host,
                    remote=any(
                        part.split("=", 1)[0] in {"--machine", "--remote"} for part in route
                    ),
                ),
                "mission_progress_verified": False,
            }
        )
    cursor = None
    if len(candidates) > limit:
        last = candidates[limit - 1]
        cursor = (
            base64.urlsafe_b64encode(
                json.dumps([str(last["communication_started"]), last["entity_id"]]).encode()
            )
            .decode()
            .rstrip("=")
        )
    return {"communications": rows, "has_more": len(candidates) > limit, "next_cursor": cursor}
