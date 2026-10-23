"""Projection guard integration with the installed native SDK; no session is opened."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from herdr_engineering import pi_recovery_host as host


def sdk_proof():
    try:
        return host.trusted_sdk(Path.home(), os.getpid())
    except (OSError, ValueError):
        pytest.skip("owned native Pi SDK required for canonical projection integration")


def fixture(tmp_path, monkeypatch, entries):
    proof = sdk_proof()
    monkeypatch.setattr(host, "trusted_sdk", lambda *_: proof)
    directory = tmp_path / ".pi/agent/sessions/repo"
    directory.mkdir(parents=True)
    session = "00000000-0000-0000-0000-000000000001"
    path = directory / ("timestamp_" + session + ".jsonl")
    header = {"type": "session", "version": 3, "id": session, "cwd": str(tmp_path)}
    text = "\n".join(json.dumps(e) if isinstance(e, dict) else e for e in [header, *entries]) + "\n"
    path.write_text(text)
    return path, session, text.encode()


def project(tmp_path, monkeypatch, entries):
    path, session, data = fixture(tmp_path, monkeypatch, entries)
    before = path.read_bytes()
    result = host.canonical_projection(tmp_path, os.getpid(), path, session, data)
    assert path.read_bytes() == before
    assert (
        result.get("digest") == hashlib.sha256(before).hexdigest()
        or result.get("verified") is False
    )
    return result


def message(entry_id, parent, *, call=False):
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent,
        "message": {
            "role": "assistant",
            "provider": "metabolomics",
            "model": "working",
            "stopReason": "error",
            "errorMessage": "Stream ended without finish_reason",
            "content": [
                {
                    "type": "toolCall",
                    "id": "call1",
                    "name": "write",
                    "arguments": {"content": "PRIVATE CONTENT"},
                }
            ]
            if call
            else [],
        },
    }


def test_native_context_edit_removes_only_reconciled_call(tmp_path, monkeypatch):
    result = project(
        tmp_path,
        monkeypatch,
        [
            message("old", None, call=True),
            {
                "type": "context_edit",
                "id": "edit",
                "parentId": "old",
                "targetId": "old",
                "replacement": None,
            },
            message("latest", "edit"),
        ],
    )
    assert result["verified"] is True and result["pendingToolCalls"] == 0
    assert result["lastMessage"]["outage"] is True
    assert "PRIVATE" not in json.dumps(result)


def test_visible_unresolved_call_remains_a_blocker(tmp_path, monkeypatch):
    result = project(
        tmp_path, monkeypatch, [message("old", None, call=True), message("latest", "old")]
    )
    assert result["verified"] is True and result["pendingToolCalls"] == 1


def test_malformed_off_chain_line_is_accepted_only_with_complete_chain(tmp_path, monkeypatch):
    malformed = '{"type":"message","id":"offchain","parentId":"old","message":BROKEN}'
    result = project(
        tmp_path, monkeypatch, [message("old", None), malformed, message("latest", "old")]
    )
    assert result["verified"] is True and result["invalidHistoricalLines"] == 1


def test_malformed_active_parent_is_rejected(tmp_path, monkeypatch):
    malformed = '{"type":"message","id":"missing","parentId":"old","message":BROKEN}'
    result = project(
        tmp_path, monkeypatch, [message("old", None), malformed, message("latest", "missing")]
    )
    assert result["verified"] is False


def test_compaction_checkpoint_can_bound_a_missing_older_parent(tmp_path, monkeypatch):
    entries = [
        message("kept", "pruned-history"),
        {
            "type": "compaction",
            "id": "checkpoint",
            "parentId": "kept",
            "firstKeptEntryId": "kept",
            "tokensBefore": 100,
            "summary": "PRIVATE SUMMARY",
        },
        message("latest", "checkpoint"),
    ]
    result = project(tmp_path, monkeypatch, entries)
    assert result["verified"] is True and result["checkpointUsed"] is True
    assert "PRIVATE" not in json.dumps(result)


def test_missing_parent_without_checkpoint_is_rejected(tmp_path, monkeypatch):
    assert project(tmp_path, monkeypatch, [message("latest", "missing")])["verified"] is False


def test_changed_transcript_digest_is_rejected(tmp_path, monkeypatch):
    path, session, data = fixture(tmp_path, monkeypatch, [message("latest", None)])
    path.write_text(path.read_text() + json.dumps(message("changed", "latest")) + "\n")
    assert (
        host.canonical_projection(tmp_path, os.getpid(), path, session, data)["verified"] is False
    )


def test_inspect_uses_native_projection_for_reconciled_history(tmp_path, monkeypatch):
    entries = [
        message("old", None, call=True),
        {
            "type": "context_edit",
            "id": "edit",
            "parentId": "old",
            "targetId": "old",
            "replacement": None,
        },
        message("latest", "edit"),
    ]
    path, session, _ = fixture(tmp_path, monkeypatch, entries)
    (tmp_path / ".pi/agent/models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "metabolomics": {
                        "models": [
                            {
                                "id": "working",
                                "contextWindow": 262144,
                                "input": ["text"],
                                "reasoning": True,
                            }
                        ]
                    }
                }
            }
        )
    )
    metadata = host.inspect(tmp_path, os.getpid(), session, native_path=str(path))
    assert metadata["failure"] == "inference"
    assert metadata["canonicalProjection"]["verified"] is True
    assert metadata["requiredContext"] == 262144


def test_actual_assistant_model_supersedes_restored_alias_until_new_model_change():
    config = {
        "providers": {"metabolomics": {"models": [{"id": "working", "contextWindow": 262144}]}}
    }
    entries = [
        {"type": "session", "id": "s"},
        {
            "type": "model_change",
            "id": "model",
            "parentId": None,
            "provider": "development",
            "modelId": "old-alias",
        },
        message("latest", "model"),
    ]
    metadata = host.transcript_metadata(entries, config)
    assert metadata["model"] == "working" and metadata["requiredContext"] == 262144
    entries.append(
        {
            "type": "model_change",
            "id": "new-selection",
            "parentId": "latest",
            "provider": "development",
            "modelId": "new-choice",
        }
    )
    assert host.transcript_metadata(entries, config)["model"] == "new-choice"


def test_sdk_projection_cannot_write_even_if_dependency_attempts_to(tmp_path, monkeypatch):
    path, session, data = fixture(tmp_path, monkeypatch, [message("latest", None)])
    sdk = tmp_path / "bad-sdk.mjs"
    sdk.write_text(
        "import {writeFileSync} from 'node:fs'; export const CURRENT_SESSION_VERSION=3; export function parseSessionEntries(t){return t.trim().split('\\n').map(JSON.parse);} export class SessionManager{static inMemory(){writeFileSync("
        + json.dumps(str(path))
        + ", 'UNAUTHORIZED');throw Error('should never write');}}"
    )
    proof = {
        "module": str(sdk),
        "version": "test",
        "files": [{"path": str(sdk), "sha256": hashlib.sha256(sdk.read_bytes()).hexdigest()}],
    }
    monkeypatch.setattr(host, "trusted_sdk", lambda *_: proof)
    assert (
        host.canonical_projection(tmp_path, os.getpid(), path, session, data)["verified"] is False
    )
    assert path.read_bytes() == data


def test_unavailable_sdk_never_clears_an_unresolved_tool_guard(tmp_path, monkeypatch):
    path, session, _ = fixture(
        tmp_path, monkeypatch, [message("old", None, call=True), message("latest", "old")]
    )
    (tmp_path / ".pi/agent/models.json").write_text('{"providers":{}}')

    def unavailable(*args):
        raise ValueError("SDK custody unavailable")

    monkeypatch.setattr(host, "trusted_sdk", unavailable)
    metadata = host.inspect(tmp_path, os.getpid(), session, native_path=str(path))
    assert metadata["failure"] is None
    assert metadata["canonicalProjection"]["verified"] is False


def test_malformed_trailing_record_is_not_ignored(tmp_path, monkeypatch):
    result = project(
        tmp_path,
        monkeypatch,
        [
            message("old", None),
            '{"type":"message","id":"partial","parentId":"old","message":BROKEN}',
        ],
    )
    assert result["verified"] is False


def test_parent_cycle_is_rejected_before_native_projection(tmp_path, monkeypatch):
    assert (
        project(tmp_path, monkeypatch, [message("first", "second"), message("second", "first")])[
            "verified"
        ]
        is False
    )


def test_malformed_history_requires_native_projection_proof(tmp_path, monkeypatch):
    path, session, _ = fixture(
        tmp_path,
        monkeypatch,
        [
            message("old", None),
            '{"type":"message","id":"offchain","parentId":"old","message":BROKEN}',
            message("latest", "old"),
        ],
    )
    (tmp_path / ".pi/agent/models.json").write_text('{"providers":{}}')
    monkeypatch.setattr(
        host, "trusted_sdk", lambda *_: (_ for _ in ()).throw(ValueError("unavailable"))
    )
    with pytest.raises(ValueError, match="canonical projection unavailable"):
        host.inspect(tmp_path, os.getpid(), session, native_path=str(path))
