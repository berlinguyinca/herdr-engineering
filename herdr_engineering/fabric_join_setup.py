"""Setup for ``/herdr::join``: where the fabric listener is, the opt-in session-start hook, and the install check.

Configuration is YAML only (no environment variables). The machine-local file ``~/.config/herdr-engineering/config.yaml`` may hold::

    fabric_join:
      control_plane_url: http://100.64.0.1:9483   # optional explicit URL; wins over listen_host/listen_port
      listen_host: 100.64.0.1          # the fabric listener (control_plane.collab.fabric_trust.listen_host)
      listen_port: 9483
      auto_join_on_session_start: false   # true installs a Claude Code SessionStart hook (default false: no hooks)

``resolve_listener`` finds the listener in this order: this host's fetched configuration, the ``fabric_join`` block above, then the
fleet config file's ``control_plane.collab.fabric_trust`` when it is readable on this machine. An explicit ``--control-plane`` always wins
(that is handled by the caller). Nothing here prints or stores a secret: a listener address is not one.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CONFIG_REL = Path(".config/herdr-engineering/config.yaml")
FLEET_RELS = (Path(".config/herdr-engineering/fleet/fleet-config.yaml"), Path(".config/herdr-engineering/fleet-config.yaml"))
HOOK_MARKER = "herdr-join-auto"
HOOKS_MANIFEST = ".herdr-join-hooks-manifest.json"
HOOK_EVENT = "SessionStart"
# Harnesses other than Claude Code: no hook is written because none could be verified safe (see docs/operations/fabric-trust.md).
UNSUPPORTED_HOOKS = {
    "codex": "hooks live in ~/.codex/hooks.json and are gated by Codex hook trust; this installer never writes trust state",
    "opencode": "plugins exist but there is no verified lifecycle-hook contract",
    "pi": "no verified session-start hook contract; Pi sessions are joined with /herdr::join",
}
FAIL_ACTIONS = {"missing", "stale", "unresolved", "refused"}
_NAME_BAD = re.compile(r"[^a-z0-9._-]")


@dataclass
class Item:
    target: str
    action: str  # installed | unchanged | preserved | removed | skipped | ok | missing | stale | unresolved | refused | unsupported | unreachable
    detail: str = ""

    def to_dict(self) -> dict:
        return {"target": self.target, "action": self.action, "detail": self.detail}


def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else Path.home()


def _url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def _valid(host, port) -> tuple[str, int] | None:
    from .control_plane.collab_fabric_trust import validate_listen_host
    try:
        h = validate_listen_host(host)
    except ValueError:
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return None
    return h, port


def machine_config(home: Path | None = None) -> dict:
    """The machine-local YAML mapping ({} when absent or unreadable). Repo-local config is deliberately not consulted."""
    try:
        import yaml
        data = yaml.safe_load((_home(home) / CONFIG_REL).read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def fabric_join_settings(home: Path | None = None) -> dict:
    block = machine_config(home).get("fabric_join")
    return block if isinstance(block, dict) else {}


def auto_join_enabled(home: Path | None = None) -> bool:
    return fabric_join_settings(home).get("auto_join_on_session_start") is True


def _from_host_config() -> tuple[str, int] | None:
    from .host_config import host_value
    collab = host_value("control_plane.collab")
    trust = collab.get("fabric_trust") if isinstance(collab, dict) else None
    return _valid(trust.get("listen_host"), trust.get("listen_port")) if isinstance(trust, dict) else None


def _explicit_url(home: Path | None) -> str | None:
    """``fabric_join.control_plane_url``: an operator-chosen URL (http or https, no credentials, no path), taken as written."""
    value = fabric_join_settings(home).get("control_plane_url")
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(https?)://([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\])(:[0-9]{1,5})?/?", value.strip())
    return value.strip().rstrip("/") if m else None


def _from_machine_config(home: Path | None) -> tuple[str, int] | None:
    block = fabric_join_settings(home)
    return _valid(block.get("listen_host"), block.get("listen_port"))


def _from_fleet_config(home: Path | None) -> tuple[str, int] | None:
    from .fleet_config import load_fleet_config
    from .fleet_config_cli import fabric_listener
    for rel in FLEET_RELS:
        path = _home(home) / rel
        if not path.is_file():
            continue
        try:
            found = fabric_listener(load_fleet_config(path))
        except Exception:  # unreadable, group-readable, invalid or no fabric_trust: try the next place
            continue
        if found and (checked := _valid(found[0], found[1])):
            return checked
    return None


def resolve_listener(home: Path | None = None) -> tuple[str, str] | None:
    """``(url, source)`` of the fabric listener or None. ``source`` is host-config | machine-config | fleet-config."""
    found = _from_host_config()
    if found:
        return _url(*found), "host-config"
    if (explicit := _explicit_url(home)):
        return explicit, "machine-config"
    for name, probe in (("machine-config", lambda: _from_machine_config(home)), ("fleet-config", lambda: _from_fleet_config(home))):
        found = probe()
        if found:
            return _url(*found), name
    return None


# --------------------------------------------------------------------------
# the machine-config listener entry
# --------------------------------------------------------------------------

def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.fj-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


_BLOCK_COMMENT = ("# Written by `herdr-eng summary install` (from the fleet config). Edit by hand; a differing value is never overwritten.\n")


def install_listener(home: Path | None = None, *, dry_run: bool = False) -> Item:
    """Record the fleet config's fabric listener under ``fabric_join`` in the machine config. Idempotent; a differing existing value is
    reported, never changed."""
    h = _home(home)
    target = str(h / CONFIG_REL)
    fleet = _from_fleet_config(h)
    if fleet is None:
        return Item(target, "skipped", "no readable fleet config with control_plane.collab.fabric_trust on this host")
    path = h / CONFIG_REL
    text = path.read_text() if path.is_file() else ""
    have = _from_machine_config(h)
    block = fabric_join_settings(h)
    explicit = _explicit_url(h)
    if explicit:
        same = explicit == _url(*fleet)
        return Item(target, "unchanged" if same else "preserved",
                    "fabric_join.control_plane_url " + ("matches the fleet config" if same else
                                                         f"differs from the fleet config ({_url(*fleet)}); left untouched"))
    if have == fleet:
        return Item(target, "unchanged", f"fabric_join listener already {_url(*fleet)}")
    if block.get("listen_host") is not None or block.get("listen_port") is not None:
        return Item(target, "preserved", f"fabric_join names a different listener; the fleet config says {_url(*fleet)}; left untouched")
    lines = f"  listen_host: {fleet[0]}\n  listen_port: {fleet[1]}\n"
    top = re.search(r"(?m)^fabric_join:[ \t]*$", text)
    if "fabric_join" in machine_config(h) and not top:
        return Item(target, "preserved", "fabric_join is not a plain block mapping; add listen_host and listen_port by hand")
    if top:
        new = text[: top.end()] + "\n" + lines.rstrip("\n") + text[top.end():]
    else:
        sep = "" if not text or text.endswith("\n") else "\n"
        new = f"{text}{sep}\n{_BLOCK_COMMENT}fabric_join:\n{lines}  auto_join_on_session_start: false\n"
    if not dry_run:
        _write_text(path, new)
    return Item(target, "installed", f"fabric_join listener {_url(*fleet)} (from the fleet config)")


# --------------------------------------------------------------------------
# opt-in session-start hook (Claude Code only), manifest tracked
# --------------------------------------------------------------------------

def _manifest_path(home: Path) -> Path:
    return home / ".local/share/herdr-engineering/skills" / HOOKS_MANIFEST


def _load_manifest(home: Path) -> dict:
    try:
        data = json.loads(_manifest_path(home).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _dq(word: str) -> str:
    return '"' + "".join("\\" + c if c in '\\"$`' else c for c in word) + '"'


def hook_command() -> str:
    import shutil
    exe = shutil.which("herdr-eng")
    base = [exe] if exe else [sys.executable, "-m", "herdr_engineering.cli"]
    words = " ".join(_dq(w) for w in base)
    return f"[ -x {_dq(base[0])} ] && {words} summary join-hook claude || true # {HOOK_MARKER}"


def _is_ours(entry, command: str) -> bool:
    return isinstance(entry, dict) and any(isinstance(h, dict) and h.get("command") == command
                                           for h in entry.get("hooks", []) if isinstance(h, dict))


def _settings(path: Path) -> dict:
    data = json.loads(path.read_text()) if path.is_file() and path.read_text().strip() else {}
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict) \
            or not isinstance(data.get("hooks", {}).get(HOOK_EVENT, []), list):
        raise ValueError("unexpected structure")
    return data


def _save_settings(path: Path, data: dict, like: str) -> None:
    indent = 2
    for line in like.splitlines():
        if line.strip() and line.lstrip(" ") != line:
            indent = len(line) - len(line.lstrip(" "))
            break
    _write_text(path, json.dumps(data, indent=indent, ensure_ascii=False) + "\n")


def _save_manifest(home: Path, record: dict) -> None:
    mp = _manifest_path(home)
    if record:
        _write_text(mp, json.dumps(record, indent=2, sort_keys=True) + "\n")
    else:
        mp.unlink(missing_ok=True)


def _remove_ours(home: Path, dry_run: bool) -> Item | None:
    record = _load_manifest(home)
    for path_s, command in list(record.items()):
        path = Path(path_s)
        try:
            data = _settings(path)
        except (OSError, ValueError):
            return Item(path_s, "preserved", "settings.json is unreadable or has an unexpected shape; hook left untouched")
        lst = data.get("hooks", {}).get(HOOK_EVENT, [])
        kept = [e for e in lst if not _is_ours(e, command)]
        if len(kept) == len(lst):
            record.pop(path_s)
            continue
        if not dry_run:
            text = path.read_text()
            if kept:
                data["hooks"][HOOK_EVENT] = kept
            else:
                del data["hooks"][HOOK_EVENT]
                if not data["hooks"]:
                    del data["hooks"]
            _save_settings(path, data, text)
            record.pop(path_s)
        return_item = Item(path_s, "removed", f"{HOOK_EVENT} auto-join hook")
        if not dry_run:
            _save_manifest(home, record)
        return return_item
    if not dry_run and not record:
        _save_manifest(home, record)
    return None


def reconcile_hooks(home: Path | None = None, *, dry_run: bool = False) -> list[Item]:
    """Install the Claude SessionStart hook only when ``fabric_join.auto_join_on_session_start`` is true; otherwise remove OUR hook
    (if any) and install none. Existing user hooks are appended to, never replaced."""
    h = _home(home)
    if not auto_join_enabled(h):
        gone = _remove_ours(h, dry_run)
        return [gone] if gone else []
    out: list[Item] = []
    settings = h / ".claude/settings.json"
    command = hook_command()
    try:
        data = _settings(settings)
        text = settings.read_text() if settings.is_file() else ""
    except (OSError, ValueError):
        out.append(Item(str(settings), "preserved", "settings.json is unreadable or has an unexpected shape; hook not installed"))
    else:
        lst = data.setdefault("hooks", {}).setdefault(HOOK_EVENT, [])
        record = _load_manifest(h)
        old = record.get(str(settings))
        if old == command and any(_is_ours(e, command) for e in lst):
            out.append(Item(str(settings), "unchanged", f"{HOOK_EVENT} auto-join hook"))
        else:
            if old:
                lst[:] = [e for e in lst if not _is_ours(e, old)]
            lst.append({"hooks": [{"type": "command", "command": command, "timeout": 15}]})
            if not dry_run:
                _save_settings(settings, data, text)
                record[str(settings)] = command
                _save_manifest(h, record)
            out.append(Item(str(settings), "installed", f"{HOOK_EVENT} auto-join hook (HerdR panes only)"))
    out += [Item(f"hook:{name}", "unsupported", why) for name, why in UNSUPPORTED_HOOKS.items()]
    return out


def uninstall_hooks(home: Path | None = None, *, dry_run: bool = False) -> list[Item]:
    gone = _remove_ours(_home(home), dry_run)
    return [gone] if gone else []


# --------------------------------------------------------------------------
# Codex execpolicy rule (the narrow way out of the Codex sandbox), manifest tracked
# --------------------------------------------------------------------------
# Codex runs shell commands in a sandbox with no network, so the join cannot reach the tailnet listener ("control plane is
# unreachable"). An execpolicy ``prefix_rule`` lets exactly this command prefix run outside the sandbox (verified with
# ``codex execpolicy check`` and a real ``codex exec``). Nothing else is widened: no network_access, sandbox_mode or approval_policy.
CODEX_RULES_REL = Path(".codex/rules/default.rules")
CODEX_RULE = 'prefix_rule(pattern=["herdr-eng", "rooms", "join-fabric"], decision="allow")'
CODEX_RULE_NOTE = "  # herdr-join: runs this one command outside the Codex sandbox so it can reach the tailnet listener"
CODEX_RULE_MANIFEST = ".herdr-join-codex-rule-manifest.json"


def _codex_manifest(home: Path) -> Path:
    return home / ".local/share/herdr-engineering/skills" / CODEX_RULE_MANIFEST


def _codex_rule_present(text: str) -> bool:
    return any(line.strip().startswith(CODEX_RULE) for line in text.splitlines())


def reconcile_codex_rule(home: Path | None = None, *, dry_run: bool = False) -> list[Item]:
    """Append the join prefix rule to ``~/.codex/rules/default.rules`` (only when ``~/.codex`` exists). Idempotent; other rules are
    never touched; a hand-written identical rule is adopted as the user's, not ours."""
    h = _home(home)
    path = h / CODEX_RULES_REL
    if not (h / ".codex").is_dir():
        return [Item(str(path), "skipped", "Codex is not installed on this host (no ~/.codex)")]
    text = path.read_text() if path.is_file() else ""
    if _codex_rule_present(text):
        return [Item(str(path), "unchanged", "execpolicy rule for `herdr-eng rooms join-fabric` present")]
    if not dry_run:
        sep = "" if not text or text.endswith("\n") else "\n"
        _write_text(path, f"{text}{sep}{CODEX_RULE}{CODEX_RULE_NOTE}\n")
        _write_text(_codex_manifest(h), json.dumps({str(path): CODEX_RULE}, indent=2) + "\n")
    return [Item(str(path), "installed", "execpolicy rule: only `herdr-eng rooms join-fabric` runs outside the Codex sandbox")]


def uninstall_codex_rule(home: Path | None = None, *, dry_run: bool = False) -> list[Item]:
    """Remove the rule line WE added (per the manifest); a rule the user wrote is never removed."""
    h = _home(home)
    manifest = _codex_manifest(h)
    try:
        record = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return []
    out: list[Item] = []
    for path_s, rule in (record.items() if isinstance(record, dict) else []):
        path = Path(path_s)
        try:
            lines = path.read_text().splitlines(keepends=True)
        except OSError:
            continue
        kept = [ln for ln in lines if not ln.strip().startswith(rule)]
        if len(kept) != len(lines):
            if not dry_run:
                _write_text(path, "".join(kept))
            out.append(Item(path_s, "removed", "execpolicy rule for `herdr-eng rooms join-fabric`"))
    if not dry_run:
        manifest.unlink(missing_ok=True)
    return out


def agent_name(harness: str, env) -> str | None:
    """``<harness>-<pane>`` lower-cased and made to fit the server's name rule; None outside a HerdR pane."""
    if not (env.get("HERDR_WORKSPACE_ID") or "").strip():
        return None
    pane = (env.get("HERDR_PANE_ID") or env.get("HERDR_WORKSPACE_ID") or "").strip()
    name = _NAME_BAD.sub("-", f"{harness}-{pane}".lower()).lstrip("-._")[:64]
    return name or None


def run_join_hook(harness: str, env=None, out=None) -> int:
    """The SessionStart hook body. Never fails the session: any error is swallowed, the exit code is always 0."""
    env = os.environ if env is None else env
    out = out or sys.stdout
    try:
        name = agent_name(harness, env)
        if not name:
            return 0
        proc = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "rooms", "join-fabric", name, "--json"],
                              capture_output=True, text=True, timeout=12, env=dict(env), stdin=subprocess.DEVNULL)
        if proc.returncode == 0:
            print(f"HerdR: joined the workspace room as {name}", file=out)
    except Exception:
        pass
    return 0


# --------------------------------------------------------------------------
# --check
# --------------------------------------------------------------------------

def _probe(url: str) -> Item:
    try:
        urllib.request.urlopen(urllib.request.Request(url + "/", method="GET"), timeout=3)  # noqa: S310
    except urllib.error.HTTPError:
        return Item("fabric listener", "ok", f"{url} answers")  # any HTTP answer proves the listener is up
    except (TimeoutError, ConnectionRefusedError, urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError):
            return Item("fabric listener", "refused", f"{url} refused the connection")
        return Item("fabric listener", "unreachable", f"{url} did not answer (off the tailnet?); not a failure")
    return Item("fabric listener", "ok", f"{url} answers")


def check_setup(home: Path | None = None, *, probe: bool = True) -> list[Item]:
    """Listener resolution, reachability and hook state (skills are verified by session_summary.verify)."""
    h = _home(home)
    found = resolve_listener(h)
    if found is None:
        out = [Item("fabric listener", "unresolved",
                    "not in the host config, fabric_join in config.yaml or a readable fleet config; run: herdr-eng summary install")]
    else:
        out = [Item("fabric listener", "ok", f"resolved {found[0]} from {found[1]}")]
        if probe:
            out.append(_probe(found[0]))
    if (h / ".codex").is_dir():
        rules = h / CODEX_RULES_REL
        have = rules.is_file() and _codex_rule_present(rules.read_text())
        out.append(Item("codex execpolicy rule", "ok" if have else "missing",
                        "`herdr-eng rooms join-fabric` may run outside the Codex sandbox" if have else
                        "Codex's sandbox blocks the tailnet listener; run: herdr-eng summary install"))
    settings = h / ".claude/settings.json"
    command = _load_manifest(h).get(str(settings))
    present = False
    if command:
        try:
            present = any(_is_ours(e, command) for e in _settings(settings).get("hooks", {}).get(HOOK_EVENT, []))
        except (OSError, ValueError):
            present = False
    if auto_join_enabled(h):
        out.append(Item("claude SessionStart hook", "ok" if present else "missing",
                        "auto_join_on_session_start is true" + ("" if present else "; run: herdr-eng summary install")))
    else:
        out.append(Item("claude SessionStart hook", "stale" if present else "ok",
                        "auto_join_on_session_start is false (default); no hook installed" if not present
                        else "auto_join_on_session_start is false but our hook is present; run: herdr-eng summary install"))
    return out
