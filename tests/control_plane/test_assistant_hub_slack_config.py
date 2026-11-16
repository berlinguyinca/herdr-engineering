"""``personal_assistant.slack`` config normalization."""
from typing import Any

import pytest

from herdr_engineering.control_plane.assistant import assistant_config


def _slack(slack: Any = None) -> dict[str, Any]:
    pa: dict[str, Any] = {} if slack is None else {"slack": slack}
    return assistant_config({"personal_assistant": pa})["slack"]


def _on(**extra: Any) -> dict[str, Any]:
    return {"enabled": True, "bot_token_file": "/etc/slack/bot.tok", "app_token_file": "/etc/slack/app.tok",
            "owner_user_id": "U01ABC23DEF", **extra}


def test_defaults() -> None:
    assert _slack() == {"enabled": False, "base_url": "https://slack.com/api", "bot_token_file": "",
                        "app_token_file": "", "owner_user_id": "", "channel": "", "ask_ttl_minutes": 15,
                        "max_asks_per_task_per_hour": 6, "max_asks_per_worker_per_hour": 20,
                        "max_asks_per_hour": 60, "summary_idle_minutes": 30}
    assert assistant_config({})["slack"] == _slack()


def test_valid_block() -> None:
    s = _slack(_on(channel="C01ABC23DEF", ask_ttl_minutes=120, max_asks_per_task_per_hour=2))
    assert s["enabled"] is True and s["channel"] == "C01ABC23DEF" and s["ask_ttl_minutes"] == 120
    assert s["bot_token_file"] == "/etc/slack/bot.tok" and s["owner_user_id"] == "U01ABC23DEF"


def test_ask_caps_are_accepted() -> None:
    s = _slack(_on(max_asks_per_worker_per_hour=3, max_asks_per_hour=9))
    assert (s["max_asks_per_worker_per_hour"], s["max_asks_per_hour"]) == (3, 9)


def test_loopback_http_allowed() -> None:
    assert _slack(_on(base_url="http://127.0.0.1:9/api"))["base_url"] == "http://127.0.0.1:9/api"


def test_unknown_key_and_non_mapping() -> None:
    with pytest.raises(ValueError, match=r"personal_assistant\.slack has unknown keys: \['bogus'\]"):
        _slack({"bogus": 1})
    with pytest.raises(ValueError, match="must be a mapping"):
        _slack([1])


@pytest.mark.parametrize("name", ["bot_token", "app_token", "token", "secret"])
def test_inline_secret_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="bot_token_file"):
        _slack({name: "xoxb-abc"})


@pytest.mark.parametrize("bad", [
    {"enabled": "true"}, {"enabled": 1},
    {"bot_token_file": "relative.tok"}, {"app_token_file": "rel/app.tok"}, {"bot_token_file": 5},
    {"bot_token_file": "/a/same", "app_token_file": "/a/same"},
    {"base_url": "http://slack.example.org/api"}, {"base_url": "ftp://x/api"}, {"base_url": ""},
    {"base_url": "https://u:p@slack.com/api"}, {"base_url": 5},
    {"owner_user_id": "alice"}, {"owner_user_id": "u01abc"}, {"owner_user_id": 7},
    {"channel": "general"}, {"channel": "#general"},
    {"ask_ttl_minutes": 0}, {"ask_ttl_minutes": 121}, {"ask_ttl_minutes": True}, {"ask_ttl_minutes": 1.5},
    {"max_asks_per_task_per_hour": 0}, {"max_asks_per_task_per_hour": True},
    {"max_asks_per_worker_per_hour": 0}, {"max_asks_per_worker_per_hour": 1001}, {"max_asks_per_worker_per_hour": True},
    {"max_asks_per_hour": 0}, {"max_asks_per_hour": 10001}, {"max_asks_per_hour": "5"},
])
def test_bad_values_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"personal_assistant\.slack"):
        _slack(bad)


@pytest.mark.parametrize("missing", ["bot_token_file", "app_token_file", "owner_user_id"])
def test_enabled_requires_files_and_owner(missing: str) -> None:
    block = _on()
    del block[missing]
    with pytest.raises(ValueError, match=missing):
        _slack(block)
