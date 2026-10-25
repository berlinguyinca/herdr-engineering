"""Ingestion service (Phase 1b): map runtime lifecycle events to the repo.

The control-plane dashboard is populated by tapping the existing herdr system
(agents, workspaces/sessions, missions, machines) rather than custom per-host
reporters. This service defines the ingestion contract: a generic event
envelope that any emitter can POST, translated idempotently into stable,
structured entities (hosts / sessions / missions) plus their event streams —
so repeated heartbeats never duplicate a record, and the same external id maps
to one control-plane entity.
"""
from __future__ import annotations

import hashlib
import logging
import re

from ..review_gate.entities import RESERVED_ENTITY_PREFIXES

log = logging.getLogger(__name__)
from .envelope import ENVELOPE_ONLY_TYPES, EnvelopeConflict, normalize_envelope
from .events import SequenceCollision
from .federation import (
    clamp_for_principal, in_peer_namespace, is_peer_actor, is_peer_node, peer_fingerprint, peer_of,
    policy_from_source,
)
from .graph import validate_lineage
from .host_metrics_store import STATE_KEYS, clean_metrics, clean_state
from .project_links import canonical_repository
from .sink import EventAlreadyStored


def _git_identity(payload):
    """Only explicit forge identity and a dedicated full commit SHA link CI."""
    fields = ("repository_id", "forge_url", "repository_url", "repo_url", "clone_url")
    advertised = [payload[k] for k in fields if payload.get(k) is not None]
    identities = {canonical_repository(value, forge_host=payload.get("forge_host"))
                  for value in advertised}
    if None in identities or len(identities) > 1:
        raise ValueError("repository identity must identify one explicit forge repository")
    commit_sha = payload.get("commit_sha")
    if commit_sha is not None:
        if not isinstance(commit_sha, str) or not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", commit_sha):
            raise ValueError("commit_sha must be a full 40 or 64 character hexadecimal commit")
        commit_sha = commit_sha.lower()
    return {"repository_id": next(iter(identities), None), "commit_sha": commit_sha}


# Entity types an ingest principal may write. Dispatched types have handlers; the generic ones are
# plain activity records nothing authoritative reads back. Adding a type here is a security decision.
DISPATCHED_ENTITY_TYPES = frozenset({"lineage", "host", "session", "mission", "service", "inspector",
                                     "mission_activity", "test_run", "build", "review",
                                     "review_finding", "pull_request"})
# ledger / platform / lifecycle are the Pi envelope adapters' activity streams (envelopeAdapters.ts)
# repository / request / span are the opt-in system-graph adapter's lifecycle streams (graph_ingest.py, runbook
# docs/specs/herdr-system-graph-20261020/11-p0-p6-status-and-runbook.md). They were accepted before T16, nothing
# authoritative reads them back, and like mission/session they are only reflected into lineage when the operator
# turned the adapter on.
GENERIC_ENTITY_TYPES = frozenset({"task", "note", "workspace", "ledger", "platform", "lifecycle",
                                  "repository", "request", "span"})
INGEST_ENTITY_TYPES = DISPATCHED_ENTITY_TYPES | GENERIC_ENTITY_TYPES
# Event types the server itself writes for an allowed entity type: receipts other code trusts as idempotency
# markers (repo.send_message, ci_debug, bridge). A client may not name them.
SERVER_OWNED_EVENT_TYPES = {"session": frozenset({"SessionCommandQueued", "SessionCommandSettled"})}

# Sources whose host events prove the host is alive: its fabric agent, and the
# herdr feed plugin on the host (deploy/herdr-cp-feed-plugin posts host.heartbeat
# / host.inventory with no source, i.e. the default "herdr"). Any other source
# may only update host state keys (see IngestService._ingest_host_state).
LIVENESS_SOURCES = frozenset({"fabric-agent", "herdr"})
_AGENT_ONLY_HOST_KEYS = ("telemetry", "cpu", "mem", "disk", "inventory")


class IngestService:
    """Translates lifecycle event envelopes into idempotent repo mutations."""

    def __init__(self, repo, key_scopes=None, system_graph: bool = False):
        self.repo = repo
        # Per-key room/mission allow-lists for lineage addressed to a room or mission (operator YAML
        # `control_plane.lineage_key_scopes`, resolved by the server to principal ids):
        # {principal_id: {"rooms": set, "missions": set}}. A key with no entry may write only
        # org/private scopes. "*" in a set is an explicit operator opt-in for every id (and for bare, id-less scopes).
        self.key_scopes = key_scopes if key_scopes is not None else {}
        # T10: node ids inside an external agent's namespace belong to that agent alone. Set by the server
        # to the HCN service's `is_external_node`; None = no external agents on this process.
        self.reserved_node = None
        # T11: ids only the HCN service writes (delegation task and accepted-proposal nodes). Set by the server to
        # the HCN service's `is_hcn_node`; checked on EVERY endpoint of an edge, so an ingest key cannot attach
        # a fake `produced` edge to an accepted artifact either.
        self.service_node = None
        # ADR-0008 P1: hot source of the raw `control_plane.federation` YAML section (set by the server; None = no
        # federation, so no peer event is ever admitted). Read once per ingest; any failure means OFF.
        self.peer_source = None
        # System-graph live-ingest adapter (system-graph spec P2-P4). When enabled,
        # mission/session/repository/request/span lifecycle events are ALSO emitted
        # as lineage envelopes into the running graph. Opt-in; disabled by default.
        self.system_graph = None
        if system_graph:
            from .graph_ingest import GraphIngestAdapter
            self.system_graph = GraphIngestAdapter(repo, scope="org")

    def _peer_policy(self, actor):
        """The enabled YAML policy of the peer ``actor`` is, or None. The actor must be well formed, federation
        and that peer switched on, and its pinned key fingerprint must still be the configured one: removing
        or re-keying a peer in YAML refuses its next event."""
        found = peer_of(actor)
        policy = policy_from_source(self.peer_source)
        if found is None or not policy.enabled:
            return None
        peer = policy.peer(found[0])
        full = peer_fingerprint(actor)
        # the FULL pinned fingerprint, and the id's display label must be its prefix (ADR-0008 P2: P1 matched 12 chars)
        if peer is None or not peer.enabled or full is None or full[:12] != found[1] or not peer.matches(full):
            return None
        return peer

    def _peer_clamp(self, principal, entity_type):
        """ADR-0008 Decision 4. None for a non-peer. A peer is admitted only as a v2 ``lineage`` writer bound to
        its one mirror room; anything else (a v1 body, another entity type, a peer-user, an unknown or disabled
        peer) is refused with the same 403 as any lineage refusal, before anything is stored."""
        if not is_peer_actor(principal):
            return None
        deny = PermissionError("not permitted to modify this lineage object")
        policy = self._peer_policy(principal)            # (a peer-user, or any malformed actor, has no policy)
        if policy is None or entity_type != "lineage":
            raise deny
        return clamp_for_principal(principal, bind_room_id=policy.mirror_room)

    def _check_peer_namespace(self, env: dict, event_type: str, payload: dict, entity_id=None) -> None:
        """The reserved ``peer:<peer>.`` node namespace (Decision 7). Unconditional: it holds for EVERY actor kind
        (an operator, the HCN service and an open listener included), whether or not a collab service is wired.
        Nobody local may write, attach to or edge into it; a peer may write ONLY inside its own namespace, on
        EVERY endpoint, so a mirrored edge can never touch a local node, and its ``entity_id`` (the stream the row
        is stored under) must lie inside it too."""
        actor = env.get("actor") or {}
        ends = ((payload.get("from_id"), payload.get("to_id")) if event_type.startswith("edge.")
                else (payload.get("node_id"),))
        found = peer_of(actor)
        if is_peer_actor(actor):
            if found is None or not all(in_peer_namespace(e, found[0]) for e in (*ends, entity_id)):
                raise PermissionError("not permitted to modify this lineage object")
        elif any(is_peer_node(e) for e in ends):
            raise PermissionError("not permitted to modify this lineage object")

    def _check_lineage_scope(self, env: dict) -> None:
        """Fail closed: a non-operator key may address a room or mission only if the operator listed it.
        Refused with the same PermissionError (HTTP 403, reason lineage_forbidden) as any lineage refusal,
        before anything is persisted or sequenced."""
        scope = env.get("visibility_scope")
        actor = env.get("actor") or {}
        if is_peer_actor(actor):
            # a peer is checked like a non-operator key, whatever scope it asked for (the clamp made it `room`):
            # its one room comes from the hot YAML policy, never from key_scopes, and "*" is never honoured
            policy = self._peer_policy(actor)
            if (policy is None or policy.mirror_room is None or scope != "room"
                    or env.get("room_id") != policy.mirror_room):
                raise PermissionError("not permitted to modify this lineage object")
            return
        if scope not in ("room", "mission") or (
                actor.get("kind") == "operator" and actor.get("authenticated_by") not in (None, "none")):
            return
        allowed = self.key_scopes.get(actor.get("id")) or {}
        rooms, missions = allowed.get("rooms") or (), allowed.get("missions") or ()

        def ok(value, allow):
            return "*" in allow or (isinstance(value, str) and bool(value) and value in allow)
        if scope == "room":
            good = ok(env.get("room_id"), rooms)
        else:       # a mission is bound to the room that linked it: both must be addressed and allowed
            good = ok(env.get("room_id"), rooms) and ok(env.get("mission_id"), missions)
        if not good:
            raise PermissionError("not permitted to modify this lineage object")

    def _check_reserved_nodes(self, env: dict, event_type: str, payload: dict) -> None:
        """Only an external agent itself (or an authenticated operator key) may write a node or edge
        source inside that agent's namespace; anyone else is refused exactly like any lineage refusal."""
        actor = env.get("actor") or {}
        if self.service_node is not None and actor.get("kind") != "collab" and not (
                actor.get("kind") == "operator"
                and actor.get("authenticated_by") not in (None, "none", "open_listener")):   # an open listener is no operator here
            ends = (payload.get("from_id"), payload.get("to_id")) if event_type.startswith("edge.") else (payload.get("node_id"),)
            if any(isinstance(e, str) and self.service_node(e) for e in ends):
                raise PermissionError("not permitted to modify this lineage object")
        if self.reserved_node is None or actor.get("kind") == "external" or (
                # T14: the HCN service joins two nodes for a person; the edge is its own instance (writer =
                # the room's service principal) and adds nothing to the endpoint's own namespace
                actor.get("kind") == "collab" and event_type.startswith("edge.")
                and payload.get("edge_type") == "cross_link") or (
                # T16 L1: an open listener is no operator here either (it already is not for service nodes)
                actor.get("kind") == "operator" and actor.get("authenticated_by") not in (None, "none", "open_listener")):
            return
        written = payload.get("from_id") if event_type.startswith("edge.") else payload.get("node_id")
        if isinstance(written, str) and self.reserved_node(written):
            raise PermissionError("not permitted to modify this lineage object")

    def ingest(self, body: dict, principal: dict | None = None, valid=None) -> dict:
        """Apply one ingest body.

        ``valid`` (T10) is an optional zero-argument callable evaluated inside the ledger lock,
        immediately before a lineage envelope is admitted: a falsy result refuses the event exactly
        like any lineage refusal, so a credential revoked while its request was in flight cannot land.

        ``principal`` is the authenticated caller (``{kind, id, authenticated_by}``)
        and becomes the ``actor`` of a schema_version 2 envelope. v1 bodies
        (no/1 ``schema_version``) take the legacy path and ignore it.
        Raises ``EnvelopeConflict`` (HTTP 409) when a v2 envelope contradicts
        stored state.
        """
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        entity_type = body.get("entity_type")
        entity_id = body.get("entity_id")
        event_type = body.get("event_type")
        if not entity_type or not entity_id or not event_type:
            raise ValueError(
                "entity_type, entity_id, and event_type are required")
        if not all(isinstance(v, str) for v in (entity_type, entity_id, event_type)):
            raise ValueError("entity_type, entity_id, and event_type must be strings")
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        source = body.get("source") or "herdr"
        if not isinstance(source, str):
            raise ValueError("invalid source")
        source = source.lower().strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", source):
            raise ValueError("invalid source")

        # The review gate's streams decide merges; a forged verdict/resume/pin would merge unreviewed code.
        # Checked first, and as well as the allow-list so a future allow-list entry can never open a reserved prefix.
        if entity_type.strip().lower().startswith(RESERVED_ENTITY_PREFIXES):
            raise ValueError("reserved review-gate entity type is not accepted by generic ingestion")
        # Architecture snapshots are published through their validating route only.
        if entity_type.strip().lower().startswith("architecture"):
            raise ValueError("reserved architecture entity type is not accepted by generic ingestion")
        # T16 H1/M1/M5: an ALLOW-LIST. Everything else (collab, ci_*, plannotator_review, agent_identity,
        # token_usage, ...) is owned by a dedicated, authenticated adapter and can never be written here.
        if entity_type not in INGEST_ENTITY_TYPES:
            raise ValueError("entity type is not accepted by generic ingestion")
        if event_type in SERVER_OWNED_EVENT_TYPES.get(entity_type, ()):
            raise ValueError("event type is reserved for the server")
        clamp = self._peer_clamp(principal, entity_type)
        env = normalize_envelope(body, principal, self.repo._stamp(), clamp=clamp)
        if clamp is not None:
            # a peer removed or re-keyed in YAML while its request is in flight must not land: re-read the hot
            # policy under the ledger lock, immediately before admission (the T10 `valid` hook)
            outer = valid
            valid = lambda: (outer is None or outer()) and self._peer_policy(principal) is not None  # noqa: E731
        if env is None and entity_type == "lineage":
            raise ValueError("lineage events require schema_version 2")
        if env is not None and entity_type == "lineage":
            self._check_peer_namespace(env, event_type, payload, entity_id)
            self._check_lineage_scope(env)
            self._check_reserved_nodes(env, event_type, payload)
        self._prevalidate(entity_type, entity_id, payload, event_type, source)
        if env is None:
            return self._dispatch(entity_type, entity_id, event_type, payload, source)
        env["entity_type"] = entity_type
        ledger = self.repo.ledger
        ack = {"event_id": env["event_id"], "source": env["source"],
               "source_sequence": env["source_sequence"]}
        head = {"entity_type": entity_type, "entity_id": entity_id, "event_type": event_type}
        # Inspector/mission_activity payloads are redacted per field by their
        # own stores, so the envelope row never carries them.
        row_payload = {} if entity_type in ("inspector", "mission_activity") else payload
        with ledger.lock:
            if ledger.check(env) == "duplicate":
                return {**head, "envelope": {**ack, "duplicate": True}}
            if entity_type == "lineage":
                if valid is not None and not valid():
                    raise PermissionError("not permitted to modify this lineage object")
                # Ownership/capacity gate: runs under the ledger lock and BEFORE
                # the row is persisted, so a refused change is never stored and
                # its sequence is not consumed. Raises PermissionError (403).
                decision = self.repo.graph.admit(env["actor"], event_type, payload, env["source"])
                if decision["mode"] != "normal":
                    env["lineage_mode"] = decision["mode"]
                # Server-assigned monotonic admission number, persisted with the
                # row: ownership and cross-writer ordering never depend on the
                # wall clock, source names or client sequences.
                env["lineage_seq"] = self.repo.graph.next_seq()
            # The envelope row is persisted FIRST. If that fails nothing has
            # been applied and the ledger is untouched, so the emitter retries.
            try:
                self.repo.record_event(
                    entity_type, entity_id, event_type, row_payload,
                    envelope=env, feed=entity_type not in ENVELOPE_ONLY_TYPES,
                    publish=entity_type not in ENVELOPE_ONLY_TYPES)
            except SequenceCollision as exc:
                if exc.event_id_clash:
                    raise EnvelopeConflict(str(exc)) from None
                raise EnvelopeConflict("source_sequence is already used by another event", reason="sequence_conflict",
                                       last_sequence=ledger.last_sequence(env.get("actor"), env["source"])) from None
            except EventAlreadyStored:
                # Durable insert landed earlier but the ack was lost (or the
                # in-memory index evicted it): same identity => already stored.
                if self._stored_identity(env["event_id"], (env.get("actor") or {}).get("id") or "") != (env["source"], env["source_sequence"]):
                    raise EnvelopeConflict("event_id already stored for a different source sequence") from None
                ledger.commit(env)
                return {**head, "envelope": {**ack, "duplicate": True}}
            try:
                if entity_type == "lineage":
                    result = self._apply_lineage(entity_id, event_type, payload, env)
                else:
                    result = self._dispatch(entity_type, entity_id, event_type, payload, source,
                                            record_generic=False)
            finally:
                # The row is durable, so its (source, sequence) is consumed even
                # if the handler then fails; the failure is reported once.
                ledger.commit(env)
        return {**result, "envelope": {**ack, "duplicate": False}}

    def _apply_lineage(self, entity_id, event_type, payload, env) -> dict:
        """Project a persisted lineage envelope into the graph (never infers edges)."""
        self.repo.graph.apply({
            "event_id": env["event_id"], "source": env["source"],
            "source_sequence": env["source_sequence"], "event_type": event_type,
            "payload": payload, "visibility_scope": env["visibility_scope"],
            # the room/mission/workspace context a `room`/`mission` scope is resolved against
            # (the same fields a rebuild reads back from the stored row)
            "room_id": env.get("room_id"), "mission_id": env.get("mission_id"),
            "workspace_id": env.get("workspace_id"),
            "actor": env.get("actor"), "ingest_timestamp": env["received_at"],
            "source_timestamp": env["occurred_at"] or env["received_at"],
            "lineage_seq": env.get("lineage_seq"), "lineage_mode": env.get("lineage_mode")})
        return {"entity_type": "lineage", "entity_id": entity_id, "event_type": event_type}

    def _stored_identity(self, event_id, actor_id=""):
        lookup = getattr(getattr(self.repo, "_sink", None), "stored_identity", None)
        if not callable(lookup):
            raise RuntimeError("durable event identity is unavailable")
        found = lookup(event_id, actor_id)
        if found is None:
            raise RuntimeError("durable event identity is unavailable")
        return tuple(found)

    def _prevalidate(self, entity_type, entity_id, payload, event_type="", source="herdr") -> None:
        """Reject what a handler would reject BEFORE the envelope is persisted.

        Runs for both v1 (legacy) and v2 (enveloped) ingest so a malformed
        report never consumes its event id/sequence: the corrected retry with
        the same idempotency identity can still be applied.
        """
        if entity_type == "mission" and ":" in entity_id:
            raise ValueError("invalid mission id")
        if entity_type == "lineage":
            validate_lineage(event_type, payload)
        if entity_type in ("session", "mission"):
            _git_identity(payload)
        if entity_type == "service" and any(
                k in payload for k in ("repository_id", "forge_url", "repository_url",
                                       "repo_url", "clone_url", "commit_sha")):
            _git_identity(payload)
        if entity_type in ("test_run", "build"):
            self._validate_test_counts(payload)
            self._delivery_mission_key(payload.get("mission_id"), source)
        elif entity_type == "review":
            self._delivery_mission_key(payload.get("mission_id"), source)
        elif entity_type == "review_finding":
            severity = payload.get("severity")
            if not isinstance(severity, str) or not severity:
                raise ValueError("severity is required")
            if not isinstance(payload.get("summary"), str):
                raise ValueError("summary is required")
            # The finding attaches to the review named by ``entity_id``; the
            # review must already exist so a forged/unknown review can never
            # consume the source sequence of a report that will be rejected.
            if self.repo.get_review(entity_id) is None:
                raise KeyError(entity_id)
        elif entity_type == "pull_request":
            self._delivery_mission_key(payload.get("mission_id"), source)

    @staticmethod
    def _validate_test_counts(payload) -> None:
        for key in ("passed", "failed", "skipped", "total"):
            value = payload.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"{key} must be a number")
    def _dispatch(self, entity_type, entity_id, event_type, payload, source,
                  record_generic=True) -> dict:
        # System-graph live-ingest adapter (opt-in): reflect lifecycle events
        # into the running lineage graph as lineage envelopes. Best-effort: a
        # failure here is logged inside the adapter and never breaks the source
        # event's own ingestion.
        if self.system_graph is not None and entity_type in (
                "mission", "session", "repository", "request", "span"):
            # Reflect the lifecycle event into the running graph as lineage
            # envelopes, re-entering the SAME ingest path (admit + persist +
            # apply). Best-effort: failures are logged and swallowed so they
            # never break the source event's own ingestion.
            for env in self.system_graph.emit(entity_type, entity_id, event_type, payload or {}):
                try:
                    self.ingest(env, {"id": self.system_graph.actor,
                                      "kind": "agent",
                                      "authenticated_by": "api_key"})
                except (ValueError, PermissionError) as exc:
                    log.warning("system-graph ingest skipped %s %s (%s): %s",
                                entity_type, entity_id, env["event_type"], exc)
        if entity_type == "mission_activity":
            if event_type != "mission_activity.snapshot":
                raise ValueError("invalid mission activity event_type")
            result = self.repo.mission_activity.ingest(entity_id, payload, source=source)
            return {"entity_type": entity_type, "entity_id": entity_id, **result}
        if entity_type == "host":
            return self._ingest_host(entity_id, event_type, payload, source)
        if entity_type == "session":
            return self._ingest_session(entity_id, event_type, payload,
                                        source)
        if entity_type == "mission":
            if ":" in entity_id:
                raise ValueError("invalid mission id")
            return self._ingest_mission(entity_id, event_type, payload,
                                        source)
        if entity_type == "service":
            return self._ingest_service(entity_id, event_type, payload)
        if entity_type == "inspector":
            # Agent inspector detail (spec 0200). Sanitised/redacted per field
            # by the store; not recorded on the global event feed.
            if "kind" not in payload and "events" not in payload and event_type != "inspector.batch":
                payload = {**payload, "kind": event_type.removeprefix("inspector.")}
            res = self.repo.inspector.ingest(entity_id, payload)
            return {"entity_type": "inspector", "entity_id": entity_id,
                    "event_type": event_type, **res}
        if entity_type in ("test_run", "build"):
            return self._ingest_test_run(entity_id, event_type, payload, source,
                                         kind="test" if entity_type == "test_run" else "build")
        if entity_type == "review":
            return self._ingest_review(entity_id, event_type, payload, source)
        if entity_type == "review_finding":
            return self._ingest_review_finding(entity_id, payload, source)
        if entity_type == "pull_request":
            return self._ingest_pull_request(entity_id, event_type, payload, source)
        # Unknown/other entity types: record the event generically so nothing is
        # silently dropped, and the UI still sees it on the activity stream.
        if record_generic:
            self.repo.record_event(entity_type, entity_id, event_type, payload)
        return {"entity_type": entity_type, "entity_id": entity_id,
                "event_type": event_type}

    # ------------------------------------------------------ delivery entities
    # Test runs, builds, reviews, review findings and pull requests are stored
    # in the repo's delivery collections (issue #32). They mirror the SQL table
    # shapes, are idempotent on entity_id, and attach to a source-namespaced
    # mission key exactly like sessions and missions do (issue #26), so a pi
    # report for raw mission_id ``M1`` lands on ``pi:M1`` and stays visible on
    # the pi mission page instead of a bare ``M1`` the UI never queries.
    def _delivery_mission_key(self, mission_id, source):
        if mission_id is None:
            return None
        if not isinstance(mission_id, str) or not mission_id:
            raise ValueError("mission_id must be a non-empty string")
        if ":" in mission_id:
            namespace, raw = mission_id.split(":", 1)
            if not raw or ":" in raw or (source != "herdr" and namespace != source):
                raise ValueError("mission identity conflicts with its source")
            return mission_id
        return mission_id if source == "herdr" else f"{source}:{mission_id}"

    def _ingest_test_run(self, entity_id, event_type, payload, source, kind="test") -> dict:
        self._validate_test_counts(payload)
        return self.repo.upsert_test_run(
            entity_id, kind=kind,
            mission_id=self._delivery_mission_key(payload.get("mission_id"), source),
            session_id=payload.get("session_id"),
            repository=payload.get("repository"),
            status=payload.get("status"),
            passed=payload.get("passed"),
            failed=payload.get("failed"),
            skipped=payload.get("skipped"),
            total=payload.get("total"),
            started_at=payload.get("started_at"),
            ended_at=payload.get("ended_at"),
            results=payload.get("results"),
        )

    def _ingest_review(self, entity_id, event_type, payload, source) -> dict:
        return self.repo.upsert_review(
            entity_id, mission_id=self._delivery_mission_key(payload.get("mission_id"), source),
            session_id=payload.get("session_id"),
            reviewer=payload.get("reviewer"),
            status=payload.get("status"),
            verdict=payload.get("verdict"),
            started_at=payload.get("started_at"),
            completed_at=payload.get("completed_at"),
        )

    def _ingest_review_finding(self, entity_id, payload, source) -> dict:
        # The finding attaches to the review named by ``review_id`` (which the
        # ingest envelope carries in ``entity_id``); the review must already
        # exist so a forged/unknown review can never collect findings.
        severity = payload.get("severity")
        summary = payload.get("summary")
        # A client-supplied finding_id is honoured verbatim; when omitted we derive
        # a stable id from the finding content so a replayed envelope updates the
        # original finding in place instead of appending a duplicate (idempotency
        # of the ingest path, independent of the review's current finding count).
        finding_id = payload.get("finding_id")
        if not finding_id:
            content = "|\n".join([severity, summary, payload.get("detail") or "",
                                  payload.get("status") or "open"])
            finding_id = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return self.repo.add_review_finding(
            entity_id, severity=severity,
            summary=summary, detail=payload.get("detail") or "",
            status=payload.get("status") or "open",
            finding_id=finding_id,
        )

    def _ingest_pull_request(self, entity_id, event_type, payload, source) -> dict:
        return self.repo.upsert_pull_request(
            entity_id, mission_id=self._delivery_mission_key(payload.get("mission_id"), source),
            repository=payload.get("repository"),
            number=payload.get("number"),
            url=payload.get("url"),
            status=payload.get("status"),
            branch=payload.get("branch"),
            base=payload.get("base"),
            created_at=payload.get("created_at"),
        )

    def _ingest_host(self, entity_id, event_type, payload, source="herdr") -> dict:
        if source not in LIVENESS_SOURCES:
            return self._ingest_host_state(entity_id, payload)
        status = ("unavailable" if "unavailable" in event_type.lower()
                  else "healthy")
        # Validate everything before touching the host: a bad sample is a 400
        # and must leave the stored record as it was.
        telemetry = payload.get("telemetry")
        sample = clean_metrics(telemetry) if telemetry is not None else None
        state = {k: clean_state(k, payload.get(k)) for k in STATE_KEYS}
        legacy = {k: clean_metrics({k: payload.get(k)}).get(k) for k in ("cpu", "mem", "disk")}
        cpu = sample.get("cpu") if sample is not None else legacy["cpu"]
        mem = sample.get("mem") if sample is not None else legacy["mem"]
        name = payload.get("host_name") or payload.get("name")
        ip = payload.get("tailnet_ip") or payload.get("ip")
        inventory = payload.get("inventory")
        # A routine heartbeat (known healthy host, same identity, no inventory)
        # only refreshes liveness in memory: a durable HostHeartbeat per 30 s
        # sample would grow the event log and the Activity feed without bound.
        # Registration, identity changes, inventory and status transitions
        # still go through upsert_host and are recorded as events.
        existing = self.repo.get_host_record(entity_id)
        if (existing is not None and status == "healthy"
                and existing.get("status") == "healthy"
                and (not name or name == existing.get("host_name"))
                and (not ip or ip == existing.get("tailnet_ip"))
                and not inventory):
            self.repo.record_heartbeat(entity_id, cpu=cpu, mem=mem)
            host = self.repo.get_host_record(entity_id) or existing
        else:
            host = self.repo.upsert_host(
                entity_id, name or entity_id, ip,
                cpu=cpu, mem=mem, status=status, inventory=inventory)
        if sample is not None or any(v is not None for v in state.values()):
            self.repo.record_host_metrics(entity_id, sample, **state)
            return host
        legacy = {k: v for k, v in legacy.items() if v is not None}
        if legacy:
            self.repo.record_telemetry(entity_id, {
                "cpu_pct": legacy.get("cpu"), "mem_pct": legacy.get("mem"),
                "disk_pct": legacy.get("disk")})
        return host

    def _ingest_host_state(self, entity_id, payload) -> dict:
        """A host event from a source that is not the host's own agent (e.g. the
        Ansible fleet update on the main host): it may only set host STATE keys.
        It never touches liveness (status, last_seen), identity (host_name, ip)
        or metrics, and never creates a host the agent has not registered."""
        bad = sorted(k for k in _AGENT_ONLY_HOST_KEYS if k in payload)
        if bad:
            raise ValueError("host metrics/inventory are accepted from the host agent only: "
                             + ", ".join(bad))
        state = {k: clean_state(k, payload.get(k)) for k in STATE_KEYS}
        self.repo.record_host_metrics(entity_id, None, **state)
        return self.repo.get_host(entity_id) or {"entity_id": entity_id,
                                                  "ignored": "unknown host"}

    def _ingest_session(self, entity_id, event_type, payload,
                        source="herdr") -> dict:
        if "stop" in event_type.lower():
            status = "stopped"
        elif "start" in event_type.lower():
            status = "active"
        else:
            status = payload.get("status", "active")
        return self.repo.upsert_session(
            entity_id, host_id=payload.get("host_id"),
            mission_id=payload.get("mission_id"),
            agent_role=payload.get("agent_role") or payload.get("role") or "",
            model=payload.get("model") or "", status=status,
            cwd=payload.get("cwd"), branch=payload.get("branch"),
            pid=payload.get("pid"), cmdline=payload.get("cmdline"),
            summary=payload.get("summary"), attach=payload.get("attach"),
            source=source, **_git_identity(payload),
            **{k: payload.get(k) for k in (
                "pane_id", "terminal_id", "herdr_session", "machine", "agent_session",
                "revision", "interactive_ready", "launch_pending", "state_change_seq",
                "transcript", "parent_session_id", "name", "runtime")})

    def _ingest_mission(self, entity_id, event_type, payload,
                        source="herdr") -> dict:
        if "stop" in event_type.lower():
            stage = "complete"
        elif "start" in event_type.lower():
            stage = "active"
        else:
            stage = payload.get("stage", "active")
        return self.repo.upsert_mission(
            entity_id, title=payload.get("title") or "",
            purpose=payload.get("purpose") or "", stage=stage,
            cwd=payload.get("cwd"), source=source, **_git_identity(payload))

    def _ingest_service(self, entity_id, event_type, payload) -> dict:
        """A ``service`` envelope maps idempotently to a (host, name) service.

        ``payload['service_name']`` (or ``entity_id`` as a fallback) is the
        display name; ``host_id``/``port``/``route``/``status`` come from the
        payload. Repeated heartbeats for the same (host, name) update in place
        rather than duplicating. A human ``description`` and ``owner`` (the
        process) ride along when present; the repo always ensures a
        description so every service is self-explanatory.
        """
        metadata = {key: payload[key] for key in ("ownership", "session_id") if key in payload}
        identity_fields = ("repository_id", "forge_url", "repository_url", "repo_url", "clone_url")
        if any(key in payload for key in (*identity_fields, "commit_sha")):
            git = _git_identity(payload)
            if any(key in payload for key in identity_fields):
                metadata["repository_id"] = git["repository_id"]
            if "commit_sha" in payload:
                metadata["commit_sha"] = git["commit_sha"]
        if event_type in {"service.metadata", "service.listener_observed"}:
            observer = self.repo.observe_service_metadata if event_type == "service.metadata" else self.repo.observe_service_listener
            row = observer(entity_id, {**payload, **metadata})
            return {"entity_type": "service", "entity_id": entity_id, "event_type": event_type,
                    "metadata_observation": row.get("metadata_observation")}
        status = ("unavailable" if "unavailable" in event_type.lower()
                  else payload.get("status", "healthy"))
        svcid = self.repo.upsert_service(
            payload.get("host_id") or "",
            payload.get("service_name") or entity_id,
            int(payload["port"]) if payload.get("port") is not None else 0,
            status=status,
            route=payload.get("route"),
            description=payload.get("description"),
            owner=payload.get("owner"),
            project=payload.get("project"),
            source_port=payload.get("source_port"),
            **metadata,
        )
        return {"entity_type": "service", "entity_id": svcid,
                "event_type": event_type}
