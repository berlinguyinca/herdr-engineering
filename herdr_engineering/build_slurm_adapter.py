"""Spec 11: default-off, NON-EXECUTING adapter contract for whole-build Slurm jobs.

Nothing here invokes Slurm. There is no runner: :class:`NullRunner` always reports
``runner_unavailable`` and the sbatch builder only renders argv/script text as DATA
for review. A read-only placement probe (control_plane/slurm_placement.py) is not
runner support and is deliberately not used.

Contract summary (what a future, separately approved runner must satisfy):

* Opt-in: ``fabric_build_cache.enabled`` AND ``fabric_build_cache.slurm.enabled``,
  an allow-listed partition and an account NAME. Otherwise build locally.
* Staging: a sha256 manifest of the staged source tree plus the toolchain fingerprint
  (:func:`stage_manifest`). Symlinks in the source tree are refused.
* Ownership: every job carries build id, attempt id and the registry fencing epoch in
  its name and comment. A request whose epoch is no longer the registry's current
  lease epoch is rejected (``stale_epoch``).
* Cancellation: only a job whose comment equals the binding's rendered comment may be
  cancelled, and only by exact numeric job id (:func:`cancel_target`); never by
  name pattern or user. Cancel is idempotent.
* Outputs: returned artifacts are checked for path containment, symlinks, size and
  sha256 against the expected list; unexpected files are rejected too.
* Fallback: any refusal yields a :class:`FallbackReason`; the caller then follows
  ``execution.remote_fallback`` (``local`` or ``fail``).
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from .build_cache_config import BuildCacheConfig

NOT_SUBMITTED_MESSAGE = "not submitted; no runner verified"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_JOB_ID_RE = re.compile(r"^[0-9]{1,12}$")
_ARG_RE = re.compile(r"^[A-Za-z0-9._:=/+,@%-]+$")
_SKIP_DIRS = frozenset({".git", "target", "node_modules", "__pycache__"})
_CARGO_SUBCOMMANDS = frozenset({"check", "build", "test", "clippy", "nextest"})
_MAX_STAGE_FILES = 200_000


class FallbackReason(StrEnum):
    DISABLED = "disabled"
    RUNNER_UNAVAILABLE = "runner_unavailable"
    PARTITION_NOT_ALLOWED = "partition_not_allowed"
    STALE_EPOCH = "stale_epoch"
    QUEUE_TIMEOUT = "queue_timeout"
    SUBMIT_REJECTED = "submit_rejected"
    OUTPUT_INVALID = "output_invalid"
    CANCELLED = "cancelled"


class PlanError(ValueError):
    """The request cannot be rendered safely."""


class StagingError(ValueError):
    """The source tree cannot be staged (symlink, too large)."""


@dataclass(frozen=True)
class StagingManifest:
    source_digest: str
    toolchain_digest: str
    manifest_digest: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class LeaseBinding:
    """Links a job to the build registry: ids plus the fencing epoch."""
    build_id: str
    attempt_id: str
    scope: str
    epoch: int


@dataclass(frozen=True)
class SubmitRequest:
    binding: LeaseBinding
    manifest: StagingManifest
    partition: str
    account: str
    cpus: int
    mem_gb: int
    time_limit_minutes: int
    argv: tuple[str, ...]
    staging_dir: str


@dataclass(frozen=True)
class SubmitResponse:
    accepted: bool
    job_id: str | None = None
    reason: FallbackReason | None = None


@dataclass(frozen=True)
class CancelRequest:
    job_id: str
    binding: LeaseBinding
    why: str


@dataclass(frozen=True)
class CancelResult:
    cancelled: bool
    reason: FallbackReason | None = None


@dataclass(frozen=True)
class CancelTarget:
    allowed: bool
    job_id: str | None = None


@dataclass(frozen=True)
class ExpectedArtifact:
    relpath: str
    sha256: str
    size: int


@dataclass(frozen=True)
class OutputProblem:
    relpath: str
    code: str  # path_escape|symlink|missing|size_mismatch|digest_mismatch|unexpected_file


@dataclass(frozen=True)
class Decision:
    use_slurm: bool
    reason: FallbackReason | None
    fallback: str  # "local" | "fail": the config's remote_fallback


@dataclass(frozen=True)
class SbatchPlan:
    argv: tuple[str, ...]
    script: str


@runtime_checkable
class SlurmRunner(Protocol):
    """What a real runner must offer. ``verified`` is True only after a separately
    approved verification against a real test partition; nothing sets it today."""
    verified: bool

    def submit(self, req: SubmitRequest) -> SubmitResponse: ...
    def cancel(self, req: CancelRequest) -> CancelResult: ...


class NullRunner:
    """The only runner that exists: never submits, never cancels, never touches Slurm."""
    verified = False

    def submit(self, req: SubmitRequest) -> SubmitResponse:
        return SubmitResponse(False, None, FallbackReason.RUNNER_UNAVAILABLE)

    def cancel(self, req: CancelRequest) -> CancelResult:
        return CancelResult(False, FallbackReason.RUNNER_UNAVAILABLE)


# --- staging ------------------------------------------------------------------
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage_manifest(root: str | os.PathLike[str], toolchain_fingerprint: str) -> StagingManifest:
    """Deterministic digest of the source tree (relative path + content) and toolchain."""
    base = Path(root)
    entries: list[tuple[str, str]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(dirnames) + sorted(filenames):
            p = Path(dirpath, name)
            if stat.S_ISLNK(p.lstat().st_mode):
                raise StagingError(f"symlink in source tree: {p.relative_to(base)}")
        for name in sorted(filenames):
            p = Path(dirpath, name)
            data = p.read_bytes()
            total += len(data)
            entries.append((p.relative_to(base).as_posix(), _sha(data)))
            if len(entries) > _MAX_STAGE_FILES:
                raise StagingError("source tree has too many files to stage")
    entries.sort()
    source = _sha("\n".join(f"{h}  {r}" for r, h in entries).encode())
    tool = _sha(toolchain_fingerprint.encode())
    return StagingManifest(source, tool, _sha(f"{source}:{tool}".encode()), len(entries), total)


# --- ownership / fencing -------------------------------------------------------
def render_name(b: LeaseBinding) -> str:
    return f"hbc-{b.build_id[:12]}-e{b.epoch}"


def render_comment(b: LeaseBinding) -> str:
    scope = _sha(b.scope.encode())[:12]
    return f"herdr-build-cache build={b.build_id} attempt={b.attempt_id} epoch={b.epoch} scope={scope}"


def fence_ok(b: LeaseBinding, lease_row: dict[str, Any] | None) -> bool:
    """True only if the registry still shows this exact attempt+epoch holding the lease."""
    return (lease_row is not None and lease_row.get("released_ts") is None
            and lease_row.get("owner_attempt_id") == b.attempt_id
            and lease_row.get("epoch") == b.epoch)


def cancel_target(b: LeaseBinding, job_comment: str, *, job_id: str) -> CancelTarget:
    if not _JOB_ID_RE.match(job_id) or job_comment != render_comment(b):
        return CancelTarget(False)
    return CancelTarget(True, job_id)


# --- decisions -----------------------------------------------------------------
def decide_placement(cfg: BuildCacheConfig, runner: SlurmRunner, req: SubmitRequest, *,
                     current_epoch: int | None) -> Decision:
    fb = cfg.execution.remote_fallback.value

    def no(reason: FallbackReason) -> Decision:
        return Decision(False, reason, fb)

    if not cfg.slurm_runner_requested:
        return no(FallbackReason.DISABLED)
    if req.partition not in cfg.slurm.allowed_partitions:
        return no(FallbackReason.PARTITION_NOT_ALLOWED)
    if current_epoch is None or current_epoch != req.binding.epoch:
        return no(FallbackReason.STALE_EPOCH)
    if not getattr(runner, "verified", False):
        return no(FallbackReason.RUNNER_UNAVAILABLE)
    return Decision(True, None, fb)


# --- rendering (data only, never executed) --------------------------------------
def _check_req(req: SubmitRequest) -> None:
    for label, v in (("partition", req.partition), ("account", req.account)):
        if not _NAME_RE.match(v or ""):
            raise PlanError(f"{label} {v!r} is not a plain name")
    for label, v in (("cpus", req.cpus), ("mem_gb", req.mem_gb),
                     ("time_limit_minutes", req.time_limit_minutes)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise PlanError(f"{label} must be a positive integer")
    sd = req.staging_dir
    if not sd.startswith("/") or ".." in PurePosixPath(sd).parts or not _ARG_RE.match(sd):
        raise PlanError("staging_dir must be a clean absolute path")
    a = req.argv
    if len(a) < 2 or a[0] != "cargo" or a[1] not in _CARGO_SUBCOMMANDS:
        raise PlanError("build argv must be `cargo <check|build|test|clippy|nextest> ...`")
    for x in a:
        if not _ARG_RE.match(x):
            raise PlanError(f"argument {x!r} contains characters outside the safe set")


def build_sbatch(req: SubmitRequest) -> SbatchPlan:
    """Render argv + script text for review. Executes nothing."""
    _check_req(req)
    b = req.binding
    argv = ("sbatch", "--parsable", f"--job-name={render_name(b)}",
            f"--comment={render_comment(b)}", f"--partition={req.partition}",
            f"--account={req.account}", "--nodes=1", "--ntasks=1",
            f"--cpus-per-task={req.cpus}", f"--mem={req.mem_gb}G",
            f"--time={req.time_limit_minutes}", "--no-requeue", "--export=NONE",
            f"--chdir={req.staging_dir}")
    m = req.manifest
    script = "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        f"# herdr build cache: build={b.build_id} attempt={b.attempt_id} epoch={b.epoch}",
        f"# staged source sha256 {m.source_digest}",
        f"# toolchain sha256 {m.toolchain_digest}",
        "export CARGO_INCREMENTAL=0",
        "exec " + " ".join(shlex.quote(x) for x in req.argv),
        ""])
    return SbatchPlan(argv, script)


# --- output validation -----------------------------------------------------------
def validate_outputs(out_dir: str | os.PathLike[str],
                     expected: Sequence[ExpectedArtifact]) -> list[OutputProblem]:
    """Check returned artifacts: containment, no symlinks, size, sha256, nothing extra."""
    root = Path(out_dir)
    problems: list[OutputProblem] = []
    seen: set[str] = set()
    for art in expected:
        rel = art.relpath
        parts = PurePosixPath(rel).parts
        if not rel or rel.startswith("/") or ".." in parts or "\x00" in rel:
            problems.append(OutputProblem(rel, "path_escape"))
            continue
        cur, link = root, False
        for part in parts:
            cur = cur / part
            try:
                if stat.S_ISLNK(cur.lstat().st_mode):
                    link = True
                    break
            except OSError:
                break
        if link:
            problems.append(OutputProblem(rel, "symlink"))
            continue
        seen.add(PurePosixPath(rel).as_posix())
        try:
            st = cur.lstat()
        except OSError:
            problems.append(OutputProblem(rel, "missing"))
            continue
        if not stat.S_ISREG(st.st_mode):
            problems.append(OutputProblem(rel, "missing"))
        elif st.st_size != art.size:
            problems.append(OutputProblem(rel, "size_mismatch"))
        elif _sha(cur.read_bytes()) != art.sha256:
            problems.append(OutputProblem(rel, "digest_mismatch"))
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for f in files:
            rel = Path(dirpath, f).relative_to(root).as_posix()
            if rel not in seen:
                problems.append(OutputProblem(rel, "unexpected_file"))
    return problems


# --- preview (used by the CLI) -----------------------------------------------------
def preview(cfg: BuildCacheConfig, source_dir: str, partition: str | None,
            toolchain: str) -> dict[str, Any]:
    """Would-be submission as data. Uses a placeholder binding: no lease is acquired."""
    part = partition or (cfg.slurm.allowed_partitions[0] if cfg.slurm.allowed_partitions else "")
    out: dict[str, Any] = {
        "submitted": False, "runner_verified": False, "message": NOT_SUBMITTED_MESSAGE,
        "slurm_requested": cfg.slurm_runner_requested, "partition": part,
        "binding": "placeholder (no registry lease acquired)",
        "decision": None, "sbatch_argv": None, "script": None, "error": None}
    try:
        manifest = stage_manifest(source_dir, toolchain)
        req = SubmitRequest(
            binding=LeaseBinding("0" * 32, "0" * 32, source_dir, 0), manifest=manifest,
            partition=part, account=cfg.slurm.account or "unset", cpus=cfg.slurm.cpus,
            mem_gb=cfg.slurm.mem_gb, time_limit_minutes=cfg.slurm.time_limit_minutes,
            argv=("cargo", "check", "--workspace"), staging_dir="/staging/PLACEHOLDER")
    except StagingError as exc:
        out["error"] = f"staging: {exc}"
        out["decision"] = {"use_slurm": False, "reason": None, "fallback":
                           cfg.execution.remote_fallback.value}
        return out
    d = decide_placement(cfg, NullRunner(), req, current_epoch=0)
    out["decision"] = {"use_slurm": d.use_slurm, "reason": d.reason.value if d.reason else None,
                       "fallback": d.fallback}
    out["manifest"] = asdict(manifest)
    try:
        plan = build_sbatch(req)
        out["sbatch_argv"], out["script"] = list(plan.argv), plan.script
    except PlanError as exc:
        out["error"] = f"plan: {exc}"
    return out
