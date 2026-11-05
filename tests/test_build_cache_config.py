"""Spec 02: fabric_build_cache config validation (fails closed, default off)."""
import copy
import os
import textwrap
from pathlib import Path

import pytest

from herdr_engineering.build_cache_config import (
    Mode,
    check_credentials_file,
    validate,
)
from herdr_engineering.config import DEFAULTS, load_config
from herdr_engineering.errors import InvalidError

REPO = Path(__file__).resolve().parent.parent


def sec(**over):
    s = copy.deepcopy(DEFAULTS["fabric_build_cache"])
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update(v)
        else:
            s[k] = v
    return s


def bad(path_fragment, **over):
    with pytest.raises(InvalidError) as e:
        validate(sec(**over))
    assert path_fragment in str(e.value)


def slurm_ok(**extra):
    return {"slurm_enabled": True, "allowed_clusters": ["hive"],
            "allowed_partitions": ["high"], **extra}


def test_defaults_all_off_and_local_only():
    c = validate(None)
    assert validate({}) == c
    assert not c.enabled and c.effective_mode is Mode.LOCAL_ONLY
    assert not c.shared_cache.enabled and not c.shared_writes_effective
    assert not c.slurm_effective and c.cleanup.dry_run and c.warnings == ()
    assert c.allowed_hosts == ()


def test_disabled_forces_local_only_even_with_flags():
    c = validate(sec(mode="slurm_full_build", execution=slurm_ok(),
                     shared_cache={"enabled": True, "endpoint_ref": "cache", "writes_enabled": True}))
    assert c.mode is Mode.SLURM_FULL_BUILD and c.effective_mode is Mode.LOCAL_ONLY
    assert not c.slurm_effective and not c.shared_writes_effective


def test_enabled_shared_cache_passes():
    c = validate(sec(enabled=True, mode="shared_cache",
                     shared_cache={"enabled": True, "endpoint_ref": "build-cache.hive",
                                   "writes_enabled": True}))
    assert c.effective_mode is Mode.SHARED_CACHE and c.shared_writes_effective


def test_unknown_keys_rejected_at_every_level():
    bad("fabric_build_cache.enabeld", enabeld=True)
    bad("fabric_build_cache.cleanup.dryrun", cleanup={"dryrun": False})
    bad("fabric_build_cache.execution.slurm_enable", execution={"slurm_enable": True})


@pytest.mark.parametrize("over,frag", [
    ({"enabled": "false"}, "fabric_build_cache.enabled"),
    ({"cleanup": {"dry_run": "false"}}, "cleanup.dry_run"),
    ({"execution": {"slurm_enabled": 1}}, "execution.slurm_enabled"),
    ({"shared_cache": {"tls_required": "true"}}, "shared_cache.tls_required"),
    ({"local": {"capacity_gib": "20"}}, "local.capacity_gib"),
    ({"local": {"capacity_gib": True}}, "local.capacity_gib"),
])
def test_strict_types(over, frag):
    bad(frag, **over)


@pytest.mark.parametrize("over,frag", [
    ({"local": {"capacity_gib": 0}}, "capacity_gib"),
    ({"local": {"ram_scratch_gib": -1}}, "ram_scratch_gib"),
    ({"local": {"min_free_disk_percent": -1}}, "min_free_disk_percent"),
    ({"local": {"min_free_disk_percent": 100}}, "min_free_disk_percent"),
    ({"shared_cache": {"max_total_gib": 0}}, "max_total_gib"),
    ({"cleanup": {"max_evict_per_pass_gib": 0}}, "max_evict_per_pass_gib"),
    ({"cleanup": {"min_idle_hours": 0}}, "min_idle_hours"),
    ({"execution": {"max_build_slots": 0}}, "max_build_slots"),
    ({"execution": {"max_build_slots": 17}}, "max_build_slots"),
    ({"execution": {"max_build_slots": 2.5}}, "max_build_slots"),
    ({"mode": "turbo"}, "fabric_build_cache.mode"),
    ({"execution": {"remote_fallback": "ignore"}}, "remote_fallback"),
])
def test_ranges_and_enums(over, frag):
    bad(frag, **over)


def test_range_boundaries_pass():
    c = validate(sec(local={"ram_scratch_gib": 0, "min_free_disk_percent": 0},
                     cleanup={"min_idle_hours": 1}, execution={"max_build_slots": 16}))
    assert c.execution.max_build_slots == 16
    validate(sec(execution={"max_build_slots": 1}, local={"min_free_disk_percent": 99.9}))


def test_mode_implications():
    bad("fabric_build_cache.mode", mode="shared_cache")
    bad("writes_enabled", shared_cache={"writes_enabled": True})
    for m in ("slurm_full_build", "distributed"):
        bad("fabric_build_cache.mode", mode=m)
        bad("allowed_clusters", mode=m, execution=slurm_ok(allowed_clusters=[]))
        bad("allowed_partitions", mode=m, execution=slurm_ok(allowed_partitions=[]))
        assert validate(sec(enabled=True, mode=m, execution=slurm_ok())).effective_mode.value == m


@pytest.mark.parametrize("ep", ["https://cache.example", "user@host", "a/b", "a b", "Cache",
                                "", "-x", "a" * 64, 5])
def test_endpoint_ref_must_be_identifier(ep):
    bad("endpoint_ref", shared_cache={"endpoint_ref": ep})


def test_enabled_shared_cache_needs_endpoint():
    bad("endpoint_ref", shared_cache={"enabled": True})


def test_tls_can_only_drop_for_local_test_endpoint():
    bad("tls_required", shared_cache={"tls_required": False})
    bad("tls_required", shared_cache={"tls_required": False, "endpoint_ref": "cache"})
    c = validate(sec(shared_cache={"tls_required": False, "endpoint_ref": "x.local-test"}))
    assert c.shared_cache.tls_required is False


@pytest.mark.parametrize("key", ["secret_key", "password", "auth_token", "access_key_id",
                                 "api_key", "Secret"])
def test_embedded_secret_keys_rejected(key):
    bad(key, shared_cache={key: "hunter2"})
    bad(key, **{key: "x"})


def test_credentials_file_is_absolute_path_only():
    bad("credentials_file", shared_cache={"credentials_file": "relative/creds"})
    bad("credentials_file", shared_cache={"credentials_file": 12})
    c = validate(sec(shared_cache={"credentials_file": "/etc/creds"}))
    assert c.shared_cache.credentials_file == "/etc/creds"


def test_warnings_for_gpu_and_non_dry_run():
    c = validate(sec(execution={"gpu_for_cpu_builds": True}, cleanup={"dry_run": False}))
    assert len(c.warnings) == 2
    assert any("gpu_for_cpu_builds" in w for w in c.warnings)
    assert any("dry_run" in w for w in c.warnings)


def test_credentials_file_checks(tmp_path):
    good = tmp_path / "ok"
    good.write_text("s")
    good.chmod(0o600)
    assert check_credentials_file(str(good)) == good
    loose = tmp_path / "loose"
    loose.write_text("s")
    loose.chmod(0o644)
    with pytest.raises(InvalidError, match="group/other"):
        check_credentials_file(str(loose))
    link = tmp_path / "link"
    link.symlink_to(good)
    with pytest.raises(InvalidError, match="symlink"):
        check_credentials_file(str(link))
    with pytest.raises(InvalidError, match="regular file"):
        check_credentials_file(str(tmp_path))
    with pytest.raises(InvalidError, match="not readable"):
        check_credentials_file(str(tmp_path / "missing"))
    assert os.stat(good).st_uid == os.geteuid()


def test_public_view_has_no_secrets():
    c = validate(sec(enabled=True, mode="shared_cache",
                     shared_cache={"enabled": True, "endpoint_ref": "cache",
                                   "credentials_file": "/very/secret/path"}))
    view = c.public_view()
    assert "/very/secret/path" not in repr(view) and "credentials_file" not in repr(view)
    assert view["shared_cache"]["endpoint_ref"] == "cache"
    assert view["shared_cache"]["credentials_configured"] is True
    assert view["effective_mode"] == "shared_cache"


def _write(path, body):
    path.write_text(textwrap.dedent(body))
    return path


def test_load_config_defaults_and_invalid_fail_closed(tmp_path):
    cfg = load_config([])
    assert cfg["fabric_build_cache"]["enabled"] is False
    p = _write(tmp_path / "c.yaml", """
        fabric_build_cache:
          enabled: true
          cleanup:
            dry_run: "false"
    """)
    with pytest.raises(InvalidError, match="cleanup.dry_run"):
        load_config([p], strict_build_cache=True)
    # non-strict (every other command): fail closed to disabled, no crash
    assert load_config([p])["fabric_build_cache"]["enabled"] is False
    p = _write(tmp_path / "d.yaml", """
        fabric_build_cache:
          enabled: true
          mode: shared_cache
          shared_cache: {enabled: true, endpoint_ref: cache}
    """)
    assert load_config([p])["fabric_build_cache"]["shared_cache"]["endpoint_ref"] == "cache"


def test_legacy_config_loads_unchanged(tmp_path):
    legacy = _write(tmp_path / "legacy.yaml", """
        version: 1
        web:
          port: 9999
        build_cleanup:
          enabled: true
          roots: [/tmp/x]
    """)
    cfg = load_config([legacy])
    ref = load_config([])
    assert cfg["web"]["port"] == 9999 and cfg["build_cleanup"]["roots"] == ["/tmp/x"]
    others = {k: v for k, v in cfg.items() if k not in ("web", "build_cleanup")}
    assert others == {k: v for k, v in ref.items() if k not in ("web", "build_cleanup")}
    assert cfg["fabric_build_cache"] == ref["fabric_build_cache"]
    assert not validate(cfg["fabric_build_cache"]).enabled


def test_shipped_example_loads_with_feature_off():
    cfg = load_config([REPO / "config" / "herdr-engineering.example.yaml"])
    c = validate(cfg["fabric_build_cache"])
    assert not c.enabled and c.effective_mode is Mode.LOCAL_ONLY and c.cleanup.dry_run


def test_registry_file_is_absolute_path_only_and_not_in_public_view():
    bad("registry_file", shared_cache={"registry_file": "relative/reg.yaml"})
    bad("registry_file", shared_cache={"registry_file": 3})
    assert validate(sec()).shared_cache.registry_file is None
    c = validate(sec(shared_cache={"registry_file": "/very/private/reg.yaml"}))
    assert c.shared_cache.registry_file == "/very/private/reg.yaml"
    view = c.public_view()
    assert view["shared_cache"]["registry_configured"] is True
    assert "/very/private" not in repr(view)


def test_check_credentials_file_label_names_the_key(tmp_path):
    loose = tmp_path / "reg.yaml"
    loose.write_text("x")
    loose.chmod(0o644)
    with pytest.raises(InvalidError, match="registry_file"):
        check_credentials_file(str(loose), "registry_file")


def test_repo_local_config_cannot_set_build_cache_policy(tmp_path, monkeypatch):
    home, repo = tmp_path / "home", tmp_path / "repo"
    (home / ".config" / "herdr-engineering").mkdir(parents=True)
    repo.mkdir()
    (home / ".config" / "herdr-engineering" / "config.yaml").write_text(
        "fabric_build_cache:\n  enabled: true\n  shared_cache: {writes_enabled: false}\n")
    (repo / "herdr-engineering.yaml").write_text(
        "fabric_build_cache:\n  enabled: true\n  shared_cache: {writes_enabled: true,"
        " namespace: evil-ns}\nbuild_cleanup: {enabled: true}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERDR_ENGINEERING_CONFIG", raising=False)
    monkeypatch.chdir(repo)
    cfg = load_config()
    assert cfg["fabric_build_cache"]["shared_cache"]["writes_enabled"] is False
    assert cfg["fabric_build_cache"]["shared_cache"]["namespace"] != "evil-ns"
    assert cfg["build_cleanup"]["enabled"] is True  # other sections still merge


def test_invalid_repo_local_section_does_not_break_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.delenv("HERDR_ENGINEERING_CONFIG", raising=False)
    (tmp_path / "herdr-engineering.yaml").write_text(
        "fabric_build_cache: {enabled: yes-please}\n")
    monkeypatch.chdir(tmp_path)
    assert load_config(strict_build_cache=True)["fabric_build_cache"]["enabled"] is False
