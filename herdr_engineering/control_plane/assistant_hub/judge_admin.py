"""Owner-facing judge status and check (read-only views; no key material, no key path, no endpoint path).

Everything here is derived on demand: the key fingerprint is computed from the key file at call time and never stored.
``check`` makes one synthetic consult with a fixed harmless key and records nothing.
"""
from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .calibration import build_report, gate_satisfied
from .jev import JevClient, JudgeUnavailable, read_key_file
from .judge import consult
from .judge_register import fingerprint
from .judge_version import judge_version
from .verbs import VERB_RISK, ActionKey, FlagClass, RiskClass, Verb

if TYPE_CHECKING:
    from .judge import JudgeRuntime

__all__ = ["CHECK_KEY", "check_view", "gate_by_class", "key_fingerprint", "mcp_view", "status_view"]

CHECK_KEY = ActionKey(tool="Bash", verb=Verb.SHELL_READ_ONLY, resource_scope="check", target="",
                      flag_class=FlagClass.NONE, risk_class=VERB_RISK[Verb.SHELL_READ_ONLY])


def endpoint_host(endpoint: object) -> str:
    try:
        return urlsplit(str(endpoint)).hostname or ""
    except ValueError:
        return ""


def key_fingerprint(path: object) -> str:
    """Fingerprint of the key file now on disk, or ``""`` when it is absent or not usable."""
    try:
        return fingerprint(read_key_file(str(path)))
    except JudgeUnavailable:
        return ""


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _cfg(judge: JudgeRuntime | None, fallback: Mapping[str, Any]) -> Mapping[str, Any]:
    if judge is None:
        return fallback
    try:
        return judge.cfg_getter()
    except Exception:
        return {}


def gate_by_class(report: Any, cfg: Mapping[str, Any]) -> dict[str, tuple[bool, str]]:
    return {rc.value: gate_satisfied(report, cfg, rc.value) for rc in RiskClass}


def status_view(store: Any, judge: JudgeRuntime | None, fallback_cfg: Mapping[str, Any], tenant_id: str, user_id: str,
                now: datetime) -> dict[str, Any]:
    cfg = _cfg(judge, fallback_cfg)
    report = build_report(store.list_judge_records(tenant_id, user_id), cfg, now, tenant_id, user_id)
    recorder = judge.recorder if judge is not None else None
    return {
        "enabled": cfg.get("enabled") is True, "model": str(cfg.get("model", "")), "judge_version": judge_version(cfg),
        "backend_confirmed": cfg.get("backend_confirmed") is True,
        "endpoint_host": endpoint_host(cfg.get("endpoint", "")),
        "key_fingerprint": key_fingerprint(cfg["api_key_file"]) if cfg.get("api_key_file") else "",
        "recorder": {"queue": recorder.queued() if recorder is not None else 0,
                     "dropped": recorder.dropped if recorder is not None else 0},
        "calibration": _jsonable(report),
        "gate": {k: {"satisfied": ok, "code": code} for k, (ok, code) in gate_by_class(report, cfg).items()},
    }


def mcp_view(store: Any, judge: JudgeRuntime | None, tenant_id: str, user_id: str, now: datetime) -> dict[str, Any]:
    """The worker-visible aggregate: no endpoint, no fingerprint, nothing per request."""
    if judge is None:
        return {"enabled": False, "mode": "shadow", "judge_version": "",
                "labeled_by_class": {rc.value: 0 for rc in RiskClass}, "gate": {rc.value: False for rc in RiskClass}}
    cfg = _cfg(judge, {})
    if not cfg:
        return {"enabled": False, "mode": "shadow", "judge_version": "",
                "labeled_by_class": {rc.value: 0 for rc in RiskClass}, "gate": {rc.value: False for rc in RiskClass}}
    report = build_report(store.list_judge_records(tenant_id, user_id), cfg, now, tenant_id, user_id)
    labeled = {rc.value: (report.classes[rc.value].labeled if rc.value in report.classes else 0) for rc in RiskClass}
    return {"enabled": cfg.get("enabled") is True, "mode": "shadow", "judge_version": judge_version(cfg),
            "labeled_by_class": labeled, "gate": {k: ok for k, (ok, _) in gate_by_class(report, cfg).items()}}


def check_view(judge: JudgeRuntime | None, fallback_cfg: Mapping[str, Any]) -> dict[str, Any]:
    """One synthetic consult.  Records no ``JudgeRecord`` and touches no grant; only a short code is reported."""
    cfg = _cfg(judge, fallback_cfg)
    if cfg.get("enabled") is not True:
        return {"ok": False, "code": "judge_disabled", "latency_ms": 0, "model": ""}
    started = time.monotonic()
    try:
        client = judge.client_factory() if judge is not None else JevClient(
            str(cfg["endpoint"]), str(cfg["api_key_file"]), str(cfg["model"]), float(cfg["timeout_seconds"]))
        if client is None:
            raise JudgeUnavailable("config")
        verdict = consult(client, CHECK_KEY, cfg)          # type: ignore[arg-type]
        ok, code, model = True, "", verdict.model
    except JudgeUnavailable as exc:
        ok, code, model = False, exc.code, ""
    except Exception:
        ok, code, model = False, "internal", ""
    return {"ok": ok, "code": code, "latency_ms": int((time.monotonic() - started) * 1000), "model": model}
