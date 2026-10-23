"""Bounded same-user metadata/probe helper, executed on each target host.

Only sanitized metadata leaves the host; provider credentials and conversation
content remain local. This helper never edits a transcript or controls a process.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODEL_ID = re.compile(r"[A-Za-z0-9_.:-]+\Z")
OUTAGE = re.compile(
    r"timeout|timed out|stream.*error|stream ended without (?:a )?finish[_ ]reason|connection|overload|unavailable|no.*capacity|503|502|429|rate.limit|fetch failed|not.found|unknown.model",
    re.I,
)


def owned_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("same-user regular file required")
    return path


def read_json(path):
    owned_file(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("metadata exceeds bounded read")
    return json.loads(path.read_text())


def process_birth(pid):
    if type(pid) is not int or pid < 1:
        raise ValueError("invalid process")
    proc = Path("/proc") / str(pid)
    if proc.exists():
        if proc.stat().st_uid != os.getuid():
            raise ValueError("process belongs to another user")
        return (proc / "stat").read_text().rsplit(")", 1)[1].split()[19]
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "uid=", "-o", "lstart="],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    uid, birth = result.stdout.strip().split(maxsplit=1)
    if int(uid) != os.getuid():
        raise ValueError("process belongs to another user")
    return birth


def recovery_progress(entries, baseline):
    """Content-free evidence from a new assistant descending from the recorded turn."""
    result = {
        "baselinePresent": False,
        "baselineAncestor": False,
        "assistant": None,
        "successfulToolResults": [],
    }
    indexed = {}
    positions = {}
    for index, entry in enumerate(entries):
        entry_id = entry.get("id")
        if entry.get("type") == "session" or not isinstance(entry_id, str):
            continue
        if entry_id in indexed:
            return result
        indexed[entry_id] = entry
        positions[entry_id] = index
    if not isinstance(baseline, str) or baseline not in indexed:
        return result
    result["baselinePresent"] = True
    assistants = [
        e
        for e in entries
        if e.get("type") == "message"
        and e.get("message", {}).get("role") == "assistant"
        and e.get("id") in positions
        and positions[e["id"]] > positions[baseline]
    ]
    if not assistants:
        result["baselineAncestor"] = True
        return result
    latest = assistants[-1]

    def descends(entry, ancestor):
        while entry.get("id") != ancestor:
            parent = entry.get("parentId")
            if parent not in indexed or positions[parent] >= positions.get(entry.get("id"), -1):
                return False
            entry = indexed[parent]
        return True

    result["baselineAncestor"] = descends(latest, baseline)
    if not result["baselineAncestor"]:
        return result
    message = latest["message"]

    def identifier(value):
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,256}", value)
            else None
        )

    result["assistant"] = {
        "id": identifier(latest["id"]),
        "provider": identifier(message.get("provider")),
        "model": identifier(message.get("model")),
        "stopReason": message.get("stopReason")
        if message.get("stopReason") in ("stop", "error", "toolUse", "aborted", "length")
        else "unknown",
    }
    calls = (
        {
            p.get("id")
            for p in message.get("content", [])
            if isinstance(p, dict) and p.get("type") == "toolCall"
        }
        if isinstance(message.get("content"), list)
        else set()
    )
    for entry in entries[positions[latest["id"]] + 1 :]:
        tool = entry.get("message", {})
        if (
            entry.get("type") == "message"
            and tool.get("role") == "toolResult"
            and tool.get("isError") is False
            and tool.get("toolCallId") in calls
            and identifier(entry.get("id"))
            and descends(entry, latest["id"])
        ):
            result["successfulToolResults"].append(entry["id"])
    result["successfulToolResults"] = result["successfulToolResults"][-20:]
    return result


def transcript_metadata(entries, config):
    header = entries[0]
    model = None
    provider = None
    last_message = None
    vision = False
    usage = 0
    pending_calls = set()
    unsafe_error_turn = False
    branched = False
    previous_id = None
    for entry in entries:
        if entry.get("type") != "session" and entry.get("id"):
            if previous_id is not None and entry.get("parentId") != previous_id:
                branched = True
            previous_id = entry["id"]
        if entry.get("type") == "model_change":
            model, provider = entry.get("modelId"), entry.get("provider")
        message = entry.get("message")
        if entry.get("type") == "message" and isinstance(message, dict):
            if message.get("role") in ("user", "assistant"):
                last_message = entry
                unsafe_error_turn = False
            if message.get("role") == "toolResult":
                pending_calls.discard(message.get("toolCallId"))
            for part in (
                message.get("content", []) if isinstance(message.get("content"), list) else []
            ):
                if isinstance(part, dict) and part.get("type") == "image":
                    vision = True
                if isinstance(part, dict) and part.get("type") == "toolCall":
                    pending_calls.add(part.get("id"))
                    if message.get("stopReason") == "error":
                        unsafe_error_turn = True
            total = message.get("usage", {}).get("totalTokens", 0)
            if type(total) is int:
                usage = max(usage, total)
    providers = config.get("providers", {})
    selected = next(
        (m for m in providers.get(provider, {}).get("models", []) if m.get("id") == model), None
    )
    message = (last_message or {}).get("message", {})
    failure = (
        "inference"
        if message.get("role") == "assistant"
        and message.get("stopReason") == "error"
        and OUTAGE.search(str(message.get("errorMessage", "")))
        else None
    )
    failure_evidence = "current_inference_error" if failure else "no_current_inference_error"
    if pending_calls or unsafe_error_turn or branched:
        failure = None
        failure_evidence = (
            "unreconciled_tool_calls"
            if pending_calls
            else "error_turn_tool_calls"
            if unsafe_error_turn
            else "ambiguous_branch_history"
        )
    return {
        "sessionId": header.get("id"),
        "model": model,
        "provider": provider,
        "requiredContext": max(selected["contextWindow"], usage + 8192)
        if selected and type(selected.get("contextWindow")) is int
        else None,
        "requiresVision": vision,
        "requiresReasoning": selected.get("reasoning") is True if selected else True,
        "lastAssistantId": last_message.get("id") if last_message else None,
        "failure": failure,
        "failureEvidence": failure_evidence,
        "models": [
            {k: m.get(k) for k in ("id", "contextWindow", "input", "reasoning")}
            for m in providers.get("metabolomics", {}).get("models", [])
        ],
    }


def inspect(home, pid, session_id, shell_pid=None, native_path=None, baseline=None):
    if not re.fullmatch(r"[0-9a-f-]{36}", session_id):
        raise ValueError("invalid conversation identity")
    birth = process_birth(pid)
    root = home / ".pi/agent/sessions"
    if native_path is not None:
        path = Path(native_path)
        if (
            not path.is_absolute()
            or not path.resolve().is_relative_to(root.resolve())
            or path.suffix != ".jsonl"
        ):
            raise ValueError("conversation outside bounded same-user store")
        matches = [path]
    else:
        matches = list(root.glob("*/*_" + session_id + ".jsonl"))
    if len(matches) != 1:
        raise ValueError("conversation file missing or ambiguous")
    path = owned_file(matches[0])
    if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("conversation outside bounded same-user store")
    entries = []
    with path.open() as stream:
        for line in stream:
            entries.append(json.loads(line))
    if not entries or entries[0].get("type") != "session" or entries[0].get("id") != session_id:
        raise ValueError("conversation header mismatch")
    metadata = transcript_metadata(entries, read_json(home / ".pi/agent/models.json"))
    if process_birth(pid) != birth:
        raise ValueError("process changed during metadata read")
    return {
        **metadata,
        "owned": True,
        "uid": os.getuid(),
        "birth": birth,
        "sessionPath": str(path),
        "sessionCwd": entries[0].get("cwd"),
        "progressEvidence": recovery_progress(entries, baseline) if baseline is not None else None,
    }


def screen(session, pane, expected_model=None):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", session) or not re.fullmatch(
        r"[A-Za-z0-9_.:-]+", pane
    ):
        raise ValueError("invalid native target")
    result = subprocess.run(
        [
            "herdr",
            "--session",
            session,
            "agent",
            "read",
            pane,
            "--source",
            "visible",
            "--lines",
            "35",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout)
    full_text = text
    lines = text.splitlines()
    # Pi's split view appends a sidebar. Only a repeated, aligned editor border
    # can establish its boundary; a user-entered separator is not sufficient.
    columns = {}
    for index, line in enumerate(lines):
        for column, char in enumerate(line):
            if char in "│┃" and re.fullmatch(r"\s*[─━]{10,}\s*", line[:column]):
                columns.setdefault(column, []).append(index)
    aligned = [(column, rows) for column, rows in columns.items() if len(rows) >= 2]
    if len(aligned) == 1:
        column, rows = aligned[0]
        if all(
            len(line) > column and line[column] in "│┃" for line in lines[rows[-2] : rows[-1] + 1]
        ):
            text = "\n".join(line[:column] for line in lines)
    acknowledgements = re.findall(
        r"(?:^|\n)\s*Model:\s*((?:metabolomics/)?[A-Za-z0-9_.:-]+)\s*(?:\n|$)", text
    )
    if isinstance(expected_model, str) and MODEL_ID.fullmatch(expected_model):
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = re.fullmatch(r"\s*Model:\s*((?:metabolomics/)?[A-Za-z0-9_.:-]+)\s*", line)
            if not match:
                continue
            value = match[1]
            for following in lines[index + 1 : index + 4]:
                if value in (expected_model, "metabolomics/" + expected_model):
                    acknowledgements.append(value)
                    break
                if not re.fullmatch(r"[A-Za-z0-9_.:-]+", following.strip()):
                    break
                value += following.strip()
            if value in (expected_model, "metabolomics/" + expected_model):
                acknowledgements.append(value)
    model_picker = bool(
        re.search(
            r"select.*model|choose.*model|tab.*scope.*all/scoped|enter to select", full_text, re.I
        )
    )
    borders = [
        i for i, line in enumerate(text.splitlines()) if re.fullmatch(r"\s*[─━]{10,}\s*", line)
    ]
    blank_prompt = len(borders) >= 2 and all(
        not line.strip() for line in text.splitlines()[borders[-2] + 1 : borders[-1]]
    )
    picker = bool(
        re.search(
            r"select.*model|choose.*model|allow.*deny|approve|permission required|esc to cancel|enter to select",
            full_text,
            re.I,
        )
    )
    busy = bool(
        re.search(
            r"(?:^|\n)\s*(?:[─━]+\s*)?(?:[^\w\s]\s*)?(?:Working\s*[·•]|Compacting[.…])|esc to interrupt",
            full_text,
            re.I,
        )
    )
    operator_paused = bool(re.search(r"PAUSED\s*\(operator\)|operator[- ]paused", full_text, re.I))
    return {
        "modelAck": acknowledgements[-1] if acknowledgements else None,
        "operatorPausedUI": operator_paused,
        "reloadAck": "Reloadedkeybindings,extensions,skills,prompts,themes,andcontextfiles"
        in re.sub(r"\s+", "", text),
        "busyUI": busy,
        "promptReady": blank_prompt
        and not picker
        and not model_picker
        and not busy
        and not operator_paused,
        "modelPicker": model_picker,
        "modelScopeBlocked": model_picker and bool(re.search(r"no matching models", text, re.I)),
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(home, model, opener=None):
    if not MODEL_ID.fullmatch(model):
        return {"healthy": False}
    provider = (
        read_json(home / ".pi/agent/models.json").get("providers", {}).get("metabolomics", {})
    )
    if not any(m.get("id") == model for m in provider.get("models", [])):
        return {"healthy": False}
    base = provider.get("baseUrl", "")
    if not base.startswith("https://") or not base.endswith("/v1"):
        return {"healthy": False}
    key = provider.get("apiKey")
    if not isinstance(key, str) or key.startswith("!"):
        return {"healthy": False}
    key = os.environ.get(key, key)
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Call recovery_health with ok=true."}],
        "max_tokens": 512,
        "stream": False,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "recovery_health",
                    "description": "Confirm inference health",
                    "parameters": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                },
            }
        ],
        "tool_choice": "required",
    }
    opener = opener or urllib.request.build_opener(NoRedirect()).open
    request = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(body).encode(), headers=headers
    )
    try:
        with opener(request, timeout=30) as response:
            data = json.loads(response.read(1024 * 1024))
        choices = data.get("choices", [])
        calls = choices[0].get("message", {}).get("tool_calls", []) if choices else []
        healthy = bool(
            choices and choices[0].get("finish_reason") in ("tool_calls", "stop")
        ) and any(
            c.get("function", {}).get("name") == "recovery_health"
            and json.loads(c["function"].get("arguments", "null")) == {"ok": True}
            for c in calls
        )
        return {
            "healthy": healthy,
            "model": model,
            "evidence": "validated_tool_call" if healthy else "invalid_completion",
        }
    except urllib.error.HTTPError as error:
        return {"healthy": False, "model": model, "evidence": "http_" + str(error.code)}
    except TimeoutError:
        return {"healthy": False, "model": model, "evidence": "timeout"}
    except OSError:
        return {"healthy": False, "model": model, "evidence": "transport_error"}
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return {"healthy": False, "model": model, "evidence": "invalid_completion"}


def main():
    action, fields = sys.argv[1], json.loads(sys.argv[2])
    home = Path.home()
    try:
        if action == "root":
            installed = read_json(home / ".local/share/herdr-engineering/pi/installed.json")
            root = Path(installed["sourceRoot"])
            expected = home / ".local/share/herdr-engineering/pi/releases" / installed["revision"]
            if root != expected or not (root / "scripts/pi-engineering.ts").is_file():
                raise ValueError("installed runtime root unavailable")
            result = {"root": str(root)}
        elif action == "inspect":
            result = inspect(
                home,
                fields["pid"],
                fields["sessionId"],
                fields.get("shellPid"),
                fields.get("nativePath"),
                fields.get("baseline"),
            )
        elif action == "screen":
            result = screen(fields["session"], fields["pane"], fields.get("expectedModel"))
        elif action == "probe":
            result = probe(home, fields["model"])
        else:
            raise ValueError("unsupported metadata operation")
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        codes = {
            "conversation file missing or ambiguous": "transcript_missing_or_ambiguous",
            "conversation header mismatch": "transcript_header_mismatch",
            "conversation outside bounded same-user store": "transcript_scope_or_size",
            "process belongs to another user": "process_owner_mismatch",
            "process changed during metadata read": "process_changed",
        }
        code = codes.get(str(error), "metadata_unavailable")
        if isinstance(error, json.JSONDecodeError):
            code = "metadata_invalid_json"
        print(json.dumps({"error": code}))


if __name__ == "__main__":
    main()
