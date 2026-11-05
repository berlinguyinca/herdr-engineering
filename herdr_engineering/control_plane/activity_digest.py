"""Pure, default-off recorded-activity preview. No capture, delivery or I/O.

Trusted enrollment/bindings are caller-owned inputs, never derived from payload
labels. Counts describe producer reports, not independent test verification.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import quote, urlsplit

MAX_BYTES = 3072
MAX_COUNT = 2**31 - 1
_OPERATIONS = frozenset({"read", "write", "edit", "delete"})
_SENSITIVE = re.compile(
    r"(^|/)(\.env(?:\.[^/]*)?|\.git|\.aws|\.ssh|\.netrc|\.npmrc|\.pypirc|secrets?(?:\.[^/]*)?|credentials?(?:\.[^/]*)?|id_(?:rsa|dsa|ecdsa|ed25519)|[^/]*\.(?:pem|key|p12|pfx|kdbx))($|/)",
    re.I,
)


@dataclass(frozen=True)
class DigestEnrollment:
    project_id: str
    canonical_repository_id: str
    approved_code_paths: frozenset[str]
    approved_producer_binding_ids: frozenset[str]
    enabled: bool = False
    allowed_file_operations: frozenset[str] = frozenset({"write", "edit", "delete"})
    configured_private_origin: str | None = None


@dataclass(frozen=True)
class ProducerBinding:
    binding_id: str
    reporter_identity: str
    canonical_repository_id: str
    supported_metadata_kinds: frozenset[str]
    reports_test_completion: bool = False


@dataclass(frozen=True)
class SessionBinding:
    canonical_session_id: str
    native_context_epoch: str
    canonical_repository_id: str
    producer_binding_id: str


@dataclass(frozen=True)
class EventBinding:
    sequence: int
    stable_source_identity: str
    canonical_repository_id: str
    canonical_session_id: str
    native_context_epoch: str
    producer_binding_id: str
    completed_test_run_id: str | None = None


@dataclass(frozen=True)
class RevisionObservation:
    commit_sha: str
    canonical_repository_id: str
    stable_source_identity: str
    canonical_session_id: str
    native_context_epoch: str
    producer_binding_id: str
    observed: bool = False
    sensitive: bool = False
    withheld: bool = False


@dataclass(frozen=True)
class ActivityMetadata:
    sequence: int
    revision: int  # freshness only; NEVER used for selection or semantic identity
    kind: str
    path: str | None = None
    operation: str | None = None
    run_id: str | None = None
    status: str | None = None
    passed: object = None
    failed: object = None
    skipped: object = None
    sensitive: bool = False
    withheld: bool = False


@dataclass(frozen=True)
class Omissions:
    invalid_metadata: int = 0
    outside_manifest: int = 0
    confidential: int = 0
    unsupported_source: int = 0
    cap: int = 0
    byte_budget: int = 0


@dataclass(frozen=True)
class FileActivity:
    path: str
    operation: str
    source_hash: str


@dataclass(frozen=True)
class ReportedCounts:
    passed: int | None
    failed: int | None
    skipped: int | None


@dataclass(frozen=True)
class CompletedTest:
    run_hash: str
    reported_status: str
    reported_counts: ReportedCounts
    source_hash: str


@dataclass(frozen=True)
class ObservedRevision:
    commit_sha: str
    source_hash: str


@dataclass(frozen=True)
class ActivityDigest:
    project_id: str
    files: tuple[FileActivity, ...]
    completed_tests: tuple[CompletedTest, ...]
    observed_revisions: tuple[ObservedRevision, ...]
    source_hashes: tuple[str, ...]
    private_session_link: str | None
    omitted: Omissions
    schema_version: int = field(default=1, init=False)
    capability: str = field(default="recorded_engineering_activity", init=False)
    basis: str = field(default="reported_metadata_not_independent_proof", init=False)


@dataclass(frozen=True)
class PreviewResult:
    state: str
    digest: ActivityDigest | None = None
    canonical_text: str | None = None
    content_sha256: str | None = None
    omitted: Omissions = Omissions()
    preview_only: bool = field(default=True, init=False)
    delivery_enabled: bool = field(default=False, init=False)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _integer(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def _identity(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and re.fullmatch(r"[A-Za-z0-9_.:@/%-]+", value) is not None
    )


def _path(value):
    if not isinstance(value, str) or not value or any(c in value for c in ("\\", ":", "%")):
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return False
    try:
        return len(value.encode("utf-8")) <= 1024 and all(
            part not in ("", ".", "..") for part in value.split("/")
        )
    except UnicodeError:
        return False


def _private_origin(origin):
    if origin is None:
        return None
    if (
        not isinstance(origin, str)
        or len(origin) > 512
        or any(ord(c) < 33 or ord(c) == 127 for c in origin)
    ):
        raise ValueError("invalid private origin")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (parsed.port is not None and not 1 <= parsed.port <= 65535)
    ):
        raise ValueError("invalid private origin")
    # The explicit operator enrollment approves the origin. Syntax validation
    # cannot establish private routing from a DNS suffix, and never resolves it.
    try:
        origin.encode("utf-8")
    except UnicodeError:
        raise ValueError("invalid origin encoding") from None
    return origin.rstrip("/")


def _context_valid(binding, enrollment, session):
    return (
        binding.canonical_repository_id == enrollment.canonical_repository_id
        and binding.canonical_session_id == session.canonical_session_id
        and binding.native_context_epoch == session.native_context_epoch
    )


def preview_activity(
    records,
    *,
    enrollment=None,
    session=None,
    producers=(),
    event_bindings=(),
    revision_observations=(),
):
    """Build only a bounded metadata document; explicit preview enables no retention."""
    if not isinstance(enrollment, DigestEnrollment) or enrollment.enabled is False:
        return PreviewResult("not_enrolled")
    if (
        type(enrollment.enabled) is not bool
        or not isinstance(enrollment.project_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", enrollment.project_id)
        or enrollment.project_id.startswith(("personal", "assistant", "rakazo"))
        or not _identity(enrollment.canonical_repository_id)
        or not isinstance(enrollment.approved_code_paths, frozenset)
        or not isinstance(enrollment.allowed_file_operations, frozenset)
        or not enrollment.allowed_file_operations <= _OPERATIONS
        or not isinstance(enrollment.approved_producer_binding_ids, frozenset)
        or not all(_identity(value) for value in enrollment.approved_producer_binding_ids)
        or not isinstance(session, SessionBinding)
        or not _identity(session.canonical_session_id)
        or not _identity(session.native_context_epoch)
        or not _identity(session.producer_binding_id)
        or session.canonical_repository_id != enrollment.canonical_repository_id
    ):
        return PreviewResult("invalid_context")
    try:
        origin = _private_origin(enrollment.configured_private_origin)
    except ValueError:
        return PreviewResult("invalid_context")
    by_producer = {}
    for producer in producers:
        if (
            not isinstance(producer, ProducerBinding)
            or not _identity(producer.binding_id)
            or not _identity(producer.reporter_identity)
            or producer.canonical_repository_id != enrollment.canonical_repository_id
            or producer.binding_id not in enrollment.approved_producer_binding_ids
            or producer.binding_id in by_producer
            or not isinstance(producer.supported_metadata_kinds, frozenset)
            or not producer.supported_metadata_kinds <= {"file", "test", "revision"}
            or type(producer.reports_test_completion) is not bool
        ):
            return PreviewResult("untrusted_source")
        by_producer[producer.binding_id] = producer
    if session.producer_binding_id not in by_producer:
        return PreviewResult("untrusted_source")
    bindings = {}
    for binding in event_bindings:
        if isinstance(binding, EventBinding) and _integer(binding.sequence):
            bindings.setdefault(binding.sequence, []).append(binding)
    omitted = dict.fromkeys(Omissions.__dataclass_fields__, 0)
    files, tests, revisions = {}, {}, {}
    # Only KIND-specific approved fields participate in deduplication. Raw rev,
    # excluded prose and irrelevant fields cannot choose an item or a count.
    by_sequence = {}
    for row in records:
        if not isinstance(row, ActivityMetadata) or row.kind not in ("file", "test"):
            continue
        if not _integer(row.sequence):
            omitted["invalid_metadata"] += 1
            continue
        values = (
            (row.path, row.operation)
            if row.kind == "file"
            else (row.run_id, row.status, row.passed, row.failed, row.skipped)
        )
        values = (row.kind, row.sensitive, row.withheld, *values)
        variants = by_sequence.setdefault(row.sequence, {})
        try:
            key = tuple((type(value).__name__, value) for value in values)
            variants[key] = row
        except TypeError:
            variants[("invalid-metadata",)] = None
    for _sequence, variants in sorted(by_sequence.items()):
        # Mixed snapshots/conflicting reports are rejected, not resolved by rev
        # or list order. The Inspector accessor supplies one latest frame/seq.
        if len(variants) != 1 or next(iter(variants.values())) is None:
            omitted["invalid_metadata"] += 1
            continue
        row = next(iter(variants.values()))
        if row.sensitive is not False or row.withheld is not False:
            omitted["confidential"] += 1
            continue
        candidates = bindings.get(row.sequence, ())
        binding = candidates[0] if len(candidates) == 1 else None
        producer = (
            by_producer.get(binding.producer_binding_id)
            if binding and _identity(binding.producer_binding_id)
            else None
        )
        if (
            not binding
            or not _context_valid(binding, enrollment, session)
            or not producer
            or binding.producer_binding_id != session.producer_binding_id
            or row.kind not in producer.supported_metadata_kinds
            or not _identity(binding.stable_source_identity)
        ):
            omitted["unsupported_source"] += 1
            continue
        source_hash = _hash(
            [
                enrollment.canonical_repository_id,
                session.canonical_session_id,
                session.native_context_epoch,
                producer.binding_id,
                producer.reporter_identity,
                binding.stable_source_identity,
            ]
        )
        if row.kind == "file":
            if (
                not _path(row.path)
                or not isinstance(row.operation, str)
                or row.operation not in _OPERATIONS
            ):
                omitted["invalid_metadata"] += 1
            elif _SENSITIVE.search(row.path):
                omitted["confidential"] += 1
            elif (
                row.path not in enrollment.approved_code_paths
                or row.operation not in enrollment.allowed_file_operations
            ):
                omitted["outside_manifest"] += 1
            else:
                files[row.path] = FileActivity(row.path, row.operation, source_hash)
        else:
            counts = (row.passed, row.failed, row.skipped)
            if (
                producer.reports_test_completion is not True
                or not isinstance(row.run_id, str)
                or not row.run_id
                or binding.completed_test_run_id != row.run_id
            ):
                omitted["unsupported_source"] += 1
            elif row.status not in ("passed", "failed") or not all(
                value is None or _integer(value) for value in counts
            ):
                omitted["invalid_metadata"] += 1
            else:
                run_hash = _hash(
                    [
                        enrollment.canonical_repository_id,
                        session.canonical_session_id,
                        session.native_context_epoch,
                        producer.binding_id,
                        row.run_id,
                    ]
                )
                tests[run_hash] = CompletedTest(
                    run_hash, row.status, ReportedCounts(*counts), source_hash
                )
    for observation in revision_observations:
        if not isinstance(observation, RevisionObservation):
            continue
        if observation.sensitive is not False or observation.withheld is not False:
            omitted["confidential"] += 1
            continue
        producer = (
            by_producer.get(observation.producer_binding_id)
            if _identity(observation.producer_binding_id)
            else None
        )
        if (
            not producer
            or "revision" not in producer.supported_metadata_kinds
            or not _context_valid(observation, enrollment, session)
            or observation.observed is not True
            or not _identity(observation.stable_source_identity)
        ):
            omitted["unsupported_source"] += 1
        elif not isinstance(observation.commit_sha, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", observation.commit_sha
        ):
            omitted["invalid_metadata"] += 1
        else:
            source_hash = _hash(
                [
                    enrollment.canonical_repository_id,
                    session.canonical_session_id,
                    session.native_context_epoch,
                    producer.binding_id,
                    producer.reporter_identity,
                    observation.stable_source_identity,
                ]
            )
            candidate = ObservedRevision(observation.commit_sha, source_hash)
            previous = revisions.get(observation.commit_sha)
            if previous is None or candidate.source_hash < previous.source_hash:
                revisions[observation.commit_sha] = candidate
    selected = [
        sorted(files.values(), key=lambda item: item.path),
        sorted(tests.values(), key=lambda item: item.run_hash),
        sorted(revisions.values(), key=lambda item: item.commit_sha),
    ]
    for items, cap in zip(selected, (8, 4, 3), strict=True):
        omitted["cap"] += max(0, len(items) - cap)
        del items[cap:]
    link = (
        origin + "/#/herd/session/" + quote(session.canonical_session_id, safe="")
        if origin
        else None
    )
    while any(selected):
        sources = tuple(sorted({item.source_hash for group in selected for item in group}))
        omissions = Omissions(**omitted)
        digest = ActivityDigest(
            enrollment.project_id, *(tuple(group) for group in selected), sources, link, omissions
        )
        document = asdict(digest)
        for receipt in document["completed_tests"]:
            receipt["reported_counts"] = {
                name: value
                for name, value in receipt["reported_counts"].items()
                if value is not None
            }
        text = _canonical(document)
        if len(text.encode("utf-8")) <= MAX_BYTES:
            return PreviewResult(
                "preview", digest, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), omissions
            )
        # Remove one complete trailing item; sources/omission JSON are rebuilt.
        next(group for group in reversed(selected) if group).pop()
        omitted["byte_budget"] += 1
    return PreviewResult("empty", omitted=Omissions(**omitted))
