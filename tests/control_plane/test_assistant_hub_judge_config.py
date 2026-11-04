"""``personal_assistant.judge`` config normalization and ``judge_version``."""
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant import assistant_config
from herdr_engineering.control_plane.assistant_hub import judge_version as jv


def _judge(judge: Any = None) -> dict[str, Any]:
    pa: dict[str, Any] = {} if judge is None else {"judge": judge}
    return assistant_config({"personal_assistant": pa})["judge"]


def _on(**extra: Any) -> dict[str, Any]:
    return {"enabled": True, "endpoint": "https://jev.example.org/v1", "api_key_file": "/etc/jev.key", **extra}


def test_judge_defaults() -> None:
    assert _judge() == {
        "enabled": False, "provider": "jev", "endpoint": "", "model": "laya-auto", "api_key_file": "",
        "timeout_seconds": 5, "fail_mode": "closed", "backend_confirmed": False,
        "thresholds": {"destructive": 0.30, "irreversible": 0.30, "severity_score": 2.0},
        "calibration": {"min_labeled_decisions": 200, "max_false_negative_bound": 0.05},
    }
    assert assistant_config({})["judge"] == _judge()


def test_judge_overrides() -> None:
    j = _judge(_on(model="m2", timeout_seconds=9, backend_confirmed=True,
                   thresholds={"destructive": 0.5, "severity_score": 3},
                   calibration={"min_labeled_decisions": 20, "max_false_negative_bound": 0.5}))
    assert j["enabled"] is True and j["model"] == "m2" and j["timeout_seconds"] == 9 and j["backend_confirmed"] is True
    assert j["thresholds"] == {"destructive": 0.5, "irreversible": 0.30, "severity_score": 3.0}
    assert j["calibration"] == {"min_labeled_decisions": 20, "max_false_negative_bound": 0.5}


def test_judge_loopback_http_allowed() -> None:
    for host in ("127.0.0.1", "localhost", "[::1]"):
        assert _judge(_on(endpoint=f"http://{host}:8080/v1"))["enabled"] is True


def test_judge_unknown_key_and_non_mapping() -> None:
    with pytest.raises(ValueError, match=r"personal_assistant\.judge has unknown keys: \['bogus'\]"):
        _judge({"bogus": 1})
    with pytest.raises(ValueError, match="must be a mapping"):
        _judge([1])
    with pytest.raises(ValueError, match="thresholds has unknown keys"):
        _judge({"thresholds": {"x": 0.1}})
    with pytest.raises(ValueError, match="calibration has unknown keys"):
        _judge({"calibration": {"x": 1}})


@pytest.mark.parametrize("name", ["api_key", "key", "token", "secret"])
def test_judge_inline_secret_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="api_key_file"):
        _judge({name: "abc"})


@pytest.mark.parametrize("bad", [
    {"enabled": "true"}, {"enabled": 1}, {"backend_confirmed": 1}, {"provider": "other"}, {"fail_mode": "open"},
    {"timeout_seconds": True}, {"timeout_seconds": 0}, {"timeout_seconds": 61}, {"timeout_seconds": 1.5},
    {"model": ""}, {"model": 3},
    {"thresholds": {"destructive": 0}}, {"thresholds": {"destructive": 1.1}}, {"thresholds": {"irreversible": True}},
    {"thresholds": {"severity_score": -0.1}}, {"thresholds": {"severity_score": 3.1}}, {"thresholds": [1]},
    {"calibration": {"min_labeled_decisions": 19}}, {"calibration": {"min_labeled_decisions": True}},
    {"thresholds": {"destructive": float("nan")}}, {"thresholds": {"severity_score": float("inf")}},
    {"thresholds": {"irreversible": float("-inf")}}, {"calibration": {"max_false_negative_bound": float("nan")}},
    {"calibration": [1]}, {"endpoint": 5}, {"api_key_file": 5},
    {"calibration": {"max_false_negative_bound": 0}}, {"calibration": {"max_false_negative_bound": 0.51}},
])
def test_judge_bad_values_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"personal_assistant\.judge"):
        _judge(bad)


@pytest.mark.parametrize("override", [
    {"endpoint": ""}, {"endpoint": "http://jev.example.org/v1"}, {"endpoint": "https://u:p@jev.example.org/v1"},
    {"endpoint": "https://jev.example.org/v1?x=1"}, {"endpoint": "https://jev.example.org/v1#f"},
    {"endpoint": "https://jev.example.org/v1?"}, {"endpoint": "https://jev.example.org/v1#"},
    {"endpoint": "https://jev.example.org/v 1"}, {"endpoint": "https://jev.example.org/v1\n"[:-2] + "\t/x"},
    {"endpoint": 5}, {"endpoint": "ftp://jev.example.org"}, {"endpoint": "https://"}, {"api_key_file": ""},
    {"api_key_file": "rel/key"},
])
def test_judge_enabled_requires_safe_endpoint_and_key_file(override: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"personal_assistant\.judge"):
        _judge(_on(**override))


def test_judge_disabled_does_not_require_endpoint() -> None:
    assert _judge({"enabled": False})["endpoint"] == ""


def test_judge_version_stable_and_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _judge()
    v = jv.judge_version(base)
    assert len(v) == 16 and int(v, 16) >= 0
    assert jv.judge_version(_judge()) == v
    assert jv.judge_version(_judge({"timeout_seconds": 9, "endpoint": ""})) == v
    assert jv.judge_version(_judge({"model": "other"})) != v
    for k in ("destructive", "irreversible", "severity_score"):
        assert jv.judge_version(_judge({"thresholds": {k: 0.1}})) != v
    monkeypatch.setattr(jv, "QUESTION_SET_VERSION", jv.QUESTION_SET_VERSION + 1)
    assert jv.judge_version(base) != v


def test_judge_endpoint_scheme_lowercased_and_zero_normalised() -> None:
    assert _judge(_on(endpoint="HTTPS://jev.example.org/v1"))["endpoint"] == "https://jev.example.org/v1"
    t = _judge({"thresholds": {"severity_score": -0.0}})["thresholds"]["severity_score"]
    assert str(t) == "0.0"


def test_judge_version_ignores_endpoint_and_calibration_and_int_float() -> None:
    v = jv.judge_version(_judge())
    other = _judge({"calibration": {"min_labeled_decisions": 50}, "backend_confirmed": True})
    assert jv.judge_version(other) == v
    assert jv.judge_version(_judge(_on(endpoint="https://other.example.org"))) == v
    assert jv.judge_version({"model": "laya-auto", "thresholds": {"destructive": 0.3, "irreversible": 0.3,
                                                                  "severity_score": 2}}) == v
