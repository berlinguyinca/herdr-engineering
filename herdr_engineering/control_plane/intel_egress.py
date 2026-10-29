"""Data-egress classes for model routing (DECISIONS D7). Default deny: unclassified content goes nowhere.

``llm_proxy`` has no data classification, so callers that route content derived from email, attachments or
scientific data must ask ``may_route`` first. A route's ``kind`` comes from operator YAML, never from the content.
"""
from __future__ import annotations

CLASSES = ("public", "internal", "private_mail", "scientific", "restricted")
LOCAL_ONLY = frozenset({"private_mail", "scientific", "restricted"})
ROUTE_KINDS = ("local", "approved_cloud", "external")


def may_route(data_class: str | None, route_kind: str | None, *, approved: frozenset[str] = frozenset()) -> tuple[bool, str]:
    """``approved`` is the operator's per-class list of cloud-approved classes (never includes LOCAL_ONLY)."""
    if data_class not in CLASSES:
        return False, "unclassified content is never routed"
    if route_kind not in ROUTE_KINDS:
        return False, "unknown route kind"
    if route_kind == "local":
        return True, "local route"
    if data_class in LOCAL_ONLY:
        return False, f"{data_class} content is local-only"
    if route_kind == "approved_cloud" and data_class in approved:
        return True, "approved cloud route"
    if route_kind == "external":
        return data_class == "public", "external routes carry public data only"
    return False, f"{data_class} is not approved for {route_kind}"
