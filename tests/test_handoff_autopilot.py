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
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from herdr_engineering import handoff_autopilot as ha


CLI = [sys.executable, "-m", "herdr_engineering.cli"]


def _cli(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(CLI + args, capture_output=True, text=True, timeout=120,
                          cwd=str(cwd), env=env)


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A real HOME/XDG tree with no handoff autopilot installed yet."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    work = tmp_path / "work"
    for path in (home, state, work):
        path.mkdir(parents=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_STATE_HOME": str(state), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    })
    env.pop("HERDR_HANDOFF_AUTOPILOT", None)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.delenv("HERDR_HANDOFF_AUTOPILOT", raising=False)
    return {"tmp": tmp_path, "home": home, "state": state, "work": work, "env": env}


def write_config(home: Path, body: str) -> Path:
    path = home / ".config" / "herdr-engineering" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def write_transcript(path: Path, *, session_id: str, input_tokens: int, cache_read: int,
                     cache_write: int = 0) -> Path:
    """A Claude Code transcript in the shape the installed CLI actually writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "sessionId": session_id, "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "sessionId": session_id, "model": "claude-opus-4-5",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}],
                     "usage": {"input_tokens": input_tokens,
                               "cache_creation_input_tokens": cache_write,
                               "cache_read_input_tokens": cache_read,
                               "output_tokens": 857}}},
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
    decision = ha.decide(session_id="s-tokens", harness="pi", percent=None,
                         tokens=170_000, context_window=200_000)
    assert decision.trigger is True
    assert decision.percent == pytest.approx(85.0)


def test_unknown_context_never_triggers(host: dict) -> None:
    decision = ha.decide(session_id="s-unknown", harness="pi", percent=None,
                         tokens=None, context_window=None)
    assert decision.trigger is False
    assert decision.code == "unknown_context"


def test_config_can_opt_out_and_the_reason_names_the_way_back(host: dict) -> None:
    write_config(host["home"], "handoff_autopilot:\n  enabled: false\n")
    decision = ha.decide(session_id="s-optout", harness="pi", percent=90.0)
    assert decision.trigger is False
    assert decision.code == "disabled"
    assert "handoff_autopilot" in decision.reason
    assert "config.yaml" in decision.reason


def test_environment_can_opt_out_for_one_session(host: dict, monkeypatch: pytest.MonkeyPatch) -> None:
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
    (root / "handoff_live.json").write_text(json.dumps(
        {"handoff_id": "handoff_live", "schema_version": 2, "state": "in_progress",
         "source": {"harness": "pi", "session_id": "s-live"}, "target": {"harness": "pi"},
         "context": {}}))
    suppressed = ha.decide(session_id="s-live", harness="pi", percent=90.0, cwd=host["work"])
    assert suppressed.trigger is False
    assert suppressed.code == "handoff_underway"
    assert "handoff_live" in suppressed.reason

    # A finished handoff for the same session is not a reason to stay silent.
    (root / "handoff_live.json").write_text(json.dumps(
        {"handoff_id": "handoff_live", "schema_version": 2, "state": "completed",
         "source": {"harness": "pi", "session_id": "s-live"}, "target": {"harness": "pi"},
         "context": {}}))
    assert ha.decide(session_id="s-live", harness="pi", percent=90.0,
                     cwd=host["work"]).trigger is True


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
    done = _cli(["handoff", "autopilot", "check", "--session-id", "s1",
                 "--harness", "pi", "--percent", "62", "--json"],
                host["env"], host["work"])
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["trigger"] is True and payload["code"] == "context_is_full"
    assert payload["prompt"] != ""
    assert not ha.state_path().exists()  # a check must not consume the trigger


def test_cli_record_consumes_the_trigger_for_that_session(host: dict) -> None:
    for percent in ("62", "90"):
        done = _cli(["handoff", "autopilot", "record", "--session-id", "s2",
                     "--harness", "pi", "--percent", percent, "--json"],
                    host["env"], host["work"])
        assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["count"] == 2
    check = _cli(["handoff", "autopilot", "check", "--session-id", "s2", "--harness", "pi",
                  "--percent", "95", "--json"], host["env"], host["work"])
    assert json.loads(check.stdout)["code"] == "already_triggered"


def test_cli_reports_invalid_percent_as_usage_error(host: dict) -> None:
    done = _cli(["handoff", "autopilot", "check", "--session-id", "s3", "--percent", "140"],
                host["env"], host["work"])
    assert done.returncode == 2
    assert "percent" in done.stdout.lower() or "percent" in done.stderr.lower()


def test_install_is_default_idempotent_and_preserves_foreign_hooks(host: dict) -> None:
    settings = host["home"] / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
            {"type": "command", "command": "printf '%s' 'someone elses hook'"}]}]},
        "theme": "dark",
    }))

    first = _cli(["handoff", "autopilot", "install", "--json"], host["env"], host["work"])
    assert first.returncode == 0, first.stderr
    created = json.loads(first.stdout)
    kinds = {item["kind"] for item in created["items"]}
    assert {"pi-extension", "claude-hook", "state-dir"} <= kinds
    assert all(item["status"] in {"created", "unchanged"} for item in created["items"])

    extension = ha.extension_path(home=host["home"])
    text = extension.read_text()
    assert "context_is_full" in text and "sendUserMessage" in text
    assert "handoff autopilot check" in text  # asks the real CLI, decides nothing itself

    merged = json.loads(settings.read_text())
    assert merged["theme"] == "dark"  # unrelated settings survive
    assert any("someone elses hook" in json.dumps(group)
               for group in merged["hooks"]["SessionStart"])
    events = set(merged["hooks"])
    assert {"Stop", "PreCompact"} & events
    our_commands = [hook["command"] for group in merged["hooks"]["Stop"] for hook in group["hooks"]]
    assert any("handoff autopilot hook" in command for command in our_commands)

    second = _cli(["handoff", "autopilot", "install", "--json"], host["env"], host["work"])
    assert second.returncode == 0, second.stderr
    assert all(item["status"] == "unchanged" for item in json.loads(second.stdout)["items"])


def test_uninstall_removes_only_what_install_added(host: dict) -> None:
    _cli(["handoff", "autopilot", "install"], host["env"], host["work"])
    settings = host["home"] / ".claude" / "settings.json"
    before = json.loads(settings.read_text())
    foreign = [hook for group in before["hooks"].get("SessionStart", []) for hook in group["hooks"]
               if "someone elses hook" in hook.get("command", "")]
    removed = _cli(["handoff", "autopilot", "uninstall", "--json"], host["env"], host["work"])
    assert removed.returncode == 0, removed.stderr
    assert not ha.extension_path(home=host["home"]).exists()
    after = json.loads(settings.read_text())
    assert not any("handoff autopilot hook" in json.dumps(group) for group in after["hooks"].get("Stop", []))
    assert foreign and any("someone elses hook" in json.dumps(group)
                           for group in after["hooks"].get("SessionStart", []))


def test_status_reports_the_policy_paths_and_recent_triggers(host: dict) -> None:
    _cli(["handoff", "autopilot", "install", "--json"], host["env"], host["work"])
    _cli(["handoff", "autopilot", "record", "--session-id", "s3", "--harness", "pi",
          "--percent", "66", "--json"], host["env"], host["work"])
    done = _cli(["handoff", "autopilot", "status", "--json"], host["env"], host["work"])
    assert done.returncode == 0, done.stderr
    status = json.loads(done.stdout)
    assert status["policy"]["enabled"] is True
    assert status["installed"]["pi_extension"] is True
    assert status["ledger"]["sessions"]["s3"]["count"] == 1
    assert "next boundary" in status["summary"]


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
    transcript = write_transcript(host["work"] / "t.jsonl", session_id="s-cc",
                                 input_tokens=2, cache_write=2_602, cache_read=104_948)
    percent = ha.transcript_percent(transcript, context_window=200_000)
    assert percent == pytest.approx((2 + 2_602 + 104_948) / 2_000.0)


def test_claude_hook_injects_the_prompt_and_always_exits_zero(host: dict) -> None:
    transcript = write_transcript(host["work"] / "full.jsonl", session_id="s-cc-full",
                                 input_tokens=2, cache_write=3_000, cache_read=150_000)
    # The hook reads its event from stdin, exactly as Claude Code passes it.
    done = subprocess.run(CLI + ["handoff", "autopilot", "hook", "--harness", "claude",
                                 "--record", "--json"],
                          input=json.dumps({"session_id": "s-cc-full",
                                            "transcript_path": str(transcript),
                                            "hook_event_name": "Stop", "cwd": str(host["work"])}),
                          capture_output=True, text=True, timeout=120, env=host["env"],
                          cwd=str(host["work"]))
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["trigger"] is True
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "handoff" in payload["hookSpecificOutput"]["additionalContext"]
    assert ha.read_ledger()["sessions"]["s-cc-full"]["count"] == 1

    # A second Stop in the same session is silenced by the per-session cap.
    again = subprocess.run(CLI + ["handoff", "autopilot", "hook", "--harness", "claude",
                                 "--record", "--json"],
                          input=json.dumps({"session_id": "s-cc-full",
                                            "transcript_path": str(transcript)}),
                          capture_output=True, text=True, timeout=120, env=host["env"],
                          cwd=str(host["work"]))
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["trigger"] is False


def test_claude_hook_fails_open_on_unusable_input(host: dict) -> None:
    for payload in ("", "{not json", json.dumps({"session_id": "s-x"}),
                    json.dumps({"session_id": "s-y", "transcript_path": "/nope/t.jsonl"})):
        done = subprocess.run(CLI + ["handoff", "autopilot", "hook", "--harness", "claude"],
                              input=payload, capture_output=True, text=True, timeout=120,
                              env=host["env"], cwd=str(host["work"]))
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
    if node is None:  # pragma: no cover - CI ships node 22 for the packages suite
        pytest.skip("node is not installed on this host")
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
    env["HERDR_ENG_BIN"] = sys.executable
    env["HERDR_ENG_ARGS"] = json.dumps(["-m", "herdr_engineering.cli"])
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
    env["HERDR_TEST_PYTHON"] = sys.executable
    done = subprocess.run([node, str(runner)], capture_output=True, text=True, timeout=180,
                          env=env, cwd=str(host["work"]))
    assert done.returncode == 0, f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}"
