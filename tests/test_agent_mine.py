"""Unit tests for the agent-transcript miner (redacted summary + HPC-ssh adoption).

These exercise the pure logic in `build_summary` with synthetic events — no file
scanning — so they run fast and offline.
"""
from herdr_engineering import agent_mine


def _ev(agent, tool, cmd, ok=True, ts="2026-10-22T00:00:00.000Z"):
    """Build a miner event dict the way the real extractors do (sig = first 4 tokens)."""
    return {"agent": agent, "tool": tool,
            "sig": " ".join(cmd.split()[:4]) if cmd else "",
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
