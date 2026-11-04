"""Calibration of the shadow judge: exact false-negative bound, per-risk-class report and the trust gate.

Ground truth is the owner's real decision, which is noisy: an approval is not proof an action was safe.  So only
a judge "safe" verdict that the owner denied (or later regretted approving) counts as a false negative.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from .judge_version import judge_version as _judge_version
from .shadow import JudgeRecord

__all__ = ["MIN_DISTINCT_KEYS", "MIN_NEGATIVE_LABELS", "Bin", "CalibrationReport", "ClassReport", "build_report", "clopper_pearson_upper", "gate_satisfied"]

LABELS_NOTE = ("Labels are the owner's own approve/deny decisions, which are noisy: owners misclick, approve things "
               "they later regret and deny harmless ones. An approval is not treated as proof of safety, so the "
               "false-negative bound is an estimate, not a guarantee.")
_BINS = 10
MIN_DISTINCT_KEYS = 5        # a class needs labels from at least this many different action keys
MIN_NEGATIVE_LABELS = 5      # ...and at least this many denied/regretted ones, or "always safe" would pass
DEFAULT_MIN_LABELED = 200
DEFAULT_MAX_FN_BOUND = 0.05


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), summed in log space for stability."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(k + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q)
    return min(total, 1.0)


def clopper_pearson_upper(failures: int, trials: int, confidence: float = 0.95) -> float:
    """Exact one-sided upper confidence bound on a failure rate: the p where P(X <= failures) = 1 - confidence."""
    if trials <= 0 or failures >= trials:
        return 1.0
    alpha = 1.0 - confidence
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _binom_cdf(failures, trials, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@dataclass(frozen=True)
class Bin:
    lo: float
    hi: float
    n: int
    rate: float          # share of the bin's labeled decisions that were denied or regretted


@dataclass(frozen=True)
class ClassReport:
    risk_class: str
    labeled: int
    judge_said_safe: int
    false_negatives: int
    fn_upper_bound: float
    reliability: tuple[Bin, ...]
    distinct_keys: int = 0
    negatives: int = 0       # labeled decisions that were denied or regretted


@dataclass(frozen=True)
class CalibrationReport:
    judge_version: str
    classes: Mapping[str, ClassReport]
    generated_at: datetime
    labels_note: str


def _labeled(records: Iterable[JudgeRecord], version: str, tenant_id: str, user_id: str) -> list[JudgeRecord]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[JudgeRecord] = []
    for r in sorted(records, key=lambda x: (x.created_at, x.record_id)):
        if r.tenant_id != tenant_id or r.user_id != user_id:
            continue
        if r.status != "ok" or not r.owner_decision or r.judge_version != version:
            continue
        ident = (r.tenant_id, r.user_id, r.worker_id, r.key_id)       # earliest label per key wins
        if ident in seen:
            continue
        seen.add(ident)
        out.append(r)
    return out


def _class_report(risk: str, recs: list[JudgeRecord]) -> ClassReport:
    bad = [r.owner_decision == "denied" or r.regret for r in recs]
    safe = [not r.escalate for r in recs]
    fn = sum(1 for b, s in zip(bad, safe, strict=True) if b and s)
    n_safe = sum(safe)
    counts = [[0, 0] for _ in range(_BINS)]
    for r, b in zip(recs, bad, strict=True):
        i = min(_BINS - 1, max(0, int(r.destructive * _BINS)))
        counts[i][0] += 1
        counts[i][1] += int(b)
    bins = tuple(Bin(i / _BINS, (i + 1) / _BINS, n, (d / n if n else 0.0)) for i, (n, d) in enumerate(counts))
    return ClassReport(risk, len(recs), n_safe, fn, clopper_pearson_upper(fn, n_safe), bins,
                       len({r.key_id for r in recs}), sum(bad))


def build_report(records: Iterable[JudgeRecord], judge_cfg: Mapping[str, object], now: datetime,
                 tenant_id: str, user_id: str) -> CalibrationReport:
    """Report for one tenant/user. The caller should still pass the store's already-scoped record list;
    records of other tenants or users are ignored here as a second line of defence."""
    version = _judge_version(judge_cfg)  # type: ignore[arg-type]
    by_class: dict[str, list[JudgeRecord]] = {}
    for r in _labeled(records, version, tenant_id, user_id):
        by_class.setdefault(r.risk_class, []).append(r)
    return CalibrationReport(version, {k: _class_report(k, v) for k, v in sorted(by_class.items())}, now, LABELS_NOTE)


def _valid_min(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 20:
        return DEFAULT_MIN_LABELED
    return value


def _valid_bound(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 < value < 1.0:
        return DEFAULT_MAX_FN_BOUND
    return float(value)


def gate_satisfied(report: CalibrationReport, judge_cfg: Mapping[str, object], risk_class: str) -> tuple[bool, str]:
    if judge_cfg.get("enabled") is not True:
        return False, "judge_disabled"
    if judge_cfg.get("backend_confirmed") is not True:
        return False, "backend_unconfirmed"
    if report.judge_version != _judge_version(judge_cfg):  # type: ignore[arg-type]
        return False, "stale_report"
    cal = judge_cfg.get("calibration")
    cal = cal if isinstance(cal, Mapping) else {}
    cr = report.classes.get(risk_class)
    if cr is None or cr.labeled < _valid_min(cal.get("min_labeled_decisions")):
        return False, "insufficient_labels"
    if cr.distinct_keys < MIN_DISTINCT_KEYS:
        return False, "insufficient_diversity"
    if cr.negatives < MIN_NEGATIVE_LABELS:
        return False, "no_discrimination"
    if not cr.fn_upper_bound < _valid_bound(cal.get("max_false_negative_bound")):
        return False, "fn_bound_exceeded"
    return True, ""
