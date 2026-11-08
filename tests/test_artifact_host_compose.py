"""Deploy wiring of the artifact host: compose topology, edge routing, the fleet slice, and a real `docker compose up`.

Parse-level tests read the compose files and the rendered edge nginx.conf; the docker tests (bottom) build the image,
bring the host up under a throwaway project, and prove the isolation Docker actually enforces.  They skip only when no
docker daemon is reachable, and FAIL under CI or HERDR_REQUIRE_DOCKER=1.  No test touches the live control-plane project:
every compose call names its own project (-p) and its own network prefix.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from herdr_artifact_host import host_config
from herdr_engineering import fleet_config_cli as fcc
from herdr_engineering.fleet_config import FleetConfigError, load_fleet_config

ROOT = Path(__file__).resolve().parents[1]
CP = ROOT / "deploy" / "control-plane"
HOST_COMPOSE = ROOT / "deploy" / "artifact-host" / "compose.yaml"
OVERLAY = CP / "compose.artifact-host.yaml"
EDGE_DIR = CP / "collab-edge"
HOST = "artifacts.example.test"
MCP_HOST = "mcp.example.test"


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------- compose topology


@pytest.fixture(scope="module")
def host() -> dict:
    return _yaml(HOST_COMPOSE)


@pytest.fixture(scope="module")
def overlay() -> dict:
    return _yaml(OVERLAY)


def test_the_host_project_has_exactly_the_three_services_volumes_and_networks(host: dict) -> None:
    assert set(host["services"]) == {
        "artifact-host-db",
        "artifact-host-public",
        "artifact-host-internal",
    }
    assert set(host["volumes"]) == {"artifact-host-blobs", "artifact-host-db"}
    assert set(host["networks"]) == {
        "artifact-host",
        "artifact-db-internal",
        "artifact-edge",
        "artifact-internal",
    }


def test_no_host_container_publishes_a_port_or_pins_a_name(host: dict) -> None:
    for name, svc in host["services"].items():
        assert "ports" not in svc, name
        assert "container_name" not in svc, (
            f"{name}: a fixed name would collide with the live stack"
        )
        assert "network_mode" not in svc, name


def test_every_host_network_is_internal_so_nothing_has_a_route_out(host: dict) -> None:
    for name, net in host["networks"].items():
        assert net.get("internal") is True, name


def test_public_and_internal_roles_share_no_network(host: dict) -> None:
    public = set(host["services"]["artifact-host-public"]["networks"])
    internal = set(host["services"]["artifact-host-internal"]["networks"])
    assert public == {"artifact-host", "artifact-edge"}
    assert internal == {"artifact-db-internal", "artifact-internal"}
    assert not public & internal
    assert set(host["services"]["artifact-host-db"]["networks"]) == {
        "artifact-host",
        "artifact-db-internal",
    }


def test_only_the_internal_role_and_the_dashboard_are_on_artifact_internal(
    host: dict, overlay: dict
) -> None:
    on_internal = {n for n, s in host["services"].items() if "artifact-internal" in s["networks"]}
    assert on_internal == {"artifact-host-internal"}
    assert [n for n, s in overlay["services"].items() if "artifact-internal" in s["networks"]] == [
        "control-plane-web"
    ]


def test_only_the_edge_joins_artifact_edge_and_no_cp_service_joins_the_other_host_networks(
    overlay: dict,
) -> None:
    assert [n for n, s in overlay["services"].items() if "artifact-edge" in s["networks"]] == [
        "collab-edge"
    ]
    assert set(overlay["services"]) == {"control-plane-web", "collab-edge"}
    for svc in overlay["services"].values():
        assert not {"artifact-host", "artifact-db-internal"} & set(svc["networks"])
    assert all(net.get("external") is True for net in overlay["networks"].values())
    assert set(overlay["networks"]) == {"artifact-internal", "artifact-edge"}


def test_the_overlay_keeps_the_networks_the_services_already_have(overlay: dict) -> None:
    """Naming `networks:` replaces the implicit default; the real services' own networks must be listed again."""
    base = _yaml(CP / "compose.collab.yaml")["services"]["collab-edge"]["networks"]
    assert set(base) <= set(overlay["services"]["collab-edge"]["networks"])
    assert "default" in overlay["services"]["control-plane-web"]["networks"]


def test_network_names_line_up_between_the_two_files(host: dict, overlay: dict) -> None:
    for net in ("artifact-internal", "artifact-edge"):
        assert host["networks"][net]["name"] == overlay["networks"][net]["name"]


def test_the_edge_network_subnet_is_fixed_and_refused_on_the_operator_listener(host: dict) -> None:
    subnet = host["networks"]["artifact-edge"]["ipam"]["config"][0]["subnet"]
    default = re.search(r"\$\{HERDR_ARTIFACT_EDGE_SUBNET:-([0-9./]+)\}", subnet)
    assert default is not None
    nginx = (EDGE_DIR / "nginx.conf").read_text(encoding="utf-8")
    assert f"deny {default.group(1)};" in nginx, (
        "nginx.conf must refuse the artifact-edge subnet on :8082"
    )
    # the other fixed subnets stay distinct
    assert default.group(1) not in nginx.replace(f"deny {default.group(1)};", "")


def test_roles_are_hardened_and_have_their_own_healthcheck(host: dict) -> None:
    for name in ("artifact-host-public", "artifact-host-internal"):
        svc = host["services"][name]
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["user"] == "10001:10001", name
        assert svc["depends_on"]["artifact-host-db"]["condition"] == "service_healthy", name
        assert svc["healthcheck"]["test"][:3] == ["CMD", "herdr-artifact-host", "healthcheck"], name
    roles = [
        host["services"][n]["healthcheck"]["test"][3]
        for n in ("artifact-host-public", "artifact-host-internal")
    ]
    assert roles == ["public", "internal"]
    assert host["services"]["artifact-host-db"]["healthcheck"]["test"][0] == "CMD-SHELL"


def test_the_internal_role_cannot_write_the_blobs_and_only_the_public_role_sees_the_sync_dir(
    host: dict,
) -> None:
    pub, internal = (
        host["services"][n]["volumes"] for n in ("artifact-host-public", "artifact-host-internal")
    )
    assert "artifact-host-blobs:/var/lib/artifact-host/blobs" in pub
    assert internal == ["artifact-host-blobs:/var/lib/artifact-host/blobs:ro"]
    assert any(v.endswith(":/sync:ro") for v in pub)
    assert not any("/sync" in v for v in internal)


def test_secrets_are_the_config_slice_and_the_db_password_only(host: dict) -> None:
    assert set(host["secrets"]) == {"artifact-host-config", "artifact-host-db-password"}
    assert host["services"]["artifact-host-public"]["secrets"] == ["artifact-host-config"]
    assert host["services"]["artifact-host-db"]["secrets"] == ["artifact-host-db-password"]
    # Compose ignores uid/gid/mode on file secrets (it says so at `up`); the file must not pretend otherwise.
    assert "uid" not in json.dumps(host["x-role"]["secrets"])
    env = host["services"]["artifact-host-db"]["environment"]
    assert (
        "POSTGRES_PASSWORD" not in env
        and env["POSTGRES_PASSWORD_FILE"] == "/run/secrets/artifact-host-db-password"
    )
    for name in ("artifact-host-public", "artifact-host-internal"):
        assert "environment" not in host["services"][name], (
            "roles read their configuration from the secret only"
        )


def test_images_are_pinned_and_the_dockerfile_ships_the_member_page() -> None:
    text = (ROOT / "deploy" / "artifact-host" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^ARG PYTHON_BASE=\S+@sha256:[0-9a-f]{64}$", text, re.M)
    assert re.search(r"^USER 10001:10001$", text, re.M)
    assert not re.search(r"^(COPY|ADD|RUN).*herdr_engineering", text, re.M)
    assert "member_static/*" in (ROOT / "deploy" / "artifact-host" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r"image: postgres:\S+@sha256:[0-9a-f]{64}", HOST_COMPOSE.read_text(encoding="utf-8")
    )


def test_the_member_page_is_in_the_package_that_the_image_installs() -> None:
    static = ROOT / "herdr_artifact_host" / "member_static"
    assert {p.name for p in static.iterdir()} >= {"member.html", "member.js", "member.css"}


# --------------------------------------------------------------------------------------------- the rendered edge


def _central(
    tmp_path: Path,
    *,
    host_section: dict | None,
    share: dict | None = None,
    library_key: str | None = "k" * 40,
):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_ah_deploy_scripts", ROOT / "tests" / "test_deploy_scripts.py"
    )
    assert spec and spec.loader
    ds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ds)
    central = tmp_path / "fleet" / "fleet-config.yaml"
    central.parent.mkdir(parents=True)
    fcc.init_config(central, dict(ds.ANSWERS))
    data = yaml.safe_load(central.read_text())
    cpv = data["sections"]["control_plane"]["values"]
    cpv["api_keys"] = {"placeholder-api-key-value": "operator"}
    cpv["collab"] = {
        "cognito_pool": "us-west-2_TESTPOOL",
        "cognito_clients": ["client-a"],
        "cognito_group": "team.test",
        "cognito_web_client": "webclient1",
        "cognito_domain": "https://pool.auth.us-west-2.amazoncognito.com",
        "public_url": f"https://{MCP_HOST}/",
        "operator_user": "owner-test",
    }
    if share is not None:
        cpv["share"] = share
    if host_section is not None:
        cpv["artifact_host"] = host_section
    if library_key is not None:
        cpv["artifact_library"] = {
            "host_url": "http://artifact-host-internal:8091",
            "host_key": library_key,
        }
    central.unlink()
    fcc.write_private_file(central, yaml.safe_dump(data, sort_keys=False))
    return central


def _host_section(**over: object) -> dict:
    section: dict = {
        "enabled": True,
        "public_host": HOST,
        "mcp_public_hosts": [MCP_HOST],
        "session_secret": secrets.token_hex(32),
        "claim_key": secrets.token_hex(32),
        "db_password": secrets.token_hex(16),
    }
    section.update(over)
    return section


@pytest.fixture()
def rendered_edge(tmp_path: Path) -> str:
    template = EDGE_DIR / "nginx.conf"
    return fcc.render_edge_conf(
        template.read_text(encoding="utf-8"), share=False, artifact_host=HOST, snippets_dir=EDGE_DIR
    )


def _code(conf: str) -> str:
    """The configuration without its comments (they may name the networks they explain)."""
    return re.sub(r"(?m)^\s*#.*$|\s#\s.*$", "", conf)


def _server_block(conf: str, name: str) -> str:
    start = conf.index(f"server_name {name}")
    begin = conf.rindex("server {", 0, start)
    depth, i = 0, begin
    while True:
        depth += {"{": 1, "}": -1}.get(conf[i], 0)
        i += 1
        if depth == 0 and i > begin + 8:
            return conf[begin:i]


def _locations(block: str) -> dict[str, str]:
    """location header -> its body, for the outermost locations of one server block."""
    out: dict[str, str] = {}
    for m in re.finditer(r"^    location (.+?) \{", block, re.M):
        i, depth = m.end(), 1
        while depth:
            depth += {"{": 1, "}": -1}.get(block[i], 0)
            i += 1
        out[m.group(1)] = block[m.end() : i - 1]
    return out


def test_disabled_the_template_is_unchanged_and_names_no_host_upstream() -> None:
    template = (EDGE_DIR / "nginx.conf").read_text(encoding="utf-8")
    assert fcc.render_edge_conf(template, share=False) == template
    assert "artifact-host-public" not in _code(template)
    assert "artifact_host" not in _code(template)


def test_the_artifact_server_routes_the_public_surface_to_the_public_role_only(
    rendered_edge: str,
) -> None:
    block = _server_block(rendered_edge, HOST)
    locs = _locations(block)
    for needed in ("/s/", "/api/member/", "= /", "/my/"):
        assert needed in locs, needed
        assert "proxy_pass http://artifact_host;" in locs[needed], needed
    assert "server artifact-host-public:8090 " in rendered_edge
    for text in (_code(rendered_edge),):
        assert "8091" not in text, "nothing at the edge may name the internal listener"
        assert "artifact-host-internal" not in text and "artifact-internal" not in text
        assert "artifact-host-db" not in text and "herdr-cp-" not in text
    # every proxy_pass in the host's server goes to the one upstream
    assert set(re.findall(r"proxy_pass (\S+);", block)) == {"http://artifact_host"}


def _select(locs: dict[str, str], path: str) -> str | None:
    """The body of the location nginx would choose for ``path`` (exact, then longest prefix, then the first regex)."""
    exact = [h for h in locs if h.startswith("= ") and h[2:] == path]
    if exact:
        return locs[exact[0]]
    prefixes = [h for h in locs if not h.startswith(("= ", "~")) and path.startswith(h)]
    for header, body in locs.items():
        if header.startswith("~ ") and re.search(header[2:].strip('"').replace("\\z", "\\Z"), path):
            return body
    return locs[max(prefixes, key=len)] if prefixes else None


def test_healthz_and_internal_are_never_forwarded(rendered_edge: str) -> None:
    for name in (HOST, "mcp.local.metabolomics.us"):
        locs = _locations(_server_block(rendered_edge, name))
        for path in (
            "/healthz",
            "/healthz/",
            "/internal",
            "/internal/",
            "/internal/healthz",
            "/internal/library/x",
        ):
            body = _select(locs, path)
            assert body is not None and "proxy_pass" not in body, (name, path)
    locs = _locations(_server_block(rendered_edge, HOST))
    assert any("healthz" in h and "internal" in h for h in locs), (
        "an explicit refusal, so a later broad location cannot"
    )
    # the helper itself must see the routes that are proxied (guards against a matcher that never matches)
    assert "proxy_pass" in (_select(locs, "/s/" + "A" * 43 + "/index.html") or "")
    assert "proxy_pass" in (_select(locs, "/api/member/artifacts") or "")
    assert "proxy_pass" in (_select(locs, "/") or "") and "proxy_pass" in (
        _select(locs, "/my/x") or ""
    )


def test_the_forwarded_for_header_is_overwritten_never_appended(rendered_edge: str) -> None:
    block = _server_block(rendered_edge, HOST)
    assert re.search(r"proxy_set_header X-Forwarded-For \$remote_addr;", block)
    assert "$proxy_add_x_forwarded_for" not in rendered_edge
    for header in ("X-Forwarded-Host", "Forwarded", "X-Real-IP"):
        assert re.search(rf'proxy_set_header {header} "";', block), header


def test_share_bodies_are_capped_and_never_forwarded_for_reads(rendered_edge: str) -> None:
    locs = _locations(_server_block(rendered_edge, HOST))
    reads = locs["/s/"]
    assert "client_max_body_size 1k;" in reads and "proxy_pass_request_body off;" in reads
    assert "if ($share_has_body) { return 404; }" in reads
    annotations = next(b for h, b in locs.items() if h.startswith("~") and "annotations" in h)
    assert "client_max_body_size 32k;" in annotations
    server = _server_block(rendered_edge, HOST)
    assert re.search(r"^    client_max_body_size 1k;", server, re.M), (
        "the server-wide default is the small one"
    )
    # a run upload chunk is the only large body, and only on its exact path
    big = [m for m in re.findall(r"client_max_body_size (\d+)([km])", server) if m[1] == "m"]
    assert sorted(big) == [("1", "m"), ("17", "m")]


def test_mcp_artifacts_is_routed_on_the_mcp_host_with_the_mcp_budget(rendered_edge: str) -> None:
    mcp = _locations(_server_block(rendered_edge, "mcp.local.metabolomics.us"))
    assert "= /mcp/artifacts" in mcp
    body = mcp["= /mcp/artifacts"]
    assert "proxy_pass http://artifact_host;" in body
    assert "limit_req zone=hcn_mcp" in body and "client_max_body_size 768k;" in body
    assert "if ($request_method !~ ^POST$) { return 404; }" in body
    # the artifact host's own name does not serve MCP
    assert "= /mcp/artifacts" not in _locations(_server_block(rendered_edge, HOST))


def test_existing_mcp_routes_are_byte_for_byte_unchanged(rendered_edge: str) -> None:
    template = (EDGE_DIR / "nginx.conf").read_text(encoding="utf-8")
    base = _locations(_server_block(template, "mcp.local.metabolomics.us"))
    now = _locations(_server_block(rendered_edge, "mcp.local.metabolomics.us"))
    assert {k: v for k, v in now.items() if k != "= /mcp/artifacts"} == base
    assert "upstream collab { server collab-public:8081; keepalive 16; }" in rendered_edge
    assert "upstream collab_operator { server collab-public:8082; keepalive 4; }" in rendered_edge


def test_the_host_and_share_public_cannot_both_be_served(tmp_path: Path) -> None:
    template = (EDGE_DIR / "nginx.conf").read_text(encoding="utf-8")
    with pytest.raises(FleetConfigError, match="not both"):
        fcc.render_edge_conf(
            template,
            share=True,
            share_host="s.example.test",
            artifact_host=HOST,
            snippets_dir=EDGE_DIR,
        )


def test_render_edge_picks_the_host_from_the_fleet_config(tmp_path: Path) -> None:
    central = _central(tmp_path, host_section=_host_section())
    out = tmp_path / "nginx.conf"
    fcc.render_edge(load_fleet_config(central), EDGE_DIR / "nginx.conf", out)
    assert f"server_name {HOST};" in out.read_text(encoding="utf-8")
    off = _central(tmp_path / "off", host_section=_host_section(enabled=False))
    fcc.render_edge(load_fleet_config(off), EDGE_DIR / "nginx.conf", tmp_path / "off.conf")
    assert (tmp_path / "off.conf").read_text(encoding="utf-8") == (
        EDGE_DIR / "nginx.conf"
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------- the fleet slice


def test_the_slice_is_accepted_by_the_hosts_own_parser_and_the_key_sets_agree(
    tmp_path: Path,
) -> None:
    central = _central(tmp_path, host_section=_host_section(health_port=9300))
    out = tmp_path / "artifact-host-config.json"
    fcc.render_artifact_host(load_fleet_config(central), out)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    data = json.loads(out.read_text())
    cfg = host_config.parse(data)
    assert set(data) == set(host_config.TOP_KEYS)
    assert fcc.ARTIFACT_HOST_SCHEMA_VERSION == host_config.SCHEMA_VERSION
    assert (
        cfg.public_host == HOST and cfg.mcp_public_hosts == (MCP_HOST,) and cfg.health_port == 9300
    )
    assert (
        cfg.group == "team.test"
        and cfg.pool_id == "us-west-2_TESTPOOL"
        and cfg.client_ids == ("client-a",)
    )
    import hashlib

    assert cfg.internal_key_digest == hashlib.sha256(b"k" * 40).hexdigest()
    assert "k" * 40 not in out.read_text(), "the dashboard's key itself never reaches the host"
    assert fcc.ARTIFACT_HOST_MIN_SECRET_BYTES == host_config.MIN_SECRET_BYTES


def test_disabled_renders_nothing_and_is_reported_as_disabled(tmp_path: Path) -> None:
    central = _central(tmp_path, host_section=None)
    out = tmp_path / "x.json"
    fcc.render_artifact_host(load_fleet_config(central), out)
    assert not out.exists()
    assert fcc.artifact_host_enabled(load_fleet_config(central)) is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"session_secret": "short"}, "session_secret"),
        ({"claim_key": None}, "claim_key"),
        ({"db_password": "x"}, "db_password"),
        ({"public_host": "https://artifacts.example.test"}, "public_host"),
        ({"public_host": "Artifacts.Example.Test"}, "public_host"),
        ({"mcp_public_hosts": [HOST]}, "mcp_public_hosts"),
        ({"health_port": 8090}, "health_port"),
        ({"session_seconds": 5}, "session_seconds"),
        ({"surprise": 1}, "unknown keys"),
    ],
)
def test_a_weak_or_malformed_slice_is_refused_naming_keys_only(
    tmp_path: Path, override: dict, message: str
) -> None:
    section = _host_section(**override)
    central = _central(tmp_path, host_section=section)
    with pytest.raises(FleetConfigError, match=message) as err:
        fcc.render_artifact_host(load_fleet_config(central), tmp_path / "x.json")
    for value in (section["session_secret"], section["db_password"]):
        if len(str(value)) >= 16:
            assert str(value) not in str(err.value)
    assert not (tmp_path / "x.json").exists()


def test_the_two_secrets_must_differ(tmp_path: Path) -> None:
    same = secrets.token_hex(32)
    central = _central(tmp_path, host_section=_host_section(session_secret=same, claim_key=same))
    with pytest.raises(FleetConfigError, match="must differ"):
        fcc.render_artifact_host(load_fleet_config(central), tmp_path / "x.json")


def test_the_dashboard_key_is_required_and_share_public_is_exclusive(tmp_path: Path) -> None:
    nokey = _central(tmp_path / "a", host_section=_host_section(), library_key=None)
    with pytest.raises(FleetConfigError, match="artifact_library.host_key"):
        fcc.render_artifact_host(load_fleet_config(nokey), tmp_path / "x.json")
    both = _central(
        tmp_path / "b",
        host_section=_host_section(),
        share={
            "enabled": True,
            "signing_secret": secrets.token_hex(32),
            "public_url": "https://share.example.test/",
        },
    )
    with pytest.raises(FleetConfigError, match="cannot both be enabled"):
        fcc.render_artifact_host(load_fleet_config(both), tmp_path / "x.json")


def test_fleet_validate_names_an_incomplete_host(tmp_path: Path) -> None:
    central = _central(tmp_path, host_section=_host_section(db_password="x"))
    with pytest.raises(FleetConfigError, match="db_password"):
        fcc.validate_config(central)


# ------------------------------------------------------------------------------------- real docker (bottom)

_STRICT = bool(os.environ.get("CI")) or os.environ.get("HERDR_REQUIRE_DOCKER") == "1"


def _docker(
    *args: str, check: bool = True, timeout: int = 600, env: dict | None = None
) -> subprocess.CompletedProcess:
    run = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if check and run.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:6])} failed ({run.returncode}): {run.stderr[-1500:]}")
    return run


@pytest.fixture(scope="module")
def docker_ok() -> None:
    reason = None
    if shutil.which("docker") is None:
        reason = "docker is not installed"
    elif subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode != 0:
        reason = "no docker daemon is reachable"
    elif (
        subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=60).returncode
        != 0
    ):
        reason = "docker compose is not installed"
    if reason:
        if _STRICT:
            pytest.fail(reason + " (required under CI or HERDR_REQUIRE_DOCKER=1)")
        pytest.skip(reason)


def test_nginx_accepts_the_rendered_edge_conf(
    docker_ok: None, tmp_path: Path, rendered_edge: str
) -> None:
    if shutil.which("openssl") is None:
        pytest.fail("openssl is required for this test") if _STRICT else pytest.skip(
            "openssl is not installed"
        )
    acme = tmp_path / "acme"
    acme.mkdir()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(acme / "privkey.pem"),
            "-out",
            str(acme / "fullchain.pem"),
            "-days",
            "1",
            "-subj",
            "/CN=test",
        ],
        check=True,
        capture_output=True,
    )
    for f in acme.iterdir():
        f.chmod(0o644)
    conf = tmp_path / "nginx.conf"
    conf.write_text(rendered_edge, encoding="utf-8")
    run = _docker(
        "run",
        "--rm",
        "--add-host",
        "collab-public:127.0.0.1",
        "-v",
        f"{conf}:/etc/nginx/nginx.conf:ro",
        "-v",
        f"{acme}:/etc/acme:ro",
        "nginx:1.29-alpine",
        "nginx",
        "-t",
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]


_STAND_IN = """\
name: placeholder
services:
  postgres:
    image: {image}
    entrypoint: [python, -c, "import time; time.sleep(3600)"]
  control-plane-web:
    image: {image}
    entrypoint: [python, -c, "import time; time.sleep(3600)"]
  collab-edge:
    image: {image}
    entrypoint: [python, -c, "import time; time.sleep(3600)"]
networks:
  collab: {{}}
  share: {{}}
  edge: {{}}
"""

_PROBE = """
import socket, sys, urllib.request, urllib.error
kind, target = sys.argv[1], sys.argv[2]
if kind == "resolve":
    try:
        socket.getaddrinfo(target, None)
    except OSError:
        sys.exit(1)
    sys.exit(0)
if kind == "connect":
    host, port = target.rsplit(":", 1)
    try:
        socket.create_connection((host, int(port)), timeout=3).close()
    except OSError:
        sys.exit(1)
    sys.exit(0)
try:
    print(urllib.request.urlopen(target, timeout=5).status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except OSError:
    sys.exit(2)
"""


class _Stack:
    """A throwaway host project plus stand-in control-plane services on the real overlay."""

    def __init__(self, tmp: Path, image: str, tag: str) -> None:
        token = uuid.uuid4().hex[:8]
        self.project, self.cp_project = f"ahtest{token}", f"ahcp{token}"
        self.tmp = tmp
        sync = tmp / "sync"
        sync.mkdir()
        (sync / "jwks.json").write_text("{}")
        (sync / "members.json").write_text(json.dumps({"updated": 0, "groups": {}, "disabled": []}))
        password = "pw-" + secrets.token_hex(12)
        config = {
            "version": 1,
            "public_host": HOST,
            "public_url": f"https://{HOST}",
            "mcp_public_hosts": [MCP_HOST],
            "session_seconds": 14400,
            "cognito": {
                "pool_id": "us-west-2_T",
                "client_ids": ["c"],
                "web_client_id": "w",
                "domain": "https://p.example.test",
                "group": "team",
            },
            "session_secret": secrets.token_hex(32),
            "claim_key": secrets.token_hex(32),
            "internal_key_digest": "a" * 64,
            "health_port": 8092,
            "database": {"user": "artifact_host", "password": password},
        }
        (tmp / "config.json").write_text(json.dumps(config))
        (tmp / "dbpw").write_text(password)
        for name in (
            "config.json",
            "dbpw",
        ):  # Compose ignores secret uid/mode: the host file's own mode decides
            (tmp / name).chmod(0o644)
        (tmp / "standin.yaml").write_text(_STAND_IN.format(image=image))
        self.env = {
            "HERDR_ARTIFACT_HOST_PROJECT": self.project,
            "HERDR_ARTIFACT_HOST_TAG": tag,
            "HERDR_ARTIFACT_NET_PREFIX": self.project,
            "HERDR_ARTIFACT_EDGE_SUBNET": "172.31.253.0/28",
            "HERDR_ARTIFACT_HOST_CONFIG": str(tmp / "config.json"),
            "HERDR_ARTIFACT_HOST_DB_PASSWORD_FILE": str(tmp / "dbpw"),
            "HERDR_COLLAB_SYNC_DIR": str(sync),
        }

    def host(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return _docker(
            "compose", "-p", self.project, "-f", str(HOST_COMPOSE), *args, check=check, env=self.env
        )

    def cp(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return _docker(
            "compose",
            "-p",
            self.cp_project,
            "-f",
            str(self.tmp / "standin.yaml"),
            "-f",
            str(OVERLAY),
            *args,
            check=check,
            env=self.env,
        )

    def probe(self, where: str, kind: str, target: str) -> subprocess.CompletedProcess:
        """Run the probe inside a container of the host project (`host:<service>`) or the stand-in project."""
        side, service = where.split(":")
        run = self.host if side == "host" else self.cp
        return run("exec", "-T", service, "python", "-c", _PROBE, kind, target, check=False)

    def down(self) -> None:
        self.cp("down", "-v", "--remove-orphans", check=False)
        self.host("down", "-v", "--remove-orphans", check=False)


@pytest.fixture(scope="module")
def stack(docker_ok: None, tmp_path_factory: pytest.TempPathFactory):
    tag = "test" + uuid.uuid4().hex[:10]
    image = f"herdr-artifact-host:{tag}"
    _docker(
        "build",
        "-q",
        "-t",
        image,
        "-f",
        str(ROOT / "deploy" / "artifact-host" / "Dockerfile"),
        str(ROOT),
        timeout=1500,
    )
    s = _Stack(tmp_path_factory.mktemp("artifact-host-stack"), image, tag)
    try:
        s.host(
            "run",
            "--rm",
            "artifact-host-public",
            "migrate",
            "--config",
            "/run/secrets/artifact-host-config",
        )
        s.host("up", "-d", "--wait", "--wait-timeout", "180")
        s.cp("up", "-d")
        yield s
    finally:
        s.down()
        _docker("rmi", image, check=False)


def test_the_stack_comes_up_healthy_without_publishing_any_port(stack: _Stack) -> None:
    rows = [
        json.loads(line)
        for line in stack.host("ps", "--format", "json").stdout.splitlines()
        if line.strip()
    ]
    assert {r["Service"] for r in rows} == {
        "artifact-host-db",
        "artifact-host-public",
        "artifact-host-internal",
    }
    assert all(r["Health"] == "healthy" for r in rows), rows
    assert all(
        not r.get("Publishers") or all(p.get("PublishedPort", 0) == 0 for p in r["Publishers"])
        for r in rows
    )


@pytest.mark.parametrize(
    "name", ["control-plane-web", "herdr-cp-web", "postgres", "herdr-cp-postgres"]
)
def test_control_plane_names_do_not_resolve_from_the_public_role(stack: _Stack, name: str) -> None:
    assert stack.probe("host:artifact-host-public", "resolve", name).returncode == 1


@pytest.mark.parametrize(
    "name", ["postgres", "herdr-cp-postgres", "collab-edge", "artifact-host-public"]
)
def test_the_internal_role_sees_neither_the_cp_database_nor_the_public_role(
    stack: _Stack, name: str
) -> None:
    assert stack.probe("host:artifact-host-internal", "resolve", name).returncode == 1


def test_the_public_role_does_not_see_the_internal_role_or_the_dashboard(stack: _Stack) -> None:
    assert (
        stack.probe("host:artifact-host-public", "resolve", "artifact-host-internal").returncode
        == 1
    )
    assert stack.probe("host:artifact-host-public", "resolve", "control-plane-web").returncode == 1


@pytest.mark.parametrize(
    "name", ["artifact-host-internal", "artifact-host-db", "artifact-host-public"]
)
def test_stand_in_cp_postgres_reaches_no_host_service(stack: _Stack, name: str) -> None:
    assert stack.probe("cp:postgres", "resolve", name).returncode == 1


def test_the_edge_reaches_the_public_role_and_only_it(stack: _Stack) -> None:
    assert stack.probe("cp:collab-edge", "resolve", "artifact-host-public").returncode == 0
    reach = stack.probe("cp:collab-edge", "http", "http://artifact-host-public:8090/s/nothing/")
    assert reach.returncode == 0 and reach.stdout.strip() == "404", reach.stdout + reach.stderr
    for name in ("artifact-host-internal", "artifact-host-db"):
        assert stack.probe("cp:collab-edge", "resolve", name).returncode == 1, name
    # /healthz is not on the public listener: the same uniform 404
    health = stack.probe("cp:collab-edge", "http", "http://artifact-host-public:8090/healthz")
    assert health.stdout.strip() == "404"


def test_the_dashboard_reaches_the_internal_role_which_demands_its_key(stack: _Stack) -> None:
    health = stack.probe(
        "cp:control-plane-web", "http", "http://artifact-host-internal:8091/internal/healthz"
    )
    assert health.stdout.strip() == "200", health.stdout + health.stderr
    # anything else needs the bearer key: a request without it is refused
    got = stack.probe(
        "cp:control-plane-web", "http", "http://artifact-host-internal:8091/internal/v1/artifacts"
    )
    assert got.returncode == 0 and got.stdout.strip() in {"401", "404"}, got.stdout + got.stderr
    assert stack.probe("cp:control-plane-web", "resolve", "artifact-host-public").returncode == 1
    assert stack.probe("cp:control-plane-web", "resolve", "artifact-host-db").returncode == 1


@pytest.mark.parametrize(
    "service", ["artifact-host-public", "artifact-host-internal", "artifact-host-db"]
)
def test_no_host_container_has_any_route_out(stack: _Stack, service: str) -> None:
    if (
        service == "artifact-host-db"
    ):  # postgres has no python: use the stand-in probe through its shell instead
        out = stack.host(
            "exec", "-T", service, "sh", "-c", "timeout 3 nc -z 1.1.1.1 443; echo $?", check=False
        )
        assert out.stdout.strip().splitlines()[-1] != "0"
        return
    assert stack.probe(f"host:{service}", "connect", "1.1.1.1:443").returncode == 1
    assert stack.probe(f"host:{service}", "resolve", "example.com").returncode == 1


def test_the_roots_are_read_only_and_the_blobs_follow_the_roles(stack: _Stack) -> None:
    write = (
        "import sys\ntry:\n    open(sys.argv[1], 'w').close()\nexcept OSError:\n    sys.exit(1)\n"
    )
    pub = stack.host(
        "exec", "-T", "artifact-host-public", "python", "-c", write, "/etc/probe", check=False
    )
    assert pub.returncode == 1
    ok = stack.host(
        "exec",
        "-T",
        "artifact-host-public",
        "python",
        "-c",
        write,
        "/var/lib/artifact-host/blobs/.probe",
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    ro = stack.host(
        "exec",
        "-T",
        "artifact-host-internal",
        "python",
        "-c",
        write,
        "/var/lib/artifact-host/blobs/.probe2",
        check=False,
    )
    assert ro.returncode == 1
    sync = stack.host(
        "exec", "-T", "artifact-host-public", "python", "-c", write, "/sync/probe", check=False
    )
    assert sync.returncode == 1, "the synced directory is mounted read-only"
