"""The ``annotation_resolve`` intent: a granted agent marks annotations addressed by a version it committed.

Light on purpose: ``collab-public`` imports this module (through ``collab_artifacts``) so that the kind is registered
there and ``append_intent`` can validate it, while the control-plane worker imports it for the handler.  Heavy
modules (``IntentContext`` lives in ``collab_intents``, which pulls in asyncpg) are imported for type checking only,
and the annotation store is imported inside the handler.

The kind is registered at import time, and registering twice raises, so every process imports this module once.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .annotation_anchor import ANNOTATION_ID_RE
from .collab_bridge_files import (
    BridgeLimits,
    Intent,
    IntentError,
    register_intent_kind,
)

if TYPE_CHECKING:
    from .collab_intents import CollabIntentApplier, IntentContext

KIND = "annotation_resolve"
MAX_IDS = 50
_KEYS = frozenset({"version_id", "annotation_ids"})
REASON_NO_GRANT = "no write grant"


@dataclass(frozen=True)
class AnnotationResolvePayload:
    version_id: str
    annotation_ids: tuple[str, ...]


def validate_annotation_resolve_payload(raw: dict, limits: BridgeLimits) -> AnnotationResolvePayload:
    """Exactly ``{version_id, annotation_ids}``; 1-50 unique ids, each a well-formed annotation id."""
    if not isinstance(raw, dict) or set(raw) != _KEYS:
        raise IntentError("invalid annotation_resolve payload")
    version_id, ids = raw["version_id"], raw["annotation_ids"]
    if not isinstance(version_id, str) or not version_id or len(version_id) > 200:
        raise IntentError("invalid annotation_resolve payload")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS:
        raise IntentError("invalid annotation_resolve payload")
    if not all(isinstance(i, str) and ANNOTATION_ID_RE.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
        raise IntentError("invalid annotation_resolve payload")
    return AnnotationResolvePayload(version_id, tuple(ids))


register_intent_kind(KIND, validate_annotation_resolve_payload, actors=frozenset({"agent"}), requires_artifact=True)


def _rejected(reason: str) -> dict:
    return {"result": "rejected", "reason": reason}


async def handle_annotation_resolve(ctx: IntentContext, intent: Intent) -> dict:
    """Resolve the payload's annotations for the calling agent.

    The registry's generic steps do not check grants or the idempotency key, so this does.  A crash between this
    return and ``record_outcome`` makes the retry report ``skipped: not_open`` for rows the first run resolved; the
    agent still reads the result file the FIRST run wrote (``_settle_from_row`` keeps an existing result file)."""
    from .artifact_annotations import (  # lazy: asyncpg-free import of this module
        AnnotationError,
        AnnotationStore,
    )

    artifact_id = intent.artifact_id
    if artifact_id is None or intent.idempotency_key is None:
        return _rejected("bad_envelope")
    agent_id = ctx.actor.id
    grant = await ctx.grants.grant_for(artifact_id, agent_id)
    expires = grant["expires_at"].timestamp() if grant is not None and hasattr(grant["expires_at"], "timestamp") else None
    if grant is None or grant["permission"] != "write" or expires is None or expires <= time.time():
        return _rejected(REASON_NO_GRANT)
    payload = intent.payload
    version = await ctx.store.get_version(payload.version_id, artifact_id)
    if version is None:
        return _rejected("bad_version")
    if version.get("created_via") != "collaborator" or version.get("collaborator_agent_id") != agent_id:
        return _rejected("not_own_version")
    try:
        out = await AnnotationStore(ctx.conn).resolve(artifact_id, list(payload.annotation_ids),
                                                      version_id=payload.version_id, actor=f"collab:{agent_id}")
    except AnnotationError as exc:
        return _rejected(exc.code if isinstance(exc.code, str) else "refused")
    return {"result": "resolved", "resolved": list(out["resolved"]), "skipped": list(out["skipped"])}


def register_annotation_resolve(applier: CollabIntentApplier) -> None:
    """Attach the handler (default lane); call next to ``register_commit`` in the worker."""
    applier.register(KIND, handle_annotation_resolve, replay_outcomes=frozenset({"resolved"}))
