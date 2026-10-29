"""Read-only lineage access for the personal assistant.

The assistant is a mission *participant*: it may read the lineage graph exactly as the user it acts for
could, through the same ``read_graph`` front door (membership and scope checks included). The principal
is resolved by the server from the run's ``user_id``; nothing in the model's tool arguments is consulted
for identity. There is no write path here.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .graph import MAX_DEPTH, MAX_NODES, GraphProjection, read_graph

OPERATIONS = ("node", "neighborhood", "list")
SOURCE = "lineage graph projection"


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LineageReader:
    def __init__(self, graph: GraphProjection, principal_for_user: Callable[[str], dict | None],
                 *, now: Callable[[], str] = _utcnow):
        self.graph = graph
        self.principal_for_user = principal_for_user
        self.now = now

    def read(self, tenant_id: str, user_id: str, payload: dict) -> dict:
        """ValueError for a malformed request; LookupError for anything the user may not see or that is absent
        (the two are indistinguishable by design)."""
        op = payload.get("operation")
        if op not in OPERATIONS:
            raise ValueError(f"operation must be one of {', '.join(OPERATIONS)}")
        principal = self.principal_for_user(user_id)
        if principal is None:
            raise LookupError("not found")
        node_id = str(payload.get("node_id") or "")
        if op in ("node", "neighborhood") and not node_id:
            raise ValueError("node_id is required")
        if op == "node":
            parts, query = ["nodes", node_id], {}
        elif op == "neighborhood":
            depth = max(1, min(int(payload.get("depth") or 2), MAX_DEPTH))
            parts, query = ["nodes", node_id, "neighborhood"], {"depth": str(depth)}
        else:
            parts, query = ["nodes"], {"node_type": str(payload.get("node_type") or ""),
                                       "limit": str(min(int(payload.get("limit") or 50), MAX_NODES))}
        try:
            data = read_graph(self.graph, principal, parts, query.get)
        except KeyError:
            raise LookupError("not found") from None
        return {"source": SOURCE, "as_of": self.now(), "data": data,
                "note": "Absent means not visible to you or not recorded; relationships are explicit graph edges only."}


def principal_resolver(keys: dict[str, str]) -> Callable[[str], dict | None]:
    """user_id -> the principal that user authenticated as, from the server's own key table.

    ``keys`` maps API key -> configured role. The id format matches ``_principal()`` in api.py. With no keys
    configured the listener is open and every caller shares one operator identity.
    """
    import hashlib

    from .federation import kind_for_role

    def resolve(user_id: str) -> dict | None:
        by_id = {f"key:{hashlib.sha256(k.encode()).hexdigest()[:12]}": kind_for_role(r) for k, r in keys.items()}
        if not keys:
            if user_id == "open-listener":
                return {"kind": "operator", "id": "open-listener", "authenticated_by": "open_listener"}
            return None
        kind = by_id.get(user_id)
        return None if kind is None else {"kind": kind, "id": user_id, "authenticated_by": "api_key"}
    return resolve
