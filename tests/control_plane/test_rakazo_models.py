# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Rakazo models: the gateway's catalog read-only, and a bot's model choice as the assistant's model policy."""
import json

from herdr_engineering.control_plane import rakazo_models as rm
from tests.control_plane.test_rakazo_bridge import alice, make_server, new_bot, srv  # noqa: F401
from tests.control_plane.test_rakazo_contract import validate


def gateway(srv, models, calls=None):
    svr = srv[0]
    svr.rakazo.svc.config["inference"] = {"endpoint": "https://gw.example/v1/chat/completions", "api_key": "k"}

    def fetch(url, key, timeout):
        if calls is not None:
            calls.append((url, key))
        return json.dumps({"data": models}).encode()
    svr.rakazo.models._fetch = fetch


def test_models_url_is_derived_from_the_inference_endpoint():
    assert rm.models_url("https://gw/v1/chat/completions") == "https://gw/v1/models"
    assert rm.models_url("https://gw/v1") == "https://gw/v1/models"
    assert rm.models_url("") is None and rm.models_url("file:///x") is None


def test_catalog_lists_gateway_models_and_caches(srv, alice):
    _, api = srv
    calls: list = []
    gateway(srv, [{"id": "qwen-27b"}, {"id": "glm", "name": "GLM"}, {"nope": 1}], calls)
    out = api.ok("models/list", {}, alice)
    validate("models/list", out)
    assert [m["id"] for m in out] == ["qwen-27b", "glm"] and out[1]["label"] == "GLM"
    api.ok("models/list", {}, alice)
    assert calls == [("https://gw.example/v1/models", "k")]                   # cached, key only sent to the gateway
    validate("models/forRole", api.ok("models/forRole", {"role": "main"}, alice))
    validate("models/role", api.ok("models/role", {"role": "main"}, alice))
    validate("models/credentials", api.ok("models/credentials", {}, alice))
    assert api.rpc("models/role", {"role": "x"}, alice)[0] == 400


def test_an_unreachable_gateway_is_an_empty_catalog(srv, alice):
    _, api = srv
    srv[0].rakazo.svc.config["inference"] = {"endpoint": "https://gw.example/v1/chat/completions", "api_key": ""}

    def boom(url, key, timeout):
        raise OSError("down")
    srv[0].rakazo.models._fetch = boom
    assert api.ok("models/list", {}, alice) == []
    assert api.ok("models/forRole", {"role": "main"}, alice) == []


def test_a_bots_model_is_its_model_policy(srv, alice):
    svr, api = srv
    bot = new_bot(api, alice)
    assert bot["modelId"] is None
    out = api.ok("bots/update", {"botId": bot["id"], "modelId": "qwen-27b"}, alice)
    assert out["modelId"] == "qwen-27b"
    assert svr.rakazo.svc.store.assistants[bot["id"]].model_policy == "manual:qwen-27b"
    assert api.ok("bots/update", {"botId": bot["id"], "modelId": None}, alice)["modelId"] is None
    assert svr.rakazo.svc.store.assistants[bot["id"]].model_policy == "auto"
