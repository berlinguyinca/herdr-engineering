# ruff: noqa: F811  (pytest fixtures imported from the bridge suite are re-declared as test arguments)
"""Agent skills, and proof that a bot's instructions, skills and model reach the model runner's request."""
import threading

from herdr_engineering.control_plane import rakazo_skills as rs
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_rakazo_bridge import (  # noqa: F401
    Api,
    alice,
    bridge_section,
    make_server,
    new_bot,
    send,
    srv,
    wait_idle,
)
from tests.control_plane.test_rakazo_contract import validate


def rpc(api, proc, inp, token):
    return api.rpc(proc, inp, token)[0]


def test_skill_md_round_trip():
    text = rs.render("Triage", "Sort the inbox", "1. read\n2. sort")
    assert rs.parse(text) == ("Triage", "Sort the inbox", "1. read\n2. sort")
    assert rs.parse("no frontmatter") == ("", "", "no frontmatter")


def test_crud_and_hide(srv, alice):
    _, api = srv
    made = api.ok("agentSkills/create", {"name": "Triage", "description": "Sort mail", "body": "do it"}, alice)
    validate("agentSkills/create", made)
    assert made["content"].startswith("---\nname: Triage")
    from_md = api.ok("agentSkills/create", {"content": "---\nname: Plan\ndescription: Plan the day\n---\nsteps"}, alice)
    assert from_md["name"] == "Plan"
    listing = api.ok("agentSkills/list", {}, alice)
    validate("agentSkills/list", listing)
    assert [s["name"] for s in listing] == ["Plan", "Triage"]
    got = api.ok("agentSkills/get", {"name": "triage"}, alice)
    validate("agentSkills/get", got)
    out = api.ok("agentSkills/update", {"skillId": made["id"], "body": "better"}, alice)
    validate("agentSkills/update", out)
    assert "better" in out["content"]
    assert rpc(api, "agentSkills/create", {"name": "TRIAGE", "description": "d", "body": ""}, alice) == 400
    assert rpc(api, "agentSkills/create", {"content": "no frontmatter"}, alice) == 400
    validate("agentSkills/remove", api.ok("agentSkills/remove", {"skillId": made["id"]}, alice))
    assert [s["name"] for s in api.ok("agentSkills/list", {}, alice)] == ["Plan"]
    assert rpc(api, "agentSkills/get", {"skillId": made["id"]}, alice) == 404


def test_instructions_skills_and_model_reach_the_model_request():
    seen = []

    def urlopen(url, payload, timeout=0):
        seen.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "inference": {"endpoint": "http://x.invalid/v1/chat/completions", "model": "base"}}}))
    svc.runner._urlopen = urlopen
    server = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc, rakazo_bridge=bridge_section())
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        api = Api(server.port)
        token = api.login()
        bot = new_bot(api, token)
        api.ok("agentSkills/create", {"name": "Triage", "description": "Sort mail", "body": "always label first"}, token)
        api.ok("bots/update", {"botId": bot["id"], "modelId": "qwen-27b"}, token)
        send(api, token, bot["id"], "hello")
        wait_idle(api, token, bot["id"])
        payload = seen[-1]
        assert payload["model"] == "qwen-27b"
        system = payload["messages"][0]
        assert system["role"] == "system" and "be brief" in system["content"]
        assert "Triage: Sort mail" in system["content"] and "always label first" in system["content"]
        # another space's skills are not in this space's prompt
        other = api.ok("spaces/create", {"name": "Other"}, token)
        status, body, _ = api.rpc("agentSkills/list", {}, token, **{"x-rakazo-space-id": other["id"]})
        assert status == 200 and body["json"] == []
    finally:
        server.shutdown()
