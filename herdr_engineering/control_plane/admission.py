"""Private attribution of an admitted envelope, never authorization or enrollment.

Only the server ingestion path constructs this after an acknowledged envelope
write. Actor attribution is distinct from client-claimed source/sequence and
from projection application or persistence. No credentials are retained.
"""

import re
from dataclasses import dataclass, replace
from datetime import datetime

from .envelope import MAX_SEQUENCE


def _text(value, maximum=512):
    if type(value) is not str or not value or any(ord(c) < 32 for c in value):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _actor_valid(actor):
    if type(actor) is not dict or set(actor) != {"kind", "id", "authenticated_by"}:
        return False
    if actor == {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}:
        return True  # Trusted private listener, not authenticated human/host proof.
    if actor == {"kind": "unknown", "id": None, "authenticated_by": "none"}:
        return True
    return (
        actor["authenticated_by"] == "api_key"
        and actor["kind"] in ("operator", "viewer", "ingest", "unknown")
        and type(actor["id"]) is str
        and re.fullmatch(r"key:[0-9a-f]{12}", actor["id"]) is not None
    )


@dataclass(frozen=True)
class AdmissionContext:
    event_id: str
    entity_id: str
    source: str
    source_sequence: int
    received_at: str
    actor_kind: str
    actor_id: str | None
    authenticated_by: str
    child_index: int = 0

    def __post_init__(self):
        actor = self.to_private()["actor"]
        if (
            not _text(self.event_id, 4096)
            or not _text(self.entity_id, 128)
            or not _text(self.source, 32)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.source)
            or type(self.source_sequence) is not int
            or not 0 <= self.source_sequence <= MAX_SEQUENCE
            or type(self.child_index) is not int
            or not 0 <= self.child_index < 256
            or not _actor_valid(actor)
            or not _text(self.received_at, 128)
        ):
            raise ValueError("invalid private admission context")
        try:
            stamp = datetime.fromisoformat(self.received_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
        except ValueError:
            raise ValueError("invalid admission receive time") from None

    def for_child(self, index):
        return replace(self, child_index=index)

    def to_private(self):
        return {
            "event_id": self.event_id,
            "entity_id": self.entity_id,
            "source": self.source,
            "source_sequence": self.source_sequence,
            "received_at": self.received_at,
            "child_index": self.child_index,
            "admission_durable": True,
            "actor": {
                "kind": self.actor_kind,
                "id": self.actor_id,
                "authenticated_by": self.authenticated_by,
            },
        }

    @classmethod
    def from_admitted(cls, envelope, entity_id):
        """Internal only: caller has already acknowledged the envelope write."""
        actor = envelope.get("actor") or {}
        clean = {key: actor.get(key) for key in ("kind", "id", "authenticated_by")}
        if not _actor_valid(clean):
            clean = {"kind": "unknown", "id": None, "authenticated_by": "none"}
        return cls.from_private(
            {
                "event_id": envelope.get("event_id"),
                "entity_id": entity_id,
                "source": envelope.get("source"),
                "source_sequence": envelope.get("source_sequence"),
                "received_at": envelope.get("received_at"),
                "child_index": 0,
                "admission_durable": True,
                "actor": clean,
            }
        )

    @classmethod
    def from_private(cls, value):
        if (
            type(value) is not dict
            or set(value)
            != {
                "event_id",
                "entity_id",
                "source",
                "source_sequence",
                "received_at",
                "child_index",
                "admission_durable",
                "actor",
            }
            or value["admission_durable"] is not True
            or not _actor_valid(value["actor"])
        ):
            return None
        try:
            return cls(
                value["event_id"],
                value["entity_id"],
                value["source"],
                value["source_sequence"],
                value["received_at"],
                value["actor"]["kind"],
                value["actor"]["id"],
                value["actor"]["authenticated_by"],
                value["child_index"],
            )
        except (TypeError, ValueError):
            return None
