"""Read-time service context and reversible archival; event history stays intact."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import PurePath
from urllib.parse import quote

from .project_links import canonical_repository, project_for_repository

INACTIVE = {"unavailable", "stopped", "dead"}
ARCHIVE_AFTER_DAYS = 7
REPORT_STALE_SECONDS = 15 * 60


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def track_availability(record, status, stamp):
    """Keep the start of an uninterrupted inactive interval across heartbeats."""
    previous = str(record.get("status") or "").lower()
    if str(status).lower() in INACTIVE:
        if previous not in INACTIVE:
            record["unavailable_since"] = stamp
        elif not record.get("unavailable_since"):
            record["unavailable_since"] = record.get("updated_at") or stamp
    else:
        record["unavailable_since"] = None


def service_lifecycle(record, now, *, archive_after_days=ARCHIVE_AFTER_DAYS):
    last_seen = record.get("updated_at")
    last_stamp = _timestamp(last_seen)
    age = max(0, now - last_stamp) if last_stamp is not None else None
    status = str(record.get("status") or "").lower()
    # Legacy structured records have no transition history; their most recent
    # inactive observation is the conservative lower bound for this interval.
    since = record.get("unavailable_since", last_seen) if status in INACTIVE else None
    since_stamp = _timestamp(since)
    archived = (status in INACTIVE and since_stamp is not None
                and now - since_stamp >= archive_after_days * 86400)
    if archived:
        state, reason = "archived", f"Unavailable for at least {archive_after_days:g} days; history retained"
    elif status in INACTIVE:
        state, reason = "unavailable", "Reporter marked this service unavailable"
    elif age is None or age > REPORT_STALE_SECONDS:
        state, reason = "unknown", "No recent service report; current health is unknown"
    elif status == "healthy":
        state, reason = "current", "Reporter recently observed this service"
    else:
        state, reason = "unknown", "Reporter has not confirmed a healthy service"
    return dict(state=state, archived=archived, unavailable_since=since,
                last_seen_at=last_seen, age_seconds=round(age, 1) if age is not None else None,
                archive_after_days=archive_after_days, reason=reason)


def normalize_project(value):
    if not isinstance(value, str):
        return None
    name = PurePath(value.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:120] if name and name not in {".", ".."} else None


def normalize_source_port(value):
    return value if type(value) is int and 0 < value <= 65535 else None


def service_listener_id(host_id, pid, start, source_port):
    """Physical listener identity is independent of display names and routes."""
    if (not isinstance(host_id, str) or not host_id or len(host_id) > 300
            or type(pid) is not int or pid < 1 or not isinstance(start, str) or not start
            or len(start) > 300 or any(ord(char) < 32 for char in host_id + start)
            or normalize_source_port(source_port) is None):
        raise ValueError("listener identity requires host, positive PID, process birth and physical port")
    encoded = json.dumps([host_id, pid, start, source_port], separators=(",", ":"), ensure_ascii=False).encode()
    return "listener_" + hashlib.sha256(encoded).hexdigest()[:32]


def normalize_service_observation(payload, now, previous=None):
    state = payload.get("state")
    observed_at = payload.get("observed_at")
    stamp = _timestamp(observed_at)
    # Match the UI clock-skew allowance; future evidence must not revive later.
    if stamp is None or stamp > now + 5:
        raise ValueError("metadata observation requires a valid timezone-aware timestamp")
    if state not in {"observed", "missing", "unconfirmed"}:
        raise ValueError("metadata state must be observed, missing or unconfirmed")
    if state == "missing" and payload.get("scan_complete") is not True:
        raise ValueError("listener absence requires a complete OS listener scan")
    previous_stamp = _timestamp((previous or {}).get("observed_at"))
    if previous_stamp is not None and stamp < previous_stamp:
        raise ValueError("metadata observation is older than the current observation")
    return {"state": state, "observed_at": observed_at, "stale_after_seconds": 90}


def normalize_service_git(repository_id, commit_sha):
    identity = canonical_repository(repository_id) if repository_id is not None else None
    if repository_id is not None and identity is None:
        raise ValueError("service repository must be an explicit forge identity")
    if commit_sha is not None and (not isinstance(commit_sha, str)
            or not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", commit_sha)):
        raise ValueError("service commit_sha must be a full hexadecimal commit")
    return {"repository_id": identity, "commit_sha": commit_sha.lower() if identity and commit_sha else None}


def normalize_service_ownership(value):
    """Allowlisted observation metadata; descriptive labels never prove ownership."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return {"kind": "invalid"}
    allowed = ("kind", "host_id", "session_id", "herdr_session", "pane_id", "terminal_id",
               "listener_start", "shell_start", "observed_at", "unit")
    result = {key: value[key] for key in allowed
              if isinstance(value.get(key), str) and 0 < len(value[key]) <= 300
              and all(ord(char) >= 32 for char in value[key])}
    for key in ("listener_pid", "shell_pid"):
        if type(value.get(key)) is int and value[key] > 0:
            result[key] = value[key]
    chain = value.get("ancestor_pids")
    if isinstance(chain, list) and 1 <= len(chain) <= 64 and all(type(pid) is int and pid > 0 for pid in chain):
        result["ancestor_pids"] = list(chain)
    return result or {"kind": "invalid"}


def _observed_service_ownership(record, session=None):
    unknown = {"scope": "unassigned", "scope_reason": "No verified HerdR launch or system-service ownership", "ownership": None}
    proof = normalize_service_ownership(record.get("ownership"))
    if proof and proof.get("kind") == "system_unit":
        if (proof.get("host_id") == record.get("host_id") and proof.get("listener_pid")
                and proof.get("listener_start") and _timestamp(proof.get("observed_at")) is not None
                and re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", proof.get("unit", ""))):
            return {"scope": "system", "scope_reason": "Observed in a system service unit", "ownership": proof}
        return unknown
    if not session or session.get("session_id") != record.get("session_id"):
        return unknown
    fields = ("host_id", "herdr_session", "pane_id", "terminal_id")
    if (any(not isinstance(session.get(key), str) or not session[key] for key in fields)
            or session["host_id"] != record.get("host_id")):
        return unknown
    expected = "herdr:" + ":".join(quote(session[key], safe="") for key in ("host_id", "herdr_session", "pane_id"))
    if session["session_id"] != expected:
        return unknown
    if proof is not None:
        chain = proof.get("ancestor_pids", [])
        if (proof.get("kind") != "herdr_process_ancestry"
                or any(proof.get(key) != session.get(key) for key in (*fields, "session_id"))
                or not chain or len(set(chain)) != len(chain)
                or chain[0] != proof.get("listener_pid") or chain[-1] != proof.get("shell_pid")
                or not proof.get("listener_start") or not proof.get("shell_start")
                or _timestamp(proof.get("observed_at")) is None):
            return unknown
        return {"scope": "herdr", "scope_reason": "Listener descends from this native HerdR pane", "ownership": proof}
    return {"scope": "unassigned", "scope_reason": "Session association has no verified launch or process ancestry proof",
            "ownership": None}


def _verified_origin(record):
    receipt = record.get("verified_origin")
    proof = normalize_service_ownership(record.get("ownership"))
    if (not isinstance(receipt, dict) or receipt.get("kind") != "herdr_launch_origin"
            or receipt.get("service_id") != record.get("service_id")
            or receipt.get("host_id") != record.get("host_id")
            or receipt.get("session_id") != record.get("session_id")
            or not proof or proof.get("kind") != "herdr_process_ancestry"
            or not isinstance(receipt.get("ownership"), dict)
            or any(receipt["ownership"].get(key) != proof.get(key) for key in (
                "kind", "host_id", "session_id", "herdr_session", "pane_id", "terminal_id",
                "listener_pid", "listener_start", "shell_pid", "shell_start"))
            or _timestamp(receipt.get("verified_at")) is None):
        return None
    return receipt


def capture_service_origin(record, session, stamp):
    """Only the control plane creates receipts after validating native identity."""
    observed = _observed_service_ownership(record, session)
    if observed["scope"] == "herdr":
        return {"kind": "herdr_launch_origin", "service_id": record["service_id"],
                "host_id": record["host_id"], "session_id": record["session_id"],
                "ownership": observed["ownership"], "verified_at": stamp}
    return _verified_origin(record)


def service_ownership(record, session=None):
    receipt = _verified_origin(record)
    if receipt:
        return {"scope": "herdr", "scope_reason": "Launch origin verified by HerdR; retained for this process instance",
                "ownership": receipt["ownership"]}
    return _observed_service_ownership(record, session)


def service_catalog(record, session=None):
    name = str(record.get("service_name") or "")
    process = str(record.get("owner") or name.split(" on ", 1)[0]).lower()
    category, label = "other", "Other services"
    purpose = "Discovered network listener; application purpose has not been reported"
    if "herdr-control-plane" in name.lower():
        category, label, purpose = "infrastructure", "Infrastructure", "HerdR fleet dashboard and API"
    elif any(token in process for token in ("llama", "inferweave", "ollama", "vllm")):
        category, label, purpose = "models", "Model serving", "Model inference process"
    elif process in {"haproxy", "gateway", "ssh", "gogw", "iw-relay"}:
        category, label, purpose = "network", "Gateways and tunnels", "Gateway, proxy or tunnel listener"
    elif any(token in process for token in ("postgres", "redis", "rustfs", "docker", "com.docke")):
        category, label, purpose = "infrastructure", "Infrastructure", "Storage or container infrastructure process"
    elif process.startswith("next-server"):
        category, label, purpose = "development", "Development apps", "Next.js application server"
    elif process.startswith("ng serve"):
        category, label, purpose = "development", "Development apps", "Angular development server"
    elif process.startswith(("codex", "budi", "pi ")):
        category, label, purpose = "agents", "Agent tools", "Coding agent or agent companion listener"
    elif process.startswith(("python", "node", "go-local", "laya-serve")):
        category, label, purpose = "development", "Development apps", "Application listener; project identifies its working directory when reported"
    project = normalize_project(record.get("project"))
    project_source = "process.cwd" if project else None
    if not project and session and isinstance(session.get("cwd"), str):
        project = normalize_project(session["cwd"])
        project_source = "session.cwd" if project else None
    source_port = normalize_source_port(record.get("source_port"))
    if source_port is None:
        match = re.search(r"\(port (\d{1,5})\)$", name)
        source_port = int(match[1]) if match and 0 < int(match[1]) <= 65535 else None
    try:
        git = normalize_service_git(record.get("repository_id"), record.get("commit_sha"))
    except ValueError:
        git = {"repository_id": None, "commit_sha": None}
    return dict(**service_ownership(record, session), **git,
                metadata_observation=record.get("metadata_observation"),
                endpoint_scope=record.get("endpoint_scope"),
                project_id=project_for_repository(git["repository_id"]),
                category=category, label=label, purpose=purpose,
                project=project or None, project_source=project_source, source_port=source_port)


def validate_archive_days(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("service_archive_days must be finite and positive")
    return value
