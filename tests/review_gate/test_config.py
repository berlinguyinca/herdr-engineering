import pytest

from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config
from herdr_engineering.review_gate.config import (
    AppConfig,
    NotifyConfig,
    RepoEnrollment,
    ReviewerConfig,
    ReviewGateConfig,
    load_review_gate_config,
)
from tests.test_fleet_config import base, write


def key_file(tmp_path, mode=0o600):
    p = tmp_path / "app.pem"
    p.write_text("not-a-real-key")
    p.chmod(mode)
    return str(p)


def section(tmp_path, **over):
    sec = {
        "enabled": True,
        "app": {"app_id": 1, "installation_id": 2, "private_key_file": key_file(tmp_path)},
        "repos": [{"repo": "https://github.com/Acme/Widget.git"}],
    }
    sec.update(over)
    return {"control_plane": {"review_gate": sec}}


def test_absent_section_is_disabled():
    for cfg in ({}, {"control_plane": {}}):
        got = load_review_gate_config(cfg)
        assert got == ReviewGateConfig(enabled=False, app=None, repos=())


def test_repo_defaults(tmp_path):
    got = load_review_gate_config(section(tmp_path))
    assert got.enabled and got.poll_seconds == 15
    assert isinstance(got.app, AppConfig) and got.app.app_id == 1
    assert got.repos == (RepoEnrollment(repo="github.com/acme/widget"),)
    r = got.repos[0]
    assert r.required_events == ("pull_request",) and r.required_checks == ()
    assert (r.main_pipeline, r.disabled_tests_file, r.review_cap, r.ci_fix_cap) == (
        None,
        None,
        4,
        3,
    )


def test_present_section_defaults_to_disabled(tmp_path):
    cfg = section(tmp_path)
    del cfg["control_plane"]["review_gate"]["enabled"]
    assert load_review_gate_config(cfg).enabled is False


@pytest.mark.parametrize("repo", ["gitlab.com/a/b", "nonsense", "github.com/onlyowner", ""])
def test_bad_repo_rejected(tmp_path, repo):
    with pytest.raises(ValueError, match="repo"):
        load_review_gate_config(section(tmp_path, repos=[{"repo": repo}]))


def test_duplicate_repos_rejected(tmp_path):
    repos = [{"repo": "github.com/a/b"}, {"repo": "https://github.com/A/B.git"}]
    with pytest.raises(ValueError, match="duplicate"):
        load_review_gate_config(section(tmp_path, repos=repos))


def test_loose_key_permissions_rejected(tmp_path):
    cfg = section(tmp_path)
    cfg["control_plane"]["review_gate"]["app"]["private_key_file"] = key_file(tmp_path, 0o644)
    with pytest.raises(ValueError, match="private_key_file must be mode 0600"):
        load_review_gate_config(cfg)


def test_relative_key_path_rejected(tmp_path):
    cfg = section(tmp_path)
    cfg["control_plane"]["review_gate"]["app"]["private_key_file"] = "app.pem"
    with pytest.raises(ValueError, match="absolute"):
        load_review_gate_config(cfg)


@pytest.mark.parametrize("where", ["section", "app", "repo"])
def test_unknown_key_rejected(tmp_path, where):
    cfg = section(tmp_path)
    sec = cfg["control_plane"]["review_gate"]
    target = {"section": sec, "app": sec["app"], "repo": sec["repos"][0]}[where]
    target["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        load_review_gate_config(cfg)


def test_caps_and_poll_bounds(tmp_path):
    with pytest.raises(ValueError, match="poll_seconds"):
        load_review_gate_config(section(tmp_path, poll_seconds=4))
    with pytest.raises(ValueError, match="review_cap"):
        load_review_gate_config(
            section(tmp_path, repos=[{"repo": "github.com/a/b", "review_cap": 0}])
        )


def test_fleet_config_accepts_section(tmp_path):
    d = base()
    d["sections"]["control_plane"]["values"]["review_gate"] = section(tmp_path)["control_plane"][
        "review_gate"
    ]
    cfg = load_fleet_config(write(tmp_path, d))
    assert load_review_gate_config({"control_plane": cfg.section("control_plane")}).enabled


def test_fleet_config_rejects_unknown_review_gate_key(tmp_path):
    d = base()
    d["sections"]["control_plane"]["values"]["review_gate"] = {"bogus": 1}
    with pytest.raises(FleetConfigError, match="bogus"):
        load_fleet_config(write(tmp_path, d))


def _with_key(tmp_path, key):
    cfg = section(tmp_path)
    cfg["control_plane"]["review_gate"]["app"]["private_key_file"] = str(key)
    return cfg


def test_key_directory_rejected(tmp_path):
    d = tmp_path / "keydir"
    d.mkdir()
    d.chmod(0o600)
    with pytest.raises(ValueError, match="regular file") as ei:
        load_review_gate_config(_with_key(tmp_path, d))
    assert str(d) not in str(ei.value)


def test_symlink_to_good_key_accepted(tmp_path):
    link = tmp_path / "link.pem"
    link.symlink_to(key_file(tmp_path))
    assert load_review_gate_config(_with_key(tmp_path, link)).enabled


def test_symlink_to_loose_key_rejected(tmp_path):
    other = tmp_path / "loose.pem"
    other.write_text("x")
    other.chmod(0o644)
    link = tmp_path / "link.pem"
    link.symlink_to(other)
    with pytest.raises(ValueError, match="0600"):
        load_review_gate_config(_with_key(tmp_path, link))


def test_missing_key_file_rejected(tmp_path):
    with pytest.raises(ValueError, match="private_key_file") as ei:
        load_review_gate_config(_with_key(tmp_path, tmp_path / "nope.pem"))
    assert str(tmp_path) not in str(ei.value)


def test_empty_required_events_rejected_but_empty_checks_ok(tmp_path):
    with pytest.raises(ValueError, match="required_events"):
        load_review_gate_config(
            section(tmp_path, repos=[{"repo": "github.com/a/b", "required_events": []}])
        )
    ok = section(tmp_path, repos=[{"repo": "github.com/a/b", "required_checks": []}])
    assert load_review_gate_config(ok).repos[0].required_checks == ()


@pytest.mark.parametrize("key", ["review_cap", "ci_fix_cap"])
def test_bool_caps_rejected(tmp_path, key):
    with pytest.raises(ValueError, match=key):
        load_review_gate_config(section(tmp_path, repos=[{"repo": "github.com/a/b", key: True}]))


def test_string_enabled_rejected(tmp_path):
    with pytest.raises(ValueError, match="enabled"):
        load_review_gate_config(section(tmp_path, enabled="false"))


def test_non_list_repos_rejected(tmp_path):
    with pytest.raises(ValueError, match="repos"):
        load_review_gate_config(section(tmp_path, repos={"repo": "github.com/a/b"}))


@pytest.mark.parametrize("bad", ["x", 3, ["a"]])
def test_non_dict_section_rejected(bad):
    with pytest.raises(ValueError, match="mapping"):
        load_review_gate_config({"control_plane": {"review_gate": bad}})


def test_null_app_and_repos_are_absent(tmp_path):
    got = load_review_gate_config(section(tmp_path, enabled=False, app=None, repos=None))
    assert got.app is None and got.repos == () and not got.enabled
    with pytest.raises(ValueError, match="requires app"):
        load_review_gate_config(section(tmp_path, app=None, repos=None))


def test_reviewer_defaults(tmp_path):
    got = load_review_gate_config(section(tmp_path))
    assert got.reviewer == ReviewerConfig(900, 400_000, 50, 3, 200_000)
    assert got.notify == NotifyConfig((), ())


def test_reviewer_overrides_parse(tmp_path):
    rev = {
        "lease_seconds": 600,
        "max_diff_bytes": 100000,
        "max_findings": 20,
        "max_attempts": 2,
        "max_output_bytes": 50000,
    }
    got = load_review_gate_config(section(tmp_path, reviewer=rev))
    assert got.reviewer == ReviewerConfig(600, 100000, 20, 2, 50000)


@pytest.mark.parametrize(
    "bad",
    [
        {"lease_seconds": 0},
        {"lease_seconds": 29},
        {"lease_seconds": 86401},
        {"max_output_bytes": 999},
        {"max_output_bytes": 2_000_001},
        {"max_diff_bytes": 999},
        {"max_diff_bytes": 5_000_001},
        {"max_attempts": 0},
        {"max_diff_bytes": True},
        {"max_findings": "5"},
        {"foo": 1},
    ],
)
def test_reviewer_rejects_bad_values(tmp_path, bad):
    with pytest.raises(ValueError):
        load_review_gate_config(section(tmp_path, reviewer=bad))


def test_reviewer_bounds_are_inclusive(tmp_path):
    for rev in (
        {"lease_seconds": 30, "max_output_bytes": 1000, "max_diff_bytes": 1000},
        {"lease_seconds": 86400, "max_output_bytes": 2_000_000, "max_diff_bytes": 5_000_000},
    ):
        got = load_review_gate_config(section(tmp_path, reviewer=rev)).reviewer
        assert (got.lease_seconds, got.max_output_bytes, got.max_diff_bytes) == tuple(rev.values())


def test_notify_commands_are_argv_lists(tmp_path):
    cmd = ["/usr/local/bin/spawn-fixer", "--repo", "{repo}", "--pr", "{number}"]
    got = load_review_gate_config(section(tmp_path, notify={"fixer_command": cmd}))
    assert got.notify.fixer_command == tuple(cmd)
    assert got.notify.notify_command == ()
    ok = ["/bin/x", "{branch}", "{sha}"]
    got = load_review_gate_config(section(tmp_path, notify={"notify_command": ok}))
    assert got.notify.notify_command == tuple(ok)


@pytest.mark.parametrize(
    "bad",
    [
        "/usr/bin/x --repo {repo}",
        [],
        ["/bin/x", ""],
        ["/bin/x", 5],
        ["relative/x"],
        ["/bin/x", "{password}"],
        ["/bin/x", "{repo.x}"],
        ["/bin/x", "{repo[0]}"],
        ["/bin/x", "{}"],
        ["/bin/x", "{0}"],
        ["/bin/x", "{repo"],
        ["/bin/x", "{repo!r}"],
        ["/bin/x", "{repo:>10}"],
        ["/opt/{repo}/fix"],
        ["/bin/{sha}", "x"],
    ],
)
def test_notify_rejects_bad_commands(tmp_path, bad):
    with pytest.raises(ValueError):
        load_review_gate_config(section(tmp_path, notify={"fixer_command": bad}))


def test_notify_unknown_key_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_review_gate_config(section(tmp_path, notify={"foo": ["/bin/x"]}))
