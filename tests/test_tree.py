"""Tests for the tree projection (spec 0090/0110/0150).

Hermetic: the live adapters are monkeypatched so the tree builds from a fixed
fixture regardless of fleet/network state. Confirms the projection never
throws on empty/failing sources and that node ids resolve through
``node_detail``.
"""
from __future__ import annotations

import herdr_engineering.tree as tree


def _fixture(monkeypatch):
    monkeypatch.setattr(tree, "_fleet_machines", lambda: [
        {"id": "beast", "label": "beast", "target": "100.96.156.65", "reachable": True}])
    monkeypatch.setattr(tree, "_workspaces", lambda: [
        {"workspace_id": "w1", "label": "fix augur", "agent_status": "working"},
        {"workspace_id": "w2", "label": "tournament", "agent_status": "idle"}])
    monkeypatch.setattr(tree, "_leases", lambda: [
        {"id": "lease1", "label": "augur web (port 18000)", "url": "http://dev.lan:18000",
         "external_port": 18000,
         "target": {"machine_id": "beast", "port": 8787}}])
    monkeypatch.setattr(tree, "_activity", lambda: [
        {"id": "ev1", "summary": "pushed branch", "occurred_at": "2026-10-11T00:00:00Z",
         "source": "pi", "herdr_session_id": "w1"}])
    monkeypatch.setattr(tree, "_detected_harnesses", lambda: [
        {"id": "harness:pi", "label": "pi", "type": "harness"}])
    monkeypatch.setattr(tree, "_live_processes", lambda: [
        {"id": "proc:1", "pid": "1", "args": "pi -p worker", "type": "process"}])


def test_build_tree_structure(monkeypatch):
    _fixture(monkeypatch)
    t = tree.build_tree()
    assert t["counts"]["machines"] == 1
    assert t["counts"]["workspaces"] == 2
    assert t["counts"]["dev_services"] == 1
    assert t["counts"]["processes"] == 1
    root = t["root"]["children"][0]
    assert root["label"] == "beast"
    ws = [c for c in root["children"] if c["type"] == "workspace"]
    assert len(ws) == 2
    # workspace leaf carries status
    assert ws[0]["status"] == "working"


def test_build_tree_fail_closed_empty(monkeypatch):
    """Empty/failing sources must not throw and must yield an empty-but-valid tree."""
    monkeypatch.setattr(tree, "_fleet_machines", lambda: [])
    monkeypatch.setattr(tree, "_workspaces", lambda: [])
    monkeypatch.setattr(tree, "_leases", lambda: [])
    monkeypatch.setattr(tree, "_activity", lambda: [])
    monkeypatch.setattr(tree, "_detected_harnesses", lambda: [])
    monkeypatch.setattr(tree, "_live_processes", lambda: [])
    t = tree.build_tree()
    assert t["root"]["children"]  # local host node present so tree is never empty
    assert t["counts"]["workspaces"] == 0


def test_node_detail_workspace(monkeypatch):
    _fixture(monkeypatch)
    d = tree.node_detail("ws:w1")
    assert d["kind"] == "workspace"
    assert d["label"] == "fix augur"
    assert d["agent_status"] == "working"
    # activity tied to the workspace is surfaced
    assert any(a["id"] == "ev1" for a in d["activity"])


def test_node_detail_lease(monkeypatch):
    _fixture(monkeypatch)
    d = tree.node_detail("lease:lease1")
    assert d["kind"] == "dev_service"
    assert d["url"] == "http://dev.lan:18000"


def test_node_detail_unknown(monkeypatch):
    _fixture(monkeypatch)
    d = tree.node_detail("nope:nothing")
    assert d["kind"] == "unknown"
    assert "error" in d
