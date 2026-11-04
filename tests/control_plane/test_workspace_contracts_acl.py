"""P01 ACL re-check hooks: traversal, search, export, subscription. A09-style no-leak checks.

Visibility is graph.can_see (the live fail-closed rule) over server-built member principals. Each no-leak
assertion is paired with a positive control for the authorized viewer (correct nodes, degrees, has_more)."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane import workspace_contracts as wc
from herdr_engineering.control_plane.graph import can_see

R1, R2 = ("room", "room-1", None, ()), ("room", "room-2", None, ())


def member(name, rooms):
    return {"kind": "member", "authenticated_by": "collab", "id": name,
            "access": {"rooms": set(rooms), "room_ws": set(), "room_missions": set()}}


ALICE, CAROL, STRANGER = member("alice", {"room-1", "room-2"}), member("carol", {"room-1"}), member("zed", set())
GUEST_ORG = {"kind": "member", "authenticated_by": "collab", "id": "g", "access": {"rooms": set(), "room_ws": set(), "room_missions": set()}}

NODES = {"n1": R1, "n2": R1, "n3": R1, "secret": R2, "org": "org"}
# edge id -> (a, b, scope); e_leak is room-1 scoped but leads to a node only room-2 members may see
EDGES = {"e12": ("n1", "n2", R1), "e13": ("n1", "n3", R1), "e_leak": ("n1", "secret", R1), "e_hid": ("n2", "n3", R2)}


def can_node(v, ref):
    return ref in NODES and can_see(v, NODES[ref], None)


def can_edge(v, ref):
    return ref in EDGES and can_see(v, EDGES[ref][2], None)


def neighbors(n):
    for eid, (a, b, _) in EDGES.items():
        if a == n:
            yield eid, b
        elif b == n:
            yield eid, a


def walk(v, **kw):
    return wc.traverse(v, "n1", neighbors, can_node, can_edge, **kw)


def test_traversal_filters_before_expansion_and_leaks_no_neighbors_or_counts():
    full = walk(ALICE)                                               # positive control: the privileged viewer sees everything reachable
    assert set(full["nodes"]) == {"n1", "n2", "n3", "secret"} and set(full["edges"]) == {"e12", "e13", "e_leak", "e_hid"}
    assert full["degree"]["n1"] == 3
    low = walk(CAROL)
    assert set(low["nodes"]) == {"n1", "n2", "n3"} and set(low["edges"]) == {"e12", "e13"}
    assert low["degree"] == {"n1": 2, "n2": 1, "n3": 1}              # degree counts exclude the hidden edge and node
    assert "secret" not in repr(low) and "e_leak" not in repr(low) and "e_hid" not in repr(low)
    assert low["has_more"] is False


def test_invisible_or_missing_root_is_indistinguishable():
    with pytest.raises(wc.NotFoundOrForbidden) as hidden:
        wc.traverse(CAROL, "secret", neighbors, can_node, can_edge)
    with pytest.raises(wc.NotFoundOrForbidden) as missing:
        wc.traverse(CAROL, "no-such-node", neighbors, can_node, can_edge)
    with pytest.raises(wc.NotFoundOrForbidden):
        wc.traverse(STRANGER, "n1", neighbors, can_node, can_edge)
    assert str(hidden.value) == str(missing.value) == "not found"
    assert wc.traverse(CAROL, "n1", neighbors, can_node, can_edge)["nodes"][0] == "n1"      # control


def test_has_more_reflects_only_visible_truncation():
    assert walk(CAROL, max_nodes=3)["has_more"] is False             # 3 visible nodes fit: the hidden 4th must not flip it
    assert walk(ALICE, max_nodes=3)["has_more"] is True              # control: a real visible node was dropped
    assert walk(CAROL, max_nodes=2)["has_more"] is True


def test_org_scope_is_fleet_data_not_a_members_and_depth_zero_is_just_the_root():
    assert can_node(ALICE, "org") is False and can_node(CAROL, "n1") is True
    assert walk(CAROL, depth=0)["nodes"] == ["n1"] and walk(CAROL, depth=0)["edges"] == {}


ENTRIES = [{"ref": "n1", "text": "ci failure report"}, {"ref": "n2", "text": "plan"},
           {"ref": "secret", "text": "ci failure root cause secret"}]


def test_search_applies_acl_before_matching_and_counts_visible_only():
    def m(e):
        return "ci failure" in e["text"]

    high = wc.search(ALICE, ENTRIES, m, can_node)
    assert [h["ref"] for h in high["hits"]] == ["n1", "secret"] and high["total"] == 2          # control
    low = wc.search(CAROL, ENTRIES, m, can_node)
    assert [h["ref"] for h in low["hits"]] == ["n1"] and low["total"] == 1
    assert wc.search(STRANGER, ENTRIES, m, can_node) == {"hits": [], "total": 0}
    assert wc.search(CAROL, ENTRIES, m, can_node, limit=0)["total"] == 1                         # total never pads with hidden hits


def test_export_filters_items_and_their_references():
    items = [{"ref": "n1", "refs": ["n2", "secret"]}, {"ref": "secret", "refs": ["n1"]}, {"ref": "n2", "entity_refs": ["secret"]}]
    low = wc.export_items(CAROL, items, can_node)
    assert low == {"items": [{"ref": "n1", "refs": ["n2"]}, {"ref": "n2", "entity_refs": []}], "count": 2}
    high = wc.export_items(ALICE, items, can_node)
    assert high["count"] == 3 and high["items"][0]["refs"] == ["n2", "secret"]                   # control
    assert wc.filter_visible(CAROL, ["n1", "secret", "nope"], can_node) == ["n1"]


def test_subscription_is_rechecked_on_every_delivery_and_closes_on_revocation():
    carol = member("carol", {"room-1"})
    ok = can_node
    g = wc.SubscriptionGuard(carol, "n1", ok)
    assert g.deliver("n2") is True and g.deliver("secret") is False and g.closed
    assert g.deliver("n2") is False                                   # once closed, stays closed even for visible events
    g2 = wc.SubscriptionGuard(carol, "n1", ok)
    carol["access"]["rooms"].discard("room-1")                        # revocation takes effect on the very next delivery
    assert g2.deliver("n2") is False and g2.closed
    with pytest.raises(wc.NotFoundOrForbidden):
        wc.SubscriptionGuard(STRANGER, "n1", ok)


def test_guest_without_room_access_sees_nothing_through_any_hook():
    for ref in NODES:
        assert not can_node(GUEST_ORG, ref)
    assert wc.search(GUEST_ORG, ENTRIES, lambda e: True, can_node) == {"hits": [], "total": 0}
    assert wc.export_items(GUEST_ORG, [{"ref": "n1"}], can_node) == {"items": [], "count": 0}
    assert not can_see(None, R1, None) and not can_see({"kind": "unknown"}, R1, None)           # fail closed on junk principals
