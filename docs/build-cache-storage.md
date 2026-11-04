# Build cache central storage (spec 04)

> **Security review update (spec 05 review H1-H3).** The shared S3 level is currently
> NOT delivered to builds. Credentials in the cargo environment are readable by every
> build script, proc macro and test binary, and an sccache server fixes config, RW mode
> and prefix at its first start, so per-build env cannot enforce policy. A fully valid
> shared request therefore runs with the local cache only and reports
> `shared_unavailable:shared_requires_server`; no `AWS_*`, `SCCACHE_CONF` or
> `SCCACHE_S3_RW_MODE` reaches cargo, and `shared.active` is never true. Each policy
> (mode, writes, namespace, endpoint, toolchain, cache dir/size) gets its own
> `SCCACHE_SERVER_PORT` (20000-59999) so builds under different policies never share a
> server. Still to build: an explicit `sccache --start-server` with credentials in the
> server's env only (file path to server child env), plus server lifetime management
> (today a server lives until sccache's own idle timeout). The sections below describe
> the intended config and remain accurate for the rendering functions. The
> `fabric_build_cache` section is read only from machine-local config
> (`~/.config/herdr-engineering/config.yaml`, `$HERDR_ENGINEERING_CONFIG`), never from a
> `herdr-engineering.yaml` in the build's working directory.

Status: code and config rendering done and unit/e2e-tested with fakes. Nothing here has run against a real sccache or a real S3 endpoint (neither exists on this host).

## Upstream facts (fetched 2026-11-04; latest stable sccache v0.19.0)

Sources: https://github.com/mozilla/sccache/blob/main/docs/Configuration.md, .../docs/S3.md, .../docs/MultiLevel.md, https://github.com/mozilla/sccache/releases (docs are `main`, not pinned to 0.19.0; UNVERIFIED that they match 0.19.0 exactly).

| Topic | Fact | Status |
|---|---|---|
| Multilevel | `[cache.multilevel] chain = ["disk","s3"]`, `write_error_policy = ignore|l0|all` (default `l0`); env `SCCACHE_MULTILEVEL_CHAIN`, `SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY`. First hit wins; slower hits are backfilled; writes go to all read-write levels; read-only levels are skipped on writes and never cause failures. | verified (docs) |
| Introduced in | v0.15.0 ("multi-tier caching", PR #2581) per the releases page. The docs do not name the config spelling for 0.15.0. 0.19.0 notes carry the fixes we rely on (optional unavailable level does not block start #2890, backfill #2913, per-level stats #2915, remote errors surface as cache errors #2928, effective access mode in `--show-stats` #2914). We therefore gate on **>= 0.19.0** (`MIN_SCCACHE_VERSION`). | partly UNVERIFIED (0.15-0.18 spelling) |
| Disk level | `[cache.disk] dir, size` (bytes). | verified |
| S3 level keys | `[cache.s3]`: `bucket`, `endpoint` (host[:port], no scheme in the docs' examples), `use_ssl`, `key_prefix`, `server_side_encryption*`. Env: `SCCACHE_BUCKET`, `SCCACHE_ENDPOINT`, `SCCACHE_REGION` (`auto` allowed with a custom endpoint), `SCCACHE_S3_USE_SSL`, `SCCACHE_S3_KEY_PREFIX`, `SCCACHE_S3_ENABLE_VIRTUAL_HOST_STYLE`. | verified (docs) |
| `region` as a TOML key | Not in Configuration.md's example. We emit `region` in `[cache.s3]`. | UNVERIFIED (fall back: put `SCCACHE_REGION` in the env) |
| `key_prefix` trailing slash / normalisation | Not documented. We emit `ns/os-arch/digest16/`. | UNVERIFIED |
| Path-style addressing (MinIO, RustFS, Versity) | Docs only mention virtual-host style (`SCCACHE_S3_ENABLE_VIRTUAL_HOST_STYLE`); path-style is not discussed. | UNVERIFIED |
| `no_credentials` | `SCCACHE_S3_NO_CREDENTIALS=true` is public/anonymous read-only. Not used by us. | verified (docs) |
| Credentials | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (env), `~/.aws/credentials` + `AWS_PROFILE`, IMDSv2, AssumeRole (`AWS_ROLE_ARN`), web identity (`AWS_WEB_IDENTITY_TOKEN_FILE`). We use only the first, in the child environment. `AWS_SESSION_TOKEN` is the standard AWS variable for short-lived creds (not shown on S3.md). | verified except session token (UNVERIFIED for sccache) |
| Read-only | Disk: `SCCACHE_LOCAL_RW_MODE=READ_ONLY`. S3: `SCCACHE_S3_RW_MODE=READ_ONLY` (S3.md). A TOML `rw_mode` key for S3 is not documented. v0.16.0 made any backend usable read-only (#2705). | env var verified in docs; **enforcement against a live endpoint UNVERIFIED**; TOML key UNVERIFIED so not emitted |
| Backend down at runtime | `SCCACHE_CACHE_READ_TIMEOUT_SECS` (default 60): a slow lookup is abandoned and the compile runs locally. Write failures follow `write_error_policy`. Docs do not say how a *refused/erroring* read is handled beyond that (0.19.0 notes: remote errors now surface as cache errors rather than silent misses; whether the compile then continues is UNVERIFIED). | partly UNVERIFIED |
| Bad S3 config at start | MultiLevel.md: with `ignore` a failing level is warned about and startup continues; `l0` requires the first level only; `all` requires every level. A missing azure auth source errors at startup (azure only). Whether a malformed `[cache.s3]` section aborts the server regardless of policy is UNVERIFIED. | partly UNVERIFIED |
| Corrupt entries | Not documented. Do NOT claim sccache detects/quarantines corrupt entries. Herdr's contract (quarantine on checksum mismatch) is NOT implemented by this spec. | UNVERIFIED / not implemented |

## What we generate

`<state_dir>/sccache/sccache.toml`, mode 0600, directory 0700, written atomically (tempfile + `os.replace`) by `materialize()`:

```toml
[cache.multilevel]
chain = ["disk", "s3"]
write_error_policy = "ignore"      # a failing cache write never fails a compile

[cache.disk]
dir = "<state_dir>/sccache"
size = <local.capacity_gib in bytes>

[cache.s3]
bucket = "..."  endpoint = "host[:port]"  region = "..."
key_prefix = "<namespace>/<os>-<arch>/<toolchain digest[:16]>/"
use_ssl = true
```

No secrets are in the file. Child env additions: `SCCACHE_CONF`, and `SCCACHE_S3_RW_MODE=READ_ONLY` unless `shared_cache.writes_enabled` is true. Credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`) are added to the child env only, via `EnvPlan.secret_env` (hidden from `repr`), and are never in events, reasons, warnings, or the TOML.

`writes_enabled: false` therefore means "S3 level present but read-only through the documented env var". If the operator cannot confirm enforcement (see verification below), use read-only credentials at the bucket policy as the real guarantee.

## Partitioning

Prefix = `<shared_cache.namespace>/<os>-<arch>/<rustc fingerprint digest[:16]>/` (OS and arch taken from the rustc host triple; the digest covers rustc version, commit, LLVM and `--target`). Only `[a-z0-9._-/]`, max 160 chars, no hostnames or paths. sccache still hashes compiler, flags and inputs; we add policy partitioning only, never hand-written keys. Different trust domains use different `namespace` values.

## Config (YAML only)

```yaml
fabric_build_cache:
  enabled: true
  mode: shared_cache
  allowed_hosts: [bender]
  shared_cache:
    enabled: true
    endpoint_ref: build-cache
    namespace: trusted-dev
    writes_enabled: false                    # start read-only
    credentials_file: /home/USER/.config/herdr-engineering/build-cache-creds.yaml   # 0600
    registry_file: /home/USER/.config/herdr-engineering/service-registry.yaml        # 0600
```

`service-registry.yaml` (no secrets, but protected like credentials):

```yaml
endpoints:
  build-cache:
    host: s3.example.org:9000      # host[:port], never a URL
    region: us-east-1
    bucket: sccache
    use_ssl: true
```

`build-cache-creds.yaml`:

```yaml
access_key_id: ...
secret_access_key: ...
session_token: ...      # optional
```

The existing `DevServiceRegistry` (devfabric.py) is a lease/port registry for dev services and cannot carry bucket/region, so a minimal registry file was added instead.

## Fallback behaviour (implemented)

The shared level is used only when: cache enabled, `mode: shared_cache`, `shared_cache.enabled`, sccache version >= 0.19.0, registry entry resolves, TLS satisfied (`tls_required` rejects `use_ssl: false`), credentials file readable with mode 0600, and a pre-build TCP+TLS probe (2 s, no S3 call, no credentials) succeeded. Otherwise the build runs with the local cache and the event records `cache.shared = {active:false, reason:"shared_unavailable:<code>", namespace_digest:null}`. Codes: `mode_not_shared, not_enabled, registry_not_configured, registry_unreadable, registry_invalid, unknown_endpoint, tls_required, credentials_not_configured, credentials_unreadable, credentials_invalid, sccache_version_unknown, sccache_version_unsupported, endpoint_unreachable, config_write_failed, no_fingerprint`. A failed probe is remembered 30 s in `<state_dir>/sccache/health-down-*` so a dead endpoint costs one probe per 30 s. Storage problems never change the build's exit code.

## What the operator must provide (not available here)

1. An approved S3-compatible endpoint reachable from Bender over TLS with a certificate the host trusts, and a registry entry for it. No endpoint is deployed: the loopback MinIO in `deploy/control-plane/compose.yaml` is for artifacts and is not used.
2. A dedicated bucket (or prefix) and scoped credentials: read-only credentials for the first canary phase; read/write limited to the bucket for the second. Put them in the 0600 credentials file.
3. Approval to install sccache >= 0.19.0 (`cargo install sccache --locked --version 0.19.0`, or a distro package).

## Verification commands (once 1-3 exist)

```
sccache --version                                  # >= 0.19.0
# config: shared_cache.enabled, writes_enabled:false, mode shared_cache
herdr-eng build-coordinate --record -- cargo build  # in a throwaway copy
tail -1 ~/.local/share/herdr-engineering/state/build-gates/builds.jsonl | python3 -m json.tool   # cache.shared.active true
SCCACHE_CONF=<state>/build-gates/sccache/sccache.toml sccache --show-stats --stats-format json   # per-level stats, effective access mode (0.19.0)
# read-only enforcement: with writes_enabled:false confirm no new objects: 
aws s3 ls s3://BUCKET/<namespace>/ --recursive | wc -l   # unchanged after a cold build
# then set writes_enabled:true, cold build, cargo clean (copy only), warm build: hits from s3 level in --show-stats
# outage: block the endpoint (firewall) -> build exits with the same code, reason shared_unavailable:endpoint_unreachable
# corrupt entry: overwrite one object in the bucket with garbage, rebuild, observe sccache behaviour (UNVERIFIED upstream; record the result)
```

Exit criteria "canary Bender shared reads/writes" and "corrupt entry rejection" remain NOT DONE.
