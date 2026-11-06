"""Spec 07: quota/eviction/admission policy + advisory slot suggestion (no mocks).

Only tmp dirs created here are ever deleted from. Real flock, real sqlite registry.
"""
import copy
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_cache_policy as pol
from herdr_engineering import build_registry as br
from herdr_engineering.build_cache_config import validate
from herdr_engineering.config import DEFAULTS
from herdr_engineering.errors import InvalidError

GIB = 1024 ** 3
NOW = 1_800_000_000.0
REPO = Path(__file__).resolve().parents[1]


def cfg(**over):
    s = copy.deepcopy(DEFAULTS["fabric_build_cache"])
    s["enabled"] = True
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        else:
            s[k] = v
    return validate(s)


def small_cfg(destructive=True, **over):
    # capacity 1 KiB-ish expressed in GiB is impossible; use policy-level byte overrides
    return cfg(eviction={"destructive_enabled": destructive}, **over)


def hold_lease(state, scope, ttl=300):
    reg = br.Registry(state)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    a = reg.register(repo_id="r", input_digest=None, cmd_kind="cargo build", pid=owner.pid)
    reg.transition(a.attempt_id, "classified", "t")
    reg.transition(a.attempt_id, "admitted", "t")
    reg.acquire_lease(scope, a.attempt_id, ttl)
    return owner


def fill(state: Path, n=6, size=1000, age_h=100):
    d = state / "sccache"
    for i in range(n):
        p = d / f"{i:x}" / "ab" / f"f{i}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        t = NOW - (age_h - i) * 3600  # f0 oldest
        os.utime(p, (t, t))
    return d


# ---- config ---------------------------------------------------------------
def test_defaults_safe():
    c = cfg()
    assert c.eviction.destructive_enabled is False
    assert c.adaptive_slots.enabled is False
    assert DEFAULTS["fabric_build_cache"]["eviction"]["destructive_enabled"] is False


@pytest.mark.parametrize("over", [
    {"eviction": {"destructive_enabled": "yes"}},
    {"eviction": {"bogus": 1}},
    {"eviction": {"hard_cap_gib": 1}},          # below local.capacity_gib (20)
    {"eviction": {"max_entries_per_run": 0}},
    {"adaptive_slots": {"enabled": "true"}},
    {"adaptive_slots": {"min_slots": 5, "max_slots": 2}},
    {"adaptive_slots": {"max_slots": 99}},
])
def test_config_fails_closed(over):
    with pytest.raises(InvalidError):
        cfg(**over)


# ---- pure eviction plan -----------------------------------------------------
def E(name, size, age_h):
    return pol.Entry(name, size, NOW - age_h * 3600, NOW - age_h * 3600)


def test_plan_report_only_within_hard_cap():
    entries = [E("a", 600, 50), E("b", 600, 40)]
    p = pol.plan_eviction(entries, capacity_bytes=1000, hard_cap_bytes=2000, now=NOW,
                          min_idle_s=3600, max_entries=10, max_bytes=10_000)
    assert p.victims == () and p.reason == "over_sccache_cap_report_only"
    assert p.used_bytes == 1200


def test_plan_within_cap():
    p = pol.plan_eviction([E("a", 10, 50)], capacity_bytes=1000, hard_cap_bytes=2000,
                          now=NOW, min_idle_s=3600, max_entries=10, max_bytes=10_000)
    assert p.victims == () and p.reason == "within_cap"


def test_plan_oldest_first_down_to_capacity():
    entries = [E("new", 500, 2), E("old", 500, 90), E("mid", 500, 50), E("recent", 500, 0.1)]
    p = pol.plan_eviction(entries, capacity_bytes=1000, hard_cap_bytes=1500, now=NOW,
                          min_idle_s=3600, max_entries=10, max_bytes=10_000)
    assert [v.path for v in p.victims] == ["old", "mid"]  # 2000 -> 1000
    assert p.planned_bytes == 1000 and p.reason == "over_hard_cap"
    assert "recent" not in [v.path for v in p.victims]   # idle floor


def test_plan_bounded_by_entries_and_bytes():
    entries = [E(f"e{i}", 100, 100 - i) for i in range(20)]  # 2000 bytes
    p = pol.plan_eviction(entries, capacity_bytes=100, hard_cap_bytes=500, now=NOW,
                          min_idle_s=3600, max_entries=3, max_bytes=10_000)
    assert len(p.victims) == 3 and p.truncated
    p = pol.plan_eviction(entries, capacity_bytes=100, hard_cap_bytes=500, now=NOW,
                          min_idle_s=3600, max_entries=100, max_bytes=450)
    assert p.planned_bytes <= 450 and p.truncated


def test_plan_skips_cargo_target_paths():
    entries = [E("proj/target/debug/x", 900, 99), E("0/ab/f", 900, 80)]
    p = pol.plan_eviction(entries, capacity_bytes=100, hard_cap_bytes=500, now=NOW,
                          min_idle_s=3600, max_entries=10, max_bytes=10_000)
    assert [v.path for v in p.victims] == ["0/ab/f"]
    assert ("proj/target/debug/x", "cargo_target") in p.skipped


# ---- run_eviction: dry-run / approval / safety -----------------------------
def tiny(c_destructive=True, **kw):
    """Config + byte-level knobs: tiny caps so 6x1000 bytes is over the hard cap."""
    return dict(cfg=small_cfg(c_destructive), capacity_bytes=2000, hard_cap_bytes=3000,
                min_idle_s=3600, max_bytes=10_000, now=NOW, **kw)


def names(d):
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())


def test_disabled_does_nothing(tmp_path):
    out = pol.run_eviction(validate(None), tmp_path / "st", apply=True)
    assert out == {"enabled": False}
    assert not (tmp_path / "st").exists()


def test_dry_run_deletes_nothing_and_creates_nothing(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    before = names(d)
    out = pol.run_eviction(state_dir=st, apply=False, **tiny())
    assert out["mode"] == "dry_run" and out["status"] == "preview"
    assert len(out["plan"]["victims"]) == 4 and out["plan"]["planned_bytes"] == 4000
    assert names(d) == before
    assert not (st / "build-maintenance.lock").exists()


def test_apply_without_config_approval_refuses(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    before = names(d)
    out = pol.run_eviction(state_dir=st, apply=True, **{**tiny(False)})
    assert out["status"] == "refused" and out["reason"] == "destructive_not_enabled"
    assert names(d) == before


def test_apply_with_approval_deletes_only_planned_oldest(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    out = pol.run_eviction(state_dir=st, apply=True, **tiny())
    assert out["status"] == "applied" and out["removed_count"] == 4
    assert out["removed_bytes"] == 4000
    assert names(d) == ["4/ab/f4", "5/ab/f5"]  # newest two survive
    assert (d).is_dir()


def test_busy_when_build_holds_shared_lock(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    before = names(d)
    st.mkdir(exist_ok=True)
    fd = os.open(st / "build-maintenance.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    try:
        out = pol.run_eviction(state_dir=st, apply=True, **tiny())
    finally:
        os.close(fd)
    assert out["status"] == "busy" and names(d) == before


def test_active_lease_blocks_everything(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    before = names(d)
    owner = hold_lease(st, str(d))
    try:
        out = pol.run_eviction(state_dir=st, apply=True, **tiny())
    finally:
        owner.kill()
    assert out["status"] == "skipped" and out["reason"] == "active_lease"
    assert names(d) == before


def test_unreadable_registry_fails_closed(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    before = names(d)
    (st / br.DB_NAME).write_bytes(b"not a sqlite database at all" * 10)
    out = pol.run_eviction(state_dir=st, apply=True, **tiny())
    assert out["status"] == "skipped" and out["reason"] == "registry_unreadable"
    assert names(d) == before


def test_symlinks_never_followed_or_removed_outside(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "precious"
    victim.write_bytes(b"keep" * 1000)
    os.utime(victim, (NOW - 999 * 3600,) * 2)
    (d / "0" / "link").symlink_to(victim)              # file symlink
    (d / "9").symlink_to(outside)                       # dir symlink
    pol.run_eviction(state_dir=st, apply=True, **tiny())
    assert victim.read_bytes() == b"keep" * 1000
    assert (d / "9").is_symlink()


def test_recheck_before_removal_skips_touched_and_swapped(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    plan = pol.plan_eviction(pol.scan_cache(d), capacity_bytes=2000, hard_cap_bytes=3000,
                             now=NOW, min_idle_s=3600, max_entries=100, max_bytes=10_000)
    first, second = (d / v.path for v in plan.victims[:2])
    os.utime(first, (NOW, NOW))                        # touched after planning
    outside = tmp_path / "keep"
    outside.write_bytes(b"k")
    second.unlink()
    second.symlink_to(outside)                         # swapped for a symlink
    removed, skipped = pol.remove_planned(d, plan, st, now=NOW, min_idle_s=3600)
    assert first.exists() and outside.exists() and second.is_symlink()
    assert {s[1] for s in skipped} >= {"changed_since_plan", "not_regular_file"}
    assert len(removed) == len(plan.victims) - 2


def test_lease_recheck_per_entry(tmp_path):
    st = tmp_path / "st"
    d = fill(st)
    plan = pol.plan_eviction(pol.scan_cache(d), capacity_bytes=2000, hard_cap_bytes=3000,
                             now=NOW, min_idle_s=3600, max_entries=100, max_bytes=10_000)
    owner = hold_lease(st, str(d / plan.victims[0].path))
    try:
        removed, skipped = pol.remove_planned(d, plan, st, now=NOW, min_idle_s=3600)
    finally:
        owner.kill()
    assert (d / plan.victims[0].path).exists()
    assert (plan.victims[0].path, "active_lease") in skipped


def test_cache_dir_symlink_refused(tmp_path):
    st = tmp_path / "st"
    st.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (st / "sccache").symlink_to(real)
    out = pol.run_eviction(state_dir=st, apply=True, **tiny())
    assert out["status"] == "refused" and out["reason"] == "cache_dir_unsafe"


# ---- disk pressure ----------------------------------------------------------
def test_low_free_disk_refuses_admission_and_deletes_nothing(tmp_path):
    from herdr_engineering import build_cache_env as bce
    c = cfg(local={"min_free_disk_percent": 99.99})
    ws = tmp_path / "ws"
    ws.mkdir()
    p = bce.plan(c, ["cargo", "build"], {}, bce.Tools(Path("/x/sccache"), "sccache 0.19.0",
                 Path("/x/rustc"), Path("/x/cargo"),
                 "rustc 1.97.0\nhost: x86_64-unknown-linux-gnu\nrelease: 1.97.0\n"
                 "commit-hash: a\ncommit-date: b\nLLVM version: 1\n"),
                 ws, tmp_path / "st", False)
    assert not p.applied and bce.Reason.LOW_FREE_DISK.value in p.reasons
    st = tmp_path / "st2"
    d = fill(st)
    before = names(d)
    out = pol.run_eviction(state_dir=st, apply=False, cfg=c, capacity_bytes=2000,
                           hard_cap_bytes=3000, min_idle_s=3600, max_bytes=10_000, now=NOW)
    assert out["admission"] == {"allowed": False, "reason": "low_free_disk"}
    assert names(d) == before
    out = pol.run_eviction(state_dir=st, apply=True, **{**tiny(), "cfg": cfg(
        local={"min_free_disk_percent": 99.99}, eviction={"destructive_enabled": True})})
    assert out["admission"]["reason"] == "low_free_disk"  # reported; eviction is policy-driven


# ---- slot suggestion --------------------------------------------------------
def rec(elapsed=60, user=50, sys_=10, rss=2_000_000, ts="2026-11-04T10:00:00+00:00"):
    return {"event": "build_finished", "ts": ts, "elapsed_s": elapsed, "user_cpu_s": user,
            "sys_cpu_s": sys_, "max_rss_kb": rss, "exit_code": 0}


def test_read_recent_builds_tolerates_garbage(tmp_path):
    f = tmp_path / "builds.jsonl"
    f.write_text("\n".join([json.dumps(rec()), "{broken", json.dumps({"event": "build_started"}),
                            json.dumps(rec(rss=1))]) + "\n")
    got = pol.read_recent_builds(f, limit=10)
    assert len(got) == 2
    assert pol.read_recent_builds(tmp_path / "missing", limit=10) == []


def test_slots_idle_host_gets_max():
    r = pol.recommend_slots([rec()] * 5, load1=0.2, cores=16, mem_available_kb=64_000_000,
                            min_slots=1, max_slots=4)
    assert r.slots == 4


def test_slots_high_load_gets_min():
    r = pol.recommend_slots([rec()] * 5, load1=40.0, cores=16, mem_available_kb=64_000_000,
                            min_slots=1, max_slots=4)
    assert r.slots == 1 and "high_load" in r.reasons


def test_slots_memory_bound():
    r = pol.recommend_slots([rec(rss=8_000_000)] * 5, load1=0.1, cores=32,
                            mem_available_kb=20_000_000, min_slots=1, max_slots=4)
    assert r.slots == 1  # 70% of 20GB / 8GB = 1.75 -> 1
    assert "memory_bound" in r.reasons


# ---- CLI ----------------------------------------------------------------------
def _cli(tmp_path, argv, config=None):
    env = {**os.environ, "HERDR_ENGINEERING_STATE": str(tmp_path / "st"), "HOME": str(tmp_path)}
    env.pop("HERDR_ENGINEERING_CONFIG", None)
    if config is not None:
        (tmp_path / "c.yaml").write_text(config)
        env["HERDR_ENGINEERING_CONFIG"] = str(tmp_path / "c.yaml")
    return subprocess.run([sys.executable, "-c", "import sys; from herdr_engineering.cli import "
                           f"main; sys.exit(main({argv!r}))"], capture_output=True, text=True,
                          env=env, cwd=str(REPO))


CONF = """
fabric_build_cache:
  enabled: true
  cleanup:
    max_evict_per_pass_gib: 100
  eviction:
    destructive_enabled: %s
"""


def _sparse(tmp_path):
    d = tmp_path / "st" / "build-gates" / "sccache" / "0" / "ab"
    d.mkdir(parents=True)
    for i in range(3):
        f = d / f"f{i}"
        with open(f, "wb") as fh:
            fh.truncate(15 * GIB)  # sparse: no real disk used
        t = time.time() - (200 - i) * 3600
        os.utime(f, (t, t))
    return d


def test_cli_disabled_is_inert(tmp_path):
    r = _cli(tmp_path, ["build-cache-evict", "--apply", "--json"])
    assert r.returncode == 0 and json.loads(r.stdout) == {"enabled": False}
    assert not (tmp_path / "st").exists()


def test_cli_dry_run_then_refused_then_applied(tmp_path):
    d = _sparse(tmp_path)
    r = _cli(tmp_path, ["build-cache-evict", "--json"], CONF % "false")
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["status"] == "preview", r.stderr
    assert out["plan"]["planned_count"] == 2 and len(list(d.iterdir())) == 3
    r = _cli(tmp_path, ["build-cache-evict", "--apply"], CONF % "false")
    assert r.returncode == 1 and json.loads(r.stdout)["reason"] == "destructive_not_enabled"
    assert len(list(d.iterdir())) == 3
    r = _cli(tmp_path, ["build-cache-evict", "--apply"], CONF % "true")
    assert r.returncode == 0 and json.loads(r.stdout)["removed_count"] == 2
    assert [p.name for p in d.iterdir()] == ["f2"]


def test_cli_status_suggests_but_never_changes_slots_by_default(tmp_path):
    r = _cli(tmp_path, ["build-coordinate", "--status", "--json"], CONF % "false")
    out = json.loads(r.stdout)
    sg = out["slot_suggestion"]
    assert sg["advisory"] is True and 1 <= sg["recommended"] <= 4
    r2 = _cli(tmp_path, ["build-coordinate", "--status", "--json"])
    assert "slot_suggestion" not in json.loads(r2.stdout)
    assert json.loads(r2.stdout)["slots"] == out["slots"]  # effective count unchanged


def test_cli_adaptive_enabled_applies_recommendation(tmp_path):
    conf = CONF % "false" + "  adaptive_slots:\n    enabled: true\n    min_slots: 3\n    max_slots: 3\n"
    r = _cli(tmp_path, ["build-coordinate", "--status", "--json"], conf)
    assert json.loads(r.stdout)["slots"] == 3
