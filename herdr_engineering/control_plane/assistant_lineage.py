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


def _bounded(value, default: int, low: int, high: int, name: str) -> int:
    """A model-supplied number, clamped. Anything that is not a finite integer is a ValueError, never a crash."""
    try:
        return max(low, min(int(value or default), high))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer") from None


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
            depth = _bounded(payload.get("depth"), 2, 1, MAX_DEPTH, "depth")
            parts, query = ["nodes", node_id, "neighborhood"], {"depth": str(depth)}
        else:
            parts, query = ["nodes"], {"node_type": str(payload.get("node_type") or ""),
                                       "limit": str(_bounded(payload.get("limit"), 50, 1, MAX_NODES, "limit"))}
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


class ActionLineageEmitter:
    """Record an assistant action in the lineage graph so it can be followed from the thread that asked.

    A ``decision:assistant.<action_id>`` node carries the correlation ids (assistant, thread, run, action,
    approval, request hash, idempotency key) and hangs off a ``thread:assistant.<thread_id>`` node through the
    existing edge kinds: ``delegation`` (work handed to a reviewer or proposed), ``produced`` (an artifact the
    action created) and ``references`` (the node it was about). Everything is ``private`` to the user the
    assistant acts for. Event ids derive from the action id, so a replay is a no-op. Emission is best effort.
    """
    DELEGATING = frozenset({"request_review", "propose_branch", "request_mission"})
    ATTRS = ("assistant_id", "thread_id", "run_id", "action_id", "approval_id", "request_hash",
             "idempotency_key", "tool_id", "delegation_id", "proposal_id", "artifact_id", "target_id")

    def __init__(self, ingest, principal_for_user: Callable[[str], dict | None]):
        self.ingest, self.principal_for_user = ingest, principal_for_user

    def emit(self, user_id: str, c: dict) -> dict:
        result = {"node": None, "thread": None, "edges": [], "error": None}
        principal = self.principal_for_user(user_id)
        action_id = str(c.get("action_id") or "")
        if principal is None or not action_id:
            result["error"] = "no principal or action id; nothing emitted"
            return result
        n = [0]
        source = f"asst-{action_id}"[:64]

        def send(event_type: str, entity_id: str, payload: dict) -> None:
            n[0] += 1
            self.ingest.ingest({
                "schema_version": 2, "event_id": f"{source}-{n[0]}"[:128], "source": source,
                "source_sequence": n[0], "entity_type": "lineage", "entity_id": entity_id,
                "event_type": event_type, "payload": payload, "visibility_scope": "private"}, principal)

        try:
            node = f"decision:assistant.{action_id}"
            attrs = {k: str(c[k])[:256] for k in self.ATTRS if c.get(k)}
            attrs["source"] = "personal-assistant"
            send("node.upsert", node, {"node_type": "decision", "node_id": node,
                                       "label": f"assistant {c.get('tool_id', 'action')}", "state": "recorded",
                                       "attrs": attrs})
            result["node"] = node
            tool = c.get("tool_id")
            thread = f"thread:assistant.{c['thread_id']}" if c.get("thread_id") else None
            if thread:
                send("node.upsert", thread, {"node_type": "thread", "node_id": thread,
                                             "label": "assistant conversation", "state": "open",
                                             "attrs": {"assistant_id": str(c.get("assistant_id") or ""),
                                                       "source": "personal-assistant"}})
                result["thread"] = thread
            edges = []
            if thread and tool in self.DELEGATING:
                edges.append(("delegation", thread, node))
            if tool == "draft_email" and c.get("artifact_id"):
                edges.append(("produced", node, f"artifact:{c['artifact_id']}"))
            if c.get("target_id") and _is_node_id(str(c["target_id"])):
                edges.append(("references", node, str(c["target_id"])))
            for edge_type, frm, to in edges:
                try:
                    send("edge.add", frm, {"edge_type": edge_type, "from_id": frm, "to_id": to})
                    result["edges"].append(f"{edge_type}:{frm}->{to}")
                except Exception as exc:  # noqa: BLE001 - e.g. a target this principal may not write; the node stands
                    result["error"] = f"edge {edge_type} skipped: {exc}"
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc) or type(exc).__name__
        return result


def _is_node_id(value: str) -> bool:
    from .graph import _node_id
    try:
        _node_id(value, "target")
        return True
    except ValueError:
        return False
