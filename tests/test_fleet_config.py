"""Central fleet config: validation, revision, per-role scoping."""

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

_DB_URL = f"postgres://{'dbuser'}:{'dbvalue'}@h/db"  # assembled, not a literal credential URL
SECRET = "placeholder-super-value"


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def base() -> dict:
    return {
        "version": 1,
        "sections": {
            "common": {"roles": ["all"], "values": {"region": "us"}},
            "llm": {"roles": ["builder"], "values": {"upstream_key": SECRET, "upstream_url": "http://x"}},
            "ci": {"roles": ["ci"], "values": {"token": "placeholder-ci-token-value"}},
            "control_plane": {"roles": ["control-plane"], "values": {"pg_dsn": _DB_URL}},
        },
        "hosts": {
            "bender": {"roles": ["builder"], "token_sha256": sha("placeholder-host-bender")},
            "bare": {"roles": [], "token_sha256": sha("placeholder-host-bare")},
            "beast": {
                "roles": ["builder", "ci"],
                "token_sha256": sha("placeholder-host-beast"),
                "overrides": {"common": {"region": "eu"}},
            },
        },
    }


def write(tmp_path: Path, data, mode=0o600, text: str | None = None) -> Path:
    p = tmp_path / "fleet-config.yaml"
    p.write_text(text if text is not None else yaml.safe_dump(data))
    p.chmod(mode)
    return p


def test_slice_contains_only_allowed_sections(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    s = cfg.slice_for("bender")
    assert set(s) == {"common", "llm"}
    assert s["llm"]["upstream_key"] == SECRET
    assert "ci" not in s and "control_plane" not in s


def test_empty_roles_host_gets_only_all_sections(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    assert set(cfg.slice_for("bare")) == {"common"}


def test_overrides_win(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    assert cfg.slice_for("beast")["common"]["region"] == "eu"
    assert cfg.slice_for("bender")["common"]["region"] == "us"


def test_control_plane_section_never_in_a_host_slice(tmp_path):
    d = base()
    d["sections"]["control_plane"]["roles"] = ["builder"]
    cfg = load_fleet_config(write(tmp_path, d))
    assert "control_plane" not in cfg.slice_for("bender")
    assert cfg.section("control_plane")["pg_dsn"].startswith("postgres://")


def test_control_plane_section_cannot_be_all(tmp_path):
    d = base()
    d["sections"]["control_plane"]["roles"] = ["all"]
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, d))


def test_section_returns_values_regardless_of_roles(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    assert cfg.section("ci") == {"token": "placeholder-ci-token-value"}
    with pytest.raises(FleetConfigError):
        cfg.section("nope")


def test_duplicate_token_hash_rejected(tmp_path):
    d = base()
    d["hosts"]["bare"]["token_sha256"] = d["hosts"]["bender"]["token_sha256"]
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, d))


def test_group_readable_file_rejected(tmp_path):
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, base(), mode=0o640))
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, base(), mode=0o604))


def test_revision_changes_with_content_not_comments(tmp_path):
    text = yaml.safe_dump(base())
    a = load_fleet_config(write(tmp_path, None, text=text)).revision
    b = load_fleet_config(write(tmp_path, None, text="# a comment\n" + text)).revision
    assert a == b and len(a) == 12
    d = base()
    d["sections"]["common"]["values"]["region"] = "ap"
    assert load_fleet_config(write(tmp_path, d)).revision != a


def test_host_for_token_unknown_returns_none(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    assert cfg.host_for_token("placeholder-host-beast") == "beast"
    assert cfg.host_for_token("nope") is None
    assert cfg.host_for_token("") is None


def test_status_has_no_values(tmp_path):
    cfg = load_fleet_config(write(tmp_path, base()))
    out = json.dumps(cfg.status())
    assert SECRET not in out and "placeholder-ci-token-value" not in out and sha("placeholder-host-bender") not in out
    st = cfg.status()
    assert st["revision"] == cfg.revision
    assert st["hosts"]["beast"] == ["builder", "ci"]


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(version=2),
    lambda d: d.pop("sections"),
    lambda d: d["hosts"]["bender"].update(token_sha256="short"),
    lambda d: d["hosts"]["bender"].update(roles="builder"),
    lambda d: d["sections"]["llm"].pop("values"),
])
def test_invalid_shapes_rejected(tmp_path, mutate):
    d = base()
    mutate(d)
    with pytest.raises(FleetConfigError):
        load_fleet_config(write(tmp_path, d))


def test_errors_do_not_leak_values(tmp_path):
    p = write(tmp_path, None, text=f"version: 1\nsections: [{SECRET}\n")
    with pytest.raises(FleetConfigError) as e:
        load_fleet_config(p)
    assert SECRET not in str(e.value)


def test_missing_file(tmp_path):
    with pytest.raises(FleetConfigError):
        load_fleet_config(tmp_path / "absent.yaml")


def test_non_json_date_value_rejected_without_leak(tmp_path):
    text = yaml.safe_dump(base()) + ""
    text = text.replace("region: us", "region: us\n      since: 2026-10-21")
    assert "since: 2026-10-21" in text
    with pytest.raises(FleetConfigError) as e:
        load_fleet_config(write(tmp_path, None, text=text))
    assert "2026" not in str(e.value) and "JSON" in str(e.value)


def test_mixed_key_types_rejected_without_leak(tmp_path):
    d = base()
    d["sections"]["common"]["values"]["m"] = {1: "a", "b": SECRET}
    with pytest.raises(FleetConfigError) as e:
        load_fleet_config(write(tmp_path, d))
    assert SECRET not in str(e.value)


def test_cp_web_sources_control_plane_section(tmp_path, monkeypatch):
    from herdr_engineering import cli
    data = base()
    data["sections"]["control_plane"]["values"].update(
        {"rustfs_endpoint": "http://rfs:9000", "minio_user": "u", "minio_password": "p"})
    f = tmp_path / "fc.yaml"
    f.write_text(yaml.safe_dump(data))
    f.chmod(0o600)
    store, cp = cli._load_cp_fleet_config(str(f))
    assert store is not None and cp["pg_dsn"].startswith("postgres://")
    assert cli._load_cp_fleet_config(str(tmp_path / "missing.yaml")) == (None, {})
    seen = {}

    class Fake:
        def __init__(self, **kw):
            seen.update(kw)

        def ensure_bucket(self):
            pass
    import herdr_engineering.control_plane.rustfs as rfs
    monkeypatch.setattr(rfs, "RustFSClient", Fake)
    monkeypatch.setenv("HERDR_CP_RUSTFS_ENDPOINT", "http://env:1")  # ignored: file only
    cli._maybe_rustfs_client(cp)
    assert seen["endpoint"] == "http://rfs:9000" and seen["access_key"] == "u"


def test_store_picks_up_a_rename_replace(tmp_path):
    """sed -i / vim / our own writers replace the file by rename (new inode): the store
    must serve the new content (deploy.sh mounts the directory, so the container sees it)."""
    import os
    from herdr_engineering.fleet_config import FleetConfigStore
    p = write(tmp_path, base())
    store = FleetConfigStore(p)
    old = store.current().revision
    d = base()
    del d["hosts"]["beast"]
    tmp = p.with_name(".edit.tmp")
    tmp.write_text(yaml.safe_dump(d))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    assert store.current().revision != old
    assert "beast" not in store.current().status()["hosts"]


def _with_github_app(app):
    data = base()
    data["sections"]["github"] = {"roles": ["ci"], "values": {"token": "", "app": app}}
    return data


def test_a_github_app_block_survives_the_loader_and_the_host_slice(tmp_path):
    from herdr_engineering.github_auth import parse_app_config
    from herdr_engineering.host_config import HostConfig

    app = {"app_id": 123456, "private_key_file": "/etc/herdr/github-app.pem",
           "installations": {"InferWeave": 11, "berlinguyinca": "auto"}}
    cfg = load_fleet_config(write(tmp_path, _with_github_app(app)))
    values = json.loads(json.dumps(cfg.slice_for("beast")))  # the host cache is JSON
    host = HostConfig("http://cp", "beast", cfg.revision, values)
    assert host.get("github.app") == app
    assert parse_app_config(host.get("github.app")).owners == ("berlinguyinca", "inferweave")


@pytest.mark.parametrize("app", [{"app_id": "x"}, {"app_id": 1, "private_key_file": "rel.pem",
                                                   "installations": {"a": 1}}, "yes"])
def test_an_invalid_github_app_block_is_refused_by_the_loader(tmp_path, app):
    with pytest.raises(FleetConfigError, match="section github: github.app is invalid"):
        load_fleet_config(write(tmp_path, _with_github_app(app)))


# ---------------------------------------------------------------- unknown keys are rejected

def _with(mutate) -> dict:
    data = base()
    mutate(data)
    return data


def test_a_misplaced_top_level_control_plane_key_is_rejected_without_its_value(tmp_path):
    data = _with(lambda d: d.update(control_plane={"ci_deployments": {"frontend": SECRET}}))
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(write(tmp_path, data))
    msg = str(exc.value)
    assert "control_plane" in msg and "sections.control_plane.values" in msg
    assert SECRET not in msg and "frontend" not in msg


def test_any_other_unknown_top_level_key_is_rejected_by_name_only(tmp_path):
    data = _with(lambda d: d.update(extras=SECRET))
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(write(tmp_path, data))
    assert "extras" in str(exc.value) and SECRET not in str(exc.value)


def test_a_key_beside_values_in_a_section_is_rejected(tmp_path):
    data = _with(lambda d: d["sections"]["control_plane"].update(ci_deployments={"a": SECRET}))
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(write(tmp_path, data))
    msg = str(exc.value)
    assert "section control_plane" in msg and "ci_deployments" in msg and SECRET not in msg


def test_an_unknown_host_key_is_rejected(tmp_path):
    data = _with(lambda d: d["hosts"]["bender"].update(token=SECRET))
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(write(tmp_path, data))
    msg = str(exc.value)
    assert "host bender" in msg and "token" in msg and SECRET not in msg


def test_the_shipped_example_uses_only_the_known_keys():
    example = Path(__file__).resolve().parent.parent / "deploy" / "control-plane" / "fleet-config.example.yaml"
    data = yaml.safe_load(example.read_text())
    assert set(data) <= {"version", "sections", "hosts"}
    assert all(set(s) <= {"roles", "values"} for s in data["sections"].values())
    assert all(set(h) <= {"roles", "token_sha256", "overrides"} for h in data["hosts"].values())


# ------------------------------------------------------- control_plane.ci_deployments at load

@pytest.mark.parametrize("value", [
    "woodpecker-ci", ["woodpecker-ci"], {5: "woodpecker-ci"}, {"Bad Name": "woodpecker-ci"},
    {"frontend": 7}, {"frontend": "two words"}])
def test_a_malformed_ci_deployments_map_is_rejected_at_load(tmp_path, value):
    data = _with(lambda d: d["sections"]["control_plane"]["values"].update(ci_deployments=value))
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(write(tmp_path, data))
    assert str(exc.value).startswith("section control_plane: ") and "ci_deployments" in str(exc.value)


def test_a_valid_ci_deployments_map_loads(tmp_path):
    data = _with(lambda d: d["sections"]["control_plane"]["values"].update(
        ci_deployments={"frontend": "woodpecker-ci"}))
    cfg = load_fleet_config(write(tmp_path, data))
    assert cfg.section("control_plane")["ci_deployments"] == {"frontend": "woodpecker-ci"}


def test_the_yaml_on_key_typo_is_rejected_at_load(tmp_path):
    # YAML 1.1 reads an unquoted `on` as boolean True: the map key is not a component name.
    data = _with(lambda d: d["sections"]["control_plane"]["values"].update(ci_deployments="ON-MARKER"))
    text = yaml.safe_dump(data).replace("ci_deployments: ON-MARKER", "ci_deployments:\n        on: woodpecker-ci")
    assert "on: woodpecker-ci" in text
    with pytest.raises(FleetConfigError, match="ci_deployments"):
        load_fleet_config(write(tmp_path, None, text=text))
