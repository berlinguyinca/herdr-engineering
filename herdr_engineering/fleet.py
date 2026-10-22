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

import hashlib
import json
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
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


class FleetError(Exception):
    pass


def _expected_hosts(inventory: Path, host: str | None) -> list[str]:
    """Read the private static inventory; never infer coverage from responders."""
    import yaml
    try:
        data = yaml.safe_load(inventory.read_text())
    except yaml.YAMLError as exc:
        raise FleetError(f"Invalid fleet inventory: {exc}") from exc
    group = data.get("all", {}).get("children", {}).get("herdr_fleet", {})
    def walk(node: dict) -> set[str]:
        result = set((node.get("hosts") or {}).keys())
        for child in (node.get("children") or {}).values():
            result.update(walk(child or {}))
        return result
    hosts = walk(group)
    if not hosts or (host is not None and host not in hosts):
        raise FleetError("Inventory must contain the exact requested host(s) in herdr_fleet")
    return [host] if host else sorted(hosts)


def _is_revision(value: str) -> bool:
    return re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None


def _parity(status: dict, expected: list[str], revision: str,
            pi_revision: str | None = None, *, pi_only: bool = False,
            expected_build_gate: str | None = None) -> dict:
    entries = status.get("hosts", [])
    actual = [entry.get("host") for entry in entries]
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    duplicates = sorted({name for name in actual if actual.count(name) > 1})
    invalid, mismatched = [], []
    for entry in entries:
        if not pi_only and entry.get("revision") != revision:
            mismatched.append(entry["host"])
        components_ok = pi_only or (
            re.fullmatch(r"herdr-eng\s+\S+", entry.get("herdr_eng", "")) is not None
            and re.fullmatch(r"[0-9a-f]{32}", entry.get("build_gate", "")) is not None
            and entry.get("agent") == "active"
            and (expected_build_gate is None or entry.get("build_gate") == expected_build_gate))
        if pi_revision and (entry.get("pi_revision") != pi_revision
                            or not entry.get("pi_path", "").startswith("/")):
            components_ok = False
        if not components_ok:
            invalid.append(entry["host"])
    return {"ok": status.get("rc") == 0 and bool(expected)
            and not (missing or unexpected or duplicates or mismatched or invalid),
            "expected_revision": revision, "expected_hosts": expected,
            "missing_hosts": missing, "unexpected_hosts": unexpected,
            "duplicate_hosts": duplicates, "mismatched_hosts": mismatched,
            "invalid_hosts": invalid}


def fleet_join(*, ssh_target: str, label: str | None = None, os_name: str = "linux",
               user: str | None = None, ansible_host: str | None = None,
               gateway: str = "http://dev.lan:29999", herdr_bin: str = "herdr",
               attach: bool = True, provision: bool = True,
               inventory: Path | None = None, host_vars_dir: Path | None = None,
               fleet_config: Path | None = None, roles: list[str] | None = None,
               control_plane_url: str | None = None) -> dict:
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

    # 2b) fleet-config credentials (opt-in via ``fleet_config``): install the
    # token on the target FIRST, only then persist its hash, so a failed ssh
    # leaves the central file unchanged. The token is never reported.
    if fleet_config is not None:
        from . import fleet_config_cli as fcc
        if not Path(fleet_config).exists():
            report["steps"]["config"] = {"ok": True, "skipped": f"{fleet_config} does not exist"}
        else:
            try:
                rep = fcc.issue_host_credentials(Path(fleet_config), host, ssh_target,
                                                 roles=roles,
                                                 control_plane_url=control_plane_url)
                report["steps"]["config"] = {"ok": True, "host": host, "token": fcc.TOKEN_PLACEHOLDER}
            except Exception as exc:
                report["steps"]["config"] = {"ok": False, "error": str(exc)}
                report.setdefault("errors", []).append(f"fleet config credentials failed: {exc}")

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
                 reinstall_local: bool = True, revision: str | None = None,
                 pi_revision: str | None = None) -> dict:
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

    try:
        expected_hosts = _expected_hosts(inv, host)
    except (OSError, ValueError, AttributeError, FleetError) as exc:
        report["error"] = str(exc)
        return report
    if (revision is not None and not _is_revision(revision)) or (
            pi_revision is not None and not _is_revision(pi_revision)):
        report["error"] = "Release revisions must be full immutable commit IDs"
        return report

    # Even --no-pull must not label modified source with its clean commit ID.
    ok, dirty = step("controller_clean", ["git", "-C", str(root), "status",
                                           "--porcelain"], 30)
    if not ok or dirty:
        report["steps"]["controller_clean"]["ok"] = False
        report["error"] = "Controller checkout must be clean before updating"
        return report
    if pull_controller:
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
        if revision is None:
            ok, revision = step("select_revision", ["git", "-C", str(root),
                                                   "rev-parse", "origin/main^{commit}"], 30)
            if not ok or not _is_revision(revision):
                report["error"] = "Cannot resolve an immutable controller revision"
                return report
        ok, _ = step("controller_pull", ["git", "-C", str(root), "merge",
                                          "--ff-only", revision], 120)
        if not ok:
            return report
    ok, controller_revision = step("controller_revision", ["git", "-C", str(root),
                                                            "rev-parse", "HEAD"], 30)
    if not ok or not _is_revision(controller_revision):
        report["error"] = "Cannot attest controller revision"
        return report
    if revision is not None and controller_revision != revision:
        report["error"] = "Controller must match the selected release before deployment"
        return report
    revision = controller_revision
    report["revision"] = revision
    try:
        gate_checksum = hashlib.md5((root / "deploy/pi-build-gate/build-gate.ts").read_bytes()).hexdigest()
    except OSError as exc:
        report["error"] = f"Cannot attest selected build gate: {exc}"
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
    if timer_installer.exists() and sys.platform.startswith("linux"):
        enabled, _, _ = _run(["systemctl", "--user", "is-enabled", "--quiet",
                              "herdr-eng-fleet-update.timer"], timeout=30)
        if enabled == 0:
            ok, _ = step("controller_update_timer", ["bash", str(timer_installer)], 120)
            if not ok:
                return report

    playbook = root / "ansible" / "playbooks" / "fleet-sync.yml"
    variables = {"fabric_agent_gateway_url": gateway, "herdr_engineering_revision": revision}
    if pi_revision:
        variables["pi_engineering_revision"] = pi_revision
    cmd = ["ansible-playbook", "-i", str(inv), str(playbook), "-e", json.dumps(variables)]
    if host:
        cmd += ["-l", host]
    ok, out = step("fleet_sync", cmd, 1800)
    # Ansible usually puts host/task failures in stdout, not stderr.
    report["steps"]["fleet_sync"]["stdout"] = out[-4000:]
    report["status"] = fleet_status(inventory=inv, host=host)
    report["steps"]["revision_parity"] = _parity(
        report["status"], expected_hosts, revision, pi_revision, expected_build_gate=gate_checksum)
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
                          "build_gate": _clean(parts[2]).replace("build-gate=", ""),
                          "agent": _clean(parts[3]).replace("agent=", ""),
                          "revision": _clean(parts[4]).removeprefix("revision=") if len(parts) > 4 else "unknown",
                          "cleanup": _clean(parts[5]).removeprefix("cleanup=") if len(parts) > 5 else "unknown",
                          "pi_revision": _clean(parts[6]).removeprefix("pi-revision=") if len(parts) > 6 else "unknown",
                          "pi_path": _clean(parts[7]).removeprefix("pi-path=") if len(parts) > 7 else ""})
    return {"rc": rc, "hosts": hosts, "stderr": err.strip()[-300:] or None}


def stage_pi_runtime(revision: str, *, host: str, inventory: Path | None = None) -> dict:
    """Install/register a pinned Pi package; never reload or restart live sessions."""
    root = repo_root()
    inv = inventory or root / "ansible" / "inventory" / "hosts.yml"
    report: dict = {"ok": False, "revision": revision, "host": host}
    if not _is_revision(revision):
        return {**report, "error": "Pi revision must be a full immutable commit ID"}
    try:
        expected = _expected_hosts(inv, host)
    except (OSError, ValueError, AttributeError, FleetError) as exc:
        return {**report, "error": str(exc)}
    cmd = ["ansible-playbook", "-i", str(inv),
           str(root / "ansible" / "playbooks" / "pi-runtime-stage.yml"), "-l", host,
           "-e", json.dumps({"pi_engineering_revision": revision})]
    rc, out, err = _run(cmd, timeout=1800)
    status = fleet_status(inventory=inv, host=host)
    parity = _parity(status, expected, revision, revision, pi_only=True)
    ok = rc == 0 and parity["ok"]
    return {**report, "ok": ok, "rc": rc,
            "sourceRoot": status["hosts"][0]["pi_path"] if ok else None,
            "stdout": out[-4000:], "stderr": err[-1000:], "status": status,
            "hosts": status.get("hosts", []), "parity": parity}


# ---- version drift plan -----------------------------------------------------
# Hosts report tool and plugin versions to the control plane; this turns the
# resulting matrix into per-host actions. It only *describes* what to run: the
# updaters differ per machine (nvm, Homebrew, pinned releases) and a wrong
# global install is hard to undo, so nothing here executes remotely.
_UPDATERS = {
    "claude": "npm install -g @anthropic-ai/claude-code@{version}",
    "codex": "npm install -g @openai/codex@{version}",
    "pi": "pi-engineering-update   # moves pi-engineering; pi itself follows its own release channel",
    "herdr-eng": "herdr-eng fleet update --host {host}",
}


def plan_version_sync(matrix: dict) -> list[dict]:
    """One entry per (host, component) that differs from the fleet reference."""
    plan = []
    for component in matrix.get("components", []):
        reference = component.get("reference")
        if not reference or reference == "dev":  # dev checkouts have no version to sync to
            continue
        name = component["name"]
        for host, cell in sorted(component.get("hosts", {}).items()):
            state = cell.get("state")
            if state == "ok":
                continue
            entry = {"host": host, "component": name, "group": component.get("group"),
                     "state": state, "current": cell.get("version"), "target": reference}
            if component.get("group") == "plugins" and name.startswith("claude:"):
                plugin = name.split(":", 1)[1]
                entry["command"] = (f"claude plugin update {plugin}" if state == "drifted"
                                    else f"claude plugin install {plugin}")
            elif name in _UPDATERS and state in {"drifted", "missing"}:
                entry["command"] = _UPDATERS[name].format(version=reference, host=host)
            elif name == "node":
                entry["command"] = None
                entry["note"] = "node differs per service manager; align it only if a tool needs it"
            else:
                entry["command"] = None
            plan.append(entry)
    return plan
