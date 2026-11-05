# Build cache central storage (spec 04)

> **Shared tier = the herdr rustfs server (2026-11-04 operator decision).** The only
> shared backend is the rustfs (MinIO drop-in) container `herdr-cp-rustfs`, bucket
> `herdr-build-cache`. No AWS or other S3 service is used. The configuration keys keep
> their historical S3-flavoured names (`endpoint_ref`, `registry_file`,
> `credentials_file`, `SCCACHE_S3_RW_MODE`, `[cache.s3]` in the sccache TOML) because
> sccache speaks the S3 protocol and renaming would break existing configs; "S3" in
> a key name means "the S3 API of rustfs".
>
> **Delivery.** `build-coordinate` starts or reuses a per-policy sccache server on the
> policy port BEFORE cargo (`herdr_engineering/build_cache_server.py`). The credentials
> and the rustfs settings are in THAT server's environment only (`AWS_*`,
> `SCCACHE_CONF`, `SCCACHE_S3_RW_MODE`, `SCCACHE_IDLE_TIMEOUT`); cargo, rustc and build
> scripts get only the port. The server is started with `close_fds=True` (no slot or
> maintenance lock fd) and is reaped by `SCCACHE_IDLE_TIMEOUT` (600 s). A signed
> list request (SigV4) first proves the credentials work; a server already on the port
> is trusted only when `--show-stats` reports a multilevel chain whose last level is the
> expected bucket and key prefix, otherwise it is stopped and restarted. `cache.shared`
> is `active: true` only after that check. Any failure builds on the local cache on a
> distinct fallback port with `shared_unavailable:<code>`.
>
> **Plain HTTP is accepted only for trusted transports:** loopback (127.0.0.0/8, `::1`,
> `localhost`) and the tailnet (100.64.0.0/10, `*.ts.net`, single-label MagicDNS
> names). Any other host needs TLS (`tls_required: false` stays limited to endpoint refs
> ending `.local-test`). Names are not resolved.
>
> **Known limits (measured/by design).** sccache does not authenticate cached objects:
> anyone with write access to the namespace can poison it (a garbage object is treated
> as a miss and overwritten; a *valid* poisoned object is trusted). Code running as the
> same user can read the server's `/proc/<pid>/environ` and the 0600 credentials file,
> so a hostile build script is not stopped by the server-env design; mitigations are the
> bucket-limited dedicated user, the bucket quota, and the read-only user for untrusted
> canaries. The `fabric_build_cache` section is read only from machine-local config
> (`~/.config/herdr-engineering/config.yaml`, `$HERDR_ENGINEERING_CONFIG`), never from a
> `herdr-engineering.yaml` in the build's working directory.

Status: measured against a REAL sccache 0.19.0 and the REAL local rustfs on 2026-11-04
(see "Measured on rustfs" below). Not measured: a second physical host, the tailnet
endpoint (see "Tailnet endpoint gap").

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

The shared level is used only when: cache enabled, `mode: shared_cache`, `shared_cache.enabled`, sccache version >= 0.19.0, registry entry resolves, TLS satisfied (`tls_required` rejects `use_ssl: false` except for loopback/tailnet hosts), credentials file readable with mode 0600, and a pre-build TCP+TLS probe (2 s, no S3 call, no credentials) succeeded. Otherwise the build runs with the local cache and the event records `cache.shared = {active:false, reason:"shared_unavailable:<code>", namespace_digest:null}`. Codes: `mode_not_shared, not_enabled, registry_not_configured, registry_unreadable, registry_invalid, unknown_endpoint, tls_required, credentials_not_configured, credentials_unreadable, credentials_invalid, sccache_version_unknown, sccache_version_unsupported, endpoint_unreachable, config_write_failed, no_fingerprint`. A failed probe is remembered 30 s in `<state_dir>/sccache/health-down-*` so a dead endpoint costs one probe per 30 s. Storage problems never change the build's exit code.

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

## Provisioning (operator, once per rustfs)

`herdr-eng build-cache-rustfs-provision` (dry-run by default; `--apply` to act) creates,
additively and idempotently: bucket `herdr-build-cache` with a hard quota (default 50 GiB),
user+policy `herdr-build-cache-rw` (bucket-limited read/write/list/delete) and
`herdr-build-cache-ro` (read/list only), and 0600 files in `~/.config/herdr-engineering/`:
`build-cache-rustfs.cred`, `build-cache-rustfs-ro.cred`, `build-cache-rustfs-registry.yaml`.
The admin credential comes from the central fleet config (`control_plane` section) and is
never printed or put in argv; the MinIO client runs in the container with a private,
removed config dir. It never touches `herdr-artifacts`, and has no delete path. Use the
RW credentials file with `writes_enabled: true`; use the RO file with `writes_enabled:
false` (belt and braces: the server also gets `SCCACHE_S3_RW_MODE=READ_ONLY`).

## Measured on rustfs (sccache 0.19.0, 2026-11-04, same host, loopback endpoint)

`tests/test_build_cache_rustfs_live.py` builds a tiny library crate (`CARGO_INCREMENTAL=0`;
sccache does not cache bin crates) through `build-coordinate --record` with a temp config:

| Step | Result |
|---|---|
| (a) cold build, state dir A | `cache.shared = {active:true, reason:"active"}`; 1 object under `<namespace>/linux-x86_64/<digest>/`; L1(s3) misses 1, writes 1 |
| (b) `cargo clean` + brand-new state/disk dir B | L1(s3) hits 1, L0(disk) backfills_to 1; `cache_hits.counts.Rust` 1 |
| (c) other namespace | L1 misses 1, writes 1: no hit (prefix isolation) |
| (d) read-only user | `put_object` -> AccessDenied; a build with the RO file and `writes_enabled:false` is `active:true`, exit 0, writes no object |

Other facts measured against the real binary: `SCCACHE_S3_RW_MODE=READ_ONLY` really stops
writes (but `effective_cache_mode` in `--show-stats` still says ReadWrite, so it is not a
verification signal); a corrupt object is a miss (build exits 0, sccache overwrites it);
the TOML needs `no_credentials = false` under `[cache.s3]` or sccache 0.19.0 rejects the
file; `region`, `key_prefix` (trailing slash, no doubling), `use_ssl=false` and path-style
addressing all work against rustfs; sccache starts happily with rejected credentials and
only fails writes quietly (hence the signed pre-check); L1 `writes` counters lag by 1-2 s;
a bare client-spawned local-only server on the policy port is detected and replaced;
`SCCACHE_IDLE_TIMEOUT` reaps the server (tested at 6 s). The server also writes a
`.sccache_check` object under its prefix at start. The build event's `sccache_stats` now
includes per-level deltas (`multi_level.L1 (s3).hits`, ...) and comes from the policy
server's port, not the default one.

NOT measured: a real second host (the (b) run is a same-host simulation with two state
dirs), a different toolchain fingerprint (isolation by namespace was measured; by
toolchain digest only unit-tested), the tailnet endpoint, concurrent builders.

## Tailnet endpoint (fixed in code; active after the agent restarts)

`100.104.39.6:9000` is held by the fabric agent (`herdr-eng devfabric agent`), whose
HTTP-aware forwarder rewrote `Host:` to `127.0.0.1:<port>` for requests addressed to a
fabric-owned name. AWS SigV4 signs the `Host` header, so every signed S3 request through the
tailnet address got `SignatureDoesNotMatch` (`credentials_rejected` in the build event) while
loopback worked. `AgentForwarder._rewrite_head` now forwards any SigV4-signed request
(`Authorization: AWS4-HMAC-SHA256 ...`, or `X-Amz-Algorithm`/`X-Amz-Signature` in the query)
byte-for-byte; unsigned requests keep the Host rewrite. The running agent keeps the old code
until the operator restarts it: `systemctl --user restart herdr-eng-fabric-agent.service`.
Verified with a throwaway forwarder on the tailnet IP: `aws s3 ls`, put/get/delete and a full
sccache cold build + warm hit all succeed through it.
