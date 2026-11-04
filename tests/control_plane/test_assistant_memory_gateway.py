"""Gateway contracts: real disposable PG journal, injected synchronous index only."""

import asyncio
import hashlib
import threading
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest

from herdr_engineering.control_plane.assistant_memory_gateway import (
    Actor,
    GatewayError,
    MemoryGateway,
)
from herdr_engineering.control_plane.assistant_memory_store import AssistantMemoryStore
from tests.control_plane.test_assistant_memory_store import pg  # noqa: F401

ROOT = "/v1/default/banks/project-fact"


@pytest.fixture
def setup_gateway(pg):  # noqa: F811
    domain = dict(
        domain_type="project",
        domain_id="canonical-project-" + uuid.uuid4().hex,
        tenant_id="tenant",
        bank_ids=dict(fact="project-fact", summary="project-summary"),
    )
    calls = []

    @asynccontextmanager
    async def store_factory(domain, actor):
        conn = await asyncpg.connect(pg.dsn)
        try:
            yield AssistantMemoryStore(
                conn,
                domain["tenant_id"],
                actor.id,
                actor.harness,
                domain_type=domain["domain_type"],
                domain_id=domain["domain_id"],
            )
        finally:
            await conn.close()

    class Index:
        fail = False
        candidates = []

        def retain(self, row):
            calls.append(("retain", row, threading.get_ident()))
            if self.fail:
                raise RuntimeError("secret backend URL")

        def forget(self, memory_id):
            calls.append(("forget", memory_id, threading.get_ident()))
            if self.fail:
                raise RuntimeError("secret backend URL")

        def recall(self, query, kind, limit):
            return self.candidates

    index = Index()
    gateway = MemoryGateway(
        {"project": domain},
        {
            "secret": dict(harness="codex", domains=["project"], read_only=False),
            "reader": dict(harness="web", domains=["project"], read_only=True),
            "outsider": dict(harness="other", domains=[], read_only=False),
        },
        store_factory,
        lambda domain: index,
    )
    return gateway, store_factory, index, calls


def test_auth_and_closed_routes(setup_gateway):
    gateway, _, _, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")
    assert actor.id == "key:" + hashlib.sha256(b"secret").hexdigest()[:12]
    for auth in [None, "", "secret", "Basic secret", "Bearer wrong", "Bearer secret "]:
        with pytest.raises(GatewayError) as error:
            gateway.authenticate(auth)
        assert error.value.status == 401

    async def exercise():
        for method, path, who in [
            ("PATCH", ROOT + "/config", actor),
            ("POST", ROOT + "/reflect", actor),
            ("GET", "/https://evil", actor),
            ("GET", ROOT, gateway.authenticate("Bearer outsider")),
            ("PUT", ROOT, gateway.authenticate("Bearer reader")),
            ("GET", ROOT, Actor(actor.id, actor.harness, actor.domains, False)),
        ]:
            with pytest.raises(GatewayError):
                await gateway.native(method, path, {}, who)
        assert (await gateway.native("PUT", ROOT, {"config": "ignored"}, actor))[0] == 200

    asyncio.run(exercise())


def test_journal_replay_cas_recall_delete_and_retry(setup_gateway):
    gateway, factory, index, calls = setup_gateway
    actor = gateway.authenticate("Bearer secret")

    async def exercise():
        payload = {
            "items": [
                {
                    "document_id": "note",
                    "content": "canonical",
                    "metadata": {"explicit_save": True, "actor": "forged", "human_verified": True},
                }
            ]
        }
        index.fail = True
        code, receipt = await gateway.native("POST", ROOT + "/memories", payload, actor)
        assert code == 202 and receipt["status"] == "pending"
        op = ROOT + "/operations/" + receipt["operation_id"]
        assert (await gateway.native("GET", op, {}, actor))[1]["status"] == "pending"
        async with factory(gateway.domains["project"], actor) as store:
            row = await store.get(receipt["memory_id"])
            assert row["provenance"]["actor"] == actor.id
            assert row["provenance"]["harness"] == "codex"
            assert row["approval_metadata"] == {
                "basis": "caller_explicit_save",
                "human_verified": False,
            }
            assert len(await store.history(receipt["memory_id"])) == 1
        again = (await gateway.native("POST", ROOT + "/memories", payload, actor))[1]
        assert again["revision"] == 1
        index.fail = False
        assert (await gateway.drain(actor, "project"))["delivered"] == 1
        assert (await gateway.native("GET", op, {}, actor))[1]["status"] == "completed"
        index.candidates = [
            dict(memory_id=row["memory_id"], revision=1, capture_kind="fact", text="evil"),
            dict(memory_id="foreign", revision=1, capture_kind="fact"),
        ]
        results = (await gateway.native("POST", ROOT + "/memories/recall", {"query": "x"}, actor))[
            1
        ]["results"]
        assert len(results) == 1 and results[0]["text"] == "canonical"
        assert results[0]["metadata"]["revision"] == "1"
        payload["items"][0]["content"] = "new"
        payload["items"][0]["base_revision"] = 0
        with pytest.raises(GatewayError) as error:
            await gateway.native("POST", ROOT + "/memories", payload, actor)
        assert error.value.status == 409
        payload["items"][0].pop("base_revision")
        await gateway.native("POST", ROOT + "/memories", payload, actor)
        assert (await gateway.native("GET", op, {}, actor))[1]["status"] == "superseded"
        assert not (await gateway.native("POST", ROOT + "/memories/recall", {"query": "x"}, actor))[
            1
        ]["results"]
        index.fail = True
        deleted = (await gateway.native("DELETE", ROOT + "/documents/note", {}, actor))[1]
        assert deleted["status"] == "pending"
        with pytest.raises(GatewayError):
            await gateway.native("POST", ROOT + "/memories", payload, actor)
        with pytest.raises(GatewayError):
            await gateway.native("GET", ROOT + "/documents/note", {}, actor)
        index.fail = False
        await gateway.drain(actor, "project")
        assert all(call[2] != threading.get_ident() for call in calls)

    asyncio.run(exercise())


def test_capture_rules_and_bounds(setup_gateway):
    gateway, _, _, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")

    async def exercise():
        for body in [
            {"items": []},
            {"items": [{}, {}]},
            {"items": [{"content": "fact"}]},
            {"items": [{"content": "x", "metadata": {"explicit_save": "true"}}]},
            {"items": [{"content": "x" * 65537, "metadata": {"explicit_save": True}}]},
        ]:
            with pytest.raises(GatewayError) as error:
                await gateway.native("POST", ROOT + "/memories", body, actor)
            assert error.value.status == 400
        assert (
            await gateway.native(
                "POST",
                ROOT.replace("fact", "summary") + "/memories",
                {"items": [{"content": "derived"}]},
                actor,
            )
        )[0] == 202

    asyncio.run(exercise())


def test_summary_tombstone_operations_history_and_status(setup_gateway):
    gateway, _, index, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")
    root = ROOT.replace("fact", "summary")

    async def exercise():
        await gateway.native(
            "POST",
            root + "/memories",
            {"items": [{"content": "summary", "document_id": "s"}]},
            actor,
        )
        index.fail = True
        receipt = (await gateway.native("DELETE", root + "/documents/s", {}, actor))[1]
        path = root + "/operations/" + receipt["operation_id"]
        assert (await gateway.native("GET", path, {}, actor))[1]["status"] == "pending"
        history = (await gateway.native("GET", root + "/documents/s/history", {}, actor))[1][
            "history"
        ]
        assert [r["operation"] for r in history] == ["RETAIN", "DELETE"]
        assert (await gateway.native("GET", root + "/status", None, actor))[1] == {
            "active": 0,
            "deleted": 1,
            "pending": 1,
        }
        index.fail = False
        await gateway.drain(actor, "project")
        assert (await gateway.native("GET", path, {}, actor))[1]["status"] == "completed"

    asyncio.run(exercise())


def test_trusted_evidence_and_config_copy(setup_gateway):
    gateway, factory, index, _ = setup_gateway
    domains = gateway.domains
    credentials = {"proof-key": dict(harness="proof", domains=["project"], read_only=False)}
    observed = []

    async def evidence(domain, actor, item):
        observed.append((domain, actor, item))
        return item.get("evidence") == "valid"

    other = MemoryGateway(
        domains, credentials, factory, lambda domain: index, evidence_checker=evidence
    )
    domains["project"]["domain_id"] = "evil"
    credentials["proof-key"]["domains"].clear()
    actor = other.authenticate("Bearer proof-key")

    async def exercise():
        await other.native(
            "POST",
            ROOT + "/memories",
            {"items": [{"content": "proven", "evidence": "valid"}]},
            actor,
        )
        async with factory(other.domains["project"], actor) as store:
            pending_status = await store.status()
            assert pending_status["active"] == 1
        assert observed[0][0]["domain_id"].startswith("canonical-project-")
        with pytest.raises(GatewayError):
            await other.native(
                "POST",
                ROOT + "/memories",
                {"items": [{"content": "unproven", "evidence": "invalid"}]},
                actor,
            )

    asyncio.run(exercise())


def test_explicit_personal_membership_and_cross_domain_operations(setup_gateway):
    gateway, factory, index, _ = setup_gateway
    domains = gateway.domains
    domains["personal"] = dict(
        domain_type="personal",
        domain_id="canonical-person",
        tenant_id="tenant",
        bank_ids=dict(fact="personal-fact", summary="personal-summary"),
    )
    other = MemoryGateway(
        domains,
        {
            "both": dict(harness="cli", domains=["project", "personal"], read_only=False),
            "project-only": dict(harness="web", domains=["project"], read_only=False),
        },
        factory,
        lambda domain: index,
    )
    actor = other.authenticate("Bearer both")
    personal = ROOT.replace("project", "personal")

    async def exercise():
        receipt = (
            await other.native(
                "POST",
                personal + "/memories",
                {
                    "items": [
                        {
                            "document_id": "same-id",
                            "content": "private",
                            "metadata": {"explicit_save": True},
                        }
                    ]
                },
                actor,
            )
        )[1]
        for path, who in [
            (personal, other.authenticate("Bearer project-only")),
            (ROOT + "/operations/" + receipt["operation_id"], actor),
            (ROOT + "/documents/same-id", actor),
        ]:
            with pytest.raises(GatewayError) as error:
                await other.native("GET", path, {}, who)
            assert error.value.status == 404
        banks = (
            await other.native(
                "GET", "/v1/default/banks", {}, other.authenticate("Bearer project-only")
            )
        )[1]["banks"]
        assert {bank["bank_id"] for bank in banks} == {"project-fact", "project-summary"}

    asyncio.run(exercise())


def test_cancelled_delivery_holds_lock_until_thread_finishes(setup_gateway):
    gateway, _, index, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")
    started, release = threading.Event(), threading.Event()

    def slow_retain(row):
        started.set()
        assert release.wait(5)

    index.retain = slow_retain

    async def exercise():
        task = asyncio.create_task(
            gateway.native(
                "POST",
                ROOT + "/memories",
                {
                    "items": [
                        {
                            "document_id": "cancel",
                            "content": "x",
                            "metadata": {"explicit_save": True},
                        }
                    ]
                },
                actor,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert gateway._lock.locked()
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0.05)
            assert gateway._lock.locked()
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not gateway._lock.locked()
        # Cancellation leaves a durable retry, even if the backend accepted the write.
        assert (await gateway.drain(actor, "project"))["delivered"] == 1

    asyncio.run(exercise())


def test_read_only_credential_can_recall_but_cannot_retain(setup_gateway):
    gateway, _, _, _ = setup_gateway
    reader = gateway.authenticate("Bearer reader")

    async def exercise():
        assert (await gateway.native("POST", ROOT + "/memories/recall", {"query": "x"}, reader))[
            1
        ] == {"results": []}
        with pytest.raises(GatewayError) as error:
            await gateway.native("POST", ROOT + "/memories", {"items": []}, reader)
        assert error.value.status == 403

    asyncio.run(exercise())


def test_portable_explicit_receipt_and_allowlisted_provenance(setup_gateway):
    gateway, factory, index, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")
    index.fail = True

    async def exercise():
        metadata = {
            "explicit": "true",
            "source_ref": "thread:1",
            "session_id": "forged-session",
            "turn_id": "turn:2",
            "host_id": "host:3",
            "evidence": '["ref:1"]',
            "actor": "forged",
            "harness": "forged",
            "document_id": "forged",
            "human_verified": True,
            "approval": "human",
            "unknown": "ignored",
        }
        body = {
            "items": [
                {"document_id": "portable", "content": "portable fact", "metadata": metadata}
            ],
            "async": False,
        }
        status, receipt = await gateway.native("POST", ROOT + "/memories", body, actor)
        assert status == 202 and receipt["status"] == "pending"
        assert {k: receipt[k] for k in ("success", "bank_id", "items_count", "async")} == {
            "success": True,
            "bank_id": "project-fact",
            "items_count": 1,
            "async": True,
        }
        replay = (await gateway.native("POST", ROOT + "/memories", body, actor))[1]
        assert replay == receipt
        async with factory(gateway.domains["project"], actor) as store:
            row = await store.get(receipt["memory_id"])
            assert row["approval_metadata"] == {
                "basis": "caller_explicit_save",
                "human_verified": False,
            }
            assert row["provenance"] == {
                "actor": actor.id,
                "harness": "codex",
                "document_id": "portable",
                "source_ref": "thread:1",
                "session_id": "forged-session",
                "turn_id": "turn:2",
                "host_id": "host:3",
                "evidence": ["ref:1"],
            }
        document = (await gateway.native("GET", ROOT + "/documents/portable", None, actor))[1]
        assert document["document_id"] == "portable" and document["text"] == "portable fact"
        assert document["metadata"]["harness"] == "codex"
        assert "human_verified" not in document["metadata"]
        for explicit in ("True", True, "false", 1):
            with pytest.raises(GatewayError) as error:
                await gateway.native(
                    "POST",
                    ROOT + "/memories",
                    {
                        "items": [
                            {"content": "invalid explicit", "metadata": {"explicit": explicit}}
                        ]
                    },
                    actor,
                )
            assert error.value.status == 400

    asyncio.run(exercise())


def test_derived_capture_schema_is_optional_and_never_identity(setup_gateway):
    import json

    gateway, factory, _, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")
    root = ROOT.replace("fact", "summary")

    async def exercise():
        for i, labels in enumerate([["derived", "unreviewed", "extractive"], ["ordinary"]]):
            content = json.dumps(
                {
                    "labels": labels,
                    "summary": "summary",
                    "provenance": {
                        "actor": "forged",
                        "harness": "forged",
                        "session_id": "capture-session",
                        "turn_id": "capture-turn",
                        "human_verified": True,
                    },
                }
            )
            receipt = (
                await gateway.native(
                    "POST",
                    root + "/memories",
                    {
                        "items": [
                            {
                                "document_id": "capture-" + str(i),
                                "content": content,
                                "metadata": {"host_id": "metadata-host"},
                            }
                        ]
                    },
                    actor,
                )
            )[1]
            async with factory(gateway.domains["project"], actor) as store:
                row = await store.get(receipt["memory_id"])
            assert row["content"] == content
            assert row["provenance"]["actor"] == actor.id
            assert row["provenance"]["harness"] == "codex"
            assert row["provenance"]["host_id"] == "metadata-host"
            assert "human_verified" not in row["provenance"]
            if i == 0:
                assert row["provenance"]["session_id"] == "capture-session"
            else:
                assert "session_id" not in row["provenance"]

    asyncio.run(exercise())


def test_native_lists_queries_kind_stats_and_tombstones(setup_gateway):
    gateway, _, _, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")
    reader = gateway.authenticate("Bearer reader")

    async def exercise():
        for root, doc, content in [
            (ROOT, "large", "x" * 64000),
            (ROOT, "small", "second"),
            (ROOT.replace("fact", "summary"), "summary", "derived"),
        ]:
            await gateway.native(
                "POST",
                root + "/memories",
                {
                    "items": [
                        {
                            "document_id": doc,
                            "content": content,
                            "metadata": {"explicit_save": True},
                        }
                    ]
                },
                actor,
            )
        status, page = await gateway.native(
            "GET", ROOT + "/documents?limit=1&offset=0", None, reader
        )
        assert status == 200 and len(page["documents"]) == 1 and page["total"] == 2
        assert len(page["documents"][0]["text"].encode()) <= 1024
        assert page["documents"][0]["metadata"]["capture_kind"] == "fact"
        assert "content" not in page["documents"][0]
        next_page = (
            await gateway.native("GET", ROOT + "/memories/list?limit=1&offset=1", None, reader)
        )[1]
        assert len(next_page["items"]) == 1
        assert page["documents"][0]["document_id"] != next_page["items"][0]["document_id"]
        full = (await gateway.native("GET", ROOT + "/documents/large", None, reader))[1]
        assert full["text"] == "x" * 64000
        assert (await gateway.native("GET", ROOT + "/stats", None, reader))[1]["active"] == 2
        summary_root = ROOT.replace("fact", "summary")
        assert (await gateway.native("GET", summary_root + "/status", None, reader))[1][
            "active"
        ] == 1
        await gateway.native("DELETE", ROOT + "/documents/large", None, actor)
        assert (await gateway.native("GET", ROOT + "/documents", None, reader))[1]["total"] == 1
        for query in [
            "limit=101",
            "offset=-1",
            "limit=true",
            "limit=1&limit=2",
            "tag=project",
            "limit=1&scope=personal",
            "offset=999999999999",
            "limit=",
            "limit=1&",
            "%6cimit=1&limit=2",
        ]:
            with pytest.raises(GatewayError):
                await gateway.native("GET", ROOT + "/documents?" + query, None, reader)
        with pytest.raises(GatewayError):
            await gateway.native("GET", ROOT + "/documents/large?limit=1", None, actor)
        with pytest.raises(GatewayError):
            await gateway.native("POST", ROOT + "/memories?limit=1", {"items": []}, actor)

    asyncio.run(exercise())


def test_knowledge_pages_permanently_disabled(setup_gateway):
    gateway, _, _, calls = setup_gateway
    actor = gateway.authenticate("Bearer reader")

    async def exercise():
        assert (await gateway.native("GET", ROOT + "/knowledge-base/tree", None, actor))[1] == {
            "nodes": []
        }
        assert (
            await gateway.native("POST", ROOT + "/knowledge-base/search", {"query": "x"}, actor)
        )[1] == {"results": []}
        assert calls == []
        for method, suffix in [
            ("POST", "/knowledge-base/pages"),
            ("PUT", "/knowledge-base/pages/a"),
            ("POST", "/knowledge-base/tree"),
            ("GET", "/knowledge-base/search"),
        ]:
            with pytest.raises(GatewayError):
                await gateway.native(method, ROOT + suffix, {}, actor)

    asyncio.run(exercise())


def test_operation_state_is_exact_beyond_delivery_page(setup_gateway):
    gateway, factory, index, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")
    index.fail = True

    async def exercise():
        async with factory(gateway.domains["project"], actor) as store:
            for i in range(1001):
                await store.record(
                    "pending-" + uuid.uuid4().hex,
                    expected_revision=0,
                    content="distractor",
                    capture_kind="fact",
                    provenance={},
                    approval_metadata={"basis": "test"},
                )
        receipt = (
            await gateway.native(
                "POST",
                ROOT + "/memories",
                {
                    "items": [
                        {
                            "document_id": "exact",
                            "content": "target",
                            "metadata": {"explicit": "true"},
                        }
                    ]
                },
                actor,
            )
        )[1]
        assert receipt["status"] == "pending"
        path = ROOT + "/operations/" + receipt["operation_id"]
        assert (await gateway.native("GET", path, None, actor))[1]["status"] == "pending"
        async with factory(gateway.domains["project"], actor) as store:
            assert len(await store.fetch_pending_deliveries(limit=1000)) == 1000
            assert await store.ack_delivery(receipt["memory_id"], receipt["revision"])
        assert (await gateway.native("GET", path, None, actor))[1]["status"] == "completed"

    asyncio.run(exercise())


def test_evidence_references_are_bounded_data_not_save_authority(setup_gateway):
    gateway, factory, _, _ = setup_gateway
    actor = gateway.authenticate("Bearer secret")

    async def exercise():
        receipt = (
            await gateway.native(
                "POST",
                ROOT + "/memories",
                {
                    "items": [
                        {
                            "content": "reference list",
                            "metadata": {"explicit": "true", "evidence": ["ref:1"]},
                        }
                    ]
                },
                actor,
            )
        )[1]
        async with factory(gateway.domains["project"], actor) as store:
            row = await store.get(receipt["memory_id"])
        assert row["provenance"]["evidence"] == ["ref:1"]
        with pytest.raises(GatewayError) as error:
            await gateway.native(
                "POST",
                ROOT + "/memories",
                {"items": [{"content": "evidence alone", "metadata": {"evidence": ["ref:1"]}}]},
                actor,
            )
        assert error.value.status == 400
        for references in [
            {"evidence": "{}"},
            {"evidence": [True]},
            {"evidence": ["r"] * 33},
            {"session_id": "s" * 2049},
            {"host_id": {"actor": "forged"}},
        ]:
            with pytest.raises(GatewayError) as error:
                await gateway.native(
                    "POST",
                    ROOT + "/memories",
                    {
                        "items": [
                            {
                                "content": "invalid references",
                                "metadata": dict(references, explicit="true"),
                            }
                        ]
                    },
                    actor,
                )
            assert error.value.status == 400

    asyncio.run(exercise())


def test_operator_principal_validation_and_bounded_issued_identity(setup_gateway):
    gateway, _, _, _ = setup_gateway
    principal = {"kind": "operator", "id": "actual-operator-key", "authenticated_by": "api_key"}
    actor = gateway.authorize_operator(principal)
    assert actor.id == principal["id"]
    assert actor.harness == "operator-ui" and actor.domains == () and not actor.read_only
    assert gateway.authorize_operator(dict(principal)) is actor
    for invalid in [
        None,
        "operator",
        {"role": "operator", "id": "body-role"},
        dict(principal, kind="viewer"),
        dict(principal, authenticated_by="bearer"),
        dict(principal, authenticated_by="body"),
        dict(principal, id=""),
        dict(principal, id="  "),
        dict(principal, id="x" * 513),
        dict(principal, id="bad\nidentity"),
        dict(principal, id=123),
    ]:
        with pytest.raises(GatewayError) as error:
            gateway.authorize_operator(invalid)
        assert error.value.status == 403
    open_actor = gateway.authorize_operator(
        dict(principal, id="actual-listener-principal", authenticated_by="open_listener")
    )
    assert open_actor.id == "actual-listener-principal"

    async def exercise():
        assert await gateway.operator_domains(actor) == []
        assert (await gateway.native("GET", "/v1/default/banks", None, actor))[1] == {"banks": []}
        with pytest.raises(GatewayError):
            await gateway.native("GET", ROOT, None, actor)
        forged = Actor(actor.id, actor.harness, actor.domains, actor.read_only)
        for method in (
            gateway.operator_domains(forged),
            gateway.native("GET", "/v1/default/banks", None, forged),
        ):
            with pytest.raises(GatewayError) as error:
                await method
            assert error.value.status == 401

    asyncio.run(exercise())


def test_operator_cache_eviction_revokes_old_actor_reference(setup_gateway):
    gateway, _, _, _ = setup_gateway

    def issue(i):
        return gateway.authorize_operator(
            {"kind": "operator", "id": "operator-" + str(i), "authenticated_by": "api_key"}
        )

    first = issue(0)
    for i in range(1, 65):
        last = issue(i)
    assert len(gateway._operator_actors) == 64
    assert issue(64) is last

    async def exercise():
        with pytest.raises(GatewayError) as error:
            await gateway.operator_domains(first)
        assert error.value.status == 401
        assert await gateway.operator_domains(last) == []
        renewed = issue(0)
        assert renewed is not first and renewed.id == first.id
        assert await gateway.operator_domains(renewed) == []
        with pytest.raises(GatewayError):
            await gateway.operator_domains(first)

    asyncio.run(exercise())


def test_operator_domains_are_scoped_canonical_redacted_and_audited(setup_gateway):
    gateway, factory, index, _ = setup_gateway
    domains = gateway.domains
    domains["project"].update(
        backend_token_file="/private/token",
        backend_url="https://private",
        credential_config={"secret": "do-not-expose"},
    )
    domains["hidden"] = dict(
        domain_type="personal",
        domain_id="hidden-personal-" + uuid.uuid4().hex,
        tenant_id="tenant",
        bank_ids=dict(fact="hidden-fact", summary="hidden-summary"),
    )
    configured = ["project"]
    other = MemoryGateway(
        domains,
        {"native-key": dict(harness="native", domains=["hidden"], read_only=False)},
        factory,
        lambda domain: index,
        operator_domains=configured,
    )
    configured.append("hidden")
    operator = other.authorize_operator(
        {"kind": "operator", "id": "key:actual-human-key", "authenticated_by": "api_key"}
    )
    peer = other.authorize_operator(
        {"kind": "operator", "id": "key:another-human-key", "authenticated_by": "api_key"}
    )
    native = other.authenticate("Bearer native-key")

    async def exercise():
        assert await other.operator_domains(operator) == [
            {
                "alias": "project",
                "domain_type": "project",
                "domain_id": domains["project"]["domain_id"],
                "bank_ids": {"fact": "project-fact", "summary": "project-summary"},
                "counts": {"active": 0, "deleted": 0, "pending": 0},
            }
        ]
        receipt = (
            await other.native(
                "POST",
                ROOT + "/memories",
                {
                    "items": [
                        {
                            "document_id": "human-note",
                            "content": "operator note",
                            "metadata": {
                                "explicit_save": True,
                                "actor": "forged",
                                "harness": "forged",
                                "human_verified": True,
                            },
                        }
                    ]
                },
                operator,
            )
        )[1]
        async with factory(domains["project"], operator) as store:
            row = await store.get(receipt["memory_id"])
            assert row["user_id"] == operator.id
            assert row["provenance"]["actor"] == operator.id
            assert row["provenance"]["harness"] == "operator-ui"
            assert row["approval_metadata"] == {
                "basis": "caller_explicit_save",
                "human_verified": False,
            }
        assert (await other.operator_domains(peer))[0]["counts"]["active"] == 1
        with pytest.raises(GatewayError) as error:
            await other.operator_domains(native)
        assert error.value.status == 403
        for path, who in [
            (ROOT, native),
            (ROOT.replace("project", "hidden"), operator),
            ("/v1/default/banks/project-fact/operator/domains", native),
        ]:
            with pytest.raises(GatewayError):
                await other.native("GET", path, None, who)
        # An ordinary bearer write may carry role-like data; it never gains operator membership.
        await other.native(
            "POST",
            ROOT.replace("project", "hidden") + "/memories",
            {
                "items": [
                    {
                        "content": "native note",
                        "metadata": {
                            "explicit_save": True,
                            "kind": "operator",
                            "authenticated_by": "api_key",
                            "id": operator.id,
                        },
                    }
                ]
            },
            native,
        )
        with pytest.raises(GatewayError):
            await other.operator_domains(native)
        result = await other.operator_domains(operator)
        result[0]["bank_ids"]["fact"] = "tampered"
        assert (await other.operator_domains(operator))[0]["bank_ids"]["fact"] == "project-fact"

    asyncio.run(exercise())


@pytest.mark.parametrize("aliases", ["project", ["missing"], ["project", "project"], [None]])
def test_operator_domain_config_is_explicit_registry_allowlist(setup_gateway, aliases):
    gateway, factory, index, _ = setup_gateway
    with pytest.raises(ValueError):
        MemoryGateway(gateway.domains, {}, factory, lambda domain: index, operator_domains=aliases)


@pytest.mark.parametrize("distinct_field", [None, "tenant_id", "domain_type", "domain_id"])
def test_domain_aliases_require_distinct_canonical_identity(distinct_field):
    domain = dict(
        tenant_id="tenant",
        domain_type="project",
        domain_id="shared-project",
        bank_ids=dict(fact="first-facts", summary="first-summaries"),
    )
    other = dict(domain, bank_ids=dict(fact="second-facts", summary="second-summaries"))
    if distinct_field is not None:
        other[distinct_field] = "personal" if distinct_field == "domain_type" else "different"
    domains = {"first": domain, "second": other}
    if distinct_field is None:
        with pytest.raises(ValueError):
            MemoryGateway(domains, {}, None, None)
    else:
        gateway = MemoryGateway(domains, {}, None, None)
        assert set(gateway.domains) == {"first", "second"}


@pytest.mark.parametrize("original_kind", ["fact", "summary"])
@pytest.mark.parametrize("revision_source", [None, "item", "body"])
@pytest.mark.parametrize("same_content", [False, True])
def test_cross_kind_replacement_is_rejected_without_mutation(
    setup_gateway, original_kind, revision_source, same_content
):
    gateway, factory, index, calls = setup_gateway
    actor = gateway.authenticate("Bearer secret")
    original_root = ROOT.replace("fact", original_kind)
    replacement_kind = "summary" if original_kind == "fact" else "fact"
    replacement_root = ROOT.replace("fact", replacement_kind)
    index.fail = True

    async def exercise():
        original = {
            "document_id": "cross-kind-note",
            "content": "Original canonical note",
            "metadata": {"explicit_save": True, "source_ref": "original-source"},
        }
        _, receipt = await gateway.native(
            "POST", original_root + "/memories", {"items": [original]}, actor
        )
        async with factory(gateway.domains["project"], actor) as store:
            before = await store.get(receipt["memory_id"])
            history = await store.history(receipt["memory_id"])
            pending = await store.fetch_pending_deliveries()
        delivery_count = len(calls)
        replacement = dict(
            original,
            content=original["content"] if same_content else "Replacement note",
            metadata={"explicit_save": True, "source_ref": "replacement-source"},
        )
        body = {"items": [replacement]}
        if revision_source == "item":
            replacement["base_revision"] = receipt["revision"]
        elif revision_source == "body":
            body["base_revision"] = receipt["revision"]
        with pytest.raises(GatewayError) as error:
            await gateway.native("POST", replacement_root + "/memories", body, actor)
        assert error.value.status == 409
        async with factory(gateway.domains["project"], actor) as store:
            assert await store.get(receipt["memory_id"]) == before
            assert await store.history(receipt["memory_id"]) == history
            assert await store.fetch_pending_deliveries() == pending
        assert len(calls) == delivery_count
        status, document = await gateway.native(
            "GET", original_root + "/documents/cross-kind-note", None, actor
        )
        assert status == 200 and document["content"] == original["content"]
        assert document["revision"] == 1 and document["capture_kind"] == original_kind
        with pytest.raises(GatewayError) as error:
            await gateway.native(
                "GET", replacement_root + "/documents/cross-kind-note", None, actor
            )
        assert error.value.status == 404

    asyncio.run(exercise())
