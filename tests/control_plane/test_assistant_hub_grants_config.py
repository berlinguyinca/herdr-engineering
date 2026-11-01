"""``personal_assistant.grants`` config normalization."""
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant import assistant_config


def _grants(grants: Any = None) -> dict[str, Any]:
    pa: dict[str, Any] = {} if grants is None else {"grants": grants}
    return assistant_config({"personal_assistant": pa})["grants"]


def test_grants_defaults() -> None:
    assert _grants() == {
        "shadow_days": 14,
        "shadow_started_at": None,
        "required_approvals": {"local_reversible": 5, "remote_reversible": 10, "outward": 30},
        "min_sessions": 3,
        "min_tasks": 2,
        "grant_ttl_days": 30,
        "extra_vetoes": [],
        "policy_version": 1,
    }
    assert assistant_config({})["grants"] == _grants()


def test_grants_overrides() -> None:
    g = _grants({"shadow_days": 7, "shadow_started_at": "2026-10-01T00:00:00+00:00", "min_sessions": 5,
                 "required_approvals": {"outward": 50}, "policy_version": 4,
                 "extra_vetoes": [{"code": "no_rm", "reason": "never rm", "argv_contains": "rm -rf"},
                                  {"code": "no_prod", "reason": "hold", "verb": "deploy.prod"}]})
    assert g["shadow_days"] == 7 and g["min_sessions"] == 5 and g["policy_version"] == 4
    assert g["required_approvals"] == {"local_reversible": 5, "remote_reversible": 10, "outward": 50}
    assert g["extra_vetoes"][1] == {"code": "no_prod", "reason": "hold", "verb": "deploy.prod"}


def test_grants_unknown_key_rejected() -> None:
    with pytest.raises(ValueError, match=r"personal_assistant\.grants has unknown keys: \['bogus'\]"):
        _grants({"bogus": 1})


def test_grants_non_mapping_rejected() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        _grants(["x"])


@pytest.mark.parametrize("key", ["shadow_days", "min_sessions", "min_tasks", "grant_ttl_days", "policy_version"])
@pytest.mark.parametrize("bad", [True, 0, -1, "3", 1.5])
def test_grants_ints_must_be_real_positive(key: str, bad: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _grants({key: bad})


def test_required_approvals_validated() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _grants({"required_approvals": {"outward": 0}})
    with pytest.raises(ValueError, match="unknown keys"):
        _grants({"required_approvals": {"nope": 3}})
    with pytest.raises(ValueError, match="must be a mapping"):
        _grants({"required_approvals": 3})


@pytest.mark.parametrize("bad", ["yesterday", 123, True])
def test_shadow_started_at_must_be_iso_or_null(bad: Any) -> None:
    with pytest.raises(ValueError, match="shadow_started_at"):
        _grants({"shadow_started_at": bad})


@pytest.mark.parametrize("entry", [
    "str",
    {"reason": "r", "verb": "git.push"},                                   # no code
    {"code": "c", "verb": "git.push"},                                     # no reason
    {"code": "c", "reason": "r"},                                          # neither verb nor argv_contains
    {"code": "", "reason": "r", "verb": "git.push"},
    {"code": "c" * 65, "reason": "r", "verb": "git.push"},
    {"code": "c", "reason": "r" * 201, "verb": "git.push"},
    {"code": "c", "reason": "r", "verb": "not.a.verb"},
    {"code": "c", "reason": "r", "argv_contains": ""},
    {"code": "c", "reason": "r", "argv_contains": "x" * 201},
    {"code": "c", "reason": "r", "verb": "git.push", "extra": 1},
    {"code": "force_push", "reason": "r", "verb": "git.push"},             # fixed code reuse
])
def test_bad_extra_vetoes_rejected(entry: Any) -> None:
    with pytest.raises(ValueError, match="extra_vetoes"):
        _grants({"extra_vetoes": [entry]})


def test_extra_vetoes_must_be_list() -> None:
    with pytest.raises(ValueError, match="extra_vetoes"):
        _grants({"extra_vetoes": {"code": "c"}})


def test_naive_shadow_started_at_rejected() -> None:
    with pytest.raises(ValueError, match="must include a UTC offset"):
        _grants({"shadow_started_at": "2026-10-01T00:00:00"})
    assert _grants({"shadow_started_at": "2026-10-01T00:00:00Z"})["shadow_started_at"]
