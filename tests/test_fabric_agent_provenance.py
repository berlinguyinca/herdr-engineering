"""Safe listener provenance and fresh control-plane reports."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import herdr_engineering.fabric_agent as fa


def test_linux_scan_retains_pid_without_command_arguments(monkeypatch):
    monkeypatch.setattr(fa.shutil, "which", lambda _: "/usr/bin/ss")
    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout='LISTEN 0 128 127.0.0.1:8080 0.0.0.0:* users:(("python",pid=123,fd=4))'))
    assert fa._scan_linux()[8080]["pid"] == 123


def test_project_name_only_exposes_bounded_directory_basename(monkeypatch):
    monkeypatch.setattr(fa.os, "uname", lambda: SimpleNamespace(sysname="Linux"))
    monkeypatch.setattr(fa.os, "readlink", lambda path: "/private/user/secret-parent/my-project")
    assert fa._listener_project(123) == "my-project"
    monkeypatch.setattr(fa.os, "readlink", lambda path: "/private/" + "x" * 200)
    assert len(fa._listener_project(123)) == 120
    monkeypatch.setattr(fa.os, "readlink", lambda path: "/private/bad\nname")
    assert fa._listener_project(123) is None


def test_inaccessible_cwd_and_invalid_pid_are_unknown(monkeypatch):
    def denied(path):
        raise PermissionError("private")
    monkeypatch.setattr(fa.os, "uname", lambda: SimpleNamespace(sysname="Linux"))
    monkeypatch.setattr(fa.os, "readlink", denied)
    assert fa._listener_project(123) is None
    assert fa._listener_project(None) is None
    assert fa._listener_project("123") is None


def test_macos_cwd_lookup_is_pid_scoped_and_ignores_parent_paths(monkeypatch):
    monkeypatch.setattr(fa.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(fa.shutil, "which", lambda _: "/usr/sbin/lsof")
    run = Mock(return_value=SimpleNamespace(stdout="p123\nn/Users/private/my-project\n"))
    monkeypatch.setattr(fa.subprocess, "run", run)
    assert fa._listener_project(123) == "my-project"
    assert run.call_args.args[0] == ["/usr/sbin/lsof", "-a", "-p", "123", "-d", "cwd", "-Fn"]


def test_keepalive_never_waits_for_control_plane(tmp_path):
    client = Mock()
    client.heartbeat.return_value = {"ok": True}
    agent = fa.FabricAgent(client, fa.AgentConfig(tailnet_ip="127.0.0.1", store_path=tmp_path / "state.json"))
    agent._report_service = Mock()
    entry = {"lease_id": "lease-1", "label": "python on host (port 8080)",
             "project": "my-project", "source_port": 8080}
    agent._keepalive("8080", entry)
    agent._report_service.assert_not_called()
    client.register.assert_not_called()
    assert entry["label"] == "python on host (port 8080)"


def test_report_contains_context_but_no_pid_or_private_paths(tmp_path, monkeypatch):
    agent = fa.FabricAgent(Mock(), fa.AgentConfig(tailnet_ip="127.0.0.1", store_path=tmp_path / "state.json", control_plane_url="http://control.test"))
    urlopen = MagicMock()
    monkeypatch.setattr(fa.urllib.request, "urlopen", urlopen)
    entry = {"label": "python on host (port 8080)", "process": "python", "pid": 123,
             "project": "my-project", "source_port": 8080, "external_port": 18001,
             "lease_id": "lease-1", "url": "http://dev:18001"}
    agent._report_service(entry, "healthy")
    payload = json.loads(urlopen.call_args.args[0].data)["payload"]
    assert payload["project"] == "my-project"
    assert payload["source_port"] == 8080
    assert "pid" not in payload
    assert "my-project" in payload["description"]
    assert "8080" in payload["description"]


def test_existing_lease_enriched_when_heartbeat_due(tmp_path, monkeypatch):
    client = Mock()
    client.healthz.return_value = {}
    client.list.return_value = {"leases": []}
    client.heartbeat.return_value = {"ok": True}
    agent = fa.FabricAgent(client, fa.AgentConfig(tailnet_ip="127.0.0.1", store_path=tmp_path / "state.json", control_plane_url="http://control.test"))
    agent._report_service = Mock(return_value=True)
    agent._store["8080"] = {"lease_id": "lease-1", "label": "python on host (port 8080)", "last_hb": 0}
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: {8080: {"process": "python", "pid": 123, "addrs": {"127.0.0.1"}}})
    monkeypatch.setattr(fa, "_listener_project", lambda pid, **kwargs: "my-project")
    agent.scan_once()
    saved = json.loads(Path(agent.cfg.store_path).read_text())["8080"]
    assert saved["project"] == "my-project"
    assert saved["source_port"] == 8080
    assert "pid" not in saved


def test_stalled_control_plane_does_not_delay_any_gateway_heartbeat(tmp_path, monkeypatch):
    client = Mock()
    client.healthz.return_value = {}
    client.list.return_value = {"leases": []}
    client.heartbeat.return_value = {"ok": True}
    agent = fa.FabricAgent(client, fa.AgentConfig(tailnet_ip="127.0.0.1", store_path=tmp_path / "state.json", control_plane_url="http://control.test"))
    listeners = {}
    for port in range(8000, 8030):
        agent._store[str(port)] = {"lease_id": str(port), "label": str(port), "last_hb": 0}
        listeners[port] = {"process": "python", "addrs": {"127.0.0.1"}}
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: listeners)
    def stalled(entry, status, **kwargs):
        assert client.heartbeat.call_count == 30
        assert 0 < kwargs["timeout"] <= 1.0
        return False
    agent._report_service = Mock(side_effect=stalled)
    agent.scan_once()
    agent._report_service.assert_called_once()
    assert not agent._cp_last_report


def test_reporting_has_separate_cadence_and_fair_bounded_batches(tmp_path, monkeypatch):
    agent = fa.FabricAgent(Mock(), fa.AgentConfig(tailnet_ip="127.0.0.1", store_path=tmp_path / "state.json", control_plane_url="http://control.test"))
    for port in range(8000, 8030):
        agent._store[str(port)] = {"label": str(port)}
    monkeypatch.setattr(fa.time, "monotonic", lambda: 1000.0)
    agent._report_service = Mock(return_value=True)
    agent._report_services()
    assert agent._report_service.call_count == 4
    for _ in range(7):
        agent._report_services()
    assert agent._report_service.call_count == 30
    agent._report_services()
    assert agent._report_service.call_count == 30
    monkeypatch.setattr(fa.time, "monotonic", lambda: 1300.0)
    agent._report_services()
    assert agent._report_service.call_count == 34
    assert agent._report_service.call_args.kwargs["heartbeat"]
