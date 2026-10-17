"""Fleet onboarding & status — the ONE-command way to join a machine into the
HerdR dev-fabric and keep every host version-synced.

`fleet join <ssh-target>` collapses the old multi-step procedure
(``herdr machine add`` + inventory edit + fabric-agent install + build-gate
install + playbook run + verification) into a single command: it attaches the
machine via the real ``herdr`` binary (``herdr machine add``), adds it to the
private ansible inventory, converges herdr-eng + the fabric auto-registration
agent + the pi build-gate extension on it, then verifies registration and
version parity.

Everything is idempotent and non-disruptive to running agents.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from .herdr_adapter import NativeHerdrAdapter

try:  # reuse the private-inventory helper from scripts/ (may live beside repo)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from fleet_inventory import add_host, list_hosts, write_host_vars
except Exception:  # pragma: no cover - best effort; inventory is optional
    def add_host(*a, **k):  # type: ignore
        raise RuntimeError("scripts/fleet_inventory.py not importable")

    def write_host_vars(*a, **k):  # type: ignore
        raise RuntimeError("scripts/fleet_inventory.py not importable")

    def list_hosts(*a, **k):  # type: ignore
        return []


def repo_root() -> Path:
    """Locate the herdr-engineering checkout that holds the ansible fleet."""
    env = os.environ.get("HERDR_ENGINEERING_REPO")
    if env:
        return Path(env)
    # walk up from cwd looking for ansible/inventory/hosts.yml
    cwd = Path.cwd()
    for p in [cwd, *cwd.parents]:
        if (p / "ansible" / "inventory" / "hosts.yml").exists():
            return p
    return cwd


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return proc.returncode, proc.stdout, proc.stderr


class FleetError(Exception):
    pass


def fleet_join(*, ssh_target: str, label: str | None = None, os_name: str = "linux",
               user: str | None = None, ansible_host: str | None = None,
               gateway: str = "http://dev.lan:29999", herdr_bin: str = "herdr",
               attach: bool = True, provision: bool = True,
               inventory: Path | None = None, host_vars_dir: Path | None = None) -> dict:
    """One command: attach via herdr + add to inventory + converge + verify.

    Returns a JSON-serializable report.
    """
    report: dict = {"target": ssh_target, "label": label or ssh_target,
                    "steps": {}, "registered": False, "ok": False}
    root = repo_root()
    inv = inventory or (root / "ansible" / "inventory" / "hosts.yml")
    hv_dir = host_vars_dir or (root / "ansible" / "inventory" / "host_vars")
    host = label or ssh_target

    # 1) attach the machine via herdr (herdr machine add)
    if attach:
        adapter = NativeHerdrAdapter(herdr_bin=herdr_bin)
        try:
            adapter.add_machine(label=host, ssh_target=ssh_target)
            report["steps"]["herdr_attach"] = {"ok": True}
        except Exception as exc:
            report["steps"]["herdr_attach"] = {"ok": False, "error": str(exc)}
            report["errors"] = [f"herdr attach failed: {exc}"]
            # attach is the gateway step; stop if it failed and we require it
            report["ok"] = False
            return report

    # 2) add to the private inventory
    try:
        added = add_host(inv, host, os_name)
        report["steps"]["inventory"] = {"added": added, "path": str(inv)}
        if ansible_host and user:
            wrote = write_host_vars(hv_dir, host, ansible_host, user)
            report["steps"]["host_vars"] = {"written": wrote, "path": str(hv_dir / f"{host}.yml")}
        else:
            report["steps"]["host_vars"] = {"written": False,
                                            "note": "pass --ansible-host/--user so ansible can reach the host"}
    except Exception as exc:
        report["steps"]["inventory"] = {"ok": False, "error": str(exc)}
        report["errors"] = [f"inventory update failed: {exc}"]

    # 3) converge herdr-eng + fabric agent + build-gate on the host
    if provision:
        playbook = root / "ansible" / "playbooks" / "fleet-sync.yml"
        cmd = ["ansible-playbook", "-i", str(inv), str(playbook), "-l", host,
               "-e", f"fabric_agent_gateway_url={gateway}"]
        rc, out, err = _run(cmd)
        report["steps"]["provision"] = {"ok": rc == 0, "rc": rc,
                                        "stderr": err.strip()[-500:] or None}
        if rc != 0:
            report.setdefault("errors", []).append(
                f"provisioning failed (rc={rc}): {err.strip()[-300:]}")

    # 4) verify registration with the fabric gateway
    try:
        adapter = NativeHerdrAdapter(herdr_bin=herdr_bin)
        leases = adapter.devfabric_list()
        registered = any(
            (str(lease.get("machine_id", "")) == host
             or str(lease.get("target", {})).find(host) >= 0)
            for lease in leases)
        report["registered"] = bool(registered)
        report["steps"]["verify"] = {"registered": bool(registered), "leases": len(leases)}
    except Exception as exc:
        report["steps"]["verify"] = {"ok": False, "error": str(exc)}

    report["ok"] = bool(report.get("registered")) and not report.get("errors")
    return report


def fleet_update(*, host: str | None = None, gateway: str = "http://dev.lan:29999",
                 inventory: Path | None = None, pull_controller: bool = True,
                 reinstall_local: bool = True) -> dict:
    """Self-update the whole fleet (controller + all reachable hosts) to the
    current version, then report parity. One command.

    Returns a JSON-serializable report.
    """
    root = repo_root()
    inv = inventory or (root / "ansible" / "inventory" / "hosts.yml")
    report: dict = {"target": host or "all inventory hosts", "steps": {}, "ok": False}

    def step(name: str, cmd: list[str], timeout: int) -> tuple[bool, str]:
        rc, out, err = _run(cmd, timeout=timeout)
        report["steps"][name] = {"ok": rc == 0, "rc": rc,
                                "stderr": err.strip()[-1000:] or None}
        return rc == 0, out.strip()

    # Never silently deploy a dirty, divergent, or failed controller update.
    if pull_controller:
        ok, dirty = step("controller_clean", ["git", "-C", str(root), "status",
                                               "--porcelain"], 30)
        if not ok or dirty:
            report["steps"]["controller_clean"]["ok"] = False
            report["error"] = "Controller checkout must be clean before updating"
            return report
        ok, branch = step("controller_branch", ["git", "-C", str(root),
                                                "branch", "--show-current"], 30)
        if not ok or branch != "main":
            report["error"] = "Controller must be on main"
            return report
        ok, _ = step("controller_fetch", ["git", "-C", str(root),
                                           "fetch", "origin", "main"], 120)
        if not ok:
            return report
        ok, ahead = step("controller_ancestry", ["git", "-C", str(root),
                                                "rev-list", "--count", "origin/main..HEAD"], 30)
        if not ok or ahead != "0":
            report["error"] = "Controller has unpublished commits; refusing automatic update"
            return report
        ok, _ = step("controller_pull", ["git", "-C", str(root), "pull",
                                          "--ff-only", "origin", "main"], 120)
        if not ok:
            return report
    if reinstall_local:
        ok, _ = step("controller_reinstall", [sys.executable, "-m", "pip", "install",
                                               "--quiet", "--upgrade", str(root)], 300)
        if not ok:
            return report

    from .config import load_config
    cleanup = load_config().get("build_cleanup", {})
    roots = cleanup.get("roots", []) if isinstance(cleanup, dict) else []
    enabled = (isinstance(cleanup, dict) and cleanup.get("enabled") is True
               and isinstance(roots, list) and bool(roots)
               and all(isinstance(path, str) and path.strip() for path in roots))
    installer = root / "scripts" / "build-cleanup.sh"
    if enabled and installer.exists():
        ok, _ = step("controller_cleanup", ["bash", str(installer)], 120)
        if not ok:
            return report

    elif not enabled and sys.platform.startswith("linux"):
        loaded, value, _ = _run(["systemctl", "--user", "show",
                                 "herdr-eng-build-cleanup.service",
                                 "-p", "LoadState", "--value"], timeout=30)
        if loaded == 0 and value.strip() == "loaded":
            ok, _ = step("controller_cleanup_disabled", ["systemctl", "--user", "disable",
                         "--now", "herdr-eng-build-cleanup.service"], 120)
            if not ok:
                return report

    timer_installer = root / "scripts" / "fleet-auto-update.sh"
    if timer_installer.exists():
        enabled, _, _ = _run(["systemctl", "--user", "is-enabled", "--quiet",
                              "herdr-eng-fleet-update.timer"], timeout=30)
        if enabled == 0:
            ok, _ = step("controller_update_timer", ["bash", str(timer_installer)], 120)
            if not ok:
                return report

    playbook = root / "ansible" / "playbooks" / "fleet-sync.yml"
    cmd = ["ansible-playbook", "-i", str(inv), str(playbook),
           "-e", f"fabric_agent_gateway_url={gateway}"]
    if host:
        cmd += ["-l", host]
    ok, out = step("fleet_sync", cmd, 1800)
    # Ansible usually puts host/task failures in stdout, not stderr.
    report["steps"]["fleet_sync"]["stdout"] = out[-4000:]
    report["status"] = fleet_status(inventory=inv, host=host)
    revision_rc, revision, _ = _run(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=30)
    revision = revision.strip()
    hosts = report["status"].get("hosts", [])
    valid_revision = revision_rc == 0 and re.fullmatch(r"[0-9a-f]{40,64}", revision) is not None
    mismatched = [entry["host"] for entry in hosts
                  if revision_rc != 0 or entry.get("revision") != revision]
    report["steps"]["revision_parity"] = {
        "ok": valid_revision and bool(hosts) and not mismatched, "expected_revision": revision,
        "mismatched_hosts": mismatched,
    }
    report["ok"] = (ok and report["status"].get("rc") == 0
                    and report["steps"]["revision_parity"]["ok"])
    return report


def fleet_status(*, inventory: Path | None = None, host: str | None = None) -> dict:
    """Read-only per-host version/registration fingerprint."""
    root = repo_root()
    inv = inventory or (root / "ansible" / "inventory" / "hosts.yml")
    playbook = root / "ansible" / "playbooks" / "fleet-status.yml"
    cmd = ["ansible-playbook", "-i", str(inv), str(playbook)]
    if host:
        cmd += ["-l", host]
    rc, out, err = _run(cmd, timeout=300)
    lines = [line for line in out.splitlines() if " | " in line and "herdr-eng=" in line]
    hosts = []
    for line in lines:
        # ansible emits the fingerprint as a debug msg, e.g.
        #   "msg": "beast | herdr-eng=herdr-eng 0.1.0 | build-gate=<md5> | agent=inactive"
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            def _clean(s: str) -> str:
                # drop ansible's leading '"msg": "...' wrapper and trailing quote
                if '"msg":' in s:
                    s = s.split('"msg":')[-1].strip().strip('"').strip()
                return s.strip().rstrip('"').strip()
            hosts.append({"host": _clean(parts[0]),
                          "herdr_eng": _clean(parts[1]).replace("herdr-eng=", ""),
                          "build_gate": _clean(parts[2]).replace("build-gate=", "")[:12],
                          "agent": _clean(parts[3]).replace("agent=", ""),
                          "revision": _clean(parts[4]).removeprefix("revision=") if len(parts) > 4 else "unknown",
                          "cleanup": _clean(parts[5]).removeprefix("cleanup=") if len(parts) > 5 else "unknown"})
    return {"rc": rc, "hosts": hosts, "stderr": err.strip()[-300:] or None}
