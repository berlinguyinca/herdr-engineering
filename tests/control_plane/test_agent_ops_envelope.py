"""Agent Ops envelope extension: optional provenance/session/model fields on envelope v2."""
import unicodedata

import pytest

from herdr_engineering.control_plane.envelope import (
    AGENT_OPS_PROVENANCE,
    normalize_envelope,
    sanitize_label,
)
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo

PRINCIPAL = {"kind": "agent", "id": "key:abc", "authenticated_by": "api_key"}
NOW = "2026-11-12T10:00:00+00:00"


def body(**extra):
    return {"schema_version": 2, "event_id": "e1", "source": "pi", "source_sequence": 0,
            "entity_type": "lifecycle", "entity_id": "a1", "event_type": "model.switched",
            "occurred_at": "2026-11-12T10:00:00Z", "payload": {}, **extra}


def test_existing_envelope_is_unchanged_and_has_no_agent_ops_block():
    env = normalize_envelope(body(), PRINCIPAL, NOW)
    assert "agent_ops" not in env


def test_existing_content_hash_is_stable_without_new_fields():
    a = normalize_envelope(body(), PRINCIPAL, NOW)
    b = normalize_envelope(body(provenance=None), PRINCIPAL, NOW)
    assert a["content_hash"] == b["content_hash"]


def test_optional_fields_are_carried_and_provenance_defaults_to_unknown():
    env = normalize_envelope(body(host_id="h1", session_id="s1", model_id="m1", parent_session_id="s0",
                                  handoff_id="ho1", status="running"), PRINCIPAL, NOW)
    ops = env["agent_ops"]
    assert ops["provenance"] == "unknown"
    assert (ops["host_id"], ops["session_id"], ops["model_id"], ops["parent_session_id"],
            ops["handoff_id"], ops["status"]) == ("h1", "s1", "m1", "s0", "ho1", "running")


@pytest.mark.parametrize("value", sorted(AGENT_OPS_PROVENANCE))
def test_each_provenance_value_is_accepted(value):
    assert normalize_envelope(body(provenance=value), PRINCIPAL, NOW)["agent_ops"]["provenance"] == value


@pytest.mark.parametrize("value", ["guess", "", 3])
def test_invalid_provenance_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_envelope(body(provenance=value), PRINCIPAL, NOW)


def test_new_fields_change_the_content_hash():
    plain = normalize_envelope(body(), PRINCIPAL, NOW)
    rich = normalize_envelope(body(model_id="m1"), PRINCIPAL, NOW)
    assert plain["content_hash"] != rich["content_hash"]


@pytest.mark.parametrize("raw", [
    "editing /Users/alice/secret/project/app.py now",
    "reading ~/.ssh/id_rsa",
    r"opening C:\Users\bob\notes.txt",
    "token sk-abcdefghijklmnopqrstuvwx used",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked",
    "Authorization: Bearer abc.def.ghi",
    "password=hunter2 and more",
    "AKIAABCDEFGHIJKLMNOP key",
])
def test_label_is_sanitized(raw):
    out = sanitize_label(raw)
    for leak in ("/Users", "alice", ".ssh", "C:\\", "bob", "sk-abc", "ghp_", "abc.def", "hunter2", "AKIA"):
        assert leak not in out


def test_label_is_truncated_and_keeps_plain_text():
    assert sanitize_label("running unit tests") == "running unit tests"
    assert len(sanitize_label("x" * 500)) <= 80


def test_label_must_be_string():
    with pytest.raises(ValueError):
        normalize_envelope(body(sanitized_label=5), PRINCIPAL, NOW)


def test_ingest_persists_sanitized_label_and_fields_in_envelope_metadata():
    repo = ControlPlaneRepo()
    IngestService(repo).ingest(body(model_id="m1", provenance="observed",
                                    sanitized_label="open /home/x/y.py"), PRINCIPAL)
    (event,) = repo.stream("lifecycle", "a1")
    ops = event["payload"]["_envelope"]["agent_ops"]
    assert ops["model_id"] == "m1" and ops["provenance"] == "observed"
    assert "/home" not in ops["sanitized_label"]


ADVERSARIAL = [
    "to​ken=abcd1234 leaked",                                   # zero-width inside a keyword
    "Bearer‮ abcdefghijklmnop",                                 # bidi override
    "key eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig-nature_ok",
    "glpat-abcdefghij0123456789 here",
    "the password is hunter2 ok",
    "see src/app/secret_module.py please",
    "mail bob.smith@example.com now",
    "line\nbreak /etc/shadow",
    "\x1b[31mred\x1b[0m ghp_abcdefghijklmnopqrstuvwxyz0123456789",
]


@pytest.mark.parametrize("raw", ADVERSARIAL)
def test_label_adversarial_corpus(raw):
    out = sanitize_label(raw)
    for leak in ("abcd1234", "abcdefghijklmnop", "eyJ", "glpat", "hunter2", "secret_module", "src/app", "bob.smith",
                 "example.com", "/etc", "ghp_", "\x1b", "\n", "​", "‮"):
        assert leak not in out
    assert all(unicodedata.category(c) not in ("Cc", "Cf") for c in out)


@pytest.mark.parametrize("field,value", [
    ("event_id", "a\r\nb"), ("event_id", "a b"), ("agent_id", "a/b"), ("agent_id", "x" * 161), ("task_id", "t\n1"),
    ("room_id", "r 1"), ("session_id", "s\x00"), ("host_id", "h/1"), ("handoff_id", "ho\n"), ("model_id", "m 1"),
])
def test_agent_ops_ids_are_constrained(field, value):
    with pytest.raises(ValueError):
        normalize_envelope(body(provenance="observed", **{field: value}), PRINCIPAL, NOW)


def test_constraints_do_not_apply_to_events_without_agent_ops_fields():
    assert "agent_ops" not in normalize_envelope(body(event_id="a b/c", agent_id="x/y"), PRINCIPAL, NOW)


@pytest.mark.parametrize("raw", ["use claude-opus-4/5 model", "fix CI/CD now", "read/write lock", "run 3/4 shards",
                                 "review auth/session flow", "the token was rotated"])
def test_label_keeps_ordinary_text(raw):
    assert sanitize_label(raw) == raw


@pytest.mark.parametrize("raw,leak", [
    ("api key: hunter2", "hunter2"), ("pass=hunter2", "hunter2"), ("PASSWORD=hunter2", "hunter2"),
    ("eyJhbGciOiJIUzI1NiJ9", "eyJ"), ("ghp‐ABCDEFGHIJKLMNOPQRST1234", "ABCDEFGHIJ"),
    ("sk＿ABCDEFGHIJKL", "ABCDEFGHIJ"), ("token = abc123", "abc123"),
    ("edit src/app/main.py", "src/app"), ("see docs/readme.md", "readme"), ("a/b/c here", "a/b/c"),
])
def test_label_hardened_redactions(raw, leak):
    assert leak not in sanitize_label(raw)


@pytest.mark.parametrize("raw", ["tests passed: 42", "bypass is enabled", "compass: north", "passport: check",
                                 "deadline 11/12/2026", "pass rate is 90%", "token: count 5"[:0] + "ratio 1/2/3"])
def test_label_short_forms_and_dates_are_not_redacted(raw):
    assert sanitize_label(raw) == raw


@pytest.mark.parametrize("raw,leak", [
    ("my secret was hunter2", "hunter2"), ("Authorization: Basic dXNlcjpwYXNzd29yZA", "dXNlcjpwYXNzd29yZA"),
    ("password -> hunter2", "hunter2"), ("pwd hunter2", "hunter2"), ("PASS = hunter2", "hunter2"),
    ("db_password: hunter2", "hunter2"), ("user_pwd=hunter2", "hunter2"),
])
def test_label_secret_forms_still_redacted(raw, leak):
    assert leak not in sanitize_label(raw)


@pytest.mark.parametrize("raw", ["pass" * 25000, "a." * 50000, "x" * 513])
def test_oversized_label_is_rejected_quickly_before_any_regex(raw):
    import time
    t = time.perf_counter()
    with pytest.raises(ValueError):
        normalize_envelope(body(provenance="observed", sanitized_label=raw), PRINCIPAL, NOW)
    assert time.perf_counter() - t < 0.5


def test_label_at_the_cap_is_accepted_and_direct_call_is_bounded():
    import time
    assert normalize_envelope(body(provenance="observed", sanitized_label="y" * 512), PRINCIPAL, NOW)
    t = time.perf_counter()
    assert len(sanitize_label("a." * 50000)) <= 80
    assert time.perf_counter() - t < 1.0
