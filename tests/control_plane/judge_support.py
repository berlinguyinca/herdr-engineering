"""Test-only support for the judge on the decision path (lives in tests/, NOT in the package).

There is no fake judge.  ``WireListener`` is a raw TCP socket in the test process that answers with the DOCUMENTED
System One success shape (not captured), so the real ``JevClient`` + ``consult`` + ``decide`` path runs end to end;
``closed_port`` produces a real connect failure.  ``force_active_grant_for_tests`` is the only way tests build an
ACTIVE grant without populating a calibration store.
"""
from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from herdr_engineering.control_plane.assistant import AssistantStore
from herdr_engineering.control_plane.assistant_hub.actiongrants import (
    ActionGrant,
    GrantState,
    as_utc,
)
from herdr_engineering.control_plane.assistant_hub.jev import JevClient
from herdr_engineering.control_plane.assistant_hub.judge import JudgeRuntime
from herdr_engineering.control_plane.assistant_hub.judge_version import judge_version
from herdr_engineering.control_plane.assistant_hub.shadow import JudgeRecord, ShadowRecorder

SAFE = {"destructive": 0.05, "irreversible_or_external": 0.05, "owner_approval_covers": 0.9, "severity_probs": (0.8, 0.2, 0.0, 0.0), "severity": 0.2}
RISKY = {"destructive": 0.9, "irreversible_or_external": 0.9, "owner_approval_covers": 0.2, "severity_probs": (0.0, 0.0, 0.2, 0.8), "severity": 2.8}


def success_body(values: dict[str, Any], model: str = "concrete-checkpoint-id") -> bytes:
    doc = {
        "model": model,
        "answers": {
            "destructive": {"type": "noul", "noul": values["destructive"]},
            "irreversible_or_external": {"type": "noul", "noul": values["irreversible_or_external"]},
            "severity": {"type": "score", "score": values["severity"], "confidence": 0.9,
                         "legend": {"0": "informational", "1": "minor", "2": "major", "3": "critical"},
                         "probabilities": {str(i): p for i, p in enumerate(values["severity_probs"])}},
            "owner_approval_covers": {"type": "noul", "noul": values["owner_approval_covers"]},
        },
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }
    return json.dumps(doc).encode()


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class WireListener:
    """Raw listener serving one fixed documented-shape body to every request; counts requests and keeps the bytes."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.hits = 0
        self.seen: list[bytes] = []
        self._lock = threading.Lock()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = int(self._srv.getsockname()[1])
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self._srv.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn: socket.socket) -> bytes:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return buf
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip() or b"0")
        while len(rest) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            rest += chunk
        return head + b"\r\n\r\n" + rest

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                data = self._read(conn)
                with self._lock:
                    self.hits += 1
                    self.seen.append(data)
                head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(self.body)
                conn.sendall(head + self.body)
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._srv.close()


def judge_cfg(endpoint: str, key_file: Path, *, enabled: bool = True, backend_confirmed: bool = True,
              model: str = "laya-auto", **calibration: Any) -> dict[str, Any]:
    from herdr_engineering.control_plane.assistant import assistant_config
    section: dict[str, Any] = {"enabled": True, "judge": {
        "enabled": enabled, "endpoint": endpoint, "api_key_file": str(key_file), "model": model,
        "backend_confirmed": backend_confirmed, "timeout_seconds": 2}}
    if calibration:
        section["judge"]["calibration"] = calibration
    return dict(assistant_config({"personal_assistant": section})["judge"])


def write_key(tmp_path: Path) -> Path:
    p = tmp_path / "jev.key"
    p.write_text("sk-test-not-a-real-key\n")
    p.chmod(0o600)
    return p


class CountingRecorder(ShadowRecorder):
    """The real recorder, observed: counts every ``submit`` call and then does exactly what the real one does."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.submitted: list[Any] = []

    def submit(self, job: Any) -> bool:
        self.submitted.append(job)
        return super().submit(job)


def make_runtime(store: AssistantStore, cfg: dict[str, Any], now: Callable[[], datetime]) -> JudgeRuntime:
    def factory() -> JevClient | None:
        if cfg.get("enabled") is not True:
            return None
        return JevClient(str(cfg["endpoint"]), str(cfg["api_key_file"]), str(cfg["model"]), float(cfg["timeout_seconds"]))
    recorder = CountingRecorder(store, lambda: cfg, factory, now)
    return JudgeRuntime(cfg_getter=lambda: cfg, client_factory=factory, recorder=recorder, now=now)


def force_active_grant_for_tests(store: AssistantStore, grants_cfg: dict[str, Any], grant_id: str, now: datetime,
                                 judge_cfg_: dict[str, Any] | None = None) -> ActionGrant:
    """ACTIVE grant WITHOUT the calibration gate: tests only.  Stamps the judge version like the real activation."""
    with store._lock:
        g = store.action_grants[grant_id]
    active = replace(g, state=GrantState.ACTIVE, activated_at=as_utc(now), confirmed_by="owner",
                     expires_at=as_utc(now) + timedelta(days=int(grants_cfg["grant_ttl_days"])),
                     judge_version=judge_version(judge_cfg_) if judge_cfg_ is not None else g.judge_version)
    store.put_action_grant(active)
    return active


def labeled_records(cfg: dict[str, Any], risk: str, *, n_safe: int = 200, n_flagged: int = 10, n_fn: int = 0,
                   keys: int = 30, tenant: str = "acme", user: str = "owner",
                   version: str | None = None) -> list[JudgeRecord]:
    """Real ``JudgeRecord`` rows labeled by the owner: ``n_safe`` judge-safe ones (``n_fn`` of them denied, i.e. false
    negatives) plus ``n_flagged`` escalated-and-denied ones, spread over ``keys`` action keys."""
    from datetime import UTC
    base = datetime(2026, 10, 1, tzinfo=UTC)
    ver = version or judge_version(cfg)
    out: list[JudgeRecord] = []
    i = 0

    def mk(escalate: bool, decision: str) -> JudgeRecord:
        nonlocal i
        i += 1
        return JudgeRecord(record_id=f"jr{i}-{ver[:4]}", tenant_id=tenant, user_id=user, worker_id=f"wk{i}",
                           request_id=f"rq{i}-{ver[:4]}", key_id=f"key{i % keys}", verb="git_push", risk_class=risk,
                           judge_version=ver, model="laya-auto", status="ok", escalate=escalate,
                           owner_decision=decision,   # type: ignore[arg-type]
                           created_at=base + timedelta(seconds=i), decided_at=base)

    for _ in range(n_safe - n_fn):
        out.append(mk(False, "approved"))
    for _ in range(n_fn):
        out.append(mk(False, "denied"))
    for _ in range(n_flagged):
        out.append(mk(True, "denied"))
    return out


def satisfy_gate(store: AssistantStore, rt: JudgeRuntime, risk: str, tenant: str = "acme", user: str = "owner") -> None:
    """Populate the store so the real calibration gate passes for ``risk`` (never touches the gate code itself)."""
    for r in labeled_records(dict(rt.cfg_getter()), risk, tenant=tenant, user=user):
        store.add_judge_record(r)


__all__ = ["labeled_records", "satisfy_gate", "RISKY", "SAFE", "WireListener", "closed_port", "force_active_grant_for_tests",
           "judge_cfg", "make_runtime", "success_body", "write_key"]
