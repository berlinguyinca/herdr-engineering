"""Audit parsing and real SQLite persistence; no substituted native processes."""

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_engineering.communications import Outbox, delivery_status, parse_prompt


@pytest.mark.parametrize(
    "args,expected",
    [
        (["agent", "prompt", "w1:p1", "hello"], ([], "w1:p1", "hello")),
        (
            ["--session", "work", "--machine", "mac", "agent", "prompt", "--", "w1:p1", "-literal"],
            (["--session", "work", "--machine", "mac"], "w1:p1", "-literal"),
        ),
        (["agent", "prompt", "w1:p1", "hello", "--wait"], ([], "w1:p1", "hello")),
        (
            ["agent", "prompt", "w1:p1", "-literal $(not shell)"],
            ([], "w1:p1", "-literal $(not shell)"),
        ),
        (
            ["--session=work", "agent", "prompt", "--wait", "--timeout", "100", "w1:p1", "hello"],
            (["--session=work"], "w1:p1", "hello"),
        ),
        (["--help"], None),
        (["pane", "send-text", "p", "text"], None),
        (["agent", "prompt", "--help"], None),
    ],
)
def test_parse_native_prompt(args, expected):
    assert parse_prompt(args) == expected


def test_receipts_are_truthful():
    receipt = {"result": {"type": "agent_prompted", "agent": {"pane_id": "w1:p1"}}}
    assert delivery_status(0, json.dumps(receipt).encode()) == "delivered"
    # A later --wait failure cannot undo an actual transport acknowledgement.
    assert delivery_status(1, json.dumps(receipt).encode()) == "delivered"
    assert delivery_status(0, b"not a receipt") == "unknown"
    assert delivery_status(0, b'{"ok":true}') == "unknown"
    assert delivery_status(0, b'{"error":{"code":"agent_blocked"}}') == "failed"
    assert delivery_status(0, b"[]") == "unknown"


def test_timeout_or_signal_is_not_proof_that_a_prompt_was_not_delivered():
    assert delivery_status(1, b"", b'{"error":{"code":"timeout"}}') == "unknown"
    assert delivery_status(-15, b"") == "unknown"
    assert delivery_status(1, b"", b'{"error":{"code":"agent_not_found"}}') == "failed"


def test_outbox_redacts_before_persisting_and_replays_phases(tmp_path):
    box = Outbox(tmp_path / "audit.sqlite")
    box.put("op-1", "queued", {"text": "Bearer topsecretvalue", "sender": {"pane_id": "w1:p1"}})
    box.put("op-1", "delivered", {"text": "Bearer topsecretvalue"})
    rows = box.pending()
    assert [r["status"] for r in rows] == ["queued", "delivered"]
    assert all("topsecretvalue" not in json.dumps(r) for r in rows)
    assert b"topsecretvalue" not in (tmp_path / "audit.sqlite").read_bytes()
    box.acknowledge(rows[0]["record_id"])
    assert len(box.pending()) == 1
    assert os.stat(tmp_path / "audit.sqlite").st_mode & 0o777 == 0o600


def test_secret_operation_identifier_is_never_persisted_raw(tmp_path):
    box = Outbox(tmp_path / "audit.sqlite")
    secret = "sk-this-is-a-credential-not-an-operation-id"
    box.put(secret, "queued", {"text": "safe"})
    assert secret not in json.dumps(box.pending())
    assert secret.encode() not in box.path.read_bytes()


def test_receipt_recipient_comes_from_the_actual_native_acknowledgement():
    from herdr_engineering.communications import receipt_recipient

    raw = json.dumps(
        {
            "result": {
                "type": "agent_prompted",
                "agent": {
                    "pane_id": "w2:p4",
                    "agent": "claude",
                    "agent_session": {
                        "source": "herdr:claude",
                        "kind": "id",
                        "agent": "claude",
                        "value": "new-session",
                    },
                },
            }
        }
    ).encode()
    assert receipt_recipient(raw)["agent_session"]["value"] == "new-session"
    assert receipt_recipient(b"not JSON") == {}
    assert receipt_recipient(b'{"result": "wrong"}') == {}
    assert receipt_recipient(b'{"result":{"agent":[]}}') == {}


@pytest.mark.parametrize(
    "args",
    [
        ["--session"],
        ["--session=x", "--help"],
        ["--unknown", "agent", "prompt", "x", "text"],
        ["agent", "prompt", "--timeout"],
        ["agent", "prompt", "one"],
        ["agent", "prompt", "one", "text", "extra"],
        ["agent", "prompt", "one", "--unknown", "text"],
    ],
)
def test_unrecognized_or_incomplete_cli_input_is_not_reinterpreted(args):
    assert parse_prompt(args) is None


def test_other_valid_native_prompt_option_forms():
    assert parse_prompt(
        ["--handoff", "agent", "prompt", "--until=idle", "--timeout=10", "one", "text"]
    ) == (["--handoff"], "one", "text")


def test_outbox_has_hard_admission_bounds_and_explicit_truncation(tmp_path):
    box = Outbox(tmp_path / "audit.sqlite", max_records=2, max_bytes=4096)
    box.put("1", "queued", {"text": "a" * 100})
    box.put("1", "queued", {"text": "a" * 100})
    box.put("2", "queued", {"text": "b" * 100})
    with pytest.raises(BufferError):
        box.put("3", "queued", {"text": "c"})
    assert len(box.pending()) == 2
    box.acknowledge(box.pending()[0]["record_id"])
    box.put("3", "queued", {"text": "c"})
    with pytest.raises(BufferError):
        Outbox(tmp_path / "small.sqlite", max_bytes=20).put("4", "queued", {"text": "a" * 100})


def test_concurrent_writers_do_not_exceed_limit(tmp_path):
    path = tmp_path / "audit.sqlite"
    Outbox(path, max_records=8)

    def write(n):
        try:
            Outbox(path, max_records=8).put(str(n), "queued", {"text": "message"})
            return True
        except BufferError:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(write, range(20))) == 8
    assert len(Outbox(path).pending()) == 8


def test_real_native_cli_refusal_smoke(tmp_path):
    """The real installed binary refuses an absent target; it sends no message."""
    from herdr_engineering.communications import run_native

    native = shutil.which("herdr")
    if not native:
        pytest.skip("Real HerdR CLI is not installed on this runner")
    box = Outbox(tmp_path / "audit.sqlite")
    code = run_native(
        native, ["agent", "prompt", "audit-target-does-not-exist", "literal cleanup audit"], box
    )
    assert code != 0
    assert box.pending()[-1]["status"] == "failed"
    assert box.pending()[-1]["text"] == "literal cleanup audit"


def test_installation_preserves_native_passthrough_and_records_refusal(tmp_path):
    from herdr_engineering.communications import install, main

    native = shutil.which("herdr")
    if not native:
        pytest.skip("Real HerdR CLI is not installed on this runner")
    target = tmp_path / "herdr"
    shutil.copy2(native, target)
    expected = subprocess.check_output([native, "--version"])
    config = tmp_path / "config.json"
    assert main(["install", "--native", str(target), "--config", str(config)]) == 0
    assert subprocess.check_output([target, "--version"]) == expected
    with pytest.raises(ValueError):
        install(target, config)
    with pytest.raises(ValueError):
        install(tmp_path / "not-an-executable", tmp_path / "other-config.json")
    cfg = json.loads(config.read_text())
    cfg["outbox"] = str(tmp_path / "audit.sqlite")
    config.write_text(json.dumps(cfg))
    run = subprocess.run(
        [target, "agent", "prompt", "audit-target-does-not-exist", "literal $(do not execute)"],
        capture_output=True,
    )
    assert run.returncode != 0
    assert Outbox(tmp_path / "audit.sqlite").pending()[-1]["text"] == "literal $(do not execute)"
    config.unlink()
    assert subprocess.check_output([target, "--version"]) == expected


def test_adapter_uses_the_real_native_literal_argument_grammar():
    from herdr_engineering.errors import InvalidError
    from herdr_engineering.herdr_adapter import NativeHerdrAdapter

    native = shutil.which("herdr")
    if not native:
        pytest.skip("Real HerdR CLI is not installed on this runner")
    with pytest.raises(InvalidError) as refusal:
        NativeHerdrAdapter(herdr_bin=native).send_input(
            "audit-target-does-not-exist", "-literal $(not shell)"
        )
    assert "usage:" not in str(refusal.value).lower()
    assert "unknown option" not in str(refusal.value).lower()


def test_unresolved_native_inventory_does_not_claim_an_observed_sender():
    from herdr_engineering.communications import _agent

    native = shutil.which("herdr")
    if not native:
        pytest.skip("Real HerdR CLI is not installed on this runner")
    assert _agent(native, [], "audit-target-does-not-exist") == {}


def test_capture_uses_the_enrolled_host_identity_when_configured(tmp_path):
    from herdr_engineering.communications import run_native

    native = shutil.which("herdr")
    if not native:
        pytest.skip("Real HerdR CLI is not installed on this runner")
    box = Outbox(tmp_path / "audit.sqlite")
    assert (
        run_native(
            native,
            ["agent", "prompt", "audit-target-does-not-exist", "hello"],
            box,
            host_id="mac.lan",
        )
        != 0
    )
    assert box.pending()[-1]["host_id"] == "mac.lan"


def test_failed_audit_storage_does_not_block_native_behavior(tmp_path):
    native = shutil.which("herdr")
    if not native:
        pytest.skip("Real HerdR CLI is not installed on this runner")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"native": native, "outbox": "/dev/null/impossible.sqlite"}))
    args = ["agent", "prompt", "audit-target-does-not-exist", "literal"]
    expected = subprocess.run([native, *args], capture_output=True)
    actual = subprocess.run(
        [
            sys.executable,
            "-m",
            "herdr_engineering.communications",
            "native",
            "--config",
            str(config),
            "--",
            *args,
        ],
        capture_output=True,
    )
    assert actual.returncode == expected.returncode
    assert actual.stdout == expected.stdout
    assert b"Traceback" not in actual.stderr
    assert b"audit" in actual.stderr


def test_large_messages_are_explicitly_truncated_and_symlinks_refused(tmp_path):
    box = Outbox(tmp_path / "audit.sqlite")
    box.put("large", "queued", {"text": "😀" * 10000})
    row = box.pending()[0]
    assert row["truncated"] is True
    assert len(row["text"].encode()) <= 16384
    linked = tmp_path / "linked.sqlite"
    linked.symlink_to(tmp_path / "audit.sqlite")
    with pytest.raises(OSError):
        Outbox(linked)


def test_main_status_and_native_paths_use_real_private_storage(tmp_path, capsys):
    from herdr_engineering.communications import main

    native = shutil.which("herdr")
    if not native:
        pytest.skip("Real HerdR CLI is not installed on this runner")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"native": native, "outbox": str(tmp_path / "audit.sqlite")}))
    assert main(["status", "--config", str(config)]) == 0
    assert '"pending": 0' in capsys.readouterr().out
    assert (
        main(
            [
                "native",
                "--config",
                str(config),
                "--",
                "agent",
                "prompt",
                "audit-target-does-not-exist",
                "test capture",
            ]
        )
        != 0
    )
    assert len(Outbox(tmp_path / "audit.sqlite").pending()) == 2
    assert main(["sync", "--config", str(config)]) != 0


def test_old_outbox_observations_expire_without_new_prompt_delivery(tmp_path):
    import sqlite3

    box = Outbox(tmp_path / "audit.sqlite")
    box.put("expired", "queued", {"text": "old"})
    with sqlite3.connect(box.path) as db:
        db.execute("UPDATE observations SET created=0")
    assert Outbox(box.path).pending() == []


def test_native_execution_failure_is_audited_and_storage_refusal_is_explicit(tmp_path, capsys):
    from herdr_engineering.communications import run_native

    box = Outbox(tmp_path / "audit.sqlite")
    with pytest.raises(FileNotFoundError):
        run_native(
            str(tmp_path / "no-such-executable"), ["agent", "prompt", "missing", "hello"], box
        )
    assert box.pending()[-1]["status"] == "failed"
    with pytest.raises(ValueError):
        box.put("bad", "completed", {"text": "unknown status"})
    native = shutil.which("herdr")
    if native:
        full = Outbox(tmp_path / "full.sqlite", max_records=0)
        assert (
            run_native(native, ["agent", "prompt", "audit-target-does-not-exist", "hello"], full)
            != 0
        )
        assert "could not be saved" in capsys.readouterr().err
        empty = Outbox(tmp_path / "empty.sqlite")
        assert run_native(native, ["--version"], empty) == 0
        assert empty.pending() == []


def test_offline_sync_retains_pending_observations(tmp_path):
    import socket

    from herdr_engineering.communications import sync

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    box = Outbox(tmp_path / "audit.sqlite")
    box.put("offline", "queued", {"text": "keep"})
    key = tmp_path / "key"
    key.write_text("test-ingestion-key")
    with pytest.raises(OSError):
        sync(box, f"http://127.0.0.1:{port}", key)
    assert len(box.pending()) == 1
    for url in ("file:///tmp/audit", "https://user:secret@example.test"):
        with pytest.raises(ValueError):
            sync(box, url, key)
