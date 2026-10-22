"""Unit tests for the agent-transcript miner (redacted summary + HPC-ssh adoption).

These exercise the pure logic in `build_summary` with synthetic events — no file
scanning — so they run fast and offline.
"""
from herdr_engineering import agent_mine


def _ev(agent, tool, cmd, ok=True, ts="2026-10-22T00:00:00.000Z"):
    """Build a miner event dict the way the real extractors do (sig via the real redacting sig_of)."""
    return {"agent": agent, "tool": tool,
            "sig": agent_mine.sig_of(cmd),
            "fam": "", "ok": ok, "dur": 0, "cmd": cmd, "ts": ts}


def test_classifies_hpc_ssh_adoption_and_since():
    events = [
        # hand-rolled ssh to HPC targets, BEFORE the install date -> not "since"
        _ev("pi", "bash", "ssh gw@hive.hpc.ucdavis.edu 'bash -lc squeue'", ts="2026-09-01T00:00:00.000Z"),
        _ev("pi", "bash", "ssh -o BatchMode=yes fry.fiehnlab.ucdavis.edu 'squeue'", ts="2026-09-02T00:00:00.000Z"),
        # the hpc-ssh wrapper, AFTER the install date -> "since"
        _ev("pi", "bash", "~/.agents/skills/ucd-hpc/ucd-hpc/scripts/hpc-ssh.sh hive squeue"),
        _ev("pi", "bash", "~/.agents/skills/ucd-hpc/ucd-hpc/scripts/hpc-ssh.sh farm 'sbatch --test'"),
        # a non-ssh command -> neither
        _ev("claude", "Bash", "echo hello"),
    ]
    s = agent_mine.build_summary(
        ["pi", "claude"], events,
        {"pi": {"files": 1}, "claude": {"files": 0}}, 45, "2026-10-21")
    hpc = s["hpc_ssh"]
    assert hpc["hand_ssh_hpc"] == 2
    assert hpc["hpc_ssh"] == 2
    assert hpc["adoption_pct"] == 50.0
    assert hpc["hpc_ssh_since"] == 2
    assert hpc["hand_ssh_hpc_since"] == 0
    assert hpc["adoption_since_pct"] == 100.0
    assert s["totals"]["events"] == 5
    assert s["agents_with_data"] == ["pi", "claude"]
    assert s["hpc_ssh"]["since_install_date"] == "2026-10-21"


def test_no_ssh_activity_yields_zero_adoption():
    events = [_ev("pi", "bash", "ls"), _ev("pi", "read", None)]
    s = agent_mine.build_summary(["pi"], events, {"pi": {"files": 1}}, 45, "2026-10-21")
    assert s["hpc_ssh"]["hand_ssh_hpc"] == 0
    assert s["hpc_ssh"]["hpc_ssh"] == 0
    assert s["hpc_ssh"]["adoption_pct"] == 0.0
    assert s["hpc_ssh"]["adoption_since_pct"] == 0.0


def test_per_agent_tool_mix():
    events = [
        _ev("pi", "bash", "ls"),
        _ev("pi", "read", None),
        _ev("pi", "bash", "git status"),
    ]
    s = agent_mine.build_summary(["pi"], events, {"pi": {"files": 1}}, 45, "2026-10-21")
    pa = s["per_agent"]["pi"]
    assert pa["events"] == 3
    assert pa["top_tools"][0][0] == "bash" and pa["top_tools"][0][1] == 2


def test_fleet_rollup_aggregates_sites_and_skips_error_stubs():
    s1 = agent_mine.build_summary(
        ["pi"], [_ev("pi", "bash", "ssh gw@hive 'x'", ts="2026-10-22T00:00:00.000Z"),
                 _ev("pi", "bash", "hpc-ssh.sh hive x")], {"pi": {"files": 1}}, 45, "2026-10-21")
    s2 = agent_mine.build_summary(
        ["claude"], [_ev("claude", "Bash", "ssh fry.fiehnlab 'x'")], {"claude": {"files": 1}}, 45, "2026-10-21")
    r = agent_mine.fleet_rollup([s1, s2, {"error": "unavailable"}, None, "junk"])
    assert r["sites"] == 2  # error stub + None + junk are skipped
    assert r["hand_ssh_hpc"] == 2  # s1:1 + s2:1
    assert r["hpc_ssh"] == 1  # s1:1 + s2:0
    assert r["events"] == 3  # s1:2 + s2:1
    assert r["adoption_pct"] == round(100.0 * 1 / 3, 1)
    assert r["per_agent"].get("pi") == 2 and r["per_agent"].get("claude") == 1


def test_signature_never_carries_arguments_or_secrets():
    secrets = [
        "export GITHUB_TOKEN=ghp_abcdef123456 && git push",
        "curl -H 'Authorization: Bearer sk-live-123' https://api.example.com",
        "mysql -pHUNTER2 -u root db",
        "ssh gw@hive.hpc.ucdavis.edu squeue",
        "AWS_SECRET_ACCESS_KEY=abc123 aws s3 ls",
        "echo hunter2",
        "cat /home/alice/.ssh/id_ed25519",
    ]
    for cmd in secrets:
        sig = agent_mine.sig_of(cmd)
        for leak in ("ghp_", "sk-live", "HUNTER2", "hunter2", "gw@", "hive.hpc", "abc123", "alice", "id_ed25519", "Bearer", "-p"):
            assert leak not in sig, (cmd, sig)
    assert agent_mine.sig_of("AWS_SECRET_ACCESS_KEY=abc123 aws s3 ls") == "aws"
    assert agent_mine.sig_of("git remote add origin git@host:x") == "git remote add"
    assert agent_mine.sig_of("/usr/bin/git status --short") == "git status"
    assert agent_mine.sig_of("") is None


def test_summary_signatures_are_redacted_end_to_end():
    events = [_ev("pi", "bash", "mysql -pHUNTER2 -u root db", ok=False)]
    s = agent_mine.build_summary(["pi"], events, {"pi": {"files": 1}}, 45, "2026-10-21")
    blob = str(s)
    assert "HUNTER2" not in blob and "root" not in blob


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n")


def test_extractors_survive_odd_lines_and_count_codex_exec_command(tmp_path):
    import json
    pi = tmp_path / "pi.jsonl"
    _write(pi, [
        "[1, 2]", '"toolCall"', "not json toolCall",
        json.dumps({"timestamp": 5, "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "a", "name": "bash", "arguments": {"command": "ls -la /secret"}}]}}),
        json.dumps({"message": {"role": "toolResult", "toolCallId": "zz", "toolName": "read", "isError": False}}),
        json.dumps({"message": {"role": "toolResult", "toolCallId": "a", "toolName": "bash", "isError": False}}),
    ])
    events = list(agent_mine.mine_pi(str(pi)))
    assert [e["sig"] for e in events] == [None, "ls"]  # the unmatched result no longer drops the pending call
    assert events[1]["ts"] is None  # a non-string timestamp is dropped, not propagated

    codex = tmp_path / "codex.jsonl"
    _write(codex, [
        json.dumps(["function_call"]),
        json.dumps({"payload": "function_call"}),
        json.dumps({"payload": {"type": "function_call", "name": "exec_command",
                                "arguments": json.dumps({"cmd": "ssh gw@hive.hpc.ucdavis.edu squeue"})}}),
        json.dumps({"payload": {"type": "function_call_output", "output": "ok"}}),
    ])
    (e,) = list(agent_mine.mine_codex(str(codex)))
    assert e["fam"] == "shell" and e["sig"] == "ssh" and "hive" in e["cmd"]


def test_run_skips_unreadable_and_broken_files_and_writes_private_summary(tmp_path, monkeypatch):
    import json, os, stat
    store = tmp_path / "pi"
    store.mkdir()
    good = store / "good.jsonl"
    _write(good, [json.dumps({"message": {"role": "assistant", "content": [
        {"type": "toolCall", "id": "a", "name": "bash", "arguments": {"command": "git status"}}]}}),
        json.dumps({"message": {"role": "toolResult", "toolCallId": "a", "toolName": "bash"}})])
    (store / "dangling.jsonl").symlink_to(tmp_path / "missing")
    monkeypatch.setitem(agent_mine.STORES, "pi", str(store))
    out = tmp_path / "out" / "summary.json"
    agent_mine.run(["pi"], 0, False, str(tmp_path / "out"), json_summary=True,
                   since_install="2026-10-21", summary_out=str(out))
    data = json.loads(out.read_text())
    assert data["per_agent"]["pi"]["events"] == 1
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
