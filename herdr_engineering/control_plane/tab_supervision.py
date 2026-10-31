"""Durable tab/job supervision policy; no launcher, task store, or runtime control.

Actors MUST come from the authenticated operator, never model-supplied flags.
One authoritative serialized writer must hydrate this stream before commands.
Current grants and the central rolling user token limiter remain mandatory.
"""
from __future__ import annotations

import math
import threading
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4

TERMINAL = frozenset({"verified", "cancelled"})
ENTITY = "tab_supervision"


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("expected nonempty bounded identity/evidence reference")
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("expected finite number")
    return value


@dataclass(frozen=True)
class Actor:
    owner_id: str
    agent_id: str | None
    scopes: frozenset[str]
    models: frozenset[str]


@dataclass(frozen=True)
class Policy:
    scopes: frozenset[str]
    models: frozenset[str]
    token_limit: int
    deadline: float

    def wire(self):
        for values in (self.scopes, self.models):
            if not isinstance(values, frozenset) or not values:
                raise ValueError("nonempty immutable scopes/models required")
            for value in values:
                _text(value)
        if type(self.token_limit) is not int or self.token_limit <= 0:
            raise ValueError("positive integer token ceiling required")
        _number(self.deadline)
        return {"scopes": sorted(self.scopes), "models": sorted(self.models),
                "token_limit": self.token_limit, "deadline": self.deadline}


@dataclass(frozen=True)
class Binding:
    server_id: str
    session_id: str
    runtime_instance_id: str
    adapter: str
    tab_id: str
    pane_id: str | None = None

    def wire(self):
        for value in (self.server_id, self.session_id, self.runtime_instance_id, self.adapter, self.tab_id):
            _text(value)
        if self.pane_id is not None:
            _text(self.pane_id)
        return asdict(self)


class Supervision:
    """Policy projection over existing required event sink.

    Token ceilings cover the entire subtree, not separate sibling allowances.
    Usage receipt IDs must come from the trusted broker, not from the model.
    ``hydrate`` accepts ONLY the complete authoritative stream, never snapshots
    imported by an agent. The owner authenticates grants on every command.
    """

    def __init__(self, sink, *, now=None):
        if not callable(getattr(sink, "record_required", None)):
            raise ValueError("required durable event sink missing")
        self._sink = sink
        self._now = now or time.time
        self._lock = threading.RLock()
        self._agents = {}
        self._receipts = {}
        self._binding_owners = {}
        self._seq = 0

    def agents(self):
        with self._lock:
            return deepcopy(list(self._agents.values()))

    def view(self, agent_id):
        with self._lock:
            return deepcopy(self._get(agent_id))

    def _get(self, agent_id):
        if agent_id not in self._agents:
            raise ValueError("unknown agent")
        return self._agents[agent_id]

    def _chain(self, agent_id):
        chain, seen = [], set()
        while agent_id is not None:
            if agent_id in seen:
                raise ValueError("supervisor cycle")
            seen.add(agent_id)
            node = self._get(agent_id)
            chain.append(node)
            agent_id = node["supervisor_id"]
        return chain

    def _authorized(self, actor, target, *, live=True):
        _text(actor.owner_id)
        node = self._get(target)
        if node["owner_id"] != actor.owner_id:
            raise ValueError("owner mismatch")
        chain = self._chain(target)
        if actor.agent_id is not None:
            if actor.agent_id not in {n["agent_id"] for n in chain}:
                raise ValueError("agent outside owned subtree")
            # Operator resolves inherited/revoked grants before constructing Actor.
            if (not set(node["policy"]["scopes"]) <= actor.scopes
                    or not set(node["policy"]["models"]) <= actor.models):
                raise ValueError("current grants do not cover target policy")
        if live and any(n["status"] in TERMINAL | {"stop_requested"} for n in chain):
            raise ValueError("ancestor is stopped or terminal")
        if live and self._now() >= min(n["policy"]["deadline"] for n in chain):
            raise ValueError("deadline elapsed")
        return node

    def _time(self):
        # One canonical microsecond timestamp survives JSON/ISO replay exactly.
        return datetime.fromtimestamp(_number(self._now()), UTC).timestamp()

    def _new_agent(self, actor, job_id, tab_id, agent_id, task_id, policy, parent):
        when = self._time()
        for value in (actor.owner_id, job_id, tab_id, agent_id, task_id):
            _text(value)
        if agent_id in self._agents:
            raise ValueError("agent identity already reserved")
        p = policy.wire()
        if not set(p["scopes"]) <= actor.scopes or not set(p["models"]) <= actor.models:
            raise ValueError("requested policy exceeds authenticated grants")
        if p["deadline"] <= when:
            raise ValueError("deadline elapsed")
        if parent:
            q = parent["policy"]
            if (not set(p["scopes"]) <= set(q["scopes"])
                    or not set(p["models"]) <= set(q["models"])
                    or p["token_limit"] > q["token_limit"]
                    or p["deadline"] > q["deadline"]):
                raise ValueError("requested policy exceeds supervisor policy")
        return {"owner_id": actor.owner_id, "job_id": job_id, "tab_id": tab_id,
                "agent_id": agent_id, "supervisor_id": parent["agent_id"] if parent else None,
                "task_id": task_id, "policy": p, "binding": None, "status": "queued",
                "used_tokens": 0, "progress": 0, "heartbeat_at": when,
                "progress_at": when, "evidence_ref": None,
                "room_id": parent["room_id"] if parent else None,
                "workspace_id": parent["workspace_id"] if parent else None}

    def create_job(self, actor, job_id, tab_id, agent_id, task_id, policy):
        with self._lock:
            if actor.agent_id is not None:
                raise ValueError("only authenticated human creates root jobs")
            if any(n["job_id"] == job_id or n["tab_id"] == tab_id for n in self._agents.values()):
                raise ValueError("job/tab identity already reserved")
            node = self._new_agent(actor, job_id, tab_id, agent_id, task_id, policy, None)
            self._commit(actor, "created", {"agent": node}, at=node["heartbeat_at"])

    def delegate(self, actor, child_id, task_id, policy):
        with self._lock:
            if actor.agent_id is None:
                raise ValueError("delegation requires authenticated supervisor")
            parent = self._authorized(actor, actor.agent_id)
            node = self._new_agent(actor, parent["job_id"], parent["tab_id"],
                                   child_id, task_id, policy, parent)
            self._commit(actor, "delegated", {"agent": node}, at=node["heartbeat_at"])

    def link_job(self, actor, job_id, room_id, workspace_id):
        """Associate an existing job with its collaboration room/workspace.

        The trusted operator constructs ``actor`` after authenticating the human.
        Links are immutable in this checkpoint; adoption or migration requires a
        separate explicit contract rather than layout inference.
        """
        with self._lock:
            if actor.agent_id is not None:
                raise ValueError("only authenticated human links jobs")
            for value in (actor.owner_id, job_id, room_id, workspace_id):
                _text(value)
            rows = [node for node in self._agents.values() if node["job_id"] == job_id]
            if not rows:
                raise ValueError("unknown job")
            root = next((node for node in rows if node["supervisor_id"] is None), None)
            if root is None or root["owner_id"] != actor.owner_id:
                raise ValueError("owner mismatch")
            current = (root["room_id"], root["workspace_id"])
            requested = (room_id, workspace_id)
            if current == requested:
                return
            if current != (None, None):
                raise ValueError("job collaboration link is immutable")
            for node in self._agents.values():
                if node["job_id"] == job_id or node.get("room_id") is None:
                    continue
                if ((node["room_id"] == room_id) != (node["workspace_id"] == workspace_id)):
                    raise ValueError("one collaboration room maps to one workspace")
            self._commit(actor, "linked", {"job_id": job_id, "room_id": room_id,
                                            "workspace_id": workspace_id})

    def job_links(self):
        """Return detached authoritative room/workspace links for integration validation."""
        with self._lock:
            roots = [node for node in self._agents.values()
                     if node["supervisor_id"] is None and node.get("room_id")]
            return deepcopy([{
                "job_id": root["job_id"], "room_id": root["room_id"],
                "workspace_id": root["workspace_id"], "root_agent_id": root["agent_id"],
                "owner_id": root["owner_id"],
                "agent_ids": sorted(node["agent_id"] for node in self._agents.values()
                                    if node["job_id"] == root["job_id"]),
            } for root in sorted(roots, key=lambda node: node["job_id"])])

    def job_agent_ids(self, job_id):
        """Return detached stable identities for pre-link collaboration validation."""
        _text(job_id)
        with self._lock:
            rows = sorted(node["agent_id"] for node in self._agents.values()
                          if node["job_id"] == job_id)
            if not rows:
                raise ValueError("unknown job")
            return rows

    def bind(self, actor, agent_id, binding):
        with self._lock:
            node = self._authorized(actor, agent_id)
            b = binding.wire()
            if b["tab_id"] != node["tab_id"]:
                raise ValueError("binding tab differs from durable job tab")
            if node["binding"] == b:
                return  # Reconnect receipts must not manufacture progress.
            if any(self._binding_owners.get(key, agent_id) != agent_id
                   for key in self._binding_keys(b)):
                raise ValueError("runtime or pane identity already reserved")
            when = self._time()
            self._commit(actor, "bound", {"agent_id": agent_id, "binding": b, "at": when}, at=when)

    @staticmethod
    def _binding_keys(binding):
        keys = [("runtime", binding["server_id"], binding["session_id"],
                 binding["runtime_instance_id"])]
        if binding["pane_id"] is not None:
            keys.append(("pane", binding["server_id"], binding["pane_id"]))
        return keys

    def checkpoint(self, actor, progress, evidence_ref):
        with self._lock:
            node = self._authorized(actor, actor.agent_id)
            when = self._time()
            if (type(progress) is not int or progress < node["progress"]
                    or when < node["heartbeat_at"] or when >= node["policy"]["deadline"]):
                raise ValueError("invalid checkpoint ordering")
            self._commit(actor, "checkpoint", {"agent_id": actor.agent_id, "progress": progress,
                                        "evidence_ref": _text(evidence_ref), "at": when}, at=when)

    def charge(self, actor, tokens, receipt_id):
        with self._lock:
            # Usage may arrive after stop: preserve billing without enabling work.
            node = self._authorized(actor, actor.agent_id, live=False)
            _text(receipt_id)
            if type(tokens) is not int or tokens <= 0:
                raise ValueError("positive integer token usage required")
            key = (node["job_id"], receipt_id)
            value = (actor.agent_id, tokens)
            if key in self._receipts:
                if self._receipts[key] != value:
                    raise ValueError("receipt already used for different usage")
                return
            if any(n["used_tokens"] + tokens > n["policy"]["token_limit"]
                   for n in self._chain(actor.agent_id)):
                raise ValueError("subtree token ceiling exceeded")
            self._commit(actor, "charged", {"agent_id": actor.agent_id, "tokens": tokens,
                                     "receipt_id": receipt_id})

    def health(self, agent_id, live_bindings, *, now=None, stale_after=60):
        with self._lock:
            n = self._get(agent_id)
            when = self._now() if now is None else _number(now)
            if _number(stale_after) <= 0:
                raise ValueError("positive stale threshold required")
            if n["status"] in TERMINAL | {"stop_requested"}:
                return n["status"]
            if when >= n["policy"]["deadline"]:
                return "deadline_exceeded"
            if any(p["used_tokens"] >= p["policy"]["token_limit"]
                   for p in self._chain(agent_id)):
                return "budget_exhausted"
            if n["binding"] is None:
                return "queued"
            if n["binding"] not in [b.wire() for b in live_bindings]:
                return "orphaned"
            if when - n["heartbeat_at"] >= stale_after:
                return "stalled"
            if when - n["progress_at"] >= stale_after:
                return "drifting"
            return "healthy"

    def cancel(self, actor, agent_id, reason):
        with self._lock:
            self._authorized(actor, agent_id, live=False)
            targets = [n["agent_id"] for n in self._agents.values()
                       if n["status"] not in TERMINAL and agent_id in
                       {p["agent_id"] for p in self._chain(n["agent_id"])}]
            if targets:
                self._commit(actor, "stopping", {"targets": targets, "reason": _text(reason)})

    def conclude(self, actor, agent_id, outcome, evidence_ref):
        with self._lock:
            node = self._authorized(actor, agent_id, live=False)
            if actor.agent_id is not None and actor.agent_id != node["supervisor_id"]:
                raise ValueError("only direct supervisor or human validates results")
            if outcome not in TERMINAL or node["status"] in TERMINAL:
                raise ValueError("invalid outcome transition")
            if outcome == "cancelled" and node["status"] != "stop_requested":
                raise ValueError("cleanup requires prior stop request")
            if outcome == "verified" and node["status"] != "running":
                raise ValueError("verification requires an established runtime binding")
            if outcome == "verified" and any(n["status"] == "stop_requested"
                                             for n in self._chain(agent_id)):
                raise ValueError("stopped task cannot report success")
            if any(n["supervisor_id"] == agent_id and n["status"] not in TERMINAL
                   for n in self._agents.values()):
                raise ValueError("children require verified integration or cleanup first")
            self._commit(actor, "concluded", {"agent_id": agent_id, "outcome": outcome,
                                       "evidence_ref": _text(evidence_ref)})

    def _commit(self, actor, kind, payload, *, at=None):
        when = datetime.fromtimestamp(self._time() if at is None else at, UTC).isoformat()
        event = {"event_id": "ev_" + uuid4().hex, "schema_version": 1,
                 "source_timestamp": when, "ingest_timestamp": when,
                 "entity_type": ENTITY, "entity_id": "supervision",
                 "sequence": self._seq + 1, "event_type": kind,
                 "payload": {**deepcopy(payload), "actor": {
                     "owner_id": actor.owner_id, "agent_id": actor.agent_id,
                     "scopes": sorted(actor.scopes), "models": sorted(actor.models)}}}
        self._sink.record_required(deepcopy(event))
        self._apply(event)

    def _apply(self, event):
        p, kind = event["payload"], event["event_type"]
        if kind in {"created", "delegated"}:
            n = deepcopy(p["agent"])
            self._agents[n["agent_id"]] = n
        elif kind == "linked":
            for node in self._agents.values():
                if node["job_id"] == p["job_id"]:
                    node.update(room_id=p["room_id"], workspace_id=p["workspace_id"])
        elif kind == "stopping":
            for aid in p["targets"]:
                self._get(aid).update(status="stop_requested", stop_reason=p["reason"])
        else:
            n = self._get(p["agent_id"])
            if kind == "bound":
                for key in self._binding_keys(p["binding"]):
                    self._binding_owners[key] = n["agent_id"]
                n.update(binding=deepcopy(p["binding"]), status="running",
                         heartbeat_at=p["at"], progress_at=p["at"])
            elif kind == "checkpoint":
                if p["progress"] > n["progress"]:
                    n["progress_at"] = p["at"]
                n.update(progress=p["progress"], heartbeat_at=p["at"], evidence_ref=p["evidence_ref"])
            elif kind == "charged":
                self._receipts[(n["job_id"], p["receipt_id"])] = (n["agent_id"], p["tokens"])
                for ancestor in self._chain(n["agent_id"]):
                    ancestor["used_tokens"] += p["tokens"]
            elif kind == "concluded":
                n.update(status=p["outcome"], evidence_ref=p["evidence_ref"])
            else:
                raise ValueError("unknown supervision event")
        self._seq = event["sequence"]

    def hydrate(self, events):
        """Validate by re-executing historical commands in an isolated projection.

        Atomic on failure. Historical actors are reconstructed only from the
        authoritative policy history, never used as current live credentials.
        """
        with self._lock:
            if self._seq:
                raise ValueError("hydrate requires empty projection")
            candidate = Supervision(_ReplaySink(), now=self._now)
            seen = set()
            for event in events:
                event_id = _text(event.get("event_id"))
                if event_id in seen:
                    raise ValueError("duplicate supervision event identity")
                seen.add(event_id)
                if (type(event.get("schema_version")) is not int
                        or type(event.get("sequence")) is not int
                        or event.get("schema_version") != 1 or event.get("entity_type") != ENTITY
                        or event.get("entity_id") != "supervision"
                        or event.get("sequence") != candidate._seq + 1):
                    raise ValueError("invalid supervision history version/sequence/identity")
                try:
                    candidate._replay(deepcopy(event))
                except (KeyError, TypeError, OverflowError) as exc:
                    raise ValueError("malformed supervision history") from exc
            self._agents, self._receipts, self._binding_owners, self._seq = (
                candidate._agents, candidate._receipts, candidate._binding_owners, candidate._seq)

    def _replay(self, event):
        p, kind = event["payload"], event["event_type"]
        audit = p["actor"]
        actor = Actor(audit["owner_id"], audit["agent_id"],
                      frozenset(audit["scopes"]), frozenset(audit["models"]))
        timestamp = datetime.fromisoformat(event["source_timestamp"]).timestamp()
        self._now = lambda: timestamp
        if kind in {"created", "delegated"}:
            n = p["agent"]
            # Version 1 histories written before room/workspace linking did not
            # carry these additive fields. Their absence means explicitly unlinked.
            n.setdefault("room_id", None)
            n.setdefault("workspace_id", None)
            q = n["policy"]
            policy = Policy(frozenset(q["scopes"]), frozenset(q["models"]),
                            q["token_limit"], q["deadline"])
            if kind == "created":
                if n["supervisor_id"] is not None:
                    raise ValueError("root has supervisor")
                self.create_job(actor, n["job_id"], n["tab_id"], n["agent_id"], n["task_id"], policy)
            else:
                if actor.agent_id != n["supervisor_id"]:
                    raise ValueError("recorded supervisor differs from authenticated caller")
                self.delegate(actor, n["agent_id"], n["task_id"], policy)
            if self._get(n["agent_id"]) != n:
                raise ValueError("creation history mismatch")
        elif kind == "linked":
            self.link_job(actor, p["job_id"], p["room_id"], p["workspace_id"])
        elif kind == "stopping":
            if not p["targets"]:
                raise ValueError("empty stop history")
            node = self._get(p["targets"][0])
            self.cancel(actor, node["agent_id"], p["reason"])
            if self._sink.last["payload"] != p:
                raise ValueError("incomplete subtree stop history")
        else:
            aid = p["agent_id"]
            if kind == "bound":
                self.bind(actor, aid, Binding(**p["binding"]))
            elif kind == "checkpoint":
                self.checkpoint(actor, p["progress"], p["evidence_ref"])
            elif kind == "charged":
                self.charge(actor, p["tokens"], p["receipt_id"])
            elif kind == "concluded":
                self.conclude(actor, aid, p["outcome"], p["evidence_ref"])
            else:
                raise ValueError("unknown supervision event")
        if self._sink.last["payload"] != p:
            raise ValueError("event payload does not match validated command")
        if self._seq != event["sequence"]:
            raise ValueError("history did not produce one mutation")



class _ReplaySink:
    def record_required(self, event):
        self.last = event
