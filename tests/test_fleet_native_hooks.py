"""Fleet hook rollout exercises only temporary configs and a fake native installer."""
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/deploy-fleet-agents.sh"


@pytest.fixture
def host(tmp_path):
    home = tmp_path / "host with spaces"
    binaries = home / ".local/bin"
    binaries.mkdir(parents=True)
    log = home / "calls.jsonl"
    installer = binaries / "herdr"
    installer.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home = Path.home()
with (home / "calls.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
assert sys.argv[1:3] == ["integration", "install"]
agent = sys.argv[3]
configs = {
    "codex": (Path(os.environ.get("CODEX_HOME", home / ".codex")),
              ["config.toml", "hooks.json", "herdr-agent-state.sh"]),
    "claude": (Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")),
               ["settings.json", "hooks/herdr-agent-state.sh"]),
    "pi": (Path(os.environ.get("PI_CODING_AGENT_DIR", home / ".pi/agent")),
           ["extensions/herdr-agent-state.ts"]),
}
root, files = configs[agent]
for name in files:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if "native-herdr-hook" not in existing:
        path.write_text(existing + "native-herdr-hook\\n")
print("private-config-placeholder-secret")
if os.environ.get("FAIL_AGENT") == agent:
    raise SystemExit(1)
''')
    installer.chmod(0o755)
    for agent, config in (("codex", ".codex"), ("claude", ".claude"),
                          ("pi", ".pi/agent/extensions")):
        runtime = binaries / agent
        runtime.write_text("#!/bin/sh\nexit 99\n")
        runtime.chmod(0o755)
        (home / config).mkdir(parents=True)
    env = dict(os.environ, HOME=str(home), PATH="/usr/bin:/bin")
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        env.pop(name, None)
    return home, env, log


def install(host):
    # Exercise the same quoted function transport used by the remote SSH shell.
    script = SCRIPT.read_text()
    function = script.split("install_native_hooks() {", 1)[1].split("\nrestart_cmd()", 1)[0]
    transport = script.split('  hook_script="$(declare -f', 1)[1].split("\n\n", 1)[0]
    transport = 'hook_script="$(declare -f' + transport
    transport = transport.replace('if ! run "$target"', 'if ! bash -c')
    source = "install_native_hooks() {" + function + "\nfailed=0\n" + transport + "\nexit $failed\n"
    return subprocess.run(["bash", "-c", source], env=host[1], text=True,
                          capture_output=True, timeout=15)


def calls(host):
    return [json.loads(line) for line in host[2].read_text().splitlines()]


def test_complete_runtime_bundle_includes_every_local_import():
    script = SCRIPT.read_text()
    files = set(re.search(r'^PLUGIN_FILES="([^"]+)"', script, re.M).group(1).split())
    assert {"session-transcript.js", "session-helpers.js"} <= files
    for name in files:
        path = ROOT / "deploy/herdr-cp-feed-plugin" / name
        assert path.exists(), name
        if name.endswith(".js"):
            imports = re.findall(r'''from\s+["']\./([^"']+)["']''', path.read_text())
            assert set(imports) <= files, (name, set(imports) - files)


def test_installs_only_native_hooks_and_preserves_configs_on_repeated_run(host):
    home, _, _ = host
    existing = home / ".codex/hooks.json"
    existing.write_text("existing-user-hooks\n")
    first = install(host)
    assert first.returncode == 0, first.stderr
    state = existing.read_text()
    second = install(host)
    assert second.returncode == 0, second.stderr
    assert existing.read_text() == state == "existing-user-hooks\nnative-herdr-hook\n"
    assert calls(host) == [["integration", "install", target]
                           for _ in range(2) for target in ("codex", "claude", "pi")]
    assert "private-config-placeholder-secret" not in first.stdout + first.stderr
    backups = home / ".local/state/herdr-engineering/hook-backups"
    assert stat.S_IMODE(backups.stat().st_mode) == 0o700
    for path in backups.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert any(p.read_text() == "existing-user-hooks\n" for p in backups.glob("codex-*/hooks.json"))


def test_failure_restores_existing_files_removes_new_files_and_continues(host):
    home, env, _ = host
    config = home / ".codex/config.toml"
    config.write_text("original-private-settings\n")
    config.chmod(0o640)
    env["FAIL_AGENT"] = "codex"
    result = install(host)
    assert result.returncode == 1
    assert config.read_text() == "original-private-settings\n"
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert not (home / ".codex/hooks.json").exists()
    assert not (home / ".codex/herdr-agent-state.sh").exists()
    assert [c[-1] for c in calls(host)] == ["codex", "claude", "pi"]
    assert "codex hooks: FAILED" in result.stdout
    assert "private-config-placeholder-secret" not in result.stdout + result.stderr


def test_missing_runtime_is_skipped_and_installed_runtime_missing_config_fails(host):
    home, _, _ = host
    (home / ".local/bin/pi").unlink()
    (home / ".claude").rmdir()
    result = install(host)
    assert result.returncode == 1
    assert "pi hooks: runtime not installed, skipped" in result.stdout
    assert "claude hooks: FAILED" in result.stdout
    assert calls(host) == [["integration", "install", "codex"]]


def test_custom_config_roots_are_backed_up_and_used(host):
    home, env, _ = host
    for variable in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        directory = home / "custom" / variable
        directory.mkdir(parents=True)
        env[variable] = str(directory)
    (Path(env["PI_CODING_AGENT_DIR"]) / "extensions").mkdir()
    result = install(host)
    assert result.returncode == 0, result.stderr
    assert (Path(env["CODEX_HOME"]) / "hooks.json").is_file()
    assert (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").is_file()
    assert (Path(env["PI_CODING_AGENT_DIR"]) / "extensions/herdr-agent-state.ts").is_file()
    assert not (home / ".codex/hooks.json").exists()
