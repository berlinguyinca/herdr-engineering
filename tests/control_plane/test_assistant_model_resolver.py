"""The assistant picks its model from what the gateway serves, and moves on when a model has no capacity."""
import json

import pytest

from herdr_engineering.control_plane.assistant import (
    AssistantService,
    AssistantStore,
    HttpModelRunnerAdapter,
    assistant_config,
)
from herdr_engineering.control_plane.inference_models import ModelResolver

URL = "http://llm:8088/v1/chat/completions"
SERVED = ["qwen3.8-27b-modality-vision-quant-q8_k_xl", "qwen3.8-flash-modality-text-quant-iq4_xs",
          "qwen3.8-flash_next-modality-text-quant-w3a8", "laya", "qwen3.8-text"]


def resolver(served=SERVED, preferred=(), clock=lambda: 0.0, calls=None):
    def fetch(url, key, timeout):
        if calls is not None:
            calls.append(url)
        if served is None:
            raise OSError("down")
        return json.dumps({"data": [{"id": m} for m in served]}).encode()
    return ModelResolver(URL, "k", list(preferred), fetch=fetch, clock=clock)


def test_a_model_the_gateway_serves_is_kept():
    assert resolver().candidates("laya")[0] == "laya"


def test_a_model_that_is_no_longer_served_gives_way_to_the_preferences_in_order():
    r = resolver(preferred=["qwen3.8-flash_next-*", "qwen3.8-text"])
    got = r.candidates("qwen3.8-flash_next-modality-text-quant-q4_k_xl")
    assert got[:2] == ["qwen3.8-flash_next-modality-text-quant-w3a8", "qwen3.8-text"]


def test_auto_without_preferences_skips_vision_models():
    got = resolver().candidates("auto")
    assert got[0] == "qwen3.8-flash-modality-text-quant-iq4_xs"
    assert "qwen3.8-27b-modality-vision-quant-q8_k_xl" not in got


def test_empty_model_means_auto():
    assert resolver(preferred=["laya"]).candidates("")[0] == "laya"


def test_an_unreachable_gateway_keeps_a_pinned_model_and_offers_nothing_for_auto():
    assert resolver(served=None).candidates("laya") == ["laya"]
    assert resolver(served=None).candidates("auto") == []


def test_the_catalog_is_cached_and_refreshed_after_the_ttl():
    now, calls = [0.0], []
    r = resolver(clock=lambda: now[0], calls=calls)
    r.candidates("laya"); r.candidates("laya")
    assert len(calls) == 1
    now[0] = 61.0
    r.candidates("laya")
    assert len(calls) == 2


class Busy(Exception):
    def __init__(self, code):
        self.code = code


def runner(model, responses, preferred=(), served=SERVED):
    seen = []

    def urlopen(endpoint, payload, timeout):
        seen.append(payload["model"])
        out = responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out
    r = HttpModelRunnerAdapter(AssistantStore(), endpoint=URL, model=model, urlopen=urlopen,
                               resolver=resolver(served, preferred))
    return r, seen


OK = {"choices": [{"message": {"content": "hi"}}]}


def test_a_429_moves_on_to_the_next_candidate():
    r, seen = runner("auto", [Busy(429), OK], preferred=["qwen3.8-flash_next-*", "laya"])
    assert r._post({"model": "auto", "messages": []}) == OK
    assert seen == ["qwen3.8-flash_next-modality-text-quant-w3a8", "laya"]


def test_an_auth_failure_is_not_retried_on_other_models():
    r, seen = runner("auto", [Busy(401), OK], preferred=["laya", "qwen3.8-text"])
    with pytest.raises(Busy):
        r._post({"model": "auto", "messages": []})
    assert len(seen) == 1


def test_every_candidate_busy_raises_the_last_error():
    r, seen = runner("laya", [Busy(429)] * 10, preferred=["qwen3.8-text"])
    with pytest.raises(Busy):
        r._post({"model": "laya", "messages": []})
    assert seen[0] == "laya" and len(seen) <= 4


def test_without_a_resolver_the_configured_model_is_sent_unchanged():
    seen = []
    r = HttpModelRunnerAdapter(AssistantStore(), endpoint=URL, model="m",
                               urlopen=lambda e, p, timeout: seen.append(p["model"]) or OK)
    r._post({"model": "m", "messages": []})
    assert seen == ["m"]


def test_the_service_builds_a_resolver_from_the_inference_section():
    cfg = assistant_config({"personal_assistant": {"enabled": True, "inference": {
        "endpoint": URL, "model": "auto", "preferred_models": ["laya"]}}})
    assert cfg["inference"]["preferred_models"] == ["laya"]
    svc = AssistantService(config=cfg, store=AssistantStore())
    assert svc.runner.resolver is not None
    with pytest.raises(ValueError, match="preferred_models"):
        assistant_config({"personal_assistant": {"inference": {"preferred_models": "laya"}}})


def test_a_bots_own_model_choice_is_never_substituted():
    r, seen = runner("laya", [Busy(404), OK], preferred=["qwen3.8-text"])
    with pytest.raises(Busy):
        r._post({"model": "some-manual-model", "messages": []})
    assert seen == ["some-manual-model"]


def test_auto_with_no_catalog_fails_clearly_instead_of_sending_auto():
    r, seen = runner("auto", [OK], served=None)
    with pytest.raises(RuntimeError, match="no model"):
        r._post({"model": "auto", "messages": []})
    assert seen == []


def test_a_failed_catalog_fetch_is_retried_soon():
    now, calls = [0.0], []
    r = resolver(served=None, clock=lambda: now[0], calls=calls)
    r.candidates("auto"); r.candidates("auto")
    assert len(calls) == 1
    now[0] = 6.0
    r.candidates("auto")
    assert len(calls) == 2


def test_an_empty_preferred_models_key_means_no_preferences():
    cfg = assistant_config({"personal_assistant": {"inference": {"endpoint": URL, "preferred_models": None}}})
    assert cfg["inference"]["preferred_models"] == []


@pytest.mark.parametrize("code", [500, 504])
def test_server_errors_move_on_to_the_next_model(code):
    r, seen = runner("auto", [Busy(code), OK], preferred=["laya", "qwen3.8-text"])
    assert r._post({"model": "auto", "messages": []}) == OK
    assert seen == ["laya", "qwen3.8-text"]


def test_a_timeout_or_reset_moves_on_to_the_next_model():
    r, seen = runner("auto", [TimeoutError("timed out"), OK], preferred=["laya", "qwen3.8-text"])
    assert r._post({"model": "auto", "messages": []}) == OK
    assert seen == ["laya", "qwen3.8-text"]


def test_a_final_failure_is_logged_with_its_cause(caplog):
    r, _ = runner("auto", [Busy(400)], preferred=["laya"])
    with caplog.at_level("WARNING"):
        assert "unavailable" in r.execute(_Run(), [])
    assert any("Busy" in m and "400" in m for m in caplog.messages)


class _Run:
    assistant_id = "asst_x"
    tenant_id = "t"
    user_id = "u"
