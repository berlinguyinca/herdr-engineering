"""Operator provisioning of the build cache's rustfs bucket (additive only).

The shared build-cache tier lives in ONE bucket (``herdr-build-cache``) of the
herdr rustfs server (the ``herdr-cp-rustfs`` container, a MinIO drop-in speaking
the S3 wire protocol). This module creates, idempotently:

* the bucket, with a hard quota,
* a dedicated read-write user whose policy is limited to that bucket,
* a dedicated read-only user (for canary hosts) limited to the same bucket,
* two 0600 credential files (YAML ``access_key_id`` / ``secret_access_key``,
  the layout :func:`build_cache_storage.read_credentials` reads) and, if absent,
  a non-secret 0600 registry file naming the endpoint.

Safety rules (tested):

* nothing existing is touched: the names are fixed constants, the bucket
  ``herdr-artifacts`` and every other user/policy are never named, listed for
  change, or deleted; there is no delete path at all;
* dry-run is the default; ``apply=True`` is explicit;
* the admin credential comes from the central fleet config (the same source
  ``deploy.sh`` renders its compose env from) into this process's memory and
  into the ``mc`` child's environment (``MC_HOST_<alias>``) only: never argv,
  never a file, never output;
* ``mc`` is the MinIO client. ``/usr/bin/mc`` on a workstation may be Midnight
  Commander, so the client is run inside the rustfs container (``docker exec``)
  unless a real MinIO ``mc`` is found on PATH.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .errors import InvalidError

BUCKET = "herdr-build-cache"
RW_USER = "herdr-build-cache-rw"
RO_USER = "herdr-build-cache-ro"
RW_POLICY = "herdr-build-cache-rw"
RO_POLICY = "herdr-build-cache-ro"
PROTECTED_BUCKETS = frozenset({"herdr-artifacts"})
DEFAULT_QUOTA_GIB = 50
DEFAULT_CONTAINER = "herdr-cp-rustfs"
DEFAULT_ENDPOINT = "http://127.0.0.1:9000"  # as seen from inside the container
ALIAS = "hbc"
CONFIG_DIR = Path.home() / ".config" / "herdr-engineering"
RW_CRED_NAME = "build-cache-rustfs.cred"
RO_CRED_NAME = "build-cache-rustfs-ro.cred"
REGISTRY_NAME = "build-cache-rustfs-registry.yaml"
CENTRAL_FLEET_CONFIG = CONFIG_DIR / "fleet" / "fleet-config.yaml"

_NAME_RE = re.compile(r"^herdr-build-cache(-[a-z0-9]+)*$")


class ProvisionError(Exception):
    """Typed failure. Messages never contain credentials."""


def policy_document(bucket: str, *, read_only: bool) -> dict:
    """IAM policy limited to ``bucket`` (read-only: list/get, never put/delete)."""
    _check_bucket(bucket)
    objects = f"arn:aws:s3:::{bucket}/*"
    bucket_arn = f"arn:aws:s3:::{bucket}"
    actions = ["s3:GetObject"] if read_only else [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
             "Resource": [bucket_arn]},
            {"Effect": "Allow", "Action": actions, "Resource": [objects]},
        ],
    }


def _check_bucket(bucket: str) -> None:
    if bucket in PROTECTED_BUCKETS:
        raise ProvisionError(f"refusing to touch protected bucket {bucket}")
    if not _NAME_RE.match(bucket):
        raise ProvisionError("bucket name must be herdr-build-cache or herdr-build-cache-<suffix>")


# --- admin credential --------------------------------------------------------

@dataclass(frozen=True)
class AdminCredential:
    user: str
    password: str

    def __repr__(self) -> str:
        return "AdminCredential(<redacted>)"

    __str__ = __repr__


def load_admin_credential(central: Path | None = None) -> AdminCredential:
    """Read the rustfs root credential from the central fleet config (0600 file).

    The same file ``deploy.sh`` renders HERDR_CP_MINIO_USER / _PASSWORD from.
    """
    from .fleet_config import CONTROL_PLANE_SECTION, FleetConfigError, load_fleet_config
    path = Path(central) if central else CENTRAL_FLEET_CONFIG
    try:
        values = load_fleet_config(path).section(CONTROL_PLANE_SECTION)
    except FleetConfigError as exc:
        raise ProvisionError(f"admin credential unavailable: {exc}") from None
    user, password = values.get("minio_user"), values.get("minio_password")
    if not isinstance(user, str) or not user or not isinstance(password, str) or not password:
        raise ProvisionError("central fleet config has no control_plane.minio_user/minio_password")
    return AdminCredential(user, password)


# --- mc runner ---------------------------------------------------------------

def detect_mc_command(container: str = DEFAULT_CONTAINER,
                      which: Callable[[str], str | None] = shutil.which) -> tuple[list[str], bool]:
    """(argv prefix running the MinIO client, runs-in-container). Host ``mc`` only if MinIO's."""
    host = which("mc")
    if host:
        try:
            out = subprocess.run([host, "--version"], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        if "minio" in out.lower() and "midnight" not in out.lower():
            return [host], False
    docker = which("docker")
    if not docker:
        raise ProvisionError("no MinIO client: /usr/bin/mc is not MinIO's and docker is absent")
    return [docker, "exec", "-i", "-e", f"MC_HOST_{ALIAS}", "-e", "MC_CONFIG_DIR",
            "-e", "MC_NO_COLOR", "-e", "MC_DISABLE_PAGER", container, "mc"], True


class Mc:
    """Run ``mc`` with the admin credential in the child's environment only."""

    def __init__(self, cred: AdminCredential, *, prefix: Sequence[str],
                 endpoint: str = DEFAULT_ENDPOINT, container: str | None = None) -> None:
        scheme, _, host = endpoint.partition("://")
        self._env_value = (f"{scheme}://{quote(cred.user, safe='')}:"
                           f"{quote(cred.password, safe='')}@{host}")
        self._prefix = list(prefix)
        self._container = container  # set: mc runs in that container, config dir lives there
        self._config_dir: str | None = None

    def __enter__(self) -> Mc:
        # Private, empty config dir (0700): mc writes nothing secret into it because the
        # alias comes from MC_HOST_<alias>; removed on exit either way.
        if self._container:
            self._config_dir = f"/tmp/hbc-mc-{secrets.token_hex(6)}"
            subprocess.run(["docker", "exec", self._container, "sh", "-c",
                            f"umask 077 && mkdir -p {self._config_dir}"],
                           capture_output=True, timeout=30, check=False)
        else:
            self._config_dir = tempfile.mkdtemp(prefix="hbc-mc-")
            os.chmod(self._config_dir, 0o700)
        return self

    def __exit__(self, *exc: object) -> None:
        if not self._config_dir:
            return
        if self._container:
            subprocess.run(["docker", "exec", self._container, "rm", "-rf", self._config_dir],
                           capture_output=True, timeout=30, check=False)
        else:
            shutil.rmtree(self._config_dir, ignore_errors=True)

    def run(self, *args: str, stdin: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, f"MC_HOST_{ALIAS}": self._env_value,
               "MC_CONFIG_DIR": self._config_dir or "/tmp/hbc-mc", "MC_NO_COLOR": "1",
               "MC_DISABLE_PAGER": "1"}
        argv = [*self._prefix, "--quiet", *args]
        try:
            r = subprocess.run(argv, env=env, input=stdin, capture_output=True, text=True,
                               timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvisionError(f"mc could not run: {type(exc).__name__}") from None
        if check and r.returncode != 0:
            # mc stderr can echo the alias URL; never include it.
            raise ProvisionError(f"mc {' '.join(args[:3])} failed (exit {r.returncode})")
        return r


# --- state + plan ------------------------------------------------------------

@dataclass(frozen=True)
class State:
    bucket: bool
    quota_gib: float | None
    rw_user: bool
    ro_user: bool
    rw_policy: bool
    ro_policy: bool
    rw_attached: bool
    ro_attached: bool
    rw_cred_file: bool
    ro_cred_file: bool
    registry_file: bool


@dataclass(frozen=True)
class Step:
    key: str
    text: str


def plan_steps(state: State, *, quota_gib: float, rotate: bool = False,
               write_registry: bool = True) -> list[Step]:
    """Pure: the additive steps still needed. Empty means already provisioned."""
    steps: list[Step] = []
    if not state.bucket:
        steps.append(Step("bucket", f"create bucket {BUCKET}"))
    if state.quota_gib is None or abs(state.quota_gib - quota_gib) > 1e-6:
        steps.append(Step("quota", f"set hard quota {quota_gib:g} GiB on {BUCKET}"))
    for user, policy, uexists, pexists, attached, cred, label in (
            (RW_USER, RW_POLICY, state.rw_user, state.rw_policy, state.rw_attached,
             state.rw_cred_file, "read-write"),
            (RO_USER, RO_POLICY, state.ro_user, state.ro_policy, state.ro_attached,
             state.ro_cred_file, "read-only")):
        if not pexists:
            steps.append(Step(f"policy:{policy}", f"create {label} policy {policy} (bucket-limited)"))
        if not uexists or rotate or (not cred):
            if uexists and not cred and not rotate:
                steps.append(Step(f"blocked:{user}", f"user {user} exists but its credential "
                                  "file is missing here; re-run with --rotate to issue a new secret"))
            else:
                verb = "rotate secret of" if uexists else "create"
                steps.append(Step(f"user:{user}", f"{verb} {label} user {user}; write 0600 credential file"))
        if not attached:
            steps.append(Step(f"attach:{user}", f"attach policy {policy} to {user}"))
    if write_registry and not state.registry_file:
        steps.append(Step("registry", f"write non-secret registry file {REGISTRY_NAME}"))
    return steps


def _exists(mc: Mc, *args: str) -> bool:
    return mc.run(*args, check=False).returncode == 0


def inspect_state(mc: Mc, config_dir: Path, bucket: str = BUCKET) -> State:
    """Read-only probes."""
    quota = None
    r = mc.run("quota", "info", "--json", f"{ALIAS}/{bucket}", check=False)
    if r.returncode == 0:
        try:
            q = json.loads(r.stdout.strip().splitlines()[-1]).get("quota")
            quota = float(q) / 1024 ** 3 if q else None
        except (ValueError, IndexError, TypeError):
            quota = None

    def attached(user: str, policy: str) -> bool:
        r = mc.run("admin", "user", "info", ALIAS, user, "--json", check=False)
        if r.returncode != 0:
            return False
        try:
            info = json.loads(r.stdout)
        except ValueError:
            return False
        pols = str(info.get("policyName", "")).split(",")
        return policy in pols

    return State(
        bucket=_exists(mc, "stat", f"{ALIAS}/{bucket}"),
        quota_gib=quota,
        rw_user=_exists(mc, "admin", "user", "info", ALIAS, RW_USER),
        ro_user=_exists(mc, "admin", "user", "info", ALIAS, RO_USER),
        rw_policy=_exists(mc, "admin", "policy", "info", ALIAS, RW_POLICY),
        ro_policy=_exists(mc, "admin", "policy", "info", ALIAS, RO_POLICY),
        rw_attached=attached(RW_USER, RW_POLICY),
        ro_attached=attached(RO_USER, RO_POLICY),
        rw_cred_file=(config_dir / RW_CRED_NAME).is_file(),
        ro_cred_file=(config_dir / RO_CRED_NAME).is_file(),
        registry_file=(config_dir / REGISTRY_NAME).is_file(),
    )


def write_private(path: Path, text: str) -> None:
    """Atomic 0600 write inside a 0700-or-stricter-created directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hbc.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def credential_yaml(access_key: str, secret_key: str) -> str:
    return f"access_key_id: {access_key}\nsecret_access_key: '{secret_key}'\n"


def registry_yaml(host: str, bucket: str, use_ssl: bool, region: str = "us-east-1") -> str:
    return (f"endpoints:\n  build-cache:\n    host: {json.dumps(host)}\n"
            f"    region: {json.dumps(region)}\n    bucket: {json.dumps(bucket)}\n"
            f"    use_ssl: {'true' if use_ssl else 'false'}\n")


@dataclass
class Report:
    applied: bool
    steps: list[Step]
    done: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        head = "APPLIED" if self.applied else "DRY RUN (nothing changed; pass --apply)"
        if not self.steps:
            return [head, "already provisioned: nothing to do", *self.notes]
        out = [head]
        out += [f"  - {s.text}" for s in self.steps]
        out += [f"  done: {d}" for d in self.done]
        return [*out, *self.notes]


def provision(mc: Mc, *, config_dir: Path = CONFIG_DIR, bucket: str = BUCKET,
              quota_gib: float = DEFAULT_QUOTA_GIB, apply: bool = False, rotate: bool = False,
              registry_host: str = "127.0.0.1:9000", registry_ssl: bool = False) -> Report:
    _check_bucket(bucket)
    if not 0 < quota_gib <= 100_000:
        raise ProvisionError("quota must be between 0 and 100000 GiB")
    state = inspect_state(mc, config_dir, bucket)
    steps = plan_steps(state, quota_gib=quota_gib, rotate=rotate)
    report = Report(apply, steps)
    blocked = [s for s in steps if s.key.startswith("blocked:")]
    if not apply:
        return report
    if blocked:
        raise ProvisionError(blocked[0].text)
    keys = {s.key for s in steps}
    config_dir.mkdir(parents=True, exist_ok=True)
    if "bucket" in keys:
        mc.run("mb", "--ignore-existing", f"{ALIAS}/{bucket}")
        report.done.append(f"bucket {bucket}")
    if "quota" in keys:
        mc.run("quota", "set", f"{ALIAS}/{bucket}", "--size", f"{quota_gib:g}GiB")
        report.done.append(f"quota {quota_gib:g} GiB")
    for user, policy, ro, cred_name in ((RW_USER, RW_POLICY, False, RW_CRED_NAME),
                                        (RO_USER, RO_POLICY, True, RO_CRED_NAME)):
        if f"policy:{policy}" in keys:
            mc.run("admin", "policy", "create", ALIAS, policy, "/dev/stdin",
                   stdin=json.dumps(policy_document(bucket, read_only=ro)))
            report.done.append(f"policy {policy}")
        if f"user:{user}" in keys:
            secret = secrets.token_hex(20)
            mc.run("admin", "user", "add", ALIAS, user, secret)
            write_private(config_dir / cred_name, credential_yaml(user, secret))
            report.done.append(f"user {user}; credential file {cred_name} (0600)")
        if f"attach:{user}" in keys:
            mc.run("admin", "policy", "attach", ALIAS, policy, "--user", user)
            report.done.append(f"attach {policy} -> {user}")
    if "registry" in keys:
        write_private(config_dir / REGISTRY_NAME, registry_yaml(registry_host, bucket, registry_ssl))
        report.done.append(f"registry file {REGISTRY_NAME}")
    return report


def run_cli(*, apply: bool, quota_gib: float, rotate: bool, central: str | None,
            container: str, registry_host: str) -> int:
    """Entry point for ``herdr-eng build-cache-rustfs-provision`` (prints no secret)."""
    try:
        cred = load_admin_credential(Path(central) if central else None)
        prefix, in_container = detect_mc_command(container)
        ssl_flag = registry_host.startswith("https://")
        host = registry_host.split("://", 1)[-1]
        with Mc(cred, prefix=prefix, container=container if in_container else None) as mc:
            rep = provision(mc, apply=apply, quota_gib=quota_gib, rotate=rotate,
                            registry_host=host, registry_ssl=ssl_flag)
    except (ProvisionError, InvalidError) as exc:
        print(f"build-cache-rustfs-provision: {exc}", file=sys.stderr)
        return 1
    print("\n".join(rep.lines()))
    if apply:
        print(f"credential files: {CONFIG_DIR / RW_CRED_NAME} and {CONFIG_DIR / RO_CRED_NAME} "
              "(0600; reference by path only)")
    return 0

