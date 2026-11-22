"""Automatic handoff triggers (herdr_engineering.handoff_autopilot).

The operator requirement this proves: a handoff is never something the human has to
ask for. The harness boundary that knows the session is nearly out of context - pi's
`context_is_full` event, Claude Code's `Stop` hook - is observed by installed code,
the verdict is decided by policy here, and the agent is told to run the `handoff`
skill itself.

Tests are real: real files under a temporary HOME, the real CLI in a subprocess, the
real Claude Code transcript format, the real Pi extension API surface. The only
stand-in is the Pi host itself (`pi.on` / `pi.sendUserMessage`), which cannot be
driven headlessly; the Node test in packages/ drives the real extension against it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering import handoff_autopilot as ha

CLI = [sys.executable, "-m", "herdr_engineering.cli"]


# Shell operators that must stay unquoted when a hook command is rebuilt token by token.
_SHELL_OPERATORS = frozenset({"[", "]", "&&", "||", "-x", "-f", "!"})


def _shell_join(tokens: list[str]) -> str:
    """Rebuild a command line, quoting arguments but leaving shell operators as operators.

    `shlex.join` quotes `[`, `]` and `&&`, which turns a guarded hook command into
    `[: missing ]` - a syntax error that looks like the guard failed.
    """
    return " ".join(token if token in _SHELL_OPERATORS else shlex.quote(token) for token in tokens)


def _cli(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        CLI + args, capture_output=True, text=True, timeout=120, cwd=str(cwd), env=env
    )


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A real HOME/XDG tree with no handoff autopilot installed yet."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    work = tmp_path / "work"
    for path in (home, state, work):
        path.mkdir(parents=True)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_STATE_HOME": str(state),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        }
    )
    # Anything the developer's own machine sets would otherwise leak into the run: the opt-out,
    # the CLI override the hook embeds, and the state and config roots that decide where files
    # go. `install status` is the worst case - it compares what is on disk against what is
    # configured, so a leaked path makes an absent install look present, or the reverse.
    inherited = [
        "HERDR_HANDOFF_AUTOPILOT",
        "HERDR_ENG_BIN",
        "HERDR_ENG_ARGS",
        "HERDR_ENGINEERING_STATE",
        "HERDR_ENGINEERING_CONFIG",
    ]
    for name in inherited:
        env.pop(name, None)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return {"tmp": tmp_path, "home": home, "state": state, "work": work, "env": env}


def write_config(home: Path, body: str) -> Path:
    path = home / ".config" / "herdr-engineering" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def write_transcript(
    path: Path,
    *,
    session_id: str,
    input_tokens: int,
    cache_read: int,
    cache_write: int = 0,
    model: str = "claude-opus-4-5",
) -> Path:
    """A Claude Code transcript in the shape the installed CLI actually writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "sessionId": session_id, "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "done"}],
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_write,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": 857,
                },
            },
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


# --------------------------------------------------------------------------- unit


def test_automatic_handoff_is_on_by_default(host: dict) -> None:
    policy = ha.policy()
    assert policy.enabled is True
    assert policy.context_percent == pytest.approx(50.0)
    assert policy.max_per_session >= 1
    assert {"pi", "claude"} <= set(policy.harnesses)


def test_triggers_when_context_is_over_the_threshold(host: dict) -> None:
    decision = ha.decide(session_id="s-full", harness="pi", percent=62.0)
    assert decision.trigger is True
    assert decision.code == "context_is_full"
    # The prompt makes the agent run the handoff skill itself; it never asks the human.
    assert "handoff" in decision.prompt
    assert "without asking" in decision.prompt
    assert "--allow-dirty-source" in decision.prompt  # the refusal edge stays honest
    assert str(int(decision.percent)) in decision.prompt


def test_does_not_trigger_while_context_is_comfortable(host: dict) -> None:
    decision = ha.decide(session_id="s-empty", harness="pi", percent=12.0)
    assert decision.trigger is False
    assert decision.code == "below_threshold"
    assert decision.prompt == ""


def test_percent_is_derived_from_tokens_when_the_harness_reports_none(host: dict) -> None:
    # pi returns percent: null when tokensUsed or contextWindow is 0.
    decision = ha.decide(
        session_id="s-tokens", harness="pi", percent=None, tokens=170_000, context_window=200_000
    )
    assert decision.trigger is True
    assert decision.percent == pytest.approx(85.0)


def test_unknown_context_never_triggers(host: dict) -> None:
    decision = ha.decide(
        session_id="s-unknown", harness="pi", percent=None, tokens=None, context_window=None
    )
    assert decision.trigger is False
    assert decision.code == "unknown_context"


def test_config_can_opt_out_and_the_reason_names_the_way_back(host: dict) -> None:
    write_config(host["home"], "handoff_autopilot:\n  enabled: false\n")
    decision = ha.decide(session_id="s-optout", harness="pi", percent=90.0)
    assert decision.trigger is False
    assert decision.code == "disabled"
    assert "handoff_autopilot" in decision.reason
    assert "config.yaml" in decision.reason


def test_environment_can_opt_out_for_one_session(
    host: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_HANDOFF_AUTOPILOT", "off")
    decision = ha.decide(session_id="s-env", harness="pi", percent=90.0)
    assert decision.trigger is False
    assert decision.code == "disabled"
    assert "HERDR_HANDOFF_AUTOPILOT" in decision.reason


def test_unsupported_harness_defers_to_the_next_boundary(host: dict) -> None:
    decision = ha.decide(session_id="s-codex", harness="codex", percent=90.0)
    assert decision.trigger is False
    assert decision.code == "harness_unsupported"
    assert "codex" in decision.reason


def test_one_trigger_per_session_by_default(host: dict) -> None:
    ha.record_trigger(session_id="s-once", harness="pi", percent=61.0, prompt="go")
    decision = ha.decide(session_id="s-once", harness="pi", percent=90.0)
    assert decision.trigger is False
    assert decision.code == "already_triggered"
    assert "1" in decision.reason


def test_repeat_limit_is_configurable(host: dict) -> None:
    write_config(host["home"], "handoff_autopilot:\n  max_per_session: 2\n")
    ha.record_trigger(session_id="s-twice", harness="pi", percent=61.0, prompt="go")
    assert ha.decide(session_id="s-twice", harness="pi", percent=90.0).trigger is True
    ha.record_trigger(session_id="s-twice", harness="pi", percent=92.0, prompt="go again")
    assert ha.decide(session_id="s-twice", harness="pi", percent=95.0).trigger is False


def test_a_handoff_already_underway_suppresses_a_second_trigger(host: dict) -> None:
    root = host["work"] / ".herdr" / "handoffs"
    root.mkdir(parents=True)
    (root / "handoff_live.json").write_text(
        json.dumps(
            {
                "handoff_id": "handoff_live",
                "schema_version": 2,
                "state": "in_progress",
                "source": {"harness": "pi", "session_id": "s-live"},
                "target": {"harness": "pi"},
                "context": {},
            }
        )
    )
    suppressed = ha.decide(session_id="s-live", harness="pi", percent=90.0, cwd=host["work"])
    assert suppressed.trigger is False
    assert suppressed.code == "handoff_underway"
    assert "handoff_live" in suppressed.reason

    # A finished handoff for the same session is not a reason to stay silent.
    (root / "handoff_live.json").write_text(
        json.dumps(
            {
                "handoff_id": "handoff_live",
                "schema_version": 2,
                "state": "completed",
                "source": {"harness": "pi", "session_id": "s-live"},
                "target": {"harness": "pi"},
                "context": {},
            }
        )
    )
    assert (
        ha.decide(session_id="s-live", harness="pi", percent=90.0, cwd=host["work"]).trigger is True
    )


def test_trigger_ledger_is_auditable_and_per_session(host: dict) -> None:
    ha.record_trigger(session_id="s-a", harness="pi", percent=61.0, prompt="go")
    ha.record_trigger(session_id="s-a", harness="pi", percent=77.0, prompt="go again")
    ha.record_trigger(session_id="s-b", harness="claude", percent=55.0, prompt="go")
    ledger = ha.read_ledger()
    assert ledger["version"] == 1
    assert ledger["sessions"]["s-a"]["count"] == 2
    assert ledger["sessions"]["s-a"]["harness"] == "pi"
    assert ledger["sessions"]["s-a"]["last_percent"] == pytest.approx(77.0)
    assert ledger["sessions"]["s-b"]["count"] == 1
    assert ledger["sessions"]["s-b"]["last_at"] != ledger["sessions"]["s-a"]["last_at"]
    mode = stat.S_IMODE(ha.state_path().stat().st_mode)
    assert mode == 0o600


def test_a_corrupt_ledger_is_ignored_but_reported(host: dict) -> None:
    ha.state_path().parent.mkdir(parents=True, exist_ok=True)
    ha.state_path().write_text("{not json")
    assert ha.read_ledger() == {"version": 1, "sessions": {}}
    assert "unreadable" in ha.status()["ledger"]["note"]


# --------------------------------------------------------------------- integration


def test_cli_check_decides_without_recording(host: dict) -> None:
    done = _cli(
        [
            "handoff",
            "autopilot",
            "check",
            "--session-id",
            "s1",
            "--harness",
            "pi",
            "--percent",
            "62",
            "--json",
        ],
        host["env"],
        host["work"],
    )
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["trigger"] is True and payload["code"] == "context_is_full"
    assert payload["prompt"] != ""
    assert not ha.state_path().exists()  # a check must not consume the trigger


def test_cli_record_consumes_the_trigger_for_that_session(host: dict) -> None:
    for percent in ("62", "90"):
        done = _cli(
            [
                "handoff",
                "autopilot",
                "record",
                "--session-id",
                "s2",
                "--harness",
                "pi",
                "--percent",
                percent,
                "--json",
            ],
            host["env"],
            host["work"],
        )
        assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["count"] == 2
    check = _cli(
        [
            "handoff",
            "autopilot",
            "check",
            "--session-id",
            "s2",
            "--harness",
            "pi",
            "--percent",
            "95",
            "--json",
        ],
        host["env"],
        host["work"],
    )
    assert json.loads(check.stdout)["code"] == "already_triggered"


def test_cli_reports_invalid_percent_as_usage_error(host: dict) -> None:
    done = _cli(
        ["handoff", "autopilot", "check", "--session-id", "s3", "--percent", "140"],
        host["env"],
        host["work"],
    )
    assert done.returncode == 2
    assert "percent" in done.stdout.lower() or "percent" in done.stderr.lower()


def test_install_is_default_idempotent_and_preserves_foreign_hooks(host: dict) -> None:
    settings = host["home"] / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {"type": "command", "command": "printf '%s' 'someone elses hook'"}
                            ],
                        }
                    ]
                },
                "theme": "dark",
            }
        )
    )

    first = _cli(["handoff", "autopilot", "install", "--json"], host["env"], host["work"])
    assert first.returncode == 0, first.stderr
    created = json.loads(first.stdout)
    kinds = {item["kind"] for item in created["items"]}
    assert {"pi-extension", "claude-hook", "state-dir"} <= kinds
    # "updated" is the honest status for a settings file that already existed with foreign hooks.
    assert all(item["status"] in {"created", "updated", "unchanged"} for item in created["items"])

    extension = ha.extension_path(home=host["home"])
    text = extension.read_text()
    assert "turn_end" in text and "getContextUsage" in text  # the boundary that still has it all
    assert "sendUserMessage" in text and "inject: false" in text  # silent, actionable injection
    # The argv it runs is the CLI's decision point; line-wrapping must not hide that it exists.
    argv = "".join(text.split())
    assert '["handoff","autopilot","check"' in argv  # asks the real CLI, decides nothing itself
    assert '["handoff","autopilot","record"' in argv  # consumes only after delivering
    assert "--percent" in argv and "--session-id" in argv  # reports what it measured

    merged = json.loads(settings.read_text())
    assert merged["theme"] == "dark"  # unrelated settings survive
    assert any(
        "someone elses hook" in json.dumps(group) for group in merged["hooks"]["SessionStart"]
    )
    events = set(merged["hooks"])
    assert {"Stop", "PreCompact"} & events
    our_commands = [hook["command"] for group in merged["hooks"]["Stop"] for hook in group["hooks"]]
    assert any("handoff autopilot hook" in command for command in our_commands)

    second = _cli(["handoff", "autopilot", "install", "--json"], host["env"], host["work"])
    assert second.returncode == 0, second.stderr
    assert all(item["status"] == "unchanged" for item in json.loads(second.stdout)["items"])


def test_uninstall_removes_only_what_install_added(host: dict) -> None:
    settings = host["home"] / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {"type": "command", "command": "printf '%s' 'someone elses hook'"}
                            ],
                        }
                    ]
                }
            }
        )
    )
    _cli(["handoff", "autopilot", "install"], host["env"], host["work"])
    before = json.loads(settings.read_text())
    foreign = [
        hook
        for group in before["hooks"].get("SessionStart", [])
        for hook in group["hooks"]
        if "someone elses hook" in hook.get("command", "")
    ]
    assert foreign, "the fixture must seed a foreign hook to preserve"
    removed = _cli(["handoff", "autopilot", "uninstall", "--json"], host["env"], host["work"])
    assert removed.returncode == 0, removed.stderr
    assert not ha.extension_path(home=host["home"]).exists()
    after = json.loads(settings.read_text())
    assert not any(
        "handoff autopilot hook" in json.dumps(group) for group in after["hooks"].get("Stop", [])
    )
    assert not any(
        "handoff autopilot hook" in json.dumps(group)
        for group in after["hooks"].get("PreCompact", [])
    )
    assert any(
        "someone elses hook" in json.dumps(group)
        for group in after["hooks"].get("SessionStart", [])
    )
    # Uninstalling twice is not an error; the ledger is kept as the audit record.
    again = _cli(["handoff", "autopilot", "uninstall", "--json"], host["env"], host["work"])
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["ok"] is True


def test_status_reports_the_policy_paths_and_recent_triggers(host: dict) -> None:
    _cli(["handoff", "autopilot", "install", "--json"], host["env"], host["work"])
    _cli(
        [
            "handoff",
            "autopilot",
            "record",
            "--session-id",
            "s3",
            "--harness",
            "pi",
            "--percent",
            "66",
            "--json",
        ],
        host["env"],
        host["work"],
    )
    done = _cli(["handoff", "autopilot", "status", "--json"], host["env"], host["work"])
    assert done.returncode == 0, done.stderr
    status = json.loads(done.stdout)
    assert status["policy"]["enabled"] is True
    assert status["installed"]["pi_extension"] is True
    assert status["ledger"]["sessions"]["s3"]["count"] == 1
    assert "next boundary" in status["summary"]
    assert status["installed"]["claude_hooks"]["Stop"] is True


def test_doctor_reports_the_autopilot_and_names_the_fix(host: dict) -> None:
    missing = ha.doctor_check(home=host["home"])
    assert missing["name"] == "handoff-autopilot"
    assert missing["status"] == "warn"
    assert "handoff autopilot install" in missing["fix"]

    _cli(["handoff", "autopilot", "install", "--json"], host["env"], host["work"])
    installed = ha.doctor_check(home=host["home"])
    assert installed["status"] == "ok"
    assert installed["data"]["context_percent"] == pytest.approx(50.0)

    write_config(host["home"], "handoff_autopilot:\n  enabled: false\n")
    opted_out = ha.doctor_check(home=host["home"])
    assert opted_out["status"] == "skip"
    assert "enabled: false" in opted_out["detail"]


# ------------------------------------------------------------------ claude boundary


def test_claude_percent_comes_from_the_real_transcript_usage_fields(host: dict) -> None:
    transcript = write_transcript(
        host["work"] / "t.jsonl",
        session_id="s-cc",
        input_tokens=2,
        cache_write=2_602,
        cache_read=104_948,
    )
    percent = ha.transcript_percent(transcript, context_window=200_000)
    assert percent == pytest.approx((2 + 2_602 + 104_948) / 2_000.0)


def test_claude_hook_injects_the_prompt_and_always_exits_zero(host: dict) -> None:
    transcript = write_transcript(
        host["work"] / "full.jsonl",
        session_id="s-cc-full",
        input_tokens=2,
        cache_write=3_000,
        cache_read=150_000,
    )
    # The hook reads its event from stdin, exactly as Claude Code passes it.
    done = subprocess.run(
        CLI + ["handoff", "autopilot", "hook", "--harness", "claude", "--record", "--json"],
        input=json.dumps(
            {
                "session_id": "s-cc-full",
                "transcript_path": str(transcript),
                "hook_event_name": "Stop",
                "cwd": str(host["work"]),
            }
        ),
        capture_output=True,
        text=True,
        timeout=120,
        env=host["env"],
        cwd=str(host["work"]),
    )
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["trigger"] is True
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "handoff" in payload["hookSpecificOutput"]["additionalContext"]
    assert ha.read_ledger()["sessions"]["s-cc-full"]["count"] == 1

    # A second Stop in the same session is silenced by the per-session cap.
    again = subprocess.run(
        CLI + ["handoff", "autopilot", "hook", "--harness", "claude", "--record", "--json"],
        input=json.dumps({"session_id": "s-cc-full", "transcript_path": str(transcript)}),
        capture_output=True,
        text=True,
        timeout=120,
        env=host["env"],
        cwd=str(host["work"]),
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["trigger"] is False


def test_claude_hook_fails_open_on_unusable_input(host: dict) -> None:
    for payload in (
        "",
        "{not json",
        json.dumps({"session_id": "s-x"}),
        json.dumps({"session_id": "s-y", "transcript_path": "/nope/t.jsonl"}),
    ):
        done = subprocess.run(
            CLI + ["handoff", "autopilot", "hook", "--harness", "claude"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
            env=host["env"],
            cwd=str(host["work"]),
        )
        assert done.returncode == 0, f"{payload!r} -> {done.returncode}: {done.stderr}"
        assert done.stdout.strip() in ("", "{}") or json.loads(done.stdout)["trigger"] is False


# ------------------------------------------------------------------------- e2e


@pytest.mark.e2e
def test_pi_extension_and_cli_hand_off_without_a_human(host: dict) -> None:
    """Install for real, then drive the real extension with a recording Pi host.

    Proves the whole chain with no mocks of our own code: the shipped extension calls
    the installed CLI, the CLI applies policy and the ledger, and the extension injects
    exactly one message.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI ships node for the packages suite
        pytest.skip("node is not installed on this host")
    # The same rule the packages suite applies, from the same probe: importing a .ts extension
    # needs Node's native TypeScript support. Skipping here rather than failing keeps the reason
    # identical in both suites, so a red run always means the extension is broken.
    probe = subprocess.run(
        [node, "-e", "process.exit(process.features.typescript ? 0 : 1)"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        pytest.skip("node cannot import a .ts extension without native TypeScript")
    installed = _cli(["handoff", "autopilot", "install", "--json"], host["env"], host["work"])
    assert installed.returncode == 0, installed.stderr
    extension = ha.extension_path(home=host["home"])
    assert extension.exists()

    runner = host["tmp"] / "drive.mjs"
    repo_root = str(Path(ha.__file__).resolve().parent.parent)
    runner.write_text(f"""
import {{ readFile }} from "node:fs/promises";
import assert from "node:assert/strict";

const handlers = new Map();
const injected = [];
const notifications = [];
const api = {{
  on(event, callback) {{ handlers.set(event, callback); }},
  sendUserMessage(message, options) {{ injected.push([message, options]); }},
  exec() {{ return Promise.resolve({{ code: 127, stdout: "", stderr: "" }}); }},
}};
const sessionManager = {{
  getSessionId: () => "s-node",
  getBranch: () => [],
  getType: () => undefined,
  getEntry: () => undefined,
  getTree: () => [],
}};
const ctx = {{
  model: {{ contextWindow: 200000 }},
  sessionId: "s-node",
  sessionManager,
  getContextUsage: () => ({{ tokensUsed: 170000, contextWindow: 200000, percent: 85 }}),
  getSelector: () => ({{ get: () => undefined }}),
  ui: {{ notify: (message, level) => notifications.push([message, level]) }},
  getState: () => ({{ thinkingLevel: "medium", isStreaming: false, tools: [], messages: [] }}),
}};

const mod = await import({json.dumps(str(extension))});
mod.default(api);
const turnEnd = handlers.get("turn_end");
assert.equal(typeof turnEnd, "function", "the installed extension must subscribe to turn_end");
await turnEnd({{ toolResults: [], textContent: "", messages: [] }}, ctx);
await new Promise((resolve) => setTimeout(resolve, 4000));
assert.equal(injected.length, 1, `expected one injected instruction, saw ${{injected.length}}`);
assert.match(injected[0][0], /handoff/);
assert.equal(injected[0][1].inject, false);
assert.equal(notifications.length, 0);
const ledger = JSON.parse(await readFile({json.dumps(str(ha.state_path().as_posix()))}, "utf8"));
assert.equal(ledger.sessions["s-node"].count, 1);
assert.equal(ledger.sessions["s-node"].harness, "pi");
""")
    env = dict(host["env"])
    # The same argv install.sh hands pi: interpreter first, module after.
    # The CLI override, with the meaning both readers give it: the executable, then its arguments.
    env["HERDR_ENG_BIN"] = sys.executable
    env["HERDR_ENG_ARGS"] = json.dumps(["-m", "herdr_engineering.cli"])
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
    env["HERDR_TEST_PYTHON"] = sys.executable
    done = subprocess.run(
        # No flag: the probe above proved this node strips types, and it does so by default.
        # `--no-experimental-strip-types` would turn the opposite off and make the .ts import
        # fail with ERR_UNKNOWN_FILE_EXTENSION, which is a broken test, not a broken extension.
        [node, str(runner)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(host["work"]),
    )
    assert done.returncode == 0, f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"


# ------------------------------------------------------- installer and docs wiring


INSTALL_SH = Path(ha.__file__).resolve().parents[1] / "scripts" / "install.sh"
DOC = Path(ha.__file__).resolve().parents[1] / "docs" / "automatic-handoffs.md"


def test_the_installer_arms_automatic_handoffs_by_default() -> None:
    """`scripts/install.sh` is the only thing an operator runs; the trigger has to be in it.

    Asserted on the script text rather than by running it: the installer clones upstreams and
    writes the operator's real home. What is checked is the part that decides whether the
    trigger is installed - the default, the opt-out flag, and the step that runs the CLI.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'HANDOFF_AUTOPILOT="${HERDR_INSTALL_HANDOFF_AUTOPILOT:-1}"' in text, (
        "automatic handoffs must be the default, with an env override for automated installs"
    )
    assert "--no-handoff-autopilot) HANDOFF_AUTOPILOT=0 ;;" in text
    # `install.sh --help` prints lines 2-36 of the script; the opt-out has to be in that text.
    assert "--no-handoff-autopilot" in "\n".join(text.splitlines()[1:36]), (
        "the opt-out must be discoverable from `install.sh --help`"
    )
    assert '"$PY" -m herdr_engineering.cli handoff autopilot install' in text
    # The managed workflow hooks write the same settings.json; the autopilot installs after them
    # and only ever rewrites its own hook group, so the order is pinned here (7c workflow hooks,
    # 7d screenwright, 7e the autopilot - which is last because it is the only one that reads
    # policy and can therefore report what the others left behind).
    order = [text.index(f"# --- {step}.") for step in ("7c", "7d", "7e")]
    assert order == sorted(order), (
        "the autopilot must install after `workflow install`, not before it"
    )
    # A step that can fail must not take the whole installer down with it (`set -euo pipefail`).
    step = text.split("# --- 7e.", 1)[1].split("# --- 8.", 1)[0]
    assert "|| warn" in step, "a failed trigger install must warn, not abort the installer"
    syntax = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True, timeout=60
    )
    assert syntax.returncode == 0, syntax.stderr


def test_the_operator_doc_names_only_commands_that_exist() -> None:
    """A doc that tells an operator to type something the CLI rejects is worse than no doc.

    The same trap the pinned handoff skill tests guard: prose written ahead of, or behind, the
    parser. Every top-level `herdr-eng <command>` in the doc must be a real subcommand.
    """
    assert DOC.is_file(), "automatic handoffs need an operator-facing doc"
    text = DOC.read_text(encoding="utf-8")
    help_out = subprocess.run(
        CLI + ["--help"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(ha.__file__).resolve().parents[1]),
    ).stdout
    choices = set(re.findall(r"\n\s{0,2}\{([a-z0-9,\-]+)\}", help_out))
    known: set[str] = set()
    for group in choices:
        known.update(group.split(","))
    assert known, "could not read the CLI's subcommands from --help"
    named = set(re.findall(r"\bherdr-eng\s+([a-z][a-z0-9-]*)", text))
    assert named, "the doc names no herdr-eng commands at all, so it proves nothing"
    unknown = sorted(word for word in named if word not in known)
    assert not unknown, f"the doc names commands the CLI does not have: {unknown}"
    # The three verbs an operator needs must be spelled out, and the switches named exactly.
    for verb in ("status", "install", "uninstall"):
        assert f"handoff autopilot {verb}" in text, (
            f"the doc never mentions `handoff autopilot {verb}`"
        )
    assert "HERDR_HANDOFF_AUTOPILOT" in text and "handoff_autopilot" in text

    # Codes the doc tells an operator to expect must be codes something in this repo really answers
    # with - the autopilot itself, or the handoff vocabulary the operations runbook and the pinned
    # skill already use. Prose inventing a status is how an operator ends up chasing a ghost.
    root = Path(ha.__file__).resolve().parents[1]
    vocabulary = [Path(ha.__file__).read_text(encoding="utf-8")]
    for source in (
        root / "docs" / "operations" / "handoff.md",
        root / "herdr_engineering" / "skill_data" / "handoff" / "SKILL.md",
    ):
        vocabulary.append(source.read_text(encoding="utf-8"))
    coded = set(re.findall(r"`([a-z][a-z0-9_]{3,})`", text))
    interesting = {
        word
        for word in coded
        if re.search(
            r"(_unconfirmed|_failed$|_underway|_unpushed|_dirty|_not_ready|_unsupported|_is_full|_unavailable$|unknown_context)",
            word,
        )
    }
    invented = sorted(
        word for word in interesting if not any(word in source for source in vocabulary)
    )
    assert not invented, f"the doc names statuses nothing in this repo produces: {invented}"


def test_the_trigger_can_be_type_checked_against_the_pi_it_runs_on() -> None:
    """pi's extension API moves between builds; the trigger must be checkable against it.

    The fields this extension reads (`ContextUsage`, `sessionManager.getSessionId`, the silent
    `sendUserMessage` options) have changed names across pi versions. A tsconfig next to the
    extension, and a script that uses it, is how that drift gets caught before install time.
    """
    root = Path(ha.__file__).resolve().parents[1]
    tsconfig = json.loads(
        (root / "herdr_engineering" / "extension_data" / "tsconfig.json").read_text()
    )
    assert "herdr-handoff-autopilot.ts" in tsconfig["include"]
    assert tsconfig["compilerOptions"]["strict"] is True
    assert tsconfig["compilerOptions"]["noEmit"] is True
    scripts = json.loads((root / "package.json").read_text())["scripts"]
    assert scripts.get("typecheck:handoff-autopilot") == (
        "tsc -p herdr_engineering/extension_data/tsconfig.json"
    )


def test_the_autopilot_and_the_managed_workflow_hooks_share_settings_json(host: dict) -> None:
    """`install.sh` writes ~/.claude/settings.json twice: workflow hooks (7c), then the autopilot (7d).

    Both installers own a `PreCompact` entry in the same file, so each has to treat the other's
    group as foreign. Nothing proves that today except this test: the two installers were written
    in separate PRs, and the second one running first is exactly how a hook disappears.
    """
    claude_dir = host["home"] / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"

    workflow = _cli(["workflow", "install"], host["env"], host["work"])
    assert workflow.returncode == 0, workflow.stderr
    assert ha.HOOK_MARKER not in settings.read_text(), "the autopilot arrived before it was asked"

    autopilot = _cli(["handoff", "autopilot", "install"], host["env"], host["work"])
    assert autopilot.returncode == 0, autopilot.stderr
    text = settings.read_text()
    assert ha.HOOK_MARKER in text and "herdr-workflow:managed" in text, text

    hooks = json.loads(text)["hooks"]
    events = {
        event: [entry for group in groups for entry in group.get("hooks", [])]
        for event, groups in hooks.items()
    }
    managed = {
        event: commands
        for event, entries in events.items()
        if (commands := [c["command"] for c in entries if "herdr-workflow:managed" in c["command"]])
    }
    ours = {
        event: commands
        for event, entries in events.items()
        if (commands := [c["command"] for c in entries if ha.HOOK_MARKER in c["command"]])
    }
    # Whatever events upstream decides to manage, the autopilot must not disturb any of them,
    # and its own events are exactly the two that can carry an instruction.
    from herdr_engineering.workflow_hooks import EVENTS as WORKFLOW_EVENTS

    assert sorted(managed) == sorted(WORKFLOW_EVENTS), (sorted(managed), WORKFLOW_EVENTS)
    assert sorted(ours) == ["PreCompact", "Stop"], sorted(ours)
    # Both PreCompact hooks are still in the same event list, in the order Claude runs them.
    assert len(events["PreCompact"]) == 2, events["PreCompact"]

    # Removing the autopilot must leave the workflow hooks exactly where they were.
    before = json.dumps(json.loads(settings.read_text())["hooks"]["SessionStart"], sort_keys=True)
    removed = _cli(["handoff", "autopilot", "uninstall"], host["env"], host["work"])
    assert removed.returncode == 0, removed.stderr
    after_text = settings.read_text()
    assert ha.HOOK_MARKER not in after_text, "the autopilot removed more than it owns"
    assert "herdr-workflow:managed" in after_text
    after = json.loads(after_text)["hooks"]
    assert json.dumps(after["SessionStart"], sort_keys=True) == before
    assert [
        c["command"] for group in after["PreCompact"] for c in group.get("hooks", [])
    ] == managed["PreCompact"]

    # ... and the other way round: re-arming the autopilot then dropping the workflow hooks.
    assert _cli(["handoff", "autopilot", "install"], host["env"], host["work"]).returncode == 0
    assert _cli(["workflow", "uninstall"], host["env"], host["work"]).returncode == 0
    final = json.loads(settings.read_text())["hooks"]
    assert "SessionStart" not in final, list(final)
    assert [c["command"] for group in final["PreCompact"] for c in group.get("hooks", [])] and all(
        ha.HOOK_MARKER in c["command"] for c in final["PreCompact"][0]["hooks"]
    ), "the autopilot's PreCompact hook was taken with the workflow hooks"
    assert any(
        ha.HOOK_MARKER in c["command"] for group in final["Stop"] for c in group.get("hooks", [])
    )


def test_the_claude_hook_command_survives_a_missing_cli(host: dict) -> None:
    """A hook whose binary is gone must not turn every turn into a hook error.

    Claude Code treats a non-zero hook exit as a failure it reports to the session, and a CLI
    that was uninstalled - or a venv that moved - makes the command exit 127. The command is
    written guarded, the same shape the managed workflow hooks use, so a missing `herdr-eng`
    is silence instead of an error on every Stop and PreCompact. The control at the end runs
    the same missing binary unguarded: without the `[ -x ]` test the assertion above would
    only be proving that bash exits 0 for a reason the guard had nothing to do with.
    """
    (host["home"] / ".claude").mkdir()
    assert _cli(["handoff", "autopilot", "install"], host["env"], host["work"]).returncode == 0
    settings = json.loads((host["home"] / ".claude" / "settings.json").read_text())
    commands = [
        hook["command"]
        for group in settings["hooks"]["Stop"]
        for hook in group["hooks"]
        if ha.HOOK_MARKER in hook["command"]
    ]
    assert len(commands) == 1, commands
    command = commands[0]
    assert command.startswith("[ -x "), command
    assert command.rstrip().endswith("|| true"), command

    # Rebuild the installed command against a binary that does not exist, token by token:
    # `[ -x <bin> ] && <bin> handoff ... || true` names it twice, and string replacement would
    # silently miss both when shlex.quote leaves the path unquoted.
    tokens = shlex.split(command)
    binary, guard_test, invocation = tokens[2], tokens[1], tokens[5]
    assert guard_test == "-x" and invocation == binary, tokens
    missing = str(Path(binary).with_name("definitely-not-here"))
    replaced = [missing if token == binary else token for token in tokens]
    guarded = _shell_join(replaced)

    # Claude Code hands the hook its event on stdin, so that is how this runs.
    event = json.dumps(
        {"hook_event_name": "Stop", "session_id": "s-guard", "transcript_path": "/nope.jsonl"}
    )
    probe = subprocess.run(
        ["bash", "-c", guarded],
        input=event,
        capture_output=True,
        text=True,
        timeout=60,
        env=host["env"],
        cwd=str(host["work"]),
    )
    assert probe.returncode == 0, (probe.returncode, probe.stdout, probe.stderr)
    assert probe.stdout.strip() == "", probe.stdout

    # Control: the same missing binary, without the guard, is the failure the guard prevents.
    # Everything after `||` belongs to the guard, so the control stops at it.
    guarded_tail = tokens.index("||")
    bare = _shell_join([missing, *tokens[6:guarded_tail]])
    control = subprocess.run(
        ["bash", "-c", bare],
        input=event,
        capture_output=True,
        text=True,
        timeout=60,
        env=host["env"],
        cwd=str(host["work"]),
    )
    assert control.returncode != 0, (control.returncode, control.stdout)


def test_a_session_the_context_hook_already_warned_is_not_told_again(host: dict) -> None:
    """`handoff context-hook claude` (opt-in, UserPromptSubmit) warns at half the window.

    It keeps its own marker file in a different state directory, so the autopilot's
    per-session budget never saw it: with both installed a session got one instruction
    from each, which is the nagging the budget exists to prevent. The autopilot honours
    the other mechanism's marker; a marker root that is not a directory is not a reason
    to stall the decision.
    """
    write_config(host["home"], "handoff_autopilot:\n  enabled: true\n")
    markers = host["tmp"] / "hero-state"
    host["env"]["HERDR_ENGINEERING_STATE"] = str(markers)

    def check(session: str) -> dict:
        result = _cli(
            [
                "handoff",
                "autopilot",
                "check",
                "--harness",
                "claude",
                "--session-id",
                session,
                "--tokens",
                "185000",
                "--context-window",
                "200000",
                "--json",
            ],
            host["env"],
            host["work"],
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    assert check("session-a")["code"] == "context_is_full"

    # The context hook got there first, on the same session.
    (markers / "handoff-context").mkdir(parents=True)
    (markers / "handoff-context" / "session-a.fired").write_text("")
    warned = check("session-a")
    assert warned["code"] == "already_triggered", warned
    assert warned["trigger"] is False
    assert not ha.state_path(host["home"]).exists(), "a warning is not a trigger"

    # Another session is still told, and an unreadable marker root never blocks the decision.
    assert check("session-b")["code"] == "context_is_full"
    blocked = host["tmp"] / "blocked"
    blocked.write_text("not a directory")
    host["env"]["HERDR_ENGINEERING_STATE"] = str(blocked / "handoff-context")
    assert check("session-c")["code"] == "context_is_full"


def test_only_xdg_state_home_moves_the_trigger_ledger(
    host: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`XDG_STATE_HOME` moves the ledger; `HERDR_ENGINEERING_STATE` never touches it.

    The two names look interchangeable and are not. `HERDR_ENGINEERING_STATE` is the *other*
    mechanism's root: the directory holding `handoff context-hook` markers, read by
    `_context_hook_warned()`. A probe that redirects it expecting to sandbox the ledger writes
    straight to the operator's real `$XDG_STATE_HOME/herdr-engineering/handoff-autopilot.json`
    and spends a trigger for a session that never happened - and that file is the audit record
    of when the machine took over on its own, the one artifact `uninstall` deliberately keeps.
    The fixture deletes the inherited variable, so it is set again here: a probe's mistake has
    to be impossible, not merely unreproduced by the test environment.
    """
    write_config(host["home"], "handoff_autopilot:\n  enabled: true\n")
    scratch = host["tmp"] / "hero-state"
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(scratch))
    host["env"]["HERDR_ENGINEERING_STATE"] = str(scratch)
    expected = host["state"] / "herdr-engineering"

    assert ha.state_path().parent == expected, "XDG_STATE_HOME must decide the ledger location"

    ha.record_trigger(session_id="probe-session", harness="claude", percent=92.5)
    ledger = json.loads(ha.state_path().read_text())
    assert ledger["sessions"]["probe-session"]["count"] == 1, ledger

    # The installed command resolves the same way, in its own process.
    result = _cli(
        [
            "handoff",
            "autopilot",
            "record",
            "--harness",
            "claude",
            "--session-id",
            "probe-session",
            "--percent",
            "92.5",
            "--json",
        ],
        host["env"],
        host["work"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ledger = json.loads(ha.state_path().read_text())
    assert ledger["sessions"]["probe-session"]["count"] == 2, ledger

    assert not scratch.exists(), (
        "HERDR_ENGINEERING_STATE must not receive the ledger; "
        f"found {[p.name for p in scratch.iterdir()]}"
    )


def test_precompact_advises_without_spending_the_trigger(host: dict) -> None:
    """PreCompact cannot reach the model, so it must not consume the per-session trigger.

    The installed command carries --record on both events. With the default
    max_per_session of 1, a PreCompact that spent the budget left the next Stop answering
    `already_triggered`: the operator got a systemMessage saying "the next Stop hook will
    do it" and the agent was never told. Delivery is what consumes the trigger, and this
    event cannot deliver.
    """
    transcript = write_transcript(
        host["work"] / "full.jsonl",
        session_id="s-precompact",
        input_tokens=2,
        cache_write=3_000,
        cache_read=150_000,
    )

    def run(event: str) -> dict:
        done = subprocess.run(
            CLI + ["handoff", "autopilot", "hook", "--harness", "claude", "--record", "--json"],
            input=json.dumps(
                {
                    "session_id": "s-precompact",
                    "transcript_path": str(transcript),
                    "hook_event_name": event,
                    "cwd": str(host["work"]),
                }
            ),
            capture_output=True,
            text=True,
            timeout=120,
            env=host["env"],
            cwd=str(host["work"]),
        )
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    compacted = run("PreCompact")
    assert compacted["trigger"] is True, compacted
    assert "hookSpecificOutput" not in compacted, compacted
    assert "Stop" in compacted["systemMessage"], compacted
    assert compacted["recorded"] is False, compacted
    assert not ha.state_path().exists(), (
        "PreCompact consumed the trigger it could not deliver: "
        f"{ha.state_path().read_text() if ha.state_path().exists() else ''}"
    )

    # The promise the systemMessage made: the next Stop is the one that tells the agent.
    stopped = run("Stop")
    assert stopped["trigger"] is True, stopped
    assert stopped["hookSpecificOutput"]["additionalContext"].startswith(
        "[HerdR automatic handoff]"
    )
    assert stopped["recorded"]["count"] == 1, stopped["recorded"]
    assert ha.read_ledger()["sessions"]["s-precompact"]["count"] == 1


def test_usage_beyond_the_assumed_window_does_not_kill_the_trigger(host: dict) -> None:
    """Usage past the assumed window proves the assumption wrong; it must not become an error.

    Claude Code reports usage but never the window size, so 200k is an assumption. A session that
    has used 250k tokens cannot live in a 200k window. Measured naively that is 125%, `decide`
    rejects a percent above 100 as invalid, the hook fails open, and the session is never told -
    silently, on every turn, for the rest of its life. The assumed window has to yield to the
    evidence, the way `handoff context-hook claude` switches to 1M past 200k.
    """
    quarter_m = _cli(
        [
            "handoff",
            "autopilot",
            "check",
            "--harness",
            "claude",
            "--session-id",
            "s-1m-quarter",
            "--tokens",
            "250000",
            "--json",
        ],
        host["env"],
        host["work"],
    )
    assert quarter_m.returncode == 0, quarter_m.stderr
    early = json.loads(quarter_m.stdout)
    assert early["code"] != "invalid", early
    assert early["percent"] == pytest.approx(25.0, abs=0.5), early
    assert early["trigger"] is False, early
    assert early["window"]["measured_against_tokens"] == 1_000_000, early

    # Past even the large window the reading is capped at 100 and the hard ceiling fires,
    # rather than the whole mechanism dropping out on an out-of-range number.
    over = _cli(
        [
            "handoff",
            "autopilot",
            "check",
            "--harness",
            "claude",
            "--session-id",
            "s-1m-over",
            "--tokens",
            "1050000",
            "--json",
        ],
        host["env"],
        host["work"],
    )
    assert over.returncode == 0, over.stderr
    over_dict = json.loads(over.stdout)
    assert over_dict["trigger"] is True, over_dict
    assert 0 <= over_dict["percent"] <= 100, over_dict


def test_a_declared_window_is_used_instead_of_the_assumption(host: dict) -> None:
    """An operator on a 1M-context model can say so, and the percentage follows the declaration.

    The window cannot be read from a Claude transcript - verified against the transcripts on this
    machine, which carry `message.usage` and `message.model` and no window size - so the
    conservative default stands until it is contradicted. Declaring the real window is the only
    correct measurement, and `check --transcript` must apply it.
    """
    config = Path(host["home"]) / ".config" / "herdr-engineering" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("handoff_autopilot:\n  context_window_tokens: 1000000\n")
    hundred_k = write_transcript(
        host["work"] / "declared.jsonl",
        session_id="s-declared",
        input_tokens=100_000,
        cache_write=0,
        cache_read=0,
    )
    verdict = _cli(
        [
            "handoff",
            "autopilot",
            "check",
            "--harness",
            "claude",
            "--session-id",
            "s-declared",
            "--transcript",
            str(hundred_k),
            "--json",
        ],
        host["env"],
        host["work"],
    )
    assert verdict.returncode == 0, verdict.stderr
    declared = json.loads(verdict.stdout)
    assert declared["trigger"] is False, declared
    assert declared["percent"] == pytest.approx(10.0, abs=0.5), declared
    assert declared["window"] == {
        "declared": True,
        "source": "config",
        "window_tokens": 1_000_000,
        "measured_against_tokens": 1_000_000,
    }, declared


def test_an_assumed_window_is_named_in_the_answer(host: dict) -> None:
    """Without a declaration the 200k assumption stands - and says so out loud.

    A handoff at a tenth of a 1M window would otherwise look like a threshold bug; the answer has
    to name what it assumed and the one config line that stops the guessing.
    """
    hundred_k = write_transcript(
        host["work"] / "assumed.jsonl",
        session_id="s-assumed",
        input_tokens=100_000,
        cache_write=0,
        cache_read=0,
    )
    verdict = _cli(
        [
            "handoff",
            "autopilot",
            "check",
            "--harness",
            "claude",
            "--session-id",
            "s-assumed",
            "--transcript",
            str(hundred_k),
            "--json",
        ],
        host["env"],
        host["work"],
    )
    assert verdict.returncode == 0, verdict.stderr
    assumed = json.loads(verdict.stdout)
    assert assumed["trigger"] is True, assumed
    assert assumed["percent"] == pytest.approx(50.0, abs=0.5), assumed
    assert assumed["window"] == {
        "declared": False,
        "source": "assumed",
        "window_tokens": 200_000,
        "measured_against_tokens": 200_000,
        "how_to_declare": assumed["window"]["how_to_declare"],
    }, assumed
    assert "context_window_tokens" in assumed["window"]["how_to_declare"], assumed


def test_the_claude_hook_measures_the_way_check_measures(host: dict) -> None:
    """The hook and `check` must never disagree about the same transcript."""
    transcript = write_transcript(
        host["work"] / "1m-hook.jsonl",
        session_id="s-1m-hook",
        input_tokens=250_000,
        cache_write=0,
        cache_read=0,
    )
    done = subprocess.run(
        CLI + ["handoff", "autopilot", "hook", "--harness", "claude", "--record", "--json"],
        input=json.dumps(
            {
                "session_id": "s-1m-hook",
                "transcript_path": str(transcript),
                "hook_event_name": "Stop",
                "cwd": str(host["work"]),
            }
        ),
        capture_output=True,
        text=True,
        timeout=120,
        env=host["env"],
        cwd=str(host["work"]),
    )
    assert done.returncode == 0, done.stderr
    verdict = json.loads(done.stdout)
    assert verdict["code"] == "below_threshold", verdict
    assert verdict["percent"] == pytest.approx(25.0, abs=0.5), verdict
    assert not ha.state_path().exists(), "a below-threshold session must not be recorded"


def test_sessions_recording_at_the_same_instant_all_survive(host: dict) -> None:
    """The ledger is written by every session on the host, so a record must not lose another's.

    `record_trigger` reads, edits and writes the file. A Pi session and a Claude session crossing
    the threshold in the same second - which is exactly what happens after a long unattended run -
    would otherwise both read the same empty ledger and the last writer would win, silently
    re-arming the lost session so its agent is told to hand off twice.
    """
    writers = 8
    sessions = [f"s-race-{index}" for index in range(writers)]
    command = [
        *CLI,
        "handoff",
        "autopilot",
        "record",
        "--harness",
        "pi",
        "--percent",
        "75",
        "--json",
    ]
    # Started with Popen rather than a Barrier inside threads: a barrier raises
    # BrokenBarrierError when the host is saturated, the thread dies with the exception swallowed,
    # and the assertion below would then blame the ledger for records that were never attempted.
    # Eight processes already running at once is the race; a recorder that fails is reported as
    # itself instead of masquerading as a lost write.
    processes = [
        subprocess.Popen(
            [*command, "--session-id", session_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=host["env"],
            cwd=str(host["work"]),
        )
        for session_id in sessions
    ]
    failures = []
    for session_id, process in zip(sessions, processes, strict=True):
        try:
            stdout, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        if process.returncode != 0:
            failures.append((session_id, process.returncode, (stderr or stdout)[-400:]))
    assert not failures, f"recorders failed: {failures}"

    ledger = json.loads(ha.state_path().read_text())
    recorded = sorted(ledger["sessions"])
    assert recorded == sorted(sessions), (
        f"{writers - len(recorded)} trigger(s) lost to a concurrent write: {recorded}"
    )
    assert all(entry["count"] == 1 for entry in ledger["sessions"].values()), ledger


def test_the_installed_command_quotes_every_part_of_itself(host: dict) -> None:
    """A CLI path with a space must survive being written into settings.json.

    The hook line is executed by the operator's shell. `HERDR_ENG_BIN` and `HERDR_ENG_ARGS` are
    the packager's way to point at a venv, and a venv under a path with a space is legal, so an
    unquoted join would make every Stop hook a shell syntax error. A malformed
    `HERDR_ENG_ARGS` is ignored rather than fatal: it is read during install, and a typo in the
    environment must not stop a machine from being installed.
    """
    (host["home"] / ".claude").mkdir()
    env = dict(host["env"])
    env["HERDR_ENG_BIN"] = str(host["work"] / "my bin" / "herdr-eng")
    env["HERDR_ENG_ARGS"] = json.dumps(["-m", "herd r.cli"])
    assert _cli(["handoff", "autopilot", "install"], env, host["work"]).returncode == 0
    settings = json.loads((host["home"] / ".claude" / "settings.json").read_text())
    command = [
        hook["command"]
        for group in settings["hooks"]["Stop"]
        for hook in group["hooks"]
        if ha.HOOK_MARKER in hook["command"]
    ][0]

    tokens = shlex.split(command)
    assert tokens[2] == env["HERDR_ENG_BIN"], command
    assert tokens[5] == env["HERDR_ENG_BIN"], command
    assert tokens[6:8] == ["-m", "herd r.cli"], command
    assert "my bin" not in command.replace(shlex.quote(env["HERDR_ENG_BIN"]), ""), command

    # Malformed override, on a machine that has not been installed yet (the first install is
    # idempotent, so its own command would legitimately still be there): install succeeds and
    # the command stays a plain, runnable one instead of carrying junk through.
    second = host["tmp"] / "home2"
    (second / ".claude").mkdir(parents=True)
    bad = dict(host["env"])
    bad["HOME"] = str(second)
    bad["USERPROFILE"] = str(second)
    bad["XDG_CONFIG_HOME"] = str(second / ".config")
    bad["HERDR_ENG_BIN"] = str(host["work"] / "herdr-eng")
    bad["HERDR_ENG_ARGS"] = "{not json"
    assert _cli(["handoff", "autopilot", "install"], bad, host["work"]).returncode == 0
    rebuilt = json.loads((second / ".claude" / "settings.json").read_text())
    again = [
        hook["command"]
        for group in rebuilt["hooks"]["Stop"]
        for hook in group["hooks"]
        if ha.HOOK_MARKER in hook["command"]
    ][0]
    assert shlex.split(again)[5:8] == [str(host["work"] / "herdr-eng"), "handoff", "autopilot"], (
        again
    )


def _check_transcript(host: dict, name: str, model: str, tokens: int) -> dict:
    transcript = write_transcript(
        host["work"] / f"{name}.jsonl",
        session_id=f"s-{name}",
        input_tokens=tokens,
        cache_write=0,
        cache_read=0,
        model=model,
    )
    verdict = _cli(
        [
            "handoff",
            "autopilot",
            "check",
            "--harness",
            "claude",
            "--session-id",
            f"s-{name}",
            "--transcript",
            str(transcript),
            "--json",
        ],
        host["env"],
        host["work"],
    )
    assert verdict.returncode == 0, verdict.stderr
    return json.loads(verdict.stdout)


def test_the_window_comes_from_the_model_the_transcript_names(host: dict) -> None:
    """A `[1m]` model is measured against 1M at 100k tokens, not against an assumed 200k."""
    answer = _check_transcript(host, "model-1m", "claude-opus-4-5[1m]", 100_000)
    assert answer["percent"] == pytest.approx(10.0, abs=0.5), answer
    assert answer["window"]["source"] == "model", answer
    assert answer["window"]["window_tokens"] == 1_000_000, answer
    assert answer["window"]["model"] == "claude-opus-4-5[1m]", answer


def test_a_yaml_model_window_map_overrides_and_extends_the_builtin(host: dict) -> None:
    config = Path(host["home"]) / ".config" / "herdr-engineering" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("handoff_autopilot:\n  model_context_windows:\n    sonnet-big: 500000\n")
    answer = _check_transcript(host, "model-yaml", "claude-sonnet-big-1", 100_000)
    assert answer["percent"] == pytest.approx(20.0, abs=0.5), answer
    assert answer["window"]["source"] == "model", answer
    assert answer["window"]["window_tokens"] == 500_000, answer


def test_an_unknown_model_still_falls_back_to_the_assumption(host: dict) -> None:
    answer = _check_transcript(host, "model-plain", "claude-opus-4-5", 100_000)
    assert answer["percent"] == pytest.approx(50.0, abs=0.5), answer
    assert answer["window"]["source"] == "assumed", answer


def test_transcript_model_reads_the_last_assistant_model(host: dict) -> None:
    transcript = write_transcript(
        host["work"] / "m.jsonl", session_id="s-m", input_tokens=1, cache_read=0, model="x[1m]"
    )
    assert ha.transcript_model(transcript) == "x[1m]"
    assert ha.transcript_model(host["work"] / "missing.jsonl") is None


def test_the_stop_hook_uses_the_model_window(host: dict) -> None:
    transcript = write_transcript(
        host["work"] / "hook.jsonl",
        session_id="s-hook-1m",
        input_tokens=150_000,
        cache_read=0,
        model="claude-opus-4-5[1m]",
    )
    out = ha.hook(
        "claude",
        json.dumps(
            {
                "session_id": "s-hook-1m",
                "hook_event_name": "Stop",
                "transcript_path": str(transcript),
                "cwd": str(host["work"]),
            }
        ),
    )
    assert out["trigger"] is False, out
    assert out["percent"] == pytest.approx(15.0, abs=0.5), out


def _settings_with_secret(host: dict) -> Path:
    settings = host["home"] / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-secret"}}) + "\n")
    settings.chmod(0o600)
    return settings


def test_install_and_uninstall_keep_a_private_settings_file_private(host: dict) -> None:
    settings = _settings_with_secret(host)
    ha.install(home=host["home"])
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert "sk-secret" in settings.read_text()
    assert "Stop" in json.loads(settings.read_text())["hooks"]
    ha.uninstall(home=host["home"])
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert "sk-secret" in settings.read_text()


def test_a_new_settings_file_is_created_private(host: dict) -> None:
    ha.install(home=host["home"])
    settings = host["home"] / ".claude" / "settings.json"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


def test_a_symlinked_settings_file_stays_a_symlink(host: dict) -> None:
    real = host["tmp"] / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"env": {"K": "v"}}) + "\n")
    real.chmod(0o600)
    link = host["home"] / ".claude" / "settings.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    ha.install(home=host["home"])
    assert link.is_symlink() and link.resolve() == real.resolve()
    assert "Stop" in json.loads(real.read_text())["hooks"]
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    ha.uninstall(home=host["home"])
    assert link.is_symlink()
    assert "hooks" not in json.loads(real.read_text()) or "Stop" not in json.loads(
        real.read_text()
    ).get("hooks", {})


@pytest.mark.parametrize("command", [["install"], ["status"]])
def test_an_invalid_policy_is_a_clean_error_and_install_writes_nothing(
    host: dict, command: list[str]
) -> None:
    write_config(host["home"], "handoff_autopilot:\n  context_percent: 500\n")
    result = _cli(["handoff", "autopilot", *command], host["env"], host["work"])
    assert result.returncode != 0
    assert "Traceback" not in result.stderr + result.stdout
    assert "context_percent" in result.stderr + result.stdout
    assert not (host["home"] / ".claude").exists()
    assert not (host["home"] / ".pi").exists()
    assert not ha.state_path(host["home"]).parent.exists()
