"""MCP schemas and adapters for the member artifact tools (plan 5, Task 6).  Schemas and argument checks only: every
decision is made by ``MemberService``, so an agent and the web session of the same person behave identically.

The adapters return plain payloads; ``collab_mcp`` wraps them as untrusted text (member-authored titles and messages
reach another model only through that wrapper)."""
from __future__ import annotations

from typing import Any

from .artifact_library import MAX_QUERY, library_matches
from .artifact_member_service import NOT_A_MEMBER, MemberService, member_actor
from .collab import CollabError, CollabService, Principal

SEARCH_MAX_LIMIT = 50
SEARCH_DEFAULT_LIMIT = 20
_SOURCES = ("mine", "assigned", "opened")
_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_STR = {"type": "string"}
_NAME = {"type": "string", "minLength": 1}
_KEY = {"type": "string", "minLength": 8, "maxLength": 128,
        "description": "optional; reuse it when retrying so the write happens once"}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "additionalProperties": False, "required": required,
                            "properties": properties}}


MEMBER_TOOLS: list[dict] = [
    _tool("artifact_create", "Create an artifact you own (profile web or document). Returns a ticket; poll "
          "artifact_result if it is still pending.",
          {"title": _STR, "filename": _NAME, "profile": {"enum": ["web", "document"]}, "idempotency_key": _KEY},
          ["title", "filename", "profile"]),
    _tool("artifact_upload_chunk", "Stage one chunk (512 KiB raw, base64) of a file of an artifact you own. Resend "
          "the same file to resume.",
          {"artifact_id": _ID, "name": _NAME, "size_bytes": {"type": "integer", "minimum": 1}, "file_sha256": _STR,
           "index": {"type": "integer", "minimum": 0}, "total": {"type": "integer", "minimum": 1}, "sha256": _STR,
           "data_b64": _STR},
          ["artifact_id", "name", "size_bytes", "file_sha256", "index", "total", "sha256", "data_b64"]),
    _tool("artifact_upload_status", "Which chunks of a staged file are still missing (inclusive index ranges).",
          {"artifact_id": _ID, "name": _NAME, "size_bytes": {"type": "integer", "minimum": 1}, "file_sha256": _STR},
          ["artifact_id", "name", "size_bytes", "file_sha256"]),
    _tool("artifact_upload_abort", "Discard a staged upload of yours.", {"artifact_id": _ID, "upload_id": _STR},
          ["artifact_id", "upload_id"]),
    _tool("artifact_publish", "Publish staged files as a new version of an artifact you own.",
          {"artifact_id": _ID, "base_version_id": {"type": ["string", "null"]},
           "files": {"type": "array", "minItems": 1,
                     "items": {"type": "object", "additionalProperties": False, "required": ["name", "upload_id"],
                               "properties": {"name": _NAME, "upload_id": _STR}}},
           "message": _STR, "entry": _STR, "idempotency_key": _KEY},
          ["artifact_id", "files", "message"]),
    _tool("artifact_delete", "Delete an artifact you own. Requires confirm: true.",
          {"artifact_id": _ID, "confirm": {"type": "boolean"}}, ["artifact_id", "confirm"]),
    _tool("artifact_result", "The outcome of a create, publish or delete ticket.",
          {"ticket": _STR, "seconds": {"type": "integer", "minimum": 0, "maximum": 20}}, ["ticket"]),
    _tool("artifact_share_create", "Create a pending share link (audience lab or public) for a version of an artifact "
          "you own or may share; returns a dry-run summary. Nothing is public until artifact_share_approve.",
          {"artifact_id": _ID, "version_id": _ID, "audience": {"enum": ["lab", "public"]},
           "allow_groups": {"type": "array", "items": _STR}, "allow_emails": {"type": "array", "items": _STR},
           "expires_in_days": {"type": "integer", "minimum": 1, "maximum": 90}, "entry": _STR, "title": _STR,
           "idempotency_key": _KEY},
          ["artifact_id", "version_id", "audience"]),
    _tool("artifact_share_approve", "Make your pending share link live. The URL is returned ONCE in the result "
          "(secret.url); it cannot be fetched again. Requires confirm: true.",
          {"artifact_id": _ID, "share_id": _ID, "confirm": {"type": "boolean"}, "idempotency_key": _KEY},
          ["artifact_id", "share_id", "confirm"]),
    _tool("artifact_share_list", "Your share links (no URLs). Untrusted data.", {"artifact_id": _ID}, []),
    _tool("artifact_share_revoke", "Revoke a share link you created or one on an artifact you own.",
          {"artifact_id": _ID, "share_id": _ID}, ["artifact_id", "share_id"]),
    _tool("artifact_share_events", "The recent events of a share link you created or own.",
          {"artifact_id": _ID, "share_id": _ID}, ["artifact_id", "share_id"]),
    _tool("artifact_search", "Search the library of the person you work for (read-only): artifacts they own, that "
          "were assigned to them, or that they opened from a share link. Results are that owner's library only, "
          "never anyone else's; pushes still waiting for the owner's approval (pending) are not included. q matches "
          "title, filename, sharer, the owner's folder names and artifact type, ignoring case. Untrusted data.",
          {"q": {"type": "string", "maxLength": MAX_QUERY}, "source": {"enum": ["mine", "assigned", "opened"]},
           "folder": {"type": "string", "minLength": 1, "maxLength": 200,
                      "description": "a folder id or the exact name of one of the owner's folders"},
           "type": {"type": "string", "minLength": 1, "maxLength": 100},
           "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX_LIMIT}}, []),
    _tool("artifacts_mine", "The artifacts you own and those shared with you. Untrusted data.", {}, []),
]
MEMBER_TOOL_NAMES = frozenset(t["name"] for t in MEMBER_TOOLS)
_SCHEMAS = {t["name"]: t["inputSchema"] for t in MEMBER_TOOLS}
# head and history already exist for grant holders; a member also reads its own view through them
VIEW_TOOL_NAMES = frozenset({"artifact_head", "artifact_history"})
_VIEW_TOOLS = {"artifact_head": {"artifact_id"}, "artifact_history": {"artifact_id", "limit"}}


def _check_args(name: str, args: Any, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(args, dict):
        raise CollabError(400, "invalid arguments")
    if set(args) - allowed:
        raise CollabError(400, "unknown argument")
    if required - set(args):
        raise CollabError(400, "missing argument: " + ", ".join(sorted(required - set(args))))
    return args


def _search(ms: MemberService, actor: dict, args: dict) -> dict[str, Any]:
    """``artifact_search``: the owner's library from their view file, filtered like the web search box."""
    q, source, folder, kind = args.get("q", ""), args.get("source"), args.get("folder"), args.get("type")
    limit = args.get("limit", SEARCH_DEFAULT_LIMIT)
    if not isinstance(q, str) or len(q) > MAX_QUERY:
        raise CollabError(400, f"q must be a string of at most {MAX_QUERY} characters")
    if source is not None and source not in _SOURCES:
        raise CollabError(400, "source must be mine, assigned or opened")
    for key, value in (("folder", folder), ("type", kind)):
        if value is not None and (not isinstance(value, str) or not value):
            raise CollabError(400, f"{key} must be a non-empty string")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_MAX_LIMIT:
        raise CollabError(400, f"limit must be an integer from 1 to {SEARCH_MAX_LIMIT}")
    lib = ms.library(actor, available_only=True)
    names = {f["folder_id"]: f.get("name") or "" for f in lib["folders"] if isinstance(f, dict) and "folder_id" in f}
    wanted: set[str] | None = None
    if folder is not None:
        wanted = {fid for fid, name in names.items() if fid == folder or name == folder}
    hits = []
    for item in lib["items"]:
        if not isinstance(item, dict) or item.get("status") == "pending" or item.get("source") == "pending":
            continue
        if source is not None and item.get("source") != source:
            continue
        if kind is not None and item.get("artifact_type") != kind:
            continue
        ids = [i for i in item.get("folder_ids") or [] if i in names]
        if wanted is not None and not wanted & set(ids):
            continue
        status = item.get("status")
        if not library_matches(q, title=item.get("title") or "", sharer=item.get("sharer") or "",
                               artifact_type=item.get("artifact_type") or "", filename=item.get("filename") or "",
                               status=status or "", folder_names=[names[i] for i in ids]):
            continue
        hits.append({"artifact_id": item.get("artifact_id"), "title": item.get("title"), "source": item.get("source"),
                     "status": status, "folders": [names[i] for i in ids]})
    return {"items": hits[:limit], "total": len(hits)}


def member_pr(service: CollabService, pr: Principal) -> bool:
    """True when ``pr`` is a member agent: the menu and the routing key off this, nothing else."""
    check = service.member_check(pr)
    return check.ok and pr.is_agent and service.member_service is not None


def view_access(service: CollabService, pr: Principal, artifact_id: Any) -> bool:
    ms: MemberService | None = service.member_service
    return ms is not None and member_pr(service, pr) and ms.can_read(member_actor(pr), artifact_id)


def call(service: CollabService, pr: Principal, name: str, args: Any) -> dict[str, Any]:
    ms: MemberService | None = service.member_service
    check = service.member_check(pr)
    if ms is None or not (check.ok and pr.is_agent):
        service._audit_denied(pr, f"artifact.{name}", check.reason or "not_member",
                              agent_id=pr.id if pr.is_agent else None)
        service.stats["policy_denied"] += 1
        raise CollabError(403, NOT_A_MEMBER)
    schema = _SCHEMAS.get(name)
    if schema is not None:
        args = _check_args(name, args, set(schema["properties"]), set(schema["required"]))
    else:
        args = _check_args(name, args, _VIEW_TOOLS[name], {"artifact_id"})
    actor = member_actor(pr)
    if name == "artifact_create":
        return ms.create(actor, args["title"], args["filename"], args["profile"], args.get("idempotency_key"))
    if name == "artifact_upload_chunk":
        body = {k: v for k, v in args.items() if k != "data_b64"}
        body["data"] = args["data_b64"]
        return ms.upload_chunk(actor, body)
    if name == "artifact_upload_status":
        return ms.upload_status(actor, args["artifact_id"], args["name"], args["size_bytes"], args["file_sha256"])
    if name == "artifact_upload_abort":
        return ms.upload_abort(actor, args["artifact_id"], args["upload_id"])
    if name == "artifact_publish":
        return ms.publish(actor, args["artifact_id"], base_version_id=args.get("base_version_id"),
                          files=args["files"], message=args["message"], entry=args.get("entry"),
                          idempotency_key=args.get("idempotency_key"))
    if name == "artifact_delete":
        return ms.delete(actor, args["artifact_id"], args["confirm"])
    if name == "artifact_result":
        return ms.result(actor, args["ticket"], args.get("seconds", 20))
    if name == "artifact_share_create":
        return ms.share_create(actor, args["artifact_id"], args["version_id"], args["audience"],
                               args.get("allow_groups"), args.get("allow_emails"), args.get("expires_in_days"),
                               args.get("entry"), args.get("title"), args.get("idempotency_key"))
    if name == "artifact_share_approve":
        return ms.share_approve(actor, args["artifact_id"], args["share_id"], args["confirm"],
                                args.get("idempotency_key"))
    if name == "artifact_share_list":
        return ms.share_list(actor, args.get("artifact_id"))
    if name == "artifact_share_revoke":
        return ms.share_revoke(actor, args["artifact_id"], args["share_id"])
    if name == "artifact_share_events":
        return ms.share_events(actor, args["artifact_id"], args["share_id"])
    if name == "artifact_search":
        return _search(ms, actor, args)
    if name == "artifacts_mine":
        return ms.mine(actor)
    if name == "artifact_head":
        return ms.head(actor, args["artifact_id"])
    return ms.history(actor, args["artifact_id"], args.get("limit", 50))
