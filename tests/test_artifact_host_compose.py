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
RELAY_CONF = ROOT / "deploy" / "artifact-host" / "relay" / "nginx.conf"
# the image the collab edge runs in production (digest included), so nginx -t checks the conf with the binary it will meet
_PRODUCTION_NGINX = yaml.safe_load((CP / "compose.collab.yaml").read_text(encoding="utf-8"))["services"][
    "collab-edge"
]["image"]
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


def test_the_host_project_has_exactly_these_services_volumes_and_networks(host: dict) -> None:
    assert set(host["services"]) == {
        "artifact-host-db",
        "artifact-host-public",
        "artifact-host-internal",
        "artifact-host-migrate",
        "artifact-host-relay",
    }
    assert set(host["volumes"]) == {"artifact-host-blobs", "artifact-host-db"}
    assert set(host["networks"]) == {
        "artifact-host",
        "artifact-db-internal",
        "artifact-edge",
        "artifact-internal",
        "artifact-relay",
    }


def test_no_host_container_publishes_a_port_or_pins_a_name(host: dict) -> None:
    for name, svc in host["services"].items():
        assert "ports" not in svc, name
        assert "container_name" not in svc, (
            f"{name}: a fixed name would collide with the live stack"
        )
        assert "network_mode" not in svc, name


def test_every_host_network_is_internal_and_gives_the_docker_host_no_address_on_it(host: dict) -> None:
    """`internal: true` removes the gateway route, but the daemon host still gets an address on the bridge and (by default)
    answers on every host port there (sshd at .1:22 was reached from the roles).  `gateway_mode_ipv4: isolated` removes it."""
    for name, net in host["networks"].items():
        assert net.get("internal") is True, name
        assert net["driver_opts"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated", name


def _nets(svc: dict) -> set[str]:
    return set(svc["networks"])


def test_public_and_internal_roles_share_no_network(host: dict) -> None:
    public = _nets(host["services"]["artifact-host-public"])
    internal = _nets(host["services"]["artifact-host-internal"])
    assert public == {"artifact-host", "artifact-edge"}
    assert internal == {"artifact-db-internal", "artifact-internal"}
    assert not public & internal
    assert _nets(host["services"]["artifact-host-db"]) == {"artifact-host", "artifact-db-internal"}
    assert _nets(host["services"]["artifact-host-migrate"]) == {"artifact-host"}
    assert _nets(host["services"]["artifact-host-relay"]) == {"artifact-internal", "artifact-relay"}


def test_the_internal_role_binds_the_address_the_compose_file_gives_it(host: dict) -> None:
    """The internal listener binds its artifact-internal address (config `bind`), not 0.0.0.0: the fleet slice and the
    compose file name the same fixed address."""
    net = host["services"]["artifact-host-internal"]["networks"]["artifact-internal"]
    assert net["ipv4_address"] == "${HERDR_ARTIFACT_INTERNAL_IP:-" + fcc.ARTIFACT_HOST_INTERNAL_BIND + "}"
    subnet = host["networks"]["artifact-internal"]["ipam"]["config"][0]["subnet"]
    assert re.search(r"\$\{HERDR_ARTIFACT_INTERNAL_SUBNET:-172\.31\.252\.16/28\}", subnet)
    import ipaddress

    assert ipaddress.ip_address(fcc.ARTIFACT_HOST_INTERNAL_BIND) in ipaddress.ip_network("172.31.252.16/28")


def test_the_dashboard_is_on_the_relay_network_only_never_on_artifact_internal(
    host: dict, overlay: dict
) -> None:
    on_internal = {n for n, s in host["services"].items() if "artifact-internal" in _nets(s)}
    assert on_internal == {"artifact-host-internal", "artifact-host-relay"}
    on_relay = {n for n, s in host["services"].items() if "artifact-relay" in _nets(s)}
    assert on_relay == {"artifact-host-relay"}
    assert [n for n, s in overlay["services"].items() if "artifact-relay" in s["networks"]] == [
        "control-plane-web"
    ]
    for svc in overlay["services"].values():
        assert "artifact-internal" not in svc["networks"]


def test_only_the_edge_joins_artifact_edge_and_no_cp_service_joins_the_other_host_networks(
    overlay: dict,
) -> None:
    assert [n for n, s in overlay["services"].items() if "artifact-edge" in s["networks"]] == [
        "collab-edge"
    ]
    assert set(overlay["services"]) == {"control-plane-web", "collab-edge"}
    for svc in overlay["services"].values():
        assert not {"artifact-host", "artifact-db-internal", "artifact-internal"} & set(svc["networks"])
    assert all(net.get("external") is True for net in overlay["networks"].values())
    assert set(overlay["networks"]) == {"artifact-relay", "artifact-edge"}


def test_the_overlay_keeps_the_networks_the_services_already_have(overlay: dict) -> None:
    """Naming `networks:` replaces the implicit default network; the real services' own networks must be listed again."""
    base = _yaml(CP / "compose.collab.yaml")["services"]["collab-edge"]["networks"]
    assert set(base) <= set(overlay["services"]["collab-edge"]["networks"])
    assert "default" in overlay["services"]["control-plane-web"]["networks"]


def test_network_names_line_up_between_the_two_files(host: dict, overlay: dict) -> None:
    for net in ("artifact-relay", "artifact-edge"):
        assert host["networks"][net]["name"] == overlay["networks"][net]["name"]


def test_the_edge_network_subnet_is_fixed_and_refused_on_the_operator_listener(host: dict) -> None:
    subnet = host["networks"]["artifact-edge"]["ipam"]["config"][0]["subnet"]
    default = re.search(r"\$\{HERDR_ARTIFACT_EDGE_SUBNET:-([0-9./]+)\}", subnet)
    assert default is not None
    nginx = (EDGE_DIR / "nginx.conf").read_text(encoding="utf-8")
    # directives only, inside the :8082 server: a comment that names the subnet must not satisfy this
    operator = _code(nginx[nginx.index("listen 8082;") :])
    denies = re.findall(r"^\s*deny\s+([0-9./]+)\s*;", operator, re.M)
    assert default.group(1) in denies, "nginx.conf must refuse the artifact-edge subnet on :8082"
    assert denies.count(default.group(1)) == 1 and len(set(denies)) == len(denies)
    # the subnet is NOT a deploy knob: deploy.sh refuses the override (see test_artifact_host_deploy.py), so the deny
    # above can never describe a different network than the one Docker creates


def test_roles_are_hardened_and_have_their_own_healthcheck(host: dict) -> None:
    for name in ("artifact-host-public", "artifact-host-internal", "artifact-host-migrate"):
        svc = host["services"][name]
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["user"] == "10001:10001", name
        assert svc["depends_on"]["artifact-host-db"]["condition"] == "service_healthy", name
    for name in ("artifact-host-public", "artifact-host-internal"):
        assert host["services"][name]["healthcheck"]["test"][:3] == ["CMD", "herdr-artifact-host", "healthcheck"], name
    roles = [
        host["services"][n]["healthcheck"]["test"][3]
        for n in ("artifact-host-public", "artifact-host-internal")
    ]
    assert roles == ["public", "internal"]
    assert host["services"]["artifact-host-db"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert "-U artifact_admin" in host["services"]["artifact-host-db"]["healthcheck"]["test"][1]
    assert host["services"]["artifact-host-migrate"]["profiles"] == ["migrate"]


def test_the_relay_is_a_pinned_hardened_one_way_nginx(host: dict) -> None:
    svc = host["services"]["artifact-host-relay"]
    assert re.fullmatch(r"nginx:\S+@sha256:[0-9a-f]{64}", svc["image"])
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"] and svc["user"] == "10001:10001"
    assert "ports" not in svc
    conf = (ROOT / "deploy" / "artifact-host" / "relay" / "nginx.conf").read_text(encoding="utf-8")
    code = _code(conf)
    assert "limit_except GET" in code and "deny all" in code
    assert "proxy_pass http://artifact_host_internal" in code
    assert "location /v1/" in code and "location /" in code
    assert "/internal" not in code, "the relay never forwards the health route"
    assert "Authorization" not in code.replace("proxy_set_header Authorization $http_authorization", "")
    assert any(v.endswith("/etc/nginx/nginx.conf:ro") for v in svc["volumes"])


def _relay_locations() -> dict[str, str]:
    code = _code(RELAY_CONF.read_text(encoding="utf-8"))
    begin = code.index("server {")
    depth, i = 0, begin
    while True:
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        i += 1
        if depth == 0 and i > begin + 8:
            break
    return _locations(code[begin:i])


def test_the_relay_allows_only_get_and_nothing_outside_v1() -> None:
    locs = _relay_locations()
    v1 = _select(locs, "/v1/content/a/1/index.html")
    assert v1 and "proxy_pass http://artifact_host_internal;" in v1
    m = re.search(r"limit_except ([^{]+)\{([^}]*)\}", v1)
    assert m, "the /v1/ location restricts its methods"
    assert m.group(1).split() == ["GET"], "GET only (nginx adds HEAD): no POST, PUT, DELETE or PATCH"
    assert re.fullmatch(r"\s*deny all;\s*", m.group(2)), "every other method is denied, none allowed"
    assert re.search(r"\bPOST\b", _code(RELAY_CONF.read_text(encoding="utf-8"))) is None
    for path in ("/", "/x", "/v1", "/v2/x", "/internal/healthz", "/healthz", "//v1/x"):
        body = _select(locs, path)
        assert body is not None and body.strip() == "return 404;", (path, body)
    assert set(re.findall(r"proxy_pass (\S+);", _code(RELAY_CONF.read_text(encoding="utf-8")))) == {
        "http://artifact_host_internal"
    }
    assert (_select(locs, "/relay-healthz") or "").strip().startswith("return 200")


def test_the_relay_streams_instead_of_spooling_to_its_small_temp_space() -> None:
    code = _code(RELAY_CONF.read_text(encoding="utf-8"))
    assert re.search(r"proxy_buffering off;", code)
    assert not re.search(r"proxy_buffering on;", code)


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


def test_each_role_mounts_only_its_own_slice_at_the_same_path(host: dict) -> None:
    assert set(host["secrets"]) == {
        "artifact-host-public-config",
        "artifact-host-internal-config",
        "artifact-host-migrate-config",
        "artifact-host-db-admin-password",
    }
    mounts = {
        "artifact-host-public": "artifact-host-public-config",
        "artifact-host-internal": "artifact-host-internal-config",
        "artifact-host-migrate": "artifact-host-migrate-config",
    }
    for service, source in mounts.items():
        assert host["services"][service]["secrets"] == [{"source": source, "target": "artifact-host-config"}], service
    assert host["services"]["artifact-host-db"]["secrets"] == ["artifact-host-db-admin-password"]
    for name, secret in host["secrets"].items():
        assert secret["file"].startswith("${HERDR_ARTIFACT_HOST_DIR:?"), name
    # Compose ignores uid/gid/mode on file secrets (it says so at `up`); the file must not pretend otherwise.
    assert "uid" not in json.dumps(host["secrets"]) and "uid" not in json.dumps(host["x-role"])
    env = host["services"]["artifact-host-db"]["environment"]
    assert env["POSTGRES_USER"] == "artifact_admin"
    assert (
        "POSTGRES_PASSWORD" not in env
        and env["POSTGRES_PASSWORD_FILE"] == "/run/secrets/artifact-host-db-admin-password"
    )
    for name in ("artifact-host-public", "artifact-host-internal", "artifact-host-migrate"):
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
    library_url: str | None = fcc.ARTIFACT_HOST_RELAY_URL,
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
        cpv["artifact_library"] = {"host_key": library_key}
        if library_url is not None:
            cpv["artifact_library"]["host_url"] = library_url
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


def _locations(block: str, indent: str = "    ") -> dict[str, str]:
    """location header -> its body, for the locations at one nesting depth of a block."""
    out: dict[str, str] = {}
    for m in re.finditer(rf"^{indent}location (.+?) \{{", block, re.M):
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
    upstream_for = {
        "/s/": "share",
        "/api/member/": "member",
        "= /": "member",
        "/my/": "member",
    }
    for needed, upstream in upstream_for.items():
        assert needed in locs, needed
        assert f"proxy_pass http://artifact_host_{upstream};" in locs[needed], needed
    assert "server artifact-host-public:8090 " in rendered_edge
    for text in (_code(rendered_edge),):
        assert "8091" not in text, "nothing at the edge may name the internal listener"
        assert "artifact-host-internal" not in text and "artifact-internal" not in text
        assert "artifact-host-db" not in text and "herdr-cp-" not in text
    # every proxy_pass in the host's server goes to the one upstream
    assert set(re.findall(r"proxy_pass (\S+);", block)) == {
        "http://artifact_host_share",
        "http://artifact_host_member",
        "http://artifact_host_upload",
    }


def _select(locs: dict[str, str], path: str) -> str | None:
    """The body of the location nginx would choose for ``path``: exact match; else the longest matching prefix (a ``^~``
    prefix that is the longest wins outright); else the first matching regex in file order; else that longest prefix.
    Locations nested in the chosen one are selected the same way, on the same path."""
    chosen = _select_one(locs, path)
    if chosen is None:
        return None
    nested = _locations(chosen, indent="        ")
    return (_select(nested, path) if nested else None) or chosen


def _select_one(locs: dict[str, str], path: str) -> str | None:
    for header, body in locs.items():
        if header.startswith("= ") and header[2:].strip() == path:
            return body
    best: tuple[int, str, str] | None = None
    for header, body in locs.items():
        if header.startswith(("= ", "~")):
            continue
        prefix = header[3:] if header.startswith("^~ ") else header
        if path.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), header, body)
    if best is not None and best[1].startswith("^~ "):
        return best[2]
    for header, body in locs.items():
        flags = 0
        if header.startswith("~* "):
            pattern, flags = header[3:], re.I
        elif header.startswith("~ "):
            pattern = header[2:]
        else:
            continue
        if re.search(pattern.strip('"').replace("\\z", "\\Z"), path, flags):
            return body
    return best[2] if best else None


def test_the_location_matcher_follows_nginx_rules() -> None:
    """The routing assertions below are only as good as this matcher: pin its behaviour on a made-up server."""
    locs = {
        "/a/": "plain",
        "^~ /b/": "carat",
        "~ ^/b/x$": "regex",
        "= /c": "exact",
        "/d/": "outer\n        location ~ ^/d/n$ {\n nested\n        }\n",
    }
    assert _select(locs, "/a/q") == "plain"
    assert _select(locs, "/b/x") == "carat"  # ^~ stops the regex search
    assert _select(locs, "/c") == "exact"
    assert _select({"/b/": "prefix", "~ ^/b/x$": "regex"}, "/b/x") == "regex"  # no ^~: the regex wins
    assert _select(locs, "/zzz") is None
    assert (_select(locs, "/d/n") or "").strip() == "nested"  # a location nested in the chosen one is followed
    assert _select(locs, "/d/other").startswith("outer")


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


def test_each_route_family_has_its_own_upstream_budget_below_the_roles_connection_cap(
    rendered_edge: str,
) -> None:
    """One shared upstream let a stalled /s/ download or upload take every connection the role accepts (128), starving the
    member page.  Each family has its own max_conns and idle pool; together they stay under the cap."""
    ups = {
        m.group(1): int(m.group(2))
        for m in re.finditer(
            r"upstream (artifact_host_\w+)\s*\{[^}]*?max_conns=(\d+)", _code(rendered_edge)
        )
    }
    assert set(ups) == {
        "artifact_host_share",
        "artifact_host_member",
        "artifact_host_upload",
        "artifact_host_mcp",
    }
    assert sum(ups.values()) < 128, ups
    assert "upstream artifact_host " not in _code(rendered_edge), "no shared upstream remains"
    locs = _locations(_server_block(rendered_edge, HOST))
    nested = _locations(locs["/api/member/"], indent="      ")
    upload = next(b for h, b in nested.items() if "runs" in h)
    assert "proxy_pass http://artifact_host_upload;" in upload
    # the page's budget is not the downloads' or the uploads'
    assert ups["artifact_host_member"] >= 32
    # every pool keeps few idle connections (they count against the role's cap while idle)
    assert all(int(k) <= 8 for k in re.findall(r"keepalive (\d+);", _code(rendered_edge).split("upstream artifact_host_share")[1].split("server {")[0]))


def test_the_artifact_server_is_never_the_default_and_unknown_names_are_dropped(
    rendered_edge: str,
) -> None:
    """Connecting by IP (no SNI, or another name) must not land on the artifact host's server: it is not a
    default_server, it needs its own name, and the one default on the port rejects the handshake."""
    code = _code(rendered_edge)
    block = _server_block(rendered_edge, HOST)
    assert "default_server" not in _code(block)
    assert f"server_name {HOST};" in _code(block)
    defaults = re.findall(r"listen 8443[^;]*default_server[^;]*;", code)
    assert len(defaults) == 1
    first = code[code.rindex("server {", 0, code.index(defaults[0])) :]
    assert "ssl_reject_handshake on;" in first.split("server {")[1]
    assert "if ($hcn_from_edge = 0) { return 444; }" in _code(block)


def test_mcp_artifacts_strips_client_identity_headers_and_keeps_the_servers_set(
    rendered_edge: str,
) -> None:
    mcp = _locations(_server_block(rendered_edge, "mcp.local.metabolomics.us"))["= /mcp/artifacts"]
    for header in ("Forwarded", "X-Real-IP", "X-Forwarded-Host"):
        assert re.search(rf'proxy_set_header {header} "";', mcp), header
    # a location-level proxy_set_header replaces the whole inherited set: what the server sets must be repeated
    server = _server_block(rendered_edge, "mcp.local.metabolomics.us")
    inherited = set(re.findall(r"^    proxy_set_header (\S+) ", server, re.M))
    here = set(re.findall(r"proxy_set_header (\S+) ", mcp))
    assert inherited <= here, inherited - here
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in mcp


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
    assert "proxy_pass http://artifact_host_mcp;" in body
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


def _render(tmp_path: Path, **over: object) -> tuple[Path, dict[str, dict]]:
    central = _central(tmp_path, host_section=_host_section(**over))
    out = tmp_path / "ah"
    fcc.render_artifact_host(load_fleet_config(central), out)
    return out, {r: json.loads((out / f"{r}.json").read_text()) for r in ("public", "internal", "migrate")}


def test_every_role_slice_is_accepted_by_the_hosts_own_parser_and_the_constants_agree(
    tmp_path: Path,
) -> None:
    out, data = _render(tmp_path, health_port=9300)
    assert oct(out.stat().st_mode & 0o777) == "0o700"
    assert sorted(p.name for p in out.iterdir()) == sorted(
        ["public.json", "internal.json", "migrate.json", "db-admin-password", "roles.sql"]
    )
    for p in out.iterdir():
        assert oct(p.stat().st_mode & 0o777) == "0o600", p.name
    cfgs = {r: host_config.parse(d) for r, d in data.items()}
    for role, d in data.items():
        assert set(d) <= host_config.ROLE_KEYS[role] and {"version", "role", "database"} <= set(d)
    pub = cfgs["public"]
    assert pub.public_host == HOST and pub.mcp_public_hosts == (MCP_HOST,) and pub.health_port == 9300
    assert pub.group == "team.test" and pub.pool_id == "us-west-2_TESTPOOL" and pub.client_ids == ("client-a",)
    import hashlib

    assert cfgs["internal"].internal_key_digest == hashlib.sha256(b"k" * 40).hexdigest()
    assert cfgs["internal"].bind == fcc.ARTIFACT_HOST_INTERNAL_BIND
    assert fcc.ARTIFACT_HOST_SCHEMA_VERSION == host_config.SCHEMA_VERSION
    assert fcc.ARTIFACT_HOST_MIN_SECRET_BYTES == host_config.MIN_SECRET_BYTES
    assert fcc.ARTIFACT_HOST_ROLE_DB_USERS == host_config.ROLE_DB_USER
    assert "k" * 40 not in "".join(p.read_text() for p in out.iterdir()), "the dashboard's key never reaches the host"


def test_no_role_holds_another_roles_secrets_or_password(tmp_path: Path) -> None:
    section_args = {"session_secret": secrets.token_hex(32), "claim_key": secrets.token_hex(32), "db_password": secrets.token_hex(16)}
    out, data = _render(tmp_path, **section_args)
    internal = (out / "internal.json").read_text()
    migrate = (out / "migrate.json").read_text()
    for value in (section_args["session_secret"], section_args["claim_key"], section_args["db_password"]):
        assert value not in internal and value not in migrate
    assert section_args["db_password"] not in "".join(p.read_text() for p in out.iterdir())
    pws = [data[r]["database"]["password"] for r in data]
    admin = (out / "db-admin-password").read_text()
    assert len({*pws, admin}) == 4, "one distinct password per role plus the admin"
    assert data["internal"]["database"]["password"] not in (out / "public.json").read_text()
    assert admin not in "".join(p.read_text() for p in out.glob("*.json"))
    # rotating the one fleet value rotates all of them; the same value renders the same passwords
    out2, data2 = _render(tmp_path / "again", **section_args)
    assert data2 == data
    out3, data3 = _render(tmp_path / "rotated", **{**section_args, "db_password": secrets.token_hex(16)})
    assert all(data3[r]["database"]["password"] != data[r]["database"]["password"] for r in data)


def _grants_to(sql: str, role: str) -> list[tuple[list[str], str]]:
    """(privileges, statement) for every GRANT or ALTER DEFAULT PRIVILEGES ... GRANT statement whose grantee list names ``role``."""
    out: list[tuple[list[str], str]] = []
    for line in sql.splitlines():
        m = re.match(r"^(?:ALTER DEFAULT PRIVILEGES .*? )?GRANT (.+?) ON .+? TO (.+?);$", line.strip())
        if m and role in [g.strip() for g in m.group(2).split(",")]:
            out.append(([x.strip() for x in m.group(1).split(",")], line))
    return out


def test_roles_sql_makes_three_non_superuser_roles_with_least_privilege(tmp_path: Path) -> None:
    out, data = _render(tmp_path)
    sql = (out / "roles.sql").read_text()
    assert "SUPERUSER" not in sql.replace("NOSUPERUSER", "")
    for role, user in host_config.ROLE_DB_USER.items():
        assert f"ALTER ROLE {user} LOGIN NOSUPERUSER" in sql
        assert data[role]["database"]["password"] in sql
    assert "ALTER ROLE artifact_internal SET default_transaction_read_only = on" in sql
    internal_grants = _grants_to(sql, "artifact_internal")
    assert len(internal_grants) >= 3, internal_grants  # not vacuous: connect, schema usage, table reads, default reads
    for privileges, target in internal_grants:
        assert set(privileges) <= {"SELECT", "USAGE", "CONNECT"}, (privileges, target)
    public_grants = _grants_to(sql, "artifact_public")
    assert any("INSERT" in privileges for privileges, _ in public_grants), "the public role does have DML"
    assert "REVOKE ALL ON DATABASE artifact_host FROM PUBLIC" in sql
    assert "ALTER DATABASE artifact_host OWNER TO artifact_owner" in sql


def test_disabled_renders_nothing_and_is_reported_as_disabled(tmp_path: Path) -> None:
    central = _central(tmp_path, host_section=None)
    out = tmp_path / "x"
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
        ({"db_user": "artifact_host"}, "unknown keys"),
        ({"session_secret": "ab" * 20}, "too repetitive"),
        ({"db_password": "a" * 20}, "too repetitive"),
        ({"public_host": "mcp.example.test", "mcp_public_hosts": []}, "must differ from the collaboration"),
    ],
)
def test_a_weak_or_malformed_slice_is_refused_naming_keys_only(
    tmp_path: Path, override: dict, message: str
) -> None:
    section = _host_section(**override)
    central = _central(tmp_path, host_section=section)
    with pytest.raises(FleetConfigError, match=message) as err:
        fcc.render_artifact_host(load_fleet_config(central), tmp_path / "x")
    for value in (section["session_secret"], section["db_password"]):
        if len(str(value)) >= 16:
            assert str(value) not in str(err.value)
    assert not (tmp_path / "x").exists()


def test_the_two_secrets_must_differ(tmp_path: Path) -> None:
    same = secrets.token_hex(32)
    central = _central(tmp_path, host_section=_host_section(session_secret=same, claim_key=same))
    with pytest.raises(FleetConfigError, match="must differ"):
        fcc.render_artifact_host(load_fleet_config(central), tmp_path / "x")


@pytest.mark.parametrize(
    "url",
    [
        "http://artifact-host-internal:8091",  # the internal role itself: the dashboard must not be able to reach it
        "https://artifact-host-relay:8091",
        "http://artifact-host-relay:8090",
        "http://artifact-host-relay:8091/v1/",
        "http://artifact-host-relay.evil.test:8091",
        "http://user@artifact-host-relay:8091",
        None,
    ],
)
def test_the_dashboard_library_must_point_at_the_relay(tmp_path: Path, url: str | None) -> None:
    central = _central(tmp_path, host_section=_host_section(), library_url=url)
    with pytest.raises(FleetConfigError, match="artifact_library.host_url"):
        fcc.render_artifact_host(load_fleet_config(central), tmp_path / "x")


@pytest.mark.parametrize("url", ["http://artifact-host-relay:8091", "http://artifact-host-relay:8091/"])
def test_the_relay_url_is_accepted(tmp_path: Path, url: str) -> None:
    central = _central(tmp_path, host_section=_host_section(), library_url=url)
    fcc.render_artifact_host(load_fleet_config(central), tmp_path / "x")


def test_the_dashboard_key_is_required_and_share_public_is_exclusive(tmp_path: Path) -> None:
    nokey = _central(tmp_path / "a", host_section=_host_section(), library_key=None)
    with pytest.raises(FleetConfigError, match="artifact_library.host_key"):
        fcc.render_artifact_host(load_fleet_config(nokey), tmp_path / "x")
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
        fcc.render_artifact_host(load_fleet_config(both), tmp_path / "x")


def test_fleet_validate_names_an_incomplete_host(tmp_path: Path) -> None:
    central = _central(tmp_path, host_section=_host_section(db_password="x"))
    with pytest.raises(FleetConfigError, match="db_password"):
        fcc.validate_config(central)


# ------------------------------------------------------------------------------------- real docker (bottom)

_STRICT = bool(os.environ.get("CI")) or os.environ.get("HERDR_REQUIRE_DOCKER") == "1"


def _docker(
    *args: str,
    check: bool = True,
    timeout: int = 600,
    env: dict | None = None,
    input: str | None = None,  # noqa: A002
) -> subprocess.CompletedProcess:
    run = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        input=input,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if check and run.returncode != 0:
        pytest.fail(f"docker {' '.join(args[:6])} failed ({run.returncode}): {run.stderr[-1500:]}")
    return run


@pytest.fixture(scope="module")
def docker_ok() -> None:
    reason = None
    if os.environ.get("HERDR_SKIP_DOCKER") == "1":  # parse-level iteration, and scripts/ci/unit-gates.sh (its agents have no docker)
        pytest.skip("HERDR_SKIP_DOCKER=1")
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
        _PRODUCTION_NGINX,
        "nginx",
        "-t",
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]


def test_nginx_accepts_the_relay_conf(docker_ok: None, host: dict) -> None:
    """The relay's own configuration, tested by the image digest compose runs it with."""
    image = host["services"]["artifact-host-relay"]["image"]
    run = _docker(
        "run",
        "--rm",
        "--add-host",
        "artifact-host-internal:127.0.0.1",
        "-v",
        f"{RELAY_CONF}:/etc/nginx/nginx.conf:ro",
        image,
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
    # listens, so "the internal role cannot reach the dashboard" is a refusal of a reachable-looking target, not a silent port
    entrypoint: [python, -m, http.server, "8080", --bind, "0.0.0.0"]
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
req = urllib.request.Request(target, data=b"x" if kind == "post" else None, method="POST" if kind == "post" else "GET")
if len(sys.argv) > 3:
    req.add_header("Authorization", sys.argv[3])
try:
    print(urllib.request.urlopen(req, timeout=5).status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except OSError:
    sys.exit(2)
"""

# Subnets for the throwaway stack: next to (never the same as) the live 172.31.252.0/28 and .16/28.
_TEST_EDGE_SUBNET = "172.31.253.0/28"
_TEST_INTERNAL_SUBNET = "172.31.253.16/28"
_TEST_INTERNAL_IP = "172.31.253.18"
_DASHBOARD_KEY = "k" * 40


class _Stack:
    """A throwaway host project plus stand-in control-plane services on the real overlay.

    The role slices, passwords and roles.sql are the REAL output of `render-artifact-host` (only the internal slice's bind
    is moved to the test subnet)."""

    def __init__(self, tmp: Path, image: str, tag: str) -> None:
        token = uuid.uuid4().hex[:8]
        self.project, self.cp_project = f"ahtest{token}", f"ahcp{token}"
        self.tmp = tmp
        sync = tmp / "sync"
        sync.mkdir()
        (sync / "jwks.json").write_text("{}")
        (sync / "members.json").write_text(json.dumps({"updated": 0, "groups": {}, "disabled": []}))
        self.section = _host_section()
        central = _central(tmp / "fleet", host_section=self.section, library_key=_DASHBOARD_KEY)
        self.dir = tmp / "ah"
        fcc.render_artifact_host(load_fleet_config(central), self.dir)
        internal = json.loads((self.dir / "internal.json").read_text())
        internal["bind"] = _TEST_INTERNAL_IP
        (self.dir / "internal.json").write_text(json.dumps(internal))
        self.dir.chmod(0o755)
        for f in self.dir.iterdir():  # Compose ignores secret uid/mode: the host file's own mode decides
            f.chmod(0o644)
        (tmp / "standin.yaml").write_text(_STAND_IN.format(image=image))
        self.env = {
            "HERDR_ARTIFACT_HOST_PROJECT": self.project,
            "HERDR_ARTIFACT_HOST_TAG": tag,
            "HERDR_ARTIFACT_NET_PREFIX": self.project,
            "HERDR_ARTIFACT_EDGE_SUBNET": _TEST_EDGE_SUBNET,
            "HERDR_ARTIFACT_INTERNAL_SUBNET": _TEST_INTERNAL_SUBNET,
            "HERDR_ARTIFACT_INTERNAL_IP": _TEST_INTERNAL_IP,
            "HERDR_ARTIFACT_HOST_DIR": str(self.dir),
            "HERDR_COLLAB_SYNC_DIR": str(sync),
        }

    def host(self, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:  # noqa: A002
        return _docker(
            "compose", "-p", self.project, "-f", str(HOST_COMPOSE), *args, check=check, env=self.env, input=input
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

    def probe(self, where: str, kind: str, target: str, *extra: str) -> subprocess.CompletedProcess:
        """Run the probe inside a container of the host project (`host:<service>`) or the stand-in project."""
        side, service = where.split(":")
        run = self.host if side == "host" else self.cp
        return run("exec", "-T", service, "python", "-c", _PROBE, kind, target, *extra, check=False)

    def container(self, side: str, service: str) -> str:
        run = self.host if side == "host" else self.cp
        return run("ps", "-q", service).stdout.strip()

    def ip(self, side: str, service: str, network: str) -> str:
        prefix = self.project
        name = f"{prefix}-{network}" if network in {"edge", "internal", "relay"} else f"{self.project}_{network}"
        fmt = '{{(index .NetworkSettings.Networks "' + name + '").IPAddress}}'
        return _docker("inspect", "-f", fmt, self.container(side, service)).stdout.strip()

    def psql(self, user: str, sql: str) -> subprocess.CompletedProcess:
        """SQL as `user` over the database container's local socket (the image trusts it; roles are separate by privilege)."""
        return self.host(
            "exec", "-T", "artifact-host-db", "psql", "-U", user, "-d", "artifact_host", "-X", "-A", "-t", "-c", sql,
            check=False,
        )

    def reapply_roles(self) -> None:
        """roles.sql again, as every later deploy does: this time the tables exist, so the `ON ALL TABLES` grants act."""
        again = self.host(
            "exec", "-T", "artifact-host-db", "psql", "-U", "artifact_admin", "-d", "artifact_host",
            "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-",
            input=(self.dir / "roles.sql").read_text(), check=False,
        )
        assert again.returncode == 0, again.stderr

    def psql_session(self, user: str, *statements: str) -> subprocess.CompletedProcess:
        """Several statements as `user` in ONE session (each its own transaction, unlike one `-c` with semicolons), stopping at
        the first error: a SET in an earlier statement is still in force for the later ones."""
        args: list[str] = []
        for sql in statements:
            args += ["-c", sql]
        return self.host(
            "exec", "-T", "artifact-host-db", "psql", "-U", user, "-d", "artifact_host", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1", *args, check=False,
        )

    def down(self) -> None:
        self.cp("down", "-v", "--remove-orphans", check=False)
        self.host("--profile", "migrate", "down", "-v", "--remove-orphans", check=False)


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
        # the deploy.sh order: database, roles, schema (as the owner), then everything else
        s.host("up", "-d", "--wait", "--wait-timeout", "120", "artifact-host-db")
        s.host(
            "exec", "-T", "artifact-host-db", "psql", "-U", "artifact_admin", "-d", "artifact_host",
            "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-",
            input=(s.dir / "roles.sql").read_text(),
        )
        s.host("run", "--rm", "artifact-host-migrate")
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
        "artifact-host-relay",
    }
    assert all(r["Health"] == "healthy" for r in rows), rows
    assert all(
        not r.get("Publishers") or all(p.get("PublishedPort", 0) == 0 for p in r["Publishers"])
        for r in rows
    )


# ------------------------------------------------------------------------------------------------ names and paths


@pytest.mark.parametrize(
    "name", ["control-plane-web", "herdr-cp-web", "postgres", "herdr-cp-postgres"]
)
def test_control_plane_names_do_not_resolve_from_the_public_role(stack: _Stack, name: str) -> None:
    assert stack.probe("host:artifact-host-public", "resolve", name).returncode == 1


@pytest.mark.parametrize(
    "name",
    ["postgres", "herdr-cp-postgres", "control-plane-web", "collab-edge", "artifact-host-public"],
)
def test_the_internal_role_sees_neither_the_cp_services_nor_the_public_role(
    stack: _Stack, name: str
) -> None:
    assert stack.probe("host:artifact-host-internal", "resolve", name).returncode == 1


def test_the_public_role_does_not_see_the_internal_role_the_relay_or_the_dashboard(
    stack: _Stack,
) -> None:
    for name in ("artifact-host-internal", "artifact-host-relay", "control-plane-web"):
        assert stack.probe("host:artifact-host-public", "resolve", name).returncode == 1, name


@pytest.mark.parametrize(
    "name",
    ["artifact-host-internal", "artifact-host-db", "artifact-host-public"],
)
def test_stand_in_cp_postgres_reaches_no_host_service(stack: _Stack, name: str) -> None:
    assert stack.probe("cp:postgres", "resolve", name).returncode == 1


def test_the_edge_reaches_the_public_role_and_only_it(stack: _Stack) -> None:
    assert stack.probe("cp:collab-edge", "resolve", "artifact-host-public").returncode == 0
    reach = stack.probe("cp:collab-edge", "http", "http://artifact-host-public:8090/s/nothing/")
    assert reach.returncode == 0 and reach.stdout.strip() == "404", reach.stdout + reach.stderr
    for name in ("artifact-host-internal", "artifact-host-db", "artifact-host-relay"):
        assert stack.probe("cp:collab-edge", "resolve", name).returncode == 1, name
    # /healthz is not on the public listener: the same uniform 404
    health = stack.probe("cp:collab-edge", "http", "http://artifact-host-public:8090/healthz")
    assert health.stdout.strip() == "404"


def test_the_dashboard_reaches_the_internal_role_only_through_the_one_way_relay(stack: _Stack) -> None:
    web = "cp:control-plane-web"
    relay = "http://artifact-host-relay:8091"
    assert stack.probe(web, "http", f"{relay}/relay-healthz").stdout.strip() == "200"
    # GET /v1/... reaches the internal role, which demands its key: without it the answer is its own 401
    got = stack.probe(web, "http", f"{relay}/v1/content/a/1/index.html")
    assert got.returncode == 0 and got.stdout.strip() == "401", got.stdout + got.stderr
    # the bearer key is passed through unread: the internal role accepts it and answers (404: there is no such artifact)
    keyed = stack.probe(web, "http", f"{relay}/v1/content/a/1/index.html", f"Bearer {_DASHBOARD_KEY}")
    assert keyed.stdout.strip() == "404", keyed.stdout + keyed.stderr
    # one way: no other method, no health route, nothing outside /v1/
    assert stack.probe(web, "post", f"{relay}/v1/content/a/1/index.html").stdout.strip() == "403"
    assert stack.probe(web, "http", f"{relay}/internal/healthz").stdout.strip() == "404"
    assert stack.probe(web, "http", f"{relay}/").stdout.strip() == "404"
    # the dashboard cannot name or reach the internal role, the public role or the database directly
    for name in ("artifact-host-internal", "artifact-host-public", "artifact-host-db"):
        assert stack.probe(web, "resolve", name).returncode == 1, name
    assert stack.probe(web, "connect", f"{_TEST_INTERNAL_IP}:8091").returncode == 1


def test_the_internal_role_cannot_reach_the_dashboard(stack: _Stack) -> None:
    web_ip = stack.ip("cp", "control-plane-web", "relay")
    assert web_ip
    # positive control: the relay shares artifact-relay with the dashboard and does reach the stand-in listener
    relay_web = stack.host(
        "exec", "-T", "artifact-host-relay", "wget", "-q", "-O", "/dev/null", "-T", "3", f"http://{web_ip}:8080/", check=False
    )
    assert relay_web.returncode == 0, relay_web.stderr
    for role in ("artifact-host-internal", "artifact-host-public", "artifact-host-db"):
        if role == "artifact-host-db":
            out = stack.host("exec", "-T", role, "nc", "-z", "-w", "3", web_ip, "8080", check=False)
            assert out.returncode != 0, role
        else:
            assert stack.probe(f"host:{role}", "connect", f"{web_ip}:8080").returncode == 1, role


def test_the_internal_listener_binds_only_its_artifact_internal_address(stack: _Stack) -> None:
    # the relay (same network) reaches it at that address (positive control) ...
    ok = stack.host(
        "exec", "-T", "artifact-host-relay", "wget", "-q", "-O", "/dev/null", "-T", "3",
        f"http://{_TEST_INTERNAL_IP}:8091/internal/healthz", check=False,
    )
    assert ok.returncode == 0, ok.stderr
    # ... but not on loopback, and not on its database-network address, which the database container can reach
    assert stack.probe("host:artifact-host-internal", "connect", "127.0.0.1:8091").returncode == 1
    db_side = stack.ip("host", "artifact-host-internal", "artifact-db-internal")
    assert db_side and db_side != _TEST_INTERNAL_IP
    out = stack.host("exec", "-T", "artifact-host-db", "nc", "-z", "-w", "3", db_side, "8091", check=False)
    assert out.returncode != 0


@pytest.mark.parametrize(
    "service", ["artifact-host-public", "artifact-host-internal", "artifact-host-relay", "artifact-host-db"]
)
def test_no_host_container_has_any_route_out(stack: _Stack, service: str) -> None:
    if service in {"artifact-host-db", "artifact-host-relay"}:  # no python in these images: busybox nc
        out = stack.host("exec", "-T", service, "nc", "-z", "-w", "3", "1.1.1.1", "443", check=False)
        assert out.returncode != 0
        return
    assert stack.probe(f"host:{service}", "connect", "1.1.1.1:443").returncode == 1
    assert stack.probe(f"host:{service}", "resolve", "example.com").returncode == 1


def _host_ipv4_addresses() -> list[str]:
    ip = shutil.which("ip")
    assert ip, "this test needs `ip` to read the Docker host's own addresses; install iproute2 (it is not skipped)"
    out = subprocess.run([ip, "-j", "-4", "addr"], capture_output=True, text=True, check=True).stdout
    return [a["local"] for iface in json.loads(out) for a in iface.get("addr_info", [])]


def _subnets_of(network: str) -> list[str]:
    cfgs = json.loads(_docker("network", "inspect", network).stdout)[0]["IPAM"]["Config"]
    return [c["Subnet"] for c in cfgs if "." in c["Subnet"]]


def _host_addresses_inside(subnets: list[str]) -> list[str]:
    import ipaddress

    nets = [ipaddress.ip_network(s) for s in subnets]
    return [a for a in _host_ipv4_addresses() if any(ipaddress.ip_address(a) in n for n in nets)]


def test_the_docker_host_has_no_address_on_any_host_network(stack: _Stack, host: dict) -> None:
    """`internal: true` alone leaves the daemon host with an address on the bridge, and its own listeners (sshd) answer
    there.  The earlier form of this test dialled the host from the roles, which a host firewall that drops bridge-to-host
    traffic turns into a pass whether or not the address exists.  This one reads the host's addresses: with
    `gateway_mode_ipv4: isolated` none of them lies in a host network's subnet, and the control (a plain internal network
    created here) shows that a leak WOULD be seen."""
    nets = _docker(
        "network", "ls", "--filter", f"label=com.docker.compose.project={stack.project}", "--format", "{{.Name}}"
    ).stdout.split()
    assert len(nets) == len(host["networks"]), (nets, sorted(host["networks"]))  # all of them, so nothing is skipped
    for net in nets:
        subnets = _subnets_of(net)
        assert subnets, net
        assert _host_addresses_inside(subnets) == [], f"the Docker host holds an address on {net} {subnets}"

    ctrl = f"{stack.project}-ctrl"
    _docker("network", "create", "--internal", "--subnet", "172.31.253.32/28", ctrl)
    try:
        assert _host_addresses_inside(_subnets_of(ctrl)) == ["172.31.253.33"], (
            "control: a plain internal network gives the host an address, so this check can see a leak"
        )
    finally:
        _docker("network", "rm", ctrl, check=False)


def test_the_relay_passes_a_blob_larger_than_its_temp_space_to_a_slow_reader(docker_ok: None, host: dict) -> None:
    """Content is streamed: a blob bigger than the relay's 16 MB cache tmpfs, read slowly, arrives whole.  The relay runs with
    compose's read-only root, user and tmpfs sizes against a real nginx serving the bytes under the internal role's name."""
    svc = host["services"]["artifact-host-relay"]
    size_mb = int(next(t for t in svc["tmpfs"] if t.startswith("/var/cache/nginx")).split("size=")[1].split("m")[0])
    total = 2 * size_mb * 1024 * 1024
    image = svc["image"]
    suffix = uuid.uuid4().hex[:8]
    net, up, relay = f"ahrelay{suffix}", f"ahrelay-up{suffix}", f"ahrelay-rl{suffix}"
    _docker("network", "create", net)
    try:
        upstream_conf = "server { listen 8091; root /srv; }"
        _docker(
            "run", "-d", "--name", up, "--network", net, "--network-alias", "artifact-host-internal", image, "sh", "-c",
            f"mkdir -p /srv/v1 && head -c {total} /dev/urandom > /srv/v1/big && "
            f"echo '{upstream_conf}' > /etc/nginx/conf.d/default.conf && exec nginx -g 'daemon off;'",
        )
        _docker(
            "run", "-d", "--name", relay, "--network", net, "--read-only", "--user", "10001:10001", "--cap-drop", "ALL",
            "--tmpfs", "/run:uid=10001,size=1m", "--tmpfs", f"/var/cache/nginx:uid=10001,size={size_mb}m",
            "-v", f"{RELAY_CONF}:/etc/nginx/nginx.conf:ro", image,
        )
        for _ in range(40):  # the upstream writes its payload before it starts
            probe = _docker("exec", relay, "wget", "-q", "-O", "/dev/null", "-T", "2", "http://127.0.0.1:8091/relay-healthz", check=False)
            seen = _docker("exec", up, "wget", "-q", "-O", "/dev/null", "-T", "2", "http://127.0.0.1:8091/v1/big", check=False)
            if probe.returncode == 0 and seen.returncode == 0:
                break
            import time

            time.sleep(0.5)
        else:
            raise AssertionError("the relay and its upstream did not come up")
        got = _docker(
            "exec", relay, "sh", "-c", "wget -q -O - http://127.0.0.1:8091/v1/big | (sleep 6; wc -c)", check=False
        )
        assert got.stdout.strip() == str(total), (got.stdout, got.stderr, _docker("logs", relay, check=False).stderr[-800:])
    finally:
        _docker("rm", "-f", relay, up, check=False)
        _docker("network", "rm", net, check=False)


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


# ---------------------------------------------------------------------------------------- secrets and database roles


def test_each_role_holds_only_its_own_secrets(stack: _Stack) -> None:
    def config(service: str) -> str:
        return stack.host("exec", "-T", service, "cat", "/run/secrets/artifact-host-config").stdout

    public, internal = config("artifact-host-public"), config("artifact-host-internal")
    sec = stack.section
    assert sec["session_secret"] in public and sec["claim_key"] in public
    assert sec["session_secret"] not in internal and sec["claim_key"] not in internal
    assert json.loads(internal)["role"] == "internal" and json.loads(public)["role"] == "public"
    assert json.loads(public)["database"]["password"] != json.loads(internal)["database"]["password"]
    assert sec["db_password"] not in public + internal
    # the migrate slice is not mounted in the long-running roles, and the admin password is mounted nowhere but the database
    for service in ("artifact-host-public", "artifact-host-internal"):
        listing = stack.host("exec", "-T", service, "ls", "/run/secrets", check=False).stdout.split()
        assert listing == ["artifact-host-config"], (service, listing)


def test_no_application_role_is_a_superuser_and_the_admin_is_the_only_one(stack: _Stack) -> None:
    rows = stack.psql(
        "artifact_admin", "SELECT rolname, rolsuper FROM pg_roles WHERE rolname LIKE 'artifact_%' ORDER BY 1"
    ).stdout.split()
    assert rows == [
        "artifact_admin|t",
        "artifact_internal|f",
        "artifact_owner|f",
        "artifact_public|f",
    ]


def test_the_public_role_cannot_run_programs_or_change_the_schema(stack: _Stack) -> None:
    program = stack.psql("artifact_public", "COPY (SELECT 1) TO PROGRAM 'id'")
    assert program.returncode != 0 and "permission denied" in program.stderr
    for sql in (
        "CREATE TABLE intruder (x int)",
        "DROP TABLE schema_migrations",
        "ALTER TABLE schema_migrations ADD COLUMN x int",
        "CREATE ROLE intruder",
        "CREATE EXTENSION IF NOT EXISTS file_fdw",
    ):
        out = stack.psql("artifact_public", sql)
        assert out.returncode != 0, sql
    # but it does have DML (the positive control)
    assert stack.psql("artifact_public", "DELETE FROM schema_migrations WHERE false").returncode == 0
    assert stack.psql("artifact_public", "SELECT count(*) FROM schema_migrations").returncode == 0


def test_the_internal_role_can_only_read(stack: _Stack) -> None:
    assert stack.psql("artifact_internal", "SELECT count(*) FROM schema_migrations").returncode == 0
    for sql in (
        "DELETE FROM schema_migrations WHERE false",
        "INSERT INTO schema_migrations SELECT * FROM schema_migrations WHERE false",
        "SET default_transaction_read_only = off; DELETE FROM schema_migrations WHERE false",
        "COPY (SELECT 1) TO PROGRAM 'id'",
        "CREATE TABLE intruder (x int)",
    ):
        assert stack.psql("artifact_internal", sql).returncode != 0, sql


_INTERNAL_WRITES = (
    "DELETE FROM schema_migrations WHERE false",
    "UPDATE schema_migrations SET name = name WHERE false",
    "INSERT INTO schema_migrations (name) SELECT name FROM schema_migrations WHERE false",
    "TRUNCATE schema_migrations",
)


@pytest.mark.parametrize("sql", _INTERNAL_WRITES)
def test_the_internal_role_is_refused_by_privilege_when_read_only_is_switched_off(stack: _Stack, sql: str) -> None:
    """default_transaction_read_only is a session setting the role may change: it must not be what keeps the role read-only."""
    stack.reapply_roles()
    out = stack.psql_session("artifact_internal", "SET default_transaction_read_only = off", sql)
    assert out.returncode != 0 and "permission denied" in out.stderr, (sql, out.stderr)
    explicit = stack.psql_session("artifact_internal", "BEGIN READ WRITE", sql, "ROLLBACK")
    assert explicit.returncode != 0 and "permission denied" in explicit.stderr, (sql, explicit.stderr)


def test_the_internal_role_holds_no_privilege_but_select_on_any_table_or_sequence(stack: _Stack) -> None:
    stack.reapply_roles()
    held = stack.psql(
        "artifact_admin",
        "SELECT c.relname || ':' || p FROM pg_class c, unnest(ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r','p') "
        "AND has_table_privilege('artifact_internal', c.oid, p)",
    )
    assert held.returncode == 0 and held.stdout.split() == [], held.stdout
    seqs = stack.psql(
        "artifact_admin",
        "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S' "
        "AND CASE WHEN c.relkind = 'S' THEN has_sequence_privilege('artifact_internal', c.oid, 'USAGE, UPDATE') ELSE false END",
    )
    assert seqs.returncode == 0 and seqs.stdout.split() == [], seqs.stdout
    reads = stack.psql(
        "artifact_admin", "SELECT has_table_privilege('artifact_internal', 'schema_migrations', 'SELECT')"
    )
    assert reads.stdout.strip() == "t", "the positive control: it can read"


def test_a_table_the_owner_creates_later_is_readable_but_not_writable_by_the_internal_role(stack: _Stack) -> None:
    """Default privileges: the migration creates tables after roles.sql ran, so the grant must follow them."""
    assert stack.psql("artifact_owner", "CREATE TABLE ah_probe_default (x int)").returncode == 0
    try:
        privs = stack.psql(
            "artifact_admin",
            "SELECT p, has_table_privilege('artifact_internal', 'ah_probe_default', p) "
            "FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p",
        ).stdout.split()
        assert privs == ["SELECT|t", "INSERT|f", "UPDATE|f", "DELETE|f", "TRUNCATE|f"], privs
        out = stack.psql_session(
            "artifact_internal", "SET default_transaction_read_only = off", "INSERT INTO ah_probe_default VALUES (1)"
        )
        assert out.returncode != 0 and "permission denied" in out.stderr, out.stderr
        public = stack.psql("artifact_public", "INSERT INTO ah_probe_default VALUES (1)")
        assert public.returncode == 0, "the public role's DML does follow new tables"
    finally:
        stack.psql("artifact_owner", "DROP TABLE IF EXISTS ah_probe_default")


def test_the_owner_role_runs_the_schema_but_is_not_a_superuser(stack: _Stack) -> None:
    assert stack.psql("artifact_owner", "COPY (SELECT 1) TO PROGRAM 'id'").returncode != 0
    assert stack.psql("artifact_owner", "SELECT count(*) FROM schema_migrations").returncode == 0
    tables = stack.psql("artifact_admin", "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
    assert tables.stdout.split() == ["artifact_owner"]


def test_roles_sql_is_idempotent_and_a_rotated_password_takes_effect(stack: _Stack) -> None:
    again = stack.host(
        "exec", "-T", "artifact-host-db", "psql", "-U", "artifact_admin", "-d", "artifact_host",
        "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-",
        input=(stack.dir / "roles.sql").read_text(),
        check=False,
    )
    assert again.returncode == 0, again.stderr
    # loopback is trusted by the official image; the container's own network address is password-checked
    db_ip = stack.host("exec", "-T", "artifact-host-db", "hostname", "-i").stdout.split()[0]
    rotated = "rot-" + secrets.token_hex(12)
    stack.psql("artifact_admin", f"ALTER ROLE artifact_public PASSWORD '{rotated}'")
    over_tcp = stack.host(
        "exec", "-T", "-e", f"PGPASSWORD={rotated}", "artifact-host-db",
        "psql", "-h", db_ip, "-U", "artifact_public", "-d", "artifact_host", "-X", "-A", "-t", "-c", "SELECT 1",
        check=False,
    )
    assert over_tcp.returncode == 0 and over_tcp.stdout.strip() == "1", over_tcp.stderr
    old = json.loads((stack.dir / "public.json").read_text())["database"]["password"]
    stale = stack.host(
        "exec", "-T", "-e", f"PGPASSWORD={old}", "artifact-host-db",
        "psql", "-h", db_ip, "-U", "artifact_public", "-d", "artifact_host", "-X", "-c", "SELECT 1", check=False,
    )
    assert stale.returncode != 0
    stack.host(  # put the rendered password back for the tests after this one
        "exec", "-T", "artifact-host-db", "psql", "-U", "artifact_admin", "-d", "artifact_host",
        "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", "-", input=(stack.dir / "roles.sql").read_text(),
    )


def test_member_api_allows_delete_only_on_the_routes_the_app_defines(rendered_edge: str) -> None:
    """The app defines DELETE /api/member/folders/<id> and /folders/<id>/items/<artifact>; the edge lets exactly those
    through (the body cap and limits stay on the location)."""
    code = _code(rendered_edge)
    m = re.search(r"map \"\$request_method:\$uri\" \$member_method_ok \{(.*?)\n  \}", code, re.S)
    assert m, "member_method_ok map missing"
    rules = re.findall(r'"~([^"]+)" 1;', m.group(1))
    assert "default 0;" in m.group(1)

    def allowed(method: str, uri: str) -> bool:
        return any(re.search(r.replace(r"\z", r"\Z"), f"{method}:{uri}") for r in rules)

    assert allowed("DELETE", "/api/member/folders/fld_a-B")
    assert allowed("DELETE", "/api/member/folders/fld_a/items/art_1")
    for bad in ("/api/member/folders", "/api/member/folders/", "/api/member/artifacts/art_1",
                "/api/member/folders/f/items", "/api/member/folders/f/items/a/b", "/api/member/folders/f\n",
                "/api/member/folders/f/extra"):
        assert not allowed("DELETE", bad), bad
    assert allowed("GET", "/api/member/artifacts") and allowed("PUT", "/api/member/folders/f")
    assert not allowed("PATCH", "/api/member/artifacts") and not allowed("OPTIONS", "/api/member/artifacts")
    block = _locations(_server_block(rendered_edge, HOST))["/api/member/"]
    assert "if ($member_method_ok = 0) { return 404; }" in _code(block)
    assert "client_max_body_size 1m;" in _code(block) and "limit_req zone=member_req" in block
