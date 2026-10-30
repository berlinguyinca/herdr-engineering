"""Pi's handoffs as the Handoffs page sees them.

Pi reports every handoff as a ``handoff:<id>`` lineage node (state, cause, from/to model, checkpoint, from/to session) and, once a
successor acknowledges, a ``handoff`` edge. The registry (``handoffs.py``) holds handoffs agents create over the API; this module
turns the lineage nodes into the same row shape so one page can show both. Rows are only ever built from nodes the caller's
principal may read (the graph's own visibility rules). Times are when the control plane received the node's first and last event
(``first_seen_at`` / ``last_seen_at``), never Pi's own clock; a reader whose history is limited gets none, and the row then has no age.
"""
from __future__ import annotations

ORIGIN = "lineage"
# Pi's handoff lifecycle (prepared -> validated -> acknowledged -> committed, or failed/aborted) in the registry's words.
STATE = {"prepared": "queued", "pending": "queued", "validated": "queued", "proposed": "queued", "running": "in_progress",
         "acknowledged": "accepted", "committed": "completed", "done": "completed", "aborted": "cancelled",
         "cancelled": "cancelled", "failed": "failed", "superseded": "cancelled", "rolled_over": "completed"}
CAUSE_TEXT = {"context_pressure": "Context pressure: the session handed its work to a fresh session",
              "quota_exhausted": "Quota exhausted: the work moved to a fallback model",
              "phase_transition": "Phase transition: the next phase starts in a new session"}


def _plain(value):
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _text(value, limit=200):
    value = _plain(value)
    return str(value)[:limit] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def _bare(session_node_id):
    text = _text(session_node_id)
    return text.split(":", 1)[1] if text and text.startswith("session:") else text


def _model_change(cause, from_model, to_model):
    """same | switched | not_pinned | unknown. A missing target model on a context-pressure handoff means the successor was
    routed normally (it could not be pinned), which is the case an operator wants to see."""
    if not to_model:
        return "not_pinned" if cause == "context_pressure" else "unknown"
    if not from_model:
        return "unknown"
    bare = lambda m: str(m).rsplit("/", 1)[-1]
    return "same" if bare(from_model) == bare(to_model) else "switched"


def row_from_node(node):
    """One registry-shaped row from a ``handoff`` node view, or None when the node is not a usable handoff."""
    if not isinstance(node, dict) or node.get("node_type") != "handoff" or not node.get("id"):
        return None
    attrs = {k: _plain(v) for k, v in (node.get("attrs") or {}).items()} if isinstance(node.get("attrs"), dict) else {}
    cause, from_model, to_model = _text(attrs.get("cause")), _text(attrs.get("from_model")), _text(attrs.get("to_model"))
    raw_state = _text(node.get("state")) or "unknown"
    state = STATE.get(raw_state, raw_state)
    change = _model_change(cause, from_model, to_model)
    goal = _text(node.get("label"), 400) or CAUSE_TEXT.get(cause or "", "") or f"{attrs.get('handoff_kind') or 'handoff'} recorded by Pi"
    provenance = {k: v for k, v in {"cause": cause, "checkpoint": _text(attrs.get("checkpoint_sha")), "kind": _text(attrs.get("handoff_kind")),
                                    "from_model": from_model, "to_model": to_model, "lineage_state": raw_state,
                                    "from_generation": _text(attrs.get("from_generation")), "to_generation": _text(attrs.get("to_generation"))}.items() if v}
    return {"handoff_id": node["id"], "origin": ORIGIN, "state": state, "schema_version": 1,
            "created_at": _text(node.get("first_seen_at")) or "", "updated_at": _text(node.get("last_seen_at")) or "",
            "created_by": "pi", "goal": CAUSE_TEXT.get(cause or "", goal) if not node.get("label") else goal,
            "source": {"harness": "pi", "agent_id": None, "session_id": _bare(attrs.get("from_session_id"))},
            "target": {"harness": "pi", "agent_id": None, "session_id": _bare(attrs.get("to_session_id")), "model_hint": to_model},
            "completed": [], "remaining": [], "decisions": [], "risks": [], "validation": [], "artifact_refs": [],
            "provenance": provenance, "mission_id": _text(attrs.get("mission_id")), "room_id": None, "history": [],
            "cause": cause, "from_model": from_model, "to_model": to_model, "model_change": change,
            "checkpoint": _text(attrs.get("checkpoint_sha")), "handoff_kind": _text(attrs.get("handoff_kind")),
            "lineage_visibility": _text(node.get("visibility_scope"), 80)}


def rows_from_nodes(views):
    return [r for r in (row_from_node(v) for v in views or []) if r]
