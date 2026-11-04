"""Local native HTTP contracts; real E5 acceptance requires explicit opt-in."""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from herdr_memory.transport import MemoryError, Transport, Unavailable


@pytest.fixture
def endpoint(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw) if raw else None
            self.server.calls.append((self.command, self.path, body))
            code, response = self.server.respond(self.command, self.path, body)
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        do_PUT = do_PATCH = do_POST = do_DELETE = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls = []
    server.respond = lambda *_: (200, {})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key = tmp_path / "key"
    key.write_text("synthetic-test-key")
    key.chmod(0o600)
    transport = Transport(url=f"http://127.0.0.1:{server.server_port}", token_file=key)
    try:
        yield server, transport
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def row(kind="fact"):
    return dict(
        memory_id="document/秘密 ?#",
        revision=3,
        capture_kind=kind,
        content="  Exact\n秘密 ",
        provenance={"credential": "DO-NOT-SEND"},
        approval_metadata={"token": "DO-NOT-SEND"},
        tenant_id="ignored-row-scope",
    )


def test_scoped_banks_exact_retain_and_allowlisted_metadata(endpoint):
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    server, transport = endpoint
    adapter = AssistantHindsight(("tenant", "user", "assistant"), transport)
    assert server.calls == []
    assert adapter.retain(row()) == {
        "memory_id": row()["memory_id"],
        "revision": 3,
        "capture_kind": "fact",
    }
    calls = server.calls
    assert [call[0] for call in calls] == ["PUT", "PATCH", "POST"]
    bank = calls[0][1]
    assert bank.startswith("/v1/default/banks/memory-")
    assert bank.endswith("-fact")
    assert not any(value in bank for value in ("tenant", "user", "assistant"))
    assert calls[0][2] == dict(
        retain_extraction_mode="chunks",
        enable_observations=False,
        enable_text_search=False,
        enable_graph_retrieval=False,
        enable_temporal_retrieval=False,
        enable_reranking=False,
    )
    assert calls[1] == (
        "PATCH",
        bank + "/config",
        {"updates": {"enable_auto_consolidation": False}},
    )
    item = calls[2][2]["items"][0]
    assert calls[2][2]["async"] is False
    assert item == {
        "content": row()["content"],
        "document_id": row()["memory_id"],
        "metadata": {"memory_id": row()["memory_id"], "revision": "3", "capture_kind": "fact"},
    }
    assert "DO-NOT-SEND" not in json.dumps(calls)
    adapter.retain(row("summary"))
    assert server.calls[-1][1] != calls[2][1]
    assert server.calls[-1][1].endswith("-summary/memories")
    peer = AssistantHindsight(("tenant", "user", "assistant"), transport)
    peer.retain(row())
    assert server.calls[-1][1] == calls[2][1]
    for scope in [
        ("other", "user", "assistant"),
        ("tenant", "other", "assistant"),
        ("tenant", "user", "other"),
        ("ten", "antuser", "assistant"),
    ]:
        AssistantHindsight(scope, transport).retain(row())
        assert server.calls[-1][1] != calls[2][1]


def test_recall_returns_only_valid_deduplicated_candidates(endpoint):
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    server, transport = endpoint
    adapter = AssistantHindsight(("t", "u", "a"), transport)
    valid = {
        "document_id": row()["memory_id"],
        "metadata": {"memory_id": row()["memory_id"], "revision": "3", "capture_kind": "fact"},
        "text": "untrusted text",
    }
    server.respond = lambda *_: (
        200,
        {
            "results": [
                valid,
                valid,
                {**valid, "document_id": "mismatch"},
                {**valid, "metadata": {**valid["metadata"], "revision": "no"}},
                {**valid, "metadata": {**valid["metadata"], "capture_kind": "summary"}},
                {"text": "no identifiers"},
            ]
        },
    )
    assert adapter.recall("  query ", "fact", 2) == [
        {"memory_id": row()["memory_id"], "revision": 3, "capture_kind": "fact"}
    ]
    method, path, body = server.calls[-1]
    assert method == "POST" and path.endswith("-fact/memories/recall")
    assert body == {
        "query": "  query ",
        "types": ["world", "experience"],
        "budget": "low",
        "max_tokens": 4096,
    }
    for kind, limit in [("invalid", 1), ("fact", 0), ("fact", 101), ("fact", True)]:
        with pytest.raises(ValueError):
            adapter.recall("query", kind, limit)


@pytest.mark.parametrize("code", [404, 401, 403, 500])
def test_forget_both_banks_only_404_is_idempotent(endpoint, code):
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    server, transport = endpoint
    adapter = AssistantHindsight(("t", "u", "a"), transport)
    server.respond = lambda *_: (code, {})
    if code == 404:
        assert adapter.forget("doc/ ?#") == {"memory_id": "doc/ ?#"}
        assert len(server.calls) == 2
        assert {call[1].split("/")[4].rsplit("-", 1)[-1] for call in server.calls} == {
            "summary",
            "fact",
        }
        assert all(
            call[0] == "DELETE" and call[1].endswith("/documents/doc%2F%20%3F%23")
            for call in server.calls
        )
    else:
        with pytest.raises(Unavailable if code == 500 else MemoryError):
            adapter.forget("doc/ ?#")


def test_transport_errors_and_invalid_input_are_not_hidden(endpoint):
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    server, transport = endpoint
    adapter = AssistantHindsight(("t", "u", "a"), transport)
    for invalid in [
        {**row(), "revision": 0},
        {**row(), "content": " "},
        {**row(), "capture_kind": "auto"},
    ]:
        with pytest.raises(ValueError):
            adapter.retain(invalid)
    assert server.calls == []
    server.respond = lambda *_: (403, {})
    with pytest.raises(MemoryError):
        adapter.retain(row())
    with pytest.raises(MemoryError):
        adapter.recall("query", "fact", 1)
    server.shutdown()
    server.server_close()
    with pytest.raises(Unavailable):
        adapter.forget("doc")


@pytest.mark.parametrize("failed_method", ["PUT", "PATCH", "POST"])
@pytest.mark.parametrize("code", [404, 403, 503])
def test_retain_does_not_hide_native_failures(endpoint, failed_method, code):
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    server, transport = endpoint
    server.respond = lambda method, *_: (code if method == failed_method else 200, {})
    adapter = AssistantHindsight(("t", "u", "a"), transport)
    with pytest.raises(Unavailable if code == 503 else MemoryError):
        adapter.retain(row())
    methods = [call[0] for call in server.calls]
    assert methods == ["PUT", "PATCH", "POST"][: methods.index(failed_method) + 1]


def test_scope_encoding_is_unambiguous_and_recall_limit_is_enforced(endpoint):
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    server, transport = endpoint
    first = AssistantHindsight(("ab", "c", "d"), transport)
    second = AssistantHindsight(("a", "bc", "d"), transport)
    assert first._bank_path("fact") != second._bank_path("fact")
    results = [
        {
            "document_id": str(i),
            "metadata": {"memory_id": str(i), "revision": "1", "capture_kind": "fact"},
        }
        for i in range(4)
    ]
    server.respond = lambda *_: (200, {"results": results})
    assert first.recall("query", "fact", 2) == [
        {"memory_id": "0", "revision": 1, "capture_kind": "fact"},
        {"memory_id": "1", "revision": 1, "capture_kind": "fact"},
    ]
    server.respond = lambda *_: (200, {"results": None})
    with pytest.raises(MemoryError):
        first.recall("query", "fact", 2)


@pytest.mark.skipif(
    not os.environ.get("HERDR_MEMORY_DEPLOYMENT_TEST_CONFIG"),
    reason="explicit native deployment opt-in required",
)
def test_native_e5_recall_promotion_and_forget():
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    config = json.loads(Path(os.environ["HERDR_MEMORY_DEPLOYMENT_TEST_CONFIG"]).read_text())
    transport = Transport(
        url=config["api_url"], token_file=config["backend_key_files"]["validation"], timeout=30
    )
    scope = ("synthetic-acceptance", uuid.uuid4().hex, "memory-index")
    adapter = AssistantHindsight(scope, transport)
    payload = {
        **row(),
        "memory_id": "synthetic-release",
        "content": "Before directing live production traffic, deploy Atlas to the blue environment.",
    }
    try:
        for kind in ("summary", "fact"):
            adapter.retain({**payload, "capture_kind": kind})
            adapter.retain(
                {
                    **payload,
                    "capture_kind": kind,
                    "revision": 4,
                    "content": payload["content"] + " Synthetic revision four.",
                }
            )
            candidates = adapter.recall(
                "Which release slot precedes switching live users?", kind, 10
            )
            assert {
                "memory_id": "synthetic-release",
                "revision": 4,
                "capture_kind": kind,
            } in candidates
            assert all(candidate["revision"] == 4 for candidate in candidates)
            state = transport.request("GET", adapter._bank_path(kind) + "/config")["config"]
            assert state["retain_extraction_mode"] == "chunks"
            assert state["enable_observations"] is False
            assert state["enable_auto_consolidation"] is False
            assert state["enable_text_search"] is False
        adapter.forget("synthetic-release")
        for kind in ("summary", "fact"):
            assert (
                adapter.recall("Which release slot precedes switching live users?", kind, 10) == []
            )
    finally:
        for kind in ("summary", "fact"):
            try:
                transport.request("DELETE", adapter._bank_path(kind))
            except MemoryError as error:
                import urllib.error

                if (
                    not isinstance(error.__cause__, urllib.error.HTTPError)
                    or error.__cause__.code != 404
                ):
                    raise


def test_operator_bank_ids_reuse_exact_project_banks(endpoint):
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    server, transport = endpoint
    configured = {"fact": "herdr-engineering-facts", "summary": "herdr-engineering-summaries"}
    adapter = AssistantHindsight(("t", "u", "a"), transport, bank_ids=configured)
    assert server.calls == []
    configured["fact"] = "mutated-after-construction"
    for kind, bank_id in [
        ("fact", "herdr-engineering-facts"),
        ("summary", "herdr-engineering-summaries"),
    ]:
        adapter.retain(row(kind))
        assert [call[1] for call in server.calls[-3:]] == [
            f"/v1/default/banks/{bank_id}",
            f"/v1/default/banks/{bank_id}/config",
            f"/v1/default/banks/{bank_id}/memories",
        ]
        server.respond = lambda *_: (200, {"results": []})
        assert adapter.recall("query", kind, 1) == []
        assert server.calls[-1][1] == f"/v1/default/banks/{bank_id}/memories/recall"
    adapter.forget("synthetic-doc")
    assert [call[1] for call in server.calls[-2:]] == [
        "/v1/default/banks/herdr-engineering-summaries/documents/synthetic-doc",
        "/v1/default/banks/herdr-engineering-facts/documents/synthetic-doc",
    ]
    assert all("/memory-" not in path for _, path, _ in server.calls)


@pytest.mark.parametrize(
    "bank_ids",
    [
        {},
        {"fact": "facts"},
        {"fact": "facts", "summary": "summaries", "other": "other"},
        {"fact": "same", "summary": "same"},
        {"fact": "", "summary": "summaries"},
        {"fact": "../facts", "summary": "summaries"},
        {"fact": "facts?token=x", "summary": "summaries"},
        {"fact": "facts#fragment", "summary": "summaries"},
        {"fact": "facts/other", "summary": "summaries"},
        {"fact": "facts%2Fother", "summary": "summaries"},
        {"fact": " facts", "summary": "summaries"},
        {"fact": "x" * 129, "summary": "summaries"},
        {"fact": 1, "summary": "summaries"},
        ["facts", "summaries"],
    ],
)
def test_operator_bank_ids_reject_invalid_configuration_without_io(endpoint, bank_ids):
    from herdr_engineering.control_plane.assistant_hindsight import AssistantHindsight

    server, transport = endpoint
    with pytest.raises(ValueError, match="bank_ids"):
        AssistantHindsight(("t", "u", "a"), transport, bank_ids=bank_ids)
    assert server.calls == []
