"""Native Hindsight index over canonical PostgreSQL memory, using shared Transport.

Synchronous dict interface (call outside the event loop when needed):
    AssistantHindsight((tenant_id, user_id, assistant_id), private_transport,
                       bank_ids=None)
    retain(row) -> {memory_id, revision, capture_kind}
    recall(query, kind, limit) -> [{memory_id, revision, capture_kind}, ...]
    forget(memory_id) -> {memory_id}

The parent supplies trusted server scope and a privately configured Transport.
Namespace/source-owner/credential mapping is parent policy, never user input.
Optional bank_ids={"fact": "<project>-facts", "summary": "<project>-summaries"}
selects existing shared project banks instead of the default private scope hash.
This mapping is trusted operator configuration, never a request-body field. The
parent must authorize shared-bank access and document ownership before deletion.
Portable native notes lacking SQL identity/revision metadata remain excluded from
recall; their import/projection policy requires a separate parent decision.
Only content and allowlisted identity/revision/kind metadata leave this adapter;
provenance, approvals, credentials and other row fields are not forwarded.
Recall returns untrusted index candidates, never authoritative notes. The parent
MUST filter against scoped canonical PG rows, current revision and tombstones.
The parent also serializes delivery per document, including promotions/deletes;
this adapter has no worker, durable state, authorization or ordering mechanism.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
from urllib.parse import quote

from herdr_memory.transport import MemoryError, Transport


class AssistantHindsight:
    """Trusted project bank mapping, or private hashed banks for one trusted scope."""

    def __init__(
        self,
        scope: tuple[str, str, str],
        transport: Transport,
        *,
        bank_ids: dict[str, str] | None = None,
    ):
        if (
            not isinstance(scope, tuple)
            or len(scope) != 3
            or any(
                not isinstance(value, str) or not value or len(value.encode()) > 512
                for value in scope
            )
        ):
            raise ValueError("scope must be a nonempty tenant/user/assistant tuple")
        canonical = json.dumps(scope, ensure_ascii=False, separators=(",", ":")).encode()
        prefix = "memory-" + hashlib.sha256(canonical).hexdigest()
        if bank_ids is None:
            bank_ids = {kind: prefix + "-" + kind for kind in ("fact", "summary")}
        elif (
            not isinstance(bank_ids, dict)
            or set(bank_ids) != {"fact", "summary"}
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value) is None
                for value in bank_ids.values()
            )
            or bank_ids["fact"] == bank_ids["summary"]
        ):
            raise ValueError("bank_ids must map fact and summary to distinct safe bank IDs")
        self._bank_ids = dict(bank_ids)
        self._transport = transport

    def _bank_path(self, kind: str):
        if kind not in ("summary", "fact"):
            raise ValueError("kind must be summary or fact")
        return "/v1/default/banks/" + self._bank_ids[kind]

    @staticmethod
    def _memory_id(memory_id):
        if not isinstance(memory_id, str) or not memory_id or len(memory_id.encode()) > 512:
            raise ValueError("invalid memory id")
        return memory_id

    def retain(self, row: dict) -> dict:
        """Index exact text with a stable document ID; synchronous native retain.

        Reapplying bank configuration avoids trusting defaults or cached state.
        No LLM extraction, observations, auto-consolidation, or nonvector arms.
        """
        memory_id = self._memory_id(row["memory_id"])
        revision = row["revision"]
        kind = row["capture_kind"]
        bank = self._bank_path(kind)
        if type(revision) is not int or not 1 <= revision <= 2147483647:
            raise ValueError("invalid revision")
        content = row["content"]
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > 65536:
            raise ValueError("content must be nonblank and at most 65536 bytes")
        self._transport.request(
            "PUT",
            bank,
            {
                "retain_extraction_mode": "chunks",
                "enable_observations": False,
                "enable_text_search": False,
                "enable_graph_retrieval": False,
                "enable_temporal_retrieval": False,
                "enable_reranking": False,
            },
        )
        # CreateBankRequest does not expose enable_auto_consolidation; use the
        # native hierarchical configuration endpoint before submitting content.
        self._transport.request(
            "PATCH",
            bank + "/config",
            {
                "updates": {"enable_auto_consolidation": False},
            },
        )
        self._transport.request(
            "POST",
            bank + "/memories",
            {
                "async": False,
                "items": [
                    {
                        "content": content,
                        "document_id": memory_id,
                        "metadata": {
                            "memory_id": memory_id,
                            "revision": str(revision),
                            "capture_kind": kind,
                        },
                    }
                ],
            },
        )
        return {"memory_id": memory_id, "revision": revision, "capture_kind": kind}

    def recall(self, query: str, kind: str, limit: int = 10) -> list[dict]:
        """Bounded, deduplicated candidate IDs/revisions from native vector recall.

        Native recall budgets tokens, not rows; limit caps the returned candidates.
        Missing/malformed/mismatched metadata is discarded, never inferred from text.
        """
        bank = self._bank_path(kind)
        if not isinstance(query, str) or not query.strip() or len(query.encode()) > 65536:
            raise ValueError("invalid query")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        result = self._transport.request(
            "POST",
            bank + "/memories/recall",
            {
                "query": query,
                "types": ["world", "experience"],
                "budget": "low",
                "max_tokens": 4096,
            },
        )
        results = result.get("results")
        if not isinstance(results, list):
            raise MemoryError("Memory service returned invalid recall results")
        candidates = []
        seen = set()
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
                continue
            metadata = item["metadata"]
            memory_id = metadata.get("memory_id")
            revision = metadata.get("revision")
            if (
                not isinstance(memory_id, str)
                or not memory_id
                or len(memory_id.encode()) > 512
                or item.get("document_id") != memory_id
                or metadata.get("capture_kind") != kind
                or not isinstance(revision, str)
                or not revision.isascii()
                or not revision.isdecimal()
                or len(revision) > 10
            ):
                continue
            revision = int(revision)
            if not 1 <= revision <= 2147483647 or (memory_id, revision) in seen:
                continue
            seen.add((memory_id, revision))
            candidates.append({"memory_id": memory_id, "revision": revision, "capture_kind": kind})
            if len(candidates) == limit:
                break
        return candidates

    def forget(self, memory_id: str) -> dict:
        """Delete stable document and all its versions from both promotion banks.

        Only native HTTP 404 is idempotent. Auth, config and network errors escape
        so the parent keeps its durable DELETE delivery pending for retry.
        """
        self._memory_id(memory_id)
        for kind in ("summary", "fact"):
            try:
                self._transport.request(
                    "DELETE", self._bank_path(kind) + "/documents/" + quote(memory_id, safe="")
                )
            except MemoryError as error:
                cause = error.__cause__
                if not isinstance(cause, urllib.error.HTTPError) or cause.code != 404:
                    raise
        return {"memory_id": memory_id}
