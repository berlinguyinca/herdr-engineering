"""Is an automatic handoff policy in place on this host, and how do the two Herdr plugins get linked.

The controller (``herdr-eng handoff controller run``) is the only thing that hands a session off at its context limit. It runs
when Herdr starts the controller plugin (``deploy/herdr-handoff-controller-plugin``, a ``[[startup]]`` command) or when somebody
runs it by hand. A host where neither is true has no automatic policy, so ``handoff auto check`` and ``doctor`` say so and name
the fix. Read-only except :func:`link_plugins`, which the installer calls.

Measured on Herdr 0.9.1 (isolated server): ``herdr plugin link`` registers the plugin and its ``[[startup]]`` command does NOT
run until the Herdr server next starts; linking a plugin id again from another path replaces the earlier link.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .fabric_join_setup import Item
from .handoff_controller.run import lock_held
from .handoff_subagent_registry import state_root

MIN_HERDR = (0, 9, 1)
CONTROLLER_PLUGIN = "herdr-handoff-controller-plugin"
SESSION_PANE_PLUGIN = "herdr-session-pane-plugin"
PLUGINS = (CONTROLLER_PLUGIN, SESSION_PANE_PLUGIN)
CONTROLLER_ID = "herdr.handoff-controller"
SESSION_PANE_ID = "herdr.session-pane"
_CALL_S = 15.0


class ControllerState(StrEnum):
    SWITCHED_OFF = "switched_off"          # HERDR_HANDOFF_AUTO=0: deliberately off
    RUNNING = "running"                    # a controller holds the lock
    LINKED = "linked"                      # plugin linked; its startup command runs when Herdr next starts
    NOT_LINKED = "not_linked"
    DISABLED = "disabled"                  # linked but switched off in Herdr: its startup command never runs
    HERDR_UNAVAILABLE = "herdr_unavailable"  # herdr missing or not answering: no way to tell, and no controller without it
    REPLY_NOT_UNDERSTOOD = "reply_not_understood"  # herdr answered, but `plugin list` was not the JSON this code reads


class SidePaneState(StrEnum):
    LINKED = "linked"
    NOT_LINKED = "not_linked"
    DISABLED = "disabled"
    HERDR_UNAVAILABLE = "herdr_unavailable"
    REPLY_NOT_UNDERSTOOD = "reply_not_understood"


class Presence(StrEnum):
    """What `herdr plugin list` says about one plugin id."""
    ENABLED = "enabled"
    DISABLED = "disabled"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    NOT_UNDERSTOOD = "not_understood"


@dataclass(frozen=True)
class Finding:
    state: ControllerState | SidePaneState
    blocking: bool  # True: no automatic policy exists; the check fails
    detail: str
    fix: str = ""


def classify_controller(*, plugin: Presence, running: bool, switched_off: bool) -> ControllerState:
    if switched_off:
        return ControllerState.SWITCHED_OFF
    if running:
        return ControllerState.RUNNING
    return {Presence.ENABLED: ControllerState.LINKED, Presence.ABSENT: ControllerState.NOT_LINKED,
            Presence.DISABLED: ControllerState.DISABLED, Presence.UNAVAILABLE: ControllerState.HERDR_UNAVAILABLE,
            Presence.NOT_UNDERSTOOD: ControllerState.REPLY_NOT_UNDERSTOOD}[plugin]


def classify_side_pane(plugin: Presence) -> SidePaneState:
    return {Presence.ENABLED: SidePaneState.LINKED, Presence.ABSENT: SidePaneState.NOT_LINKED,
            Presence.DISABLED: SidePaneState.DISABLED, Presence.UNAVAILABLE: SidePaneState.HERDR_UNAVAILABLE,
            Presence.NOT_UNDERSTOOD: SidePaneState.REPLY_NOT_UNDERSTOOD}[plugin]


def _link_fix(deploy_dir: Path, plugin: str) -> str:
    return (f"run `herdr-eng handoff controller link` (it runs `herdr plugin link` on {deploy_dir / plugin}"
            " with the absolute herdr-eng path)")


def describe_controller(state: ControllerState, deploy_dir: Path) -> Finding:
    fix_link = _link_fix(deploy_dir, CONTROLLER_PLUGIN)
    return {
        ControllerState.SWITCHED_OFF: Finding(state, False, "switched off (HERDR_HANDOFF_AUTO=0); no session is handed off automatically"),
        ControllerState.RUNNING: Finding(state, False, "a controller is running"),
        ControllerState.LINKED: Finding(state, False, "controller plugin linked, no controller running yet",
                                        "restart Herdr (the plugin starts the controller then) or run `herdr-eng handoff controller run`"),
        ControllerState.NOT_LINKED: Finding(
            state, True, "controller plugin not linked in Herdr and no controller running: nothing hands a session off",
            f"{fix_link}, or `herdr plugin link {deploy_dir / CONTROLLER_PLUGIN}`; then restart Herdr or run "
            "`herdr-eng handoff controller run`"),
        ControllerState.DISABLED: Finding(
            state, True, "controller plugin linked but disabled in Herdr, and no controller running: nothing hands a session off",
            f"run `herdr plugin enable {CONTROLLER_ID}`; then restart Herdr or run `herdr-eng handoff controller run`"),
        ControllerState.HERDR_UNAVAILABLE: Finding(
            state, True, "herdr is not installed or not answering, and no controller is running",
            "install Herdr 0.9.1 or newer, then run `herdr-eng handoff controller link`"),
        ControllerState.REPLY_NOT_UNDERSTOOD: Finding(
            state, True, "herdr reply not understood (`herdr plugin list --json` was not the expected JSON), and no controller is running",
            "update herdr-eng or Herdr so they agree on the plugin list format, or run `herdr-eng handoff controller run` by hand"),
    }[state]


def describe_side_pane(state: SidePaneState, deploy_dir: Path) -> Finding:
    return {
        SidePaneState.LINKED: Finding(state, False, "session side pane plugin linked"),
        SidePaneState.NOT_LINKED: Finding(state, False, "session side pane plugin not linked (the controller works without it)",
                                          _link_fix(deploy_dir, SESSION_PANE_PLUGIN)),
        SidePaneState.DISABLED: Finding(state, False, "session side pane plugin linked but disabled in Herdr",
                                        f"run `herdr plugin enable {SESSION_PANE_ID}`"),
        SidePaneState.HERDR_UNAVAILABLE: Finding(state, False, "herdr is not installed or not answering; side pane not checked"),
        SidePaneState.REPLY_NOT_UNDERSTOOD: Finding(state, False, "herdr reply not understood; side pane not checked"),
    }[state]


def parse_version(text: str) -> tuple[int, int, int] | None:
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


class PluginHost:
    """`herdr plugin ...` against one Herdr. `env` replaces the environment, `cwd` resolves a relative HERDR_SOCKET_PATH in it."""

    def __init__(self, binary: str = "herdr", env: Mapping[str, str] | None = None, cwd: Path | None = None) -> None:
        self._binary = binary
        self._env = dict(env) if env is not None else None
        self._cwd = cwd

    def _run(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run([self._binary, *args], env=self._env, cwd=self._cwd, capture_output=True, text=True,
                                  timeout=_CALL_S, check=False)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def version(self) -> tuple[int, int, int] | None:
        done = self._run("--version")
        return parse_version(done.stdout) if done and done.returncode == 0 else None

    def listing(self) -> tuple[dict[str, tuple[str, bool]], Presence | None]:
        """(plugin id -> (root, enabled), None) or ({}, UNAVAILABLE | NOT_UNDERSTOOD)."""
        done = self._run("plugin", "list", "--json")
        if done is None or done.returncode != 0:
            return {}, Presence.UNAVAILABLE
        try:
            plugins = json.loads(done.stdout)["result"]["plugins"]
            return {p["plugin_id"]: (p["plugin_root"], bool(p.get("enabled"))) for p in plugins}, None
        except (ValueError, KeyError, TypeError):
            return {}, Presence.NOT_UNDERSTOOD

    def plugin_roots(self) -> dict[str, str] | None:
        """Linked plugin id -> plugin root; None when herdr cannot be asked or its reply is not understood."""
        plugins, problem = self.listing()
        return None if problem else {pid: root for pid, (root, _) in plugins.items()}

    def link(self, path: Path) -> str | None:
        """None when linked, else the reason."""
        done = self._run("plugin", "link", str(path))
        if done is None:
            return "herdr did not answer"
        if done.returncode != 0:
            return (done.stdout.strip() or done.stderr.strip() or f"exit {done.returncode}").splitlines()[0][:200]
        return None


def default_deploy_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "deploy"


def controller_findings(env: Mapping[str, str] | None = None, *, herdr_bin: str | None = None, deploy_dir: Path | None = None,
                        cwd: Path | None = None) -> tuple[Finding, Finding]:
    """The controller finding and the side pane finding for this host. `herdr_bin` defaults to $HERDR_BIN, else `herdr`."""
    env = os.environ if env is None else env
    herdr_bin = herdr_bin or env.get("HERDR_BIN") or "herdr"
    deploy = deploy_dir or default_deploy_dir()
    running = lock_held(state_root(env))
    switched_off = env.get("HERDR_HANDOFF_AUTO") == "0"
    plugins, problem = PluginHost(herdr_bin, env, cwd).listing()

    def presence(plugin_id: str) -> Presence:
        if problem:
            return problem
        if plugin_id not in plugins:
            return Presence.ABSENT
        return Presence.ENABLED if plugins[plugin_id][1] else Presence.DISABLED

    controller = classify_controller(plugin=presence(CONTROLLER_ID), running=running, switched_off=switched_off)
    side = classify_side_pane(presence(SESSION_PANE_ID))
    return describe_controller(controller, deploy), describe_side_pane(side, deploy)


def check_items(env: Mapping[str, str] | None = None) -> list[Item]:
    """`handoff auto check` rows: a blocking controller finding is `missing` (exit 1), anything else `ok` or `skipped`."""
    controller, side = controller_findings(env)
    def row(target: str, finding: Finding) -> Item:
        action = "missing" if finding.blocking else ("ok" if finding.state.value in ("running", "switched_off", "linked") else "skipped")
        return Item(target, action, finding.detail + (f"; fix: {finding.fix}" if finding.fix else ""))
    return [row("controller", controller), row("side pane", side)]


def render_manifest(text: str, herdr_eng: str) -> str:
    """The manifest with every `herdr-eng` command replaced by its absolute path: Herdr starts plugin commands with the PATH of
    its own server, which usually lacks ~/.local/bin."""
    quoted = json.dumps(herdr_eng)
    out = text.replace('"herdr-eng"', quoted)
    in_sh = json.dumps(shlex.quote(herdr_eng))[1:-1]  # shell-quoted, then escaped for a TOML basic string
    out = out.replace("exec herdr-eng ", f"exec {in_sh} ")
    tomllib.loads(out)  # still valid TOML
    return out


@dataclass(frozen=True)
class EntryPoint:
    path: str
    notes: tuple[str, ...] = ()


def choose_entry_point(stable_link: Path, own_binary: Path, *, editable: bool) -> EntryPoint:
    """The `herdr-eng` the plugin manifests name: the stable link a user's shell runs (normally ~/.local/bin/herdr-eng) when it is a
    symlink to this install's own binary; otherwise the install's own binary, with a warning when another install owns the link."""
    notes: list[str] = []
    is_ours = stable_link.is_symlink() and stable_link.resolve() == own_binary.resolve()
    if is_ours:
        path = stable_link
    else:
        path = own_binary
        if stable_link.is_symlink() or stable_link.exists():
            notes.append(f"warning: {stable_link} belongs to another install and was left alone; the plugins run {own_binary}, "
                         "which may differ from the herdr-eng you type")
        else:
            notes.append(f"note: {stable_link} is not linked; the plugins run {own_binary}")
    if editable:
        notes.append(f"note: this is a checkout install; the plugins run {own_binary} inside the checkout and stop working if it is removed")
    return EntryPoint(str(path), tuple(notes))


def write_atomically(path: Path, text: str) -> None:
    """Temp file in the same directory, then replace: Herdr never reads a half-written manifest."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def link_plugins(deploy_dir: Path, herdr_eng: str, *, data_dir: Path, herdr_bin: str = "herdr",
                 env: Mapping[str, str] | None = None, cwd: Path | None = None) -> list[Item]:
    """Link both plugins into Herdr from copies under `data_dir` whose commands use the absolute `herdr_eng`. Idempotent: a plugin
    already linked to the same unchanged copy is left alone, one linked to another path is relinked (Herdr replaces it). Skipped,
    writing nothing, when herdr is absent or older than 0.9.1."""
    host = PluginHost(herdr_bin, os.environ if env is None else env, cwd)
    version = host.version()
    if version is None:
        return [Item("herdr", "skipped", "herdr is not installed or not answering; plugins not linked")]
    if version < MIN_HERDR:
        return [Item("herdr", "skipped", f"herdr {'.'.join(map(str, version))} is older than 0.9.1 (plugins need it); plugins not linked")]
    exe = Path(herdr_eng)
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        return [Item("herdr-eng", "unresolved", f"{herdr_eng} is not an executable file; plugins not linked")]
    items: list[Item] = []
    for name in PLUGINS:
        source = deploy_dir / name / "herdr-plugin.toml"
        try:
            rendered = render_manifest(source.read_text(encoding="utf-8"), herdr_eng)
            plugin_id = tomllib.loads(rendered)["id"]
        except (OSError, ValueError, KeyError) as exc:
            items.append(Item(str(source), "unresolved", f"cannot read the plugin manifest: {exc}"))
            continue
        target = (data_dir / name).resolve()
        manifest = target / "herdr-plugin.toml"
        try:
            roots = host.plugin_roots() or {}
            current = manifest.read_text(encoding="utf-8") if manifest.is_file() else None
            if current == rendered and roots.get(plugin_id) == str(target):
                items.append(Item(plugin_id, "unchanged", f"linked to {target}"))
                continue
            target.mkdir(parents=True, exist_ok=True)
            write_atomically(manifest, rendered)
        except OSError as exc:
            items.append(Item(plugin_id, "unresolved", f"cannot write {manifest}: {exc.strerror or exc}"))
            continue
        failure = host.link(target)
        items.append(Item(plugin_id, "unresolved", f"herdr plugin link failed: {failure}") if failure else
                     Item(plugin_id, "installed", f"linked to {target}"
                          + (f" (was {roots[plugin_id]})" if plugin_id in roots else "")))
    return items
