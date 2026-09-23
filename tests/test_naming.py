"""Unit tests for automatic workspace naming (spec 0010)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.naming import (
    WorkspaceIdentity,
    WorkspaceNamer,
    derive_workspace_identity,
    ensure_collision_free,
    slugify,
)


def test_user_name_wins_and_is_never_overridden():
    ident = derive_workspace_identity(user_name="My Project!",
                                      spec_title="dev fabric")
    assert ident.name_source == "user"
    assert ident.name == "my-project"


def test_spec_identity_preserved():
    ident = derive_workspace_identity(spec_id="0110", spec_title="Dev Fabric")
    assert ident.name == "0110-dev-fabric"
    assert ident.name_source == "auto_spec"


def test_issue_identity_preserved():
    ident = derive_workspace_identity(issue_number="421", issue_title="Test Tree")
    assert ident.name == "421-test-tree"
    assert ident.name_source == "auto_issue"


def test_priority_order():
    # mission title loses to spec; prompt loses to mission
    a = derive_workspace_identity(mission_title="Mission A", prompt="some prompt")
    assert a.name_source == "auto_mission"
    b = derive_workspace_identity(spec_id="0010", mission_title="Mission A")
    assert b.name_source == "auto_spec"


def test_repo_short_task_fallback():
    ident = derive_workspace_identity(repository="herdr-engineering",
                                      short_task="fix doctor")
    assert ident.name.startswith("herdr-engineering-fix-doctor")
    assert ident.name_source == "auto_prompt"


def test_never_blocks_on_empty():
    ident = derive_workspace_identity()
    assert ident.name
    assert ident.name_source in ("auto_prompt", "auto_mission")


def test_collision_free():
    assert ensure_collision_free("x", set()) == "x"
    assert ensure_collision_free("x", {"x"}) == "x-2"
    assert ensure_collision_free("x", {"x", "x-2"}) == "x-3"


def test_user_rename_never_overwritten():
    namer = WorkspaceNamer()
    namer.register_user_rename("0110-dev-fabric", "my-renamed-ws")
    ident = WorkspaceIdentity(name="0110-dev-fabric", name_source="auto_spec")
    final = namer.apply(ident)
    assert final.name == "my-renamed-ws"
    assert final.name_source == "user"
    # refinement must not clobber a user rename
    stronger = WorkspaceIdentity(name="0110-dev-fabric-v2", name_source="auto_spec")
    out = namer.refine("0110-dev-fabric", stronger)
    assert out.name == "my-renamed-ws"


def test_at_most_one_refinement():
    namer = WorkspaceNamer()
    first = WorkspaceIdentity(name="ws-a", name_source="auto_prompt")
    stronger = WorkspaceIdentity(name="0110-dev", name_source="auto_spec")
    out1 = namer.refine(first.name, stronger)
    assert out1.refined is True
    out2 = namer.refine(first.name, WorkspaceIdentity(name="other", name_source="auto_issue"))
    assert out2.name == "0110-dev"  # second refinement ignored
    assert out2.refined is True


def test_slugify():
    assert slugify("Hello World!") == "hello-world"
    assert slugify("  ") == "untitled"
