"""Read-time service context and reversible archival; event history stays intact."""
from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import PurePath

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
    return dict(category=category, label=label, purpose=purpose,
                project=project or None, project_source=project_source, source_port=source_port)


def validate_archive_days(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("service_archive_days must be finite and positive")
    return value
