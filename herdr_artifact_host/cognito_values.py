"""One rule for the shape of the Cognito identifiers, shared by the deploy-time config check and the artifact host.

Dependency free on purpose: ``herdr_engineering.fleet_config_cli`` runs on the deploy host and the host refuses to start
on a value this rejects, so the two must agree exactly.  This file exists as two byte-identical copies
(``herdr_artifact_host`` and ``herdr_engineering``; the host image ships only the former) and a test keeps them in step.
The shapes are the runtime's own (public_server's client id, cognito's pool id): a value they reject would silently turn
the lab shares, the member surface and housekeeping off while the host reports healthy."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .https_origin import https_origin

CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
POOL_ID = re.compile(r"[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]+")


def cognito_problem(cognito: Mapping[str, object]) -> tuple[str, str] | None:
    """``(key, rule)`` for the first value of ``cognito`` (keys ``pool_id``, ``web_client_id``, ``domain``,
    ``client_ids``, and ``group`` when given) that has the wrong shape, else None.  Presence is checked by the caller."""
    if https_origin(cognito.get("domain")) is None:
        return "domain", "must be a bare https origin (https://host)"
    pool = cognito.get("pool_id")
    if not isinstance(pool, str) or not POOL_ID.fullmatch(pool):
        return "pool_id", "must look like <region>_<id>"
    web = cognito.get("web_client_id")
    if not isinstance(web, str) or not CLIENT_ID.fullmatch(web):
        return "web_client_id", "must match [A-Za-z0-9_-]{1,128}"
    clients = cognito.get("client_ids")
    if not isinstance(clients, list) or not all(
        isinstance(c, str) and CLIENT_ID.fullmatch(c) for c in clients
    ):
        return "client_ids", "must each match [A-Za-z0-9_-]{1,128}"
    if "group" in cognito and (not isinstance(cognito["group"], str) or not cognito["group"]):
        return "group", "must be a non-empty string"
    return None
