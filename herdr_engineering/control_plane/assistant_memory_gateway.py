"""Thin async native memory boundary over the PG journal and private index.

Factories receive copied trusted domain config, never request scope or tags.
store_factory(domain, actor) is an async context manager; index_factory(domain)
returns a private synchronous AssistantHindsight. evidence_checker(domain, actor,
item) is an optional async trusted predicate, not a human-approval service.

Native subset: bank GET/PUT, POST memories and memories/recall, GET/DELETE
 documents/{id}, GET documents/{id}/history, GET status/stats and operations/{id}.
GET documents and memories/list accept only limit (1..100) and offset (0..100000)
query parameters. Listings contain canonical identity/metadata and <=1024-byte
text excerpts; single-document reads return full text. Requires store methods
list_records(capture_kind, limit, offset), status(capture_kind), and exact
is_pending(memory_id, revision). No truncated pending scan or backend proxy.

Knowledge pages are permanently disabled in this v1 boundary: creation/config
routes cannot enable them. GET knowledge-base/tree and POST knowledge-base/search
return empty nodes/results without backend I/O, solely for disabled SDK profiles.
This is not a complete Hindsight API. Parent owns periodic drain.

Provenance references are caller-supplied, never identity or approval authority.
Only source_ref/session_id/turn_id/host_id and bounded evidence reference strings
are preserved. Exact portable derived/unreviewed/extractive summary envelopes may
supply these references too; original content stays unchanged. Other JSON text
is opaque. Credential actor/harness and chosen document ID always win.

The optional operator_domains constructor whitelist defaults to empty. The
synchronous authorize_operator(principal) is only a trusted ControlPlaneServer
adapter after its existing private operator gate, never a request-body/native
API. async operator_domains(actor) returns safe membership and canonical counts.
Operator actors retain the actual principal ID; no human-verification claim is
inferred. All methods belong on the same gateway runtime as native operations.

One gateway serializes writes/delivery; all writers for these banks must use
that instance (or parent-provided cross-process serialization). Canonical read
filtering remains necessary even when backend state is stale. No background
threads/workers are started except bounded awaited asyncio.to_thread calls.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import hmac
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit

from .assistant_memory_store import RevisionConflict


class GatewayError(Exception):
    """Sanitized boundary error, safe for an HTTP adapter to serialize."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Actor:
    id: str
    harness: str
    domains: tuple[str, ...]
    read_only: bool


class MemoryGateway:
    def __init__(
        self,
        domains,
        credentials,
        store_factory,
        index_factory,
        *,
        evidence_checker=None,
        pending_checker=None,
        lock_timeout=5,
        operator_domains=(),
    ):
        self._domains = copy.deepcopy(domains)
        banks = set()
        identities = set()
        for alias, domain in self._domains.items():
            if (
                not isinstance(alias, str)
                or not alias
                or domain.get("domain_type") not in ("project", "personal")
                or any(
                    not isinstance(domain.get(k), str) or not domain[k]
                    for k in ("domain_id", "tenant_id")
                )
                or set(domain.get("bank_ids", {})) != {"fact", "summary"}
            ):
                raise ValueError("invalid trusted domain")
            identity = (domain["tenant_id"], domain["domain_type"], domain["domain_id"])
            if identity in identities:
                raise ValueError("duplicate canonical domain identity")
            identities.add(identity)
            for bank in domain["bank_ids"].values():
                if (
                    not isinstance(bank, str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", bank)
                    or bank in banks
                ):
                    raise ValueError("duplicate or invalid bank")
                banks.add(bank)
        if (
            not isinstance(operator_domains, (tuple, list))
            or any(
                not isinstance(alias, str) or alias not in self._domains
                for alias in operator_domains
            )
            or len(set(operator_domains)) != len(operator_domains)
        ):
            raise ValueError("invalid operator domain allowlist")
        self._operator_aliases = tuple(operator_domains)
        self._operator_actors = OrderedDict()
        self._credentials = []
        for token, config in credentials.items():
            if (
                not isinstance(token, str)
                or not token
                or len(token.encode()) > 4096
                or any(c.isspace() for c in token)
                or not isinstance(config.get("harness"), str)
                or not config["harness"]
                or not isinstance(config.get("domains"), (list, tuple))
                or any(a not in self._domains for a in config["domains"])
                or type(config.get("read_only")) is not bool
            ):
                raise ValueError("invalid credential configuration")
            digest = hashlib.sha256(token.encode()).digest()
            actor = Actor(
                "key:" + digest.hex()[:12],
                config["harness"],
                tuple(config["domains"]),
                config["read_only"],
            )
            self._credentials.append((digest, actor))
        if not 0 < lock_timeout <= 60:
            raise ValueError("invalid lock timeout")
        self._store_factory = store_factory
        self._index_factory = index_factory
        self._evidence_checker = evidence_checker
        self._pending_checker = pending_checker
        self._lock = asyncio.Lock()
        self._lock_timeout = lock_timeout

    @property
    def domains(self):
        return copy.deepcopy(self._domains)

    def authenticate(self, authorization):
        if (
            not isinstance(authorization, str)
            or len(authorization) > 4103
            or not authorization.startswith("Bearer ")
        ):
            raise GatewayError(401, "invalid bearer credential")
        token = authorization[7:]
        digest = hashlib.sha256(token.encode()).digest()
        actor = None
        # Compare fixed-length digests against every configured key.
        for expected, candidate in self._credentials:
            if hmac.compare_digest(digest, expected):
                actor = candidate
        if actor is None:
            raise GatewayError(401, "invalid bearer credential")
        return actor

    def authorize_operator(self, principal):
        """Synchronously issue an actor from a trusted server principal, with no I/O.

        Caller contract: invoke on the gateway runtime after the private existing
        ControlPlaneServer `_require_operator` gate, passing only `_principal()`.
        This dict is a trusted in-process assertion, not cryptographic proof.
        NEVER connect request bodies/metadata to this method. It has no native
        route, creates no auth service, and does not authorize public listeners.
        Reauthorize each HTTP request. The 64-entry LRU cache reuses actual IDs;
        eviction revokes old actor references, even after an ID is issued again.
        """
        if (
            type(principal) is not dict
            or principal.get("kind") != "operator"
            or principal.get("authenticated_by") not in ("api_key", "open_listener")
        ):
            raise GatewayError(403, "operator principal required")
        principal_id = principal.get("id")
        if (
            not isinstance(principal_id, str)
            or not principal_id.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in principal_id)
        ):
            raise GatewayError(403, "invalid operator principal")
        try:
            if len(principal_id.encode()) > 512:
                raise ValueError()
        except (UnicodeError, ValueError):
            raise GatewayError(403, "invalid operator principal") from None
        actor = self._operator_actors.get(principal_id)
        if actor is None:
            actor = Actor(principal_id, "operator-ui", self._operator_aliases, False)
            self._operator_actors[principal_id] = actor
        self._operator_actors.move_to_end(principal_id)
        while len(self._operator_actors) > 64:
            self._operator_actors.popitem(last=False)
        return actor

    def _actor(self, actor):
        if any(actor is configured for _, configured in self._credentials):
            return
        if (
            isinstance(actor, Actor)
            and isinstance(actor.id, str)
            and self._operator_actors.get(actor.id) is actor
        ):
            return
        raise GatewayError(401, "invalid actor")

    async def operator_domains(self, actor):
        """List only issued operator membership, safe config fields, and PG counts.

        No backend credentials, URLs, tenant IDs, or arbitrary registry fields
        are returned. Default operator membership is empty. Bearer actors cannot
        call this operator-only helper; native bank authorization is unchanged.
        """
        self._actor(actor)
        if self._operator_actors.get(actor.id) is not actor:
            raise GatewayError(403, "operator actor required")
        if not actor.domains:
            return []
        await self._acquire()
        try:
            result = []
            for alias in actor.domains:
                domain = self._domains[alias]
                async with self._store_factory(copy.deepcopy(domain), actor) as store:
                    status = await store.status()
                result.append(
                    {
                        "alias": alias,
                        "domain_type": domain["domain_type"],
                        "domain_id": domain["domain_id"],
                        "bank_ids": dict(domain["bank_ids"]),
                        "counts": {key: status[key] for key in ("active", "deleted", "pending")},
                    }
                )
            return result
        except GatewayError:
            raise
        except Exception:
            raise GatewayError(503, "memory journal unavailable") from None
        finally:
            self._lock.release()

    async def _thread(self, function, *args):
        # Cancellation must not release serialization while a thread still writes.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():  # Event-loop shutdown cancelled the child itself.
                    raise
                cancelled = True
                # A second cancellation must not cancel the underlying thread task.
                continue
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            raise asyncio.CancelledError()
        return result

    @staticmethod
    def _references(metadata):
        result = {}
        for key in ("source_ref", "session_id", "turn_id", "host_id"):
            if key in metadata:
                value = metadata[key]
                if not isinstance(value, str) or len(value.encode()) > 2048:
                    raise GatewayError(400, "invalid provenance reference")
                result[key] = value
        if "evidence" in metadata:
            evidence = metadata["evidence"]
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except (ValueError, RecursionError):
                    raise GatewayError(400, "invalid evidence references") from None
            if (
                not isinstance(evidence, list)
                or len(evidence) > 32
                or any(
                    not isinstance(ref, str) or not ref.strip() or len(ref.encode()) > 2048
                    for ref in evidence
                )
            ):
                raise GatewayError(400, "invalid evidence references")
            result["evidence"] = evidence
        return result

    @classmethod
    def _provenance(cls, content, metadata, kind, actor, document_id):
        references = {}
        if kind == "summary" and content.lstrip().startswith("{"):
            try:
                envelope = json.loads(content)
            except (ValueError, RecursionError):
                envelope = None
            if (
                isinstance(envelope, dict)
                and set(envelope) == {"labels", "summary", "provenance"}
                and isinstance(envelope["labels"], list)
                and all(isinstance(label, str) for label in envelope["labels"])
                and sorted(envelope["labels"]) == ["derived", "extractive", "unreviewed"]
                and isinstance(envelope["summary"], str)
                and isinstance(envelope["provenance"], dict)
            ):
                references.update(cls._references(envelope["provenance"]))
        references.update(cls._references(metadata))
        references.update(actor=actor.id, harness=actor.harness, document_id=document_id)
        return references

    @staticmethod
    def _public(row, *, excerpt=False):
        provenance = row["provenance"]
        metadata = {
            key: provenance[key]
            for key in (
                "actor",
                "harness",
                "source_ref",
                "session_id",
                "turn_id",
                "host_id",
                "evidence",
            )
            if key in provenance
        }
        metadata.update(
            memory_id=row["memory_id"],
            revision=str(row["revision"]),
            capture_kind=row["capture_kind"],
        )
        text = row["content"]
        if excerpt:
            text = text.encode()[:1024].decode("utf-8", errors="ignore")
        result = {
            "id": provenance.get("document_id", row["memory_id"]),
            "document_id": provenance.get("document_id", row["memory_id"]),
            "memory_id": row["memory_id"],
            "text": text,
            "metadata": metadata,
            "revision": row["revision"],
            "capture_kind": row["capture_kind"],
        }
        if excerpt:
            result["truncated"] = len(row["content"].encode()) > 1024
        else:
            result["content"] = text
        return result

    @staticmethod
    def _memory_id(domain, document_id):
        if not isinstance(document_id, str) or not document_id or len(document_id.encode()) > 512:
            raise GatewayError(400, "invalid document id")
        scope = [domain["tenant_id"], domain["domain_type"], domain["domain_id"], document_id]
        return "gateway-" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()

    @staticmethod
    def _operation(row):
        return (
            base64.urlsafe_b64encode(
                json.dumps(
                    [row["memory_id"], row["revision"], row["capture_kind"]], separators=(",", ":")
                ).encode()
            )
            .decode()
            .rstrip("=")
        )

    async def _state(self, store, row):
        if self._pending_checker is not None:
            pending = await self._pending_checker(store, row["memory_id"], row["revision"])
            return "pending" if pending else "completed"
        pending = await store.is_pending(row["memory_id"], row["revision"])
        return "pending" if pending else "completed"

    async def _receipt(self, store, row):
        return {
            "operation_id": self._operation(row),
            "memory_id": row["memory_id"],
            "revision": row["revision"],
            "status": await self._state(store, row),
        }

    async def _retain_receipt(self, store, domain, row):
        receipt = await self._receipt(store, row)
        receipt.update(success=True, bank_id=domain["bank_ids"][row["capture_kind"]], items_count=1)
        receipt["async"] = True
        return receipt

    async def _deliver(self, store, domain, row):
        try:
            index = self._index_factory(copy.deepcopy(domain))
            if row.get("operation") == "DELETE":
                await self._thread(index.forget, row["memory_id"])
            else:
                current = await store.get(row["memory_id"])
                if (
                    current is None
                    or current["revision"] != row["revision"]
                    or current["capture_kind"] != row["capture_kind"]
                ):
                    return False
                await self._thread(index.retain, current)
            return await store.ack_delivery(row["memory_id"], row["revision"])
        except Exception:
            # Journal/outbox is already committed. Never expose backend details.
            return False

    async def drain(self, actor, domain, *, limit=100):
        """One bounded retry pass for an explicitly authorized domain alias."""
        self._actor(actor)
        if actor.read_only or domain not in actor.domains:
            raise GatewayError(403, "domain write denied")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise GatewayError(400, "invalid limit")
        await self._acquire()
        try:
            config = self._domains[domain]
            async with self._store_factory(copy.deepcopy(config), actor) as store:
                pending = await store.fetch_pending_deliveries(limit=limit)
                delivered = 0
                for row in pending:
                    delivered += bool(await self._deliver(store, config, row))
                return {"attempted": len(pending), "delivered": delivered}
        except GatewayError:
            raise
        except Exception:
            raise GatewayError(503, "memory journal unavailable") from None
        finally:
            self._lock.release()

    async def _acquire(self):
        try:
            async with asyncio.timeout(self._lock_timeout):
                await self._lock.acquire()
        except TimeoutError:
            raise GatewayError(503, "memory gateway busy") from None

    async def native(self, method, path, body, actor):
        self._actor(actor)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise GatewayError(400, "body must be an object")
        try:
            if len(json.dumps(body, allow_nan=False).encode()) > 100000:
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise GatewayError(400, "invalid or oversized body") from None
        if not isinstance(path, str) or len(path) > 4096:
            raise GatewayError(400, "invalid path")
        try:
            parsed = urlsplit(path)
        except ValueError:
            raise GatewayError(404, "unsupported native route") from None
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise GatewayError(404, "unsupported native route")
        if method not in ("GET", "PUT", "POST", "DELETE"):
            raise GatewayError(404, "unsupported native route")
        parts = parsed.path.split("/")
        listing = method == "GET" and parts[5:] in (["documents"], ["memories", "list"])
        if parsed.query:
            if not listing:
                raise GatewayError(404, "unsupported native query")
            try:
                pairs = parse_qsl(
                    parsed.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=2,
                    errors="strict",
                )
                if len({key for key, _ in pairs}) != len(pairs) or any(
                    key not in ("limit", "offset") or not re.fullmatch(r"[0-9]{1,6}", value)
                    for key, value in pairs
                ):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise GatewayError(400, "invalid listing query") from None
            body = dict(body, **{key: int(value) for key, value in pairs})
        if (
            method != "GET"
            and actor.read_only
            and not (
                method == "POST"
                and parts[5:] in (["memories", "recall"], ["knowledge-base", "search"])
            )
        ):
            raise GatewayError(403, "read only credential")
        if parts == ["", "v1", "default", "banks"] and method == "GET":
            return 200, {
                "banks": [
                    {"bank_id": bank, "capture_kind": kind}
                    for alias in actor.domains
                    for kind, bank in self._domains[alias]["bank_ids"].items()
                ]
            }
        if len(parts) < 5 or parts[:4] != ["", "v1", "default", "banks"]:
            raise GatewayError(404, "unsupported native route")
        match = [
            (self._domains[a], kind)
            for a in actor.domains
            for kind, bank in self._domains[a]["bank_ids"].items()
            if bank == parts[4]
        ]
        if len(match) != 1:
            raise GatewayError(404, "bank unavailable")
        domain, kind = match[0]
        tail = parts[5:]
        if not tail and method in ("GET", "PUT"):
            return 200, {"bank_id": parts[4], "capture_kind": kind}
        supported = (
            (
                method == "GET"
                and tail
                in (
                    ["status"],
                    ["stats"],
                    ["documents"],
                    ["memories", "list"],
                    ["knowledge-base", "tree"],
                )
            )
            or (method == "POST" and tail == ["knowledge-base", "search"])
            or (method == "POST" and tail in (["memories"], ["memories", "recall"]))
            or (method == "GET" and len(tail) == 2 and tail[0] == "operations")
            or (len(tail) == 2 and tail[0] == "documents" and method in ("GET", "DELETE"))
            or (
                len(tail) == 3
                and tail[0] == "documents"
                and tail[2] == "history"
                and method == "GET"
            )
        )
        if not supported:
            raise GatewayError(404, "unsupported native route")
        # Permanently empty v1 profile; no store/index call or page creation route.
        if tail == ["knowledge-base", "tree"]:
            return 200, {"nodes": []}
        if tail == ["knowledge-base", "search"]:
            return 200, {"results": []}
        await self._acquire()
        try:
            async with self._store_factory(copy.deepcopy(domain), actor) as store:
                return await self._route(method, tail, body, actor, domain, kind, store)
        except GatewayError:
            raise
        except RevisionConflict:
            raise GatewayError(409, "revision conflict") from None
        except (ValueError, TypeError):
            raise GatewayError(400, "invalid memory request") from None
        except Exception:
            raise GatewayError(503, "memory service unavailable") from None
        finally:
            self._lock.release()

    async def _route(self, method, tail, body, actor, domain, kind, store):
        if tail in (["status"], ["stats"]) and method == "GET":
            return 200, await store.status(capture_kind=kind)
        if tail in (["documents"], ["memories", "list"]) and method == "GET":
            limit, offset = body.get("limit", 20), body.get("offset", 0)
            if (
                type(limit) is not int
                or not 1 <= limit <= 100
                or type(offset) is not int
                or not 0 <= offset <= 100000
            ):
                raise GatewayError(400, "invalid listing bounds")
            rows = await store.list_records(capture_kind=kind, limit=limit, offset=offset)
            status = await store.status(capture_kind=kind)
            # Defense in depth: factory/store still owns canonical domain scoping.
            records = [
                self._public(row, excerpt=True)
                for row in rows[:limit]
                if row["capture_kind"] == kind and row.get("deleted_at") is None
            ]
            key = "documents" if tail == ["documents"] else "items"
            return 200, {key: records, "total": status["active"], "limit": limit, "offset": offset}
        if tail == ["memories"] and method == "POST":
            items = body.get("items")
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                raise GatewayError(400, "exactly one memory item required")
            item = items[0]
            content = item.get("content")
            if not isinstance(content, str) or not content.strip() or len(content.encode()) > 65536:
                raise GatewayError(400, "invalid content")
            metadata = item.get("metadata", {})
            if not isinstance(metadata, dict) or len(json.dumps(metadata).encode()) > 16384:
                raise GatewayError(400, "invalid metadata")
            basis = "automatic_derived"
            if kind == "fact":
                if metadata.get("explicit_save") is True or metadata.get("explicit") == "true":
                    basis = "caller_explicit_save"
                elif (
                    self._evidence_checker is not None
                    and await self._evidence_checker(
                        copy.deepcopy(domain), actor, copy.deepcopy(item)
                    )
                    is True
                ):
                    basis = "trusted_evidence"
                else:
                    raise GatewayError(400, "fact requires explicit save or trusted evidence")
            document_id = item.get("document_id", hashlib.sha256(content.encode()).hexdigest())
            memory_id = self._memory_id(domain, document_id)
            current = await store.get(memory_id)
            if current and current["capture_kind"] != kind:
                raise GatewayError(409, "capture kind conflict")
            expected = item.get(
                "base_revision", body.get("base_revision", current["revision"] if current else 0)
            )
            if type(expected) is not int or not 0 <= expected < 2147483647:
                raise GatewayError(400, "invalid base revision")
            if current and current["content"] == content and current["capture_kind"] == kind:
                return 202, await self._retain_receipt(store, domain, current)
            provenance = self._provenance(content, metadata, kind, actor, document_id)
            row = await store.record(
                memory_id,
                expected_revision=expected,
                content=content,
                provenance=provenance,
                capture_kind=kind,
                approval_metadata={"basis": basis, "human_verified": False},
            )
            await self._deliver(store, domain, row)
            return 202, await self._retain_receipt(store, domain, row)
        if tail == ["memories", "recall"] and method == "POST":
            query, limit = body.get("query"), body.get("limit", 10)
            if (
                not isinstance(query, str)
                or not query.strip()
                or len(query.encode()) > 8192
                or type(limit) is not int
                or not 1 <= limit <= 100
            ):
                raise GatewayError(400, "invalid recall")
            index = self._index_factory(copy.deepcopy(domain))
            candidates = await self._thread(index.recall, query, kind, limit)
            results, seen = [], set()
            for candidate in candidates[:limit]:
                if not isinstance(candidate, dict):
                    continue
                memory_id = candidate.get("memory_id")
                if not isinstance(memory_id, str) or len(memory_id) > 512 or memory_id in seen:
                    continue
                seen.add(memory_id)
                row = await store.get(memory_id)
                if (
                    row
                    and row["capture_kind"] == kind
                    and candidate.get("capture_kind") == kind
                    and type(candidate.get("revision")) is int
                    and row["revision"] == candidate["revision"]
                ):
                    public = self._public(row)
                    results.append(
                        {key: public[key] for key in ("document_id", "text", "metadata")}
                    )
            return 200, {"results": results}
        if len(tail) == 2 and tail[0] == "operations" and method == "GET":
            try:
                raw = tail[1]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", raw):
                    raise ValueError()
                memory_id, revision, op_kind = json.loads(
                    base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                )
                if (
                    not isinstance(memory_id, str)
                    or len(memory_id) > 512
                    or type(revision) is not int
                    or not 1 <= revision < 2147483647
                    or op_kind != kind
                ):
                    raise ValueError()
            except (ValueError, TypeError, binascii.Error):
                raise GatewayError(404, "operation unavailable") from None
            rows = await store.history(memory_id, after_revision=revision - 1, limit=2)
            if (
                not rows
                or rows[0]["revision"] != revision
                or (rows[0]["capture_kind"] != kind and rows[0]["operation"] != "DELETE")
            ):
                raise GatewayError(404, "operation unavailable")
            row = rows[0]
            current = await store.get(memory_id)
            superseded = len(rows) > 1 or (current and current["revision"] > revision)
            receipt = await self._receipt(store, dict(row, capture_kind=kind))
            if superseded:
                receipt["status"] = "superseded"
            return 200, receipt
        if len(tail) in (2, 3) and tail[0] == "documents":
            document_id = unquote(tail[1], errors="strict")
            memory_id = self._memory_id(domain, document_id)
            if len(tail) == 3 and tail[2] == "history" and method == "GET":
                after, limit = body.get("after_revision", 0), body.get("limit", 100)
                rows = await store.history(memory_id, after_revision=after, limit=limit)
                return 200, {
                    "history": [
                        {
                            "revision": r["revision"],
                            "operation": r["operation"],
                            "capture_kind": r["capture_kind"],
                        }
                        for r in rows
                        if r["capture_kind"] == kind or r["operation"] == "DELETE"
                    ]
                }
            if len(tail) == 2 and method in ("GET", "DELETE"):
                row = await store.get(memory_id)
                if row is None or row["capture_kind"] != kind:
                    raise GatewayError(404, "document unavailable")
                if method == "GET":
                    return 200, self._public(row)
                expected = body.get("base_revision", row["revision"])
                revision = await store.forget(memory_id, expected_revision=expected)
                # Keep the requested bank kind on the operation receipt, including legacy tombstones.
                tombstone = dict(row, revision=revision, operation="DELETE")
                await self._deliver(store, domain, tombstone)
                return 202, await self._receipt(store, tombstone)
        raise GatewayError(404, "unsupported native route")
