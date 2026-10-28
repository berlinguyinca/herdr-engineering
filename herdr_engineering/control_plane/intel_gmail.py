"""Gmail message -> project-intelligence events (pure normaliser; no network, no OAuth).

Fetching needs a Google OAuth client and consent (blocker B1). This module only shapes already-fetched
Gmail API ``users.messages.get`` dicts. Email text is untrusted evidence: it is hashed and measured, never
interpreted, and instructions inside it are flagged but never acted on. Message bodies are not copied into
event payloads (originals belong in the artifact registry); only the subject is kept, truncated, as the title.
"""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

_ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)")
_INJECTION = re.compile(r"(ignore (all |any )?(previous|prior) instructions|disregard (the )?(above|system)|"
                        r"\bsystem\s*:|you are now|run the following (command|script)|<\s*tool_call)", re.I)


def _headers(msg: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers", []) if "name" in h}


def _walk(part: dict, path: str = "1"):
    yield path, part
    for i, child in enumerate(part.get("parts") or [], 1):
        yield from _walk(child, f"{path}.{i}")


def normalise_message(msg: dict, *, org_id: str, account: str, project_ids: tuple[str, ...],
                      security_label: str = "confidential") -> list[dict]:
    """One ``email.received`` event plus one ``email.attachment`` per attachment part."""
    mid, tid = msg.get("id"), msg.get("threadId")
    if not mid or not tid:
        raise ValueError("message needs id and threadId")
    h = _headers(msg)
    occurred = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, UTC).isoformat()
    sender = h.get("from", "")
    # Identity is the address, never the display name (two people may share a name).
    m = re.search(r"<([^>]+)>|([\w.+-]+@[\w.-]+)", sender)
    actor = "email:" + (m.group(1) or m.group(2)).lower() if m else None
    text_hash, flagged, n_chars = hashlib.sha256(), False, 0
    attachments = []
    for path, part in _walk(msg.get("payload") or {}):
        if part.get("filename"):
            attachments.append((path, part))
        elif (part.get("mimeType") or "").startswith("text/") and (part.get("body") or {}).get("data"):
            raw = part["body"]["data"]
            text_hash.update(raw.encode())
            n_chars += len(raw)
            flagged = flagged or bool(_INJECTION.search(raw))
    common = {"org_id": org_id, "source_system": "gmail", "source_instance": account, "project_ids": list(project_ids),
              "security_label": security_label, "visibility_scope": "private", "occurred_at": occurred,
              "correlation_id": tid, "actor_ref": actor}
    anchor = {"source_ref": f"gmail:{account}:{mid}", "anchor_type": "email_part"}
    events = [dict(common, source_id=mid, source_version=str(msg.get("historyId", "")), event_type="email.received",
                   object_ref=f"email:{mid}", title=(h.get("subject") or "(no subject)")[:200], category="email",
                   payload={"thread_id": tid, "message_id_header": h.get("message-id"), "body_sha256": text_hash.hexdigest(),
                            "body_chars": n_chars, "attachment_count": len(attachments), "untrusted": True,
                            "instruction_like_text": flagged, "labels": sorted(msg.get("labelIds") or [])},
                   provenance_refs=[dict(anchor, anchor_value="1")])]
    for path, part in attachments:
        body = part.get("body") or {}
        events.append(dict(common, source_id=f"{mid}:{path}", source_version=str(msg.get("historyId", "")),
                           event_type="email.attachment", object_ref=f"attachment:{mid}:{path}", category="artifact",
                           title=f"attachment {part['filename']}"[:200], causation_id=mid,
                           payload={"filename": part["filename"], "declared_type": part.get("mimeType"),
                                    "size": body.get("size"), "attachment_id": body.get("attachmentId"), "untrusted": True},
                           provenance_refs=[dict(anchor, anchor_value=path)]))
    return events


def explicit_links(msg: dict, *, org_id: str, project_ids: tuple[str, ...], account: str) -> list[dict]:
    """Relationship candidates from issue/PR URLs literally present in the text. Explicit, not inferred."""
    out, seen = [], set()
    for path, part in _walk(msg.get("payload") or {}):
        raw = (part.get("body") or {}).get("data") or ""
        for repo, kind, num in _ISSUE_URL.findall(raw):
            ref = f"{'pr' if kind == 'pull' else 'issue'}:{repo}#{num}"
            if ref in seen:
                continue
            seen.add(ref)
            out.append({"org_id": org_id, "project_ids": list(project_ids), "from_ref": f"email:{msg['id']}", "to_ref": ref,
                        "predicate": "mentions", "derivation": "explicit", "security_label": "confidential",
                        "evidence_refs": [{"source_ref": f"gmail:{account}:{msg['id']}", "anchor_type": "email_part",
                                           "anchor_value": path}]})
    return out
