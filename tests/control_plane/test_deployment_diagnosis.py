"""Deployment diagnosis and repair decisions (deploy-4 Task 7).

Pattern evidence is taken from the InferWeave sources the hosts actually run:
``deploy/hive/iw-gateway-cycle`` (the follow preflight), ``crates/iw-cli/src/join.rs``
(the superseded-link refusal and the ``--gpu`` identity checks), ``container_driver.rs``
(``invalid device ordinal``) and curl ``-f`` against the ``/health`` probe URL.
"""
import json
import threading
import time

import pytest

from herdr_engineering.control_plane import deployment_diagnosis as dd
from herdr_engineering.control_plane.deployment_definitions import Definition
from herdr_engineering.control_plane.deployment_diagnosis import (
    ACTIONS,
    KINDS,
    DiagnosisConfig,
    Finding,
    diagnose,
    next_repair,
)

# Built at runtime: no scanner-shaped literal sits in this file.
FAKE_GH = "ghp_" + "0" * 36
FAKE_BEARER = "Bearer " + "z" * 32
FAKE_PASS = "pw" + "9" * 14

STALE_GPU = (
    "2026-10-27T09:14:02Z iw-node-run: starting join for job 25084057\n"
    'Error: --gpu "GPU-a1b2c3d4-e5f6-7890-abcd-ef0123456789" is not a card of this host; '
    'its cards are ["GPU-5e6f7081-92ab-cdef-3456-7890abcdef01"]\n'
    "iw-node-pull-deploy: health probe failed after restart\n")
STALE_GPU_IDS = (
    "join: --gpu-ids no longer matches the complete allocation-visible residency inventory; "
    "refusing partial or unknown GPU identity\n")
STALE_GPU_ORDINAL = "engine exited: cudaMalloc failed: invalid device ordinal\n"
CANNOT_FOLLOW = (
    "iw-gateway-cycle: preflight on hive-gateway (job 25079911)\n"
    "iw-gateway-cycle: node job(s) 25084057 25084102 do not follow a replacement gateway "
    "(no 'join: follows the gateway published at' in their log): the new gateway could not "
    "join upstream until the old one died. Wait for them to turn over, or pass --accept-gap "
    "to cycle with an outage.\n")
SUPERSEDED = (
    'link heartbeat refused: 401 {"error":{"code":"control_auth_failed","message":'
    '"control authentication failed: connection id mismatch"}}\n'
    "join: stands down when superseded (link session 100-1-00)\n")
MOVED_HEALTH = (
    "iw-rust-pull-deploy: waiting for health on 3f9c2e1\n"
    "curl: (22) The requested URL returned error: 404\n"
    "health probe http://127.0.0.1:8080/health -> 404 Not Found\n"
    "iw-rust-pull-deploy: new image not healthy within 120s\n")
UNKNOWN = "iw-node-pull-deploy: install failed: No space left on device while writing release\n"


def definition(restart="process-restart-in-allocation", rollback="previous-version", gpu=True):
    rb = {"strategy": rollback}
    if rollback == "none":
        rb["reason"] = "single binary, no local copy"
    return Definition(
        component="hive-nodes", stage=1, order=1, host="hive", updater="iw-node-pull-deploy",
        follows={"repo": "InferWeave/inferweave", "tag": "node-ci-latest", "asset": "inferweave",
                 "branch": "main", "ci_context": "ci/woodpecker/push/rust"},
        restart=restart, gpu=gpu,
        health={"probe": "served-request", "timeout_s": 300, "hold_s": 600}, rollback=rb)


class FakeClient:
    """Captures every payload; answers with ``reply`` (a str), or raises / sleeps."""

    def __init__(self, reply=None, *, exc=None, sleep=0.0):
        self.reply, self.exc, self.sleep = reply, exc, sleep
        self.calls = []

    def __call__(self, payload, *, timeout):
        self.calls.append((json.dumps(payload), timeout))
        if self.sleep:
            time.sleep(self.sleep)
        if self.exc:
            raise self.exc
        return self.reply


def ok_reply(kind="software", summary="the engine image lacks a dependency",
             action="rollback"):
    return json.dumps({"kind": kind, "summary": summary, "suggested_action": action})


def enabled(**kw):
    base = {"enabled": True, "endpoint": "http://127.0.0.1:8899/v1", "model": "qwen-27b",
            "timeout_s": 2}
    base.update(kw)
    return DiagnosisConfig(**base)


# ------------------------------------------------------------------ patterns
@pytest.mark.parametrize("text, pattern, kind, action", [
    (STALE_GPU, "stale-gpu-ids", "host", "none"),
    (STALE_GPU_IDS, "stale-gpu-ids", "host", "none"),
    (STALE_GPU_ORDINAL, "stale-gpu-ids", "host", "none"),
    (CANNOT_FOLLOW, "gateway-cannot-follow", "software", "rollback"),
    (SUPERSEDED, "link-superseded", "software", "retry"),
    (MOVED_HEALTH, "moved-health-url", "software", "none"),
])
def test_each_known_pattern_is_classified_without_a_model_call(text, pattern, kind, action):
    client = FakeClient(ok_reply())
    f = diagnose({"component": "hive-nodes", "step": "health", "outcome": "failed",
                  "from": "a" * 40, "to": "b" * 40, "evidence": text},
                 client=client, config=enabled())
    assert isinstance(f, Finding)
    assert (f.kind, f.suggested_action, f.source) == (kind, action, "pattern:" + pattern)
    assert f.summary
    assert client.calls == []


def test_a_plain_string_is_accepted_as_evidence():
    assert diagnose(SUPERSEDED).source == "pattern:link-superseded"


def test_the_pattern_table_names_all_four_spec_patterns():
    assert {p.name for p in dd.PATTERNS} == {
        "stale-gpu-ids", "gateway-cannot-follow", "link-superseded", "moved-health-url"}
    for p in dd.PATTERNS:
        assert p.kind in KINDS and p.action in ACTIONS


def test_a_healthy_health_line_is_not_a_moved_url():
    text = "health probe http://127.0.0.1:8080/health -> 200 ok\nmodel qwen returned 404 tokens\n"
    assert diagnose(text).source == "fallback"
    assert diagnose("health: served request failed: model qwen-27b not found\n").source == "fallback"


def test_patterns_are_linear_on_hostile_input():
    hostile = ("health " * 3000 + "\n") + ("--gpu " * 3000) + ("superseded " * 3000)
    t = time.monotonic()
    diagnose(hostile)
    assert time.monotonic() - t < 2


# --------------------------------------------------------------- AI fallback
def test_unknown_evidence_goes_to_the_model_once():
    client = FakeClient(ok_reply(kind="host", summary="disk full on hive scratch", action="none"))
    f = diagnose({"evidence": UNKNOWN}, client=client, config=enabled())
    assert len(client.calls) == 1
    assert (f.kind, f.summary, f.suggested_action, f.source) == (
        "host", "disk full on hive scratch", "none", "model")


def test_disabled_model_gives_the_deterministic_fallback():
    client = FakeClient(ok_reply())
    f = diagnose(UNKNOWN, client=client, config=DiagnosisConfig())
    assert client.calls == []
    assert (f.kind, f.suggested_action, f.source) == ("host", "none", "fallback")


def test_evidence_is_redacted_before_the_model_call():
    secret_text = (UNKNOWN + f"token={FAKE_GH}\nAuthorization: {FAKE_BEARER}\n"
                   f"curl -u deploy:{FAKE_PASS} https://example.invalid/x\n"
                   "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1yc2E\n"
                   "-----END OPENSSH PRIVATE KEY-----\n")
    client = FakeClient(ok_reply())
    diagnose({"evidence": secret_text, "health": f"x-api-key: {FAKE_PASS}"},
             client=client, config=enabled())
    sent = client.calls[0][0]
    for secret in (FAKE_GH, "z" * 32, FAKE_PASS, "AAAAB3NzaC1yc2E"):
        assert secret not in sent
    assert "[REDACTED]" in sent


def test_the_prompt_is_bounded_and_keeps_the_tail():
    huge = "x" * 500_000 + "\nTAIL-MARKER install failed\n"
    client = FakeClient(ok_reply())
    diagnose({"evidence": huge, "logs": "y" * 500_000}, client=client,
             config=enabled(max_evidence_chars=4000))
    sent, timeout = client.calls[0]
    assert len(sent) < 4000 + 3000
    assert "TAIL-MARKER" in sent
    assert timeout == 2


def test_the_payload_uses_the_configured_model_and_a_bounded_reply():
    client = FakeClient(ok_reply())
    diagnose(UNKNOWN, client=client, config=enabled(max_output_tokens=300))
    payload = json.loads(client.calls[0][0])
    assert payload["model"] == "qwen-27b"
    assert payload["max_tokens"] == 300
    assert payload["temperature"] == 0
    assert payload["stream"] is False


@pytest.mark.parametrize("reply", [
    ok_reply(action="scancel 25084057"),                         # not in the closed set
    ok_reply(action="release-gpu"),
    ok_reply(kind="network"),
    json.dumps({"kind": "software", "summary": "x", "suggested_action": "retry",
                "command": "rm -rf /"}),                         # extra key
    json.dumps({"kind": "software", "summary": "x"}),            # missing key
    json.dumps({"kind": "software", "summary": 7, "suggested_action": "retry"}),
    ok_reply(summary="s" * 5000),                                # oversized summary
    "x" * 200_000,                                               # oversized reply
    "not json at all",
    "",                                                          # a reasoning model out of tokens
    None,
    json.dumps(["software", "x", "retry"]),
    "```json\n" + ok_reply() + "\n```",                          # wrapped: refused, not repaired
])
def test_a_malicious_or_malformed_reply_is_rejected(reply):
    f = diagnose(UNKNOWN, client=FakeClient(reply), config=enabled())
    assert (f.kind, f.suggested_action, f.source) == ("host", "none", "fallback")


def test_a_model_summary_is_redacted_and_single_line():
    reply = ok_reply(summary=f"leaked token={FAKE_GH}\nsecond line\x1b[31m")
    f = diagnose(UNKNOWN, client=FakeClient(reply), config=enabled())
    assert f.source == "model"
    assert FAKE_GH not in f.summary
    assert "\n" not in f.summary and "\x1b" not in f.summary


def test_a_client_error_gives_the_fallback_without_leaking_it():
    f = diagnose(UNKNOWN, client=FakeClient(exc=RuntimeError(f"boom {FAKE_GH}")),
                 config=enabled())
    assert f.source == "fallback"
    assert FAKE_GH not in f.summary


def test_a_slow_model_is_cut_at_the_hard_timeout():
    release = threading.Event()

    def stuck(payload, *, timeout):
        release.wait(10)
        return ok_reply()

    t = time.monotonic()
    f = diagnose(UNKNOWN, client=stuck, config=enabled(timeout_s=0.3))
    elapsed = time.monotonic() - t
    release.set()
    assert f.source == "fallback"
    assert elapsed < 2


def test_diagnose_never_raises_on_odd_evidence():
    for ev in (None, 42, {"evidence": 42, "logs": ["x"]}, {}, ""):
        f = diagnose(ev)
        assert f.kind in KINDS and f.suggested_action in ACTIONS


# ------------------------------------------------------------------- config
@pytest.mark.parametrize("bad", [
    {"enabled": True, "endpoint": "", "model": "m"},
    {"enabled": True, "endpoint": "http://127.0.0.1:1/v1", "model": ""},
    {"endpoint": "ftp://127.0.0.1/v1"},
    {"endpoint": "http://user:pw@127.0.0.1/v1"},
    {"endpoint": "http://127.0.0.1/v1?key=x"},
    {"timeout_s": 0},
    {"timeout_s": 600},
    {"max_evidence_chars": 10_000_000},
    {"max_output_tokens": 100_000},
    {"enabled": "yes"},
    {"api_key": "has space"},
])
def test_invalid_config_is_refused(bad):
    with pytest.raises(ValueError):
        DiagnosisConfig(**bad)


def test_the_api_key_never_shows_in_repr():
    cfg = enabled(api_key=FAKE_PASS)
    assert FAKE_PASS not in repr(cfg)


def test_config_from_mapping_refuses_unknown_keys():
    with pytest.raises(ValueError):
        DiagnosisConfig.from_mapping({"enabled": False, "fallback_url": "https://x"})
    assert DiagnosisConfig.from_mapping(None) == DiagnosisConfig()


def test_the_proxy_client_refuses_redirects_and_caps_the_reply(monkeypatch):
    client = dd.ProxyDiagnosisClient(enabled(api_key="k" * 16))
    assert client.endpoint == "http://127.0.0.1:8899/v1/chat/completions"
    assert client.redirects_refused


# ------------------------------------------------------------------ repairs
def finding(action, kind="software"):
    return Finding(kind=kind, summary="s", suggested_action=action, source="pattern:x")


def test_retry_happens_once_then_climbs_the_ladder():
    d = definition()
    assert next_repair(finding("retry"), d, attempts=1).action == "retry"
    second = next_repair(finding("retry"), d, attempts=2)
    assert (second.action, second.restart) == ("restart", "process-restart-in-allocation")


def test_the_cap_is_respected_and_ends_rolled_back():
    d = definition()
    at_cap = next_repair(finding("retry"), d, attempts=dd.ATTEMPT_CAP)
    assert at_cap.action == "rollback"
    assert "cap" in at_cap.reason
    after = next_repair(finding("retry"), d, attempts=dd.ATTEMPT_CAP + 5, rolled_back=True)
    assert after.action == "none"
    assert next_repair(finding("restart"), d, attempts=1, cap=1).action == "rollback"


def test_rolled_back_stays_rolled_back():
    assert next_repair(finding("retry"), definition(), attempts=1,
                       rolled_back=True).action == "none"


def test_rollback_needs_a_previous_version():
    d = definition(rollback="none")
    assert next_repair(finding("rollback"), d, attempts=1).action == "none"
    assert next_repair(finding("retry"), d, attempts=dd.ATTEMPT_CAP).action == "none"


def test_none_suggestion_means_no_automatic_repair():
    assert next_repair(finding("none", "host"), definition(), attempts=1).action == "none"


def test_a_gpu_releasing_restart_is_never_used():
    d = definition(restart="scancel-resubmit")
    r = next_repair(finding("restart"), d, attempts=1)
    assert r.action == "rollback"
    assert r.restart is None


def test_repair_actions_never_name_a_gpu_release():
    for a in ACTIONS:
        assert "cancel" not in a and "gpu" not in a and "release" not in a
