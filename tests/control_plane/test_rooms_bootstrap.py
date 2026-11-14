"""Pure logic of deploy/control-plane/rooms-bootstrap.py (no mocks)."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "rooms_bootstrap", Path(__file__).resolve().parents[2] / "deploy" / "control-plane" / "rooms-bootstrap.py")
rb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rb)


def test_names_are_valid_collab_handles():
    assert all(rb.valid_agent_name(n) for n in rb.AGENT_NAMES)
    assert not rb.valid_agent_name("Bad_Name") and not rb.valid_agent_name("a" * 33)
    assert len(rb.AGENT_NAMES) >= rb.GROUP_MEMBER_MIN


def test_plan_empty_then_idempotent():
    p = rb.plan([], [], None)
    assert p == {"agents": list(rb.AGENT_NAMES), "room": True, "members": list(rb.AGENT_NAMES)}
    agents = [{"name": n, "status": "active"} for n in rb.AGENT_NAMES]
    done = rb.plan(agents, [{"name": rb.ROOM_NAME}], [{"name": n} for n in rb.AGENT_NAMES])
    assert rb.is_noop(done)


def test_plan_ignores_revoked_agents_and_partial_members():
    agents = [{"name": "tui-smoke-a", "status": "revoked"}, {"name": "tui-smoke-b", "status": "active"}]
    p = rb.plan(agents, [{"name": rb.ROOM_NAME}], [{"name": "tui-smoke-b"}])
    assert p == {"agents": ["tui-smoke-a"], "room": False, "members": ["tui-smoke-a"]}


def test_redact_hides_secrets():
    out = rb.redact('cred hca_abcdefgh12345 Bearer xyz postgres://u:p@h/db "token": "s3cret"')
    assert "abcdefgh" not in out and "xyz" not in out and "u:p@h" not in out and "s3cret" not in out
