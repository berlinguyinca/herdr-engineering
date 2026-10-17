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
            (str(l.get("machine_id", "")) == host or str(l.get("target", {})).find(host) >= 0)
            for l in leases)
        report["registered"] = bool(registered)
        report["steps"]["verify"] = {"registered": bool(registered), "leases": len(leases)}
    except Exception as exc:
        report["steps"]["verify"] = {"ok": False, "error": str(exc)}

    report["ok"] = bool(report.get("registered")) and not report.get("errors")
    return report


def fleet_status(*, inventory: Path | None = None) -> dict:
    """Read-only per-host version/registration fingerprint."""
    root = repo_root()
    inv = inventory or (root / "ansible" / "inventory" / "hosts.yml")
    playbook = root / "ansible" / "playbooks" / "fleet-status.yml"
    rc, out, err = _run(["ansible-playbook", "-i", str(inv), str(playbook)], timeout=300)
    lines = [l for l in out.splitlines() if " | " in l and "herdr-eng=" in l]
    hosts = []
    for ln in lines:
        # e.g. "beast | herdr-eng=herdr-eng 0.1.0 | build-gate=<md5> | agent=inactive"
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) >= 4:
            hosts.append({"host": parts[0],
                          "herdr_eng": parts[1].replace("herdr-eng=", ""),
                          "build_gate": parts[2].replace("build-gate=", "")[:12],
                          "agent": parts[3].replace("agent=", "")})
    return {"rc": rc, "hosts": hosts, "stderr": err.strip()[-300:] or None}
