"""Conservative outage recovery through native HerdR and Pi control surfaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .fleet_rollout import (
    PiTransport,
    RolloutStore,
    _foreground_identity,
    _live,
    _operation,
    _rows,
    _target,
    inventory_targets,
)

CONTINUE = (
    "Continue the previously requested unfinished work after the inference outage. "
    "Resume existing mission IDs from this conversation; do not create duplicate missions. "
    "First reconcile the existing transcript and completed tool actions; do not repeat "
    "completed side effects. Preserve operator pauses, cancellations and approval gates."
)
IDENTITY = ("paneId", "terminalId", "sessionId", "instanceId", "pid", "birth", "uid")
TERMINAL = {"DONE", "COMPLETE", "COMPLETED", "CANCELLED", "CANCELED"}


class EvidenceError(ValueError):
    def __init__(self, code):
        self.code = (
            code
            if code
            in {
                "transcript_missing_or_ambiguous",
                "transcript_header_mismatch",
                "transcript_scope_or_size",
                "process_owner_mismatch",
                "process_changed",
                "metadata_unavailable",
                "metadata_invalid_json",
                "canonical_projection_unavailable",
            }
            else "metadata_unavailable"
        )
        super().__init__(self.code)


def error_code(error):
    if isinstance(error, EvidenceError):
        return error.code
    codes = {
        "session/process ownership changed": "session_identity_changed",
        "owned mission state changed since planning": "mission_fence_changed",
        "conversation changed since recovery planning": "conversation_changed",
        "no compatible model passed live tool-call probe": "no_healthy_compatible_model",
        "foreground PID does not identify exactly one live Pi control instance": "control_instance_unavailable_or_ambiguous",
        "native pane identity changed during foreground verification": "native_identity_changed",
        "pane changed while reading recovery metadata": "native_identity_changed",
        "current runtime or UI reports active/paused work": "session_became_busy_or_paused",
        "session no longer at a settled prompt": "session_not_settled",
        "model picker scope excludes selected model": "model_scope_blocked",
    }
    if str(error) in codes:
        return codes[str(error)]
    if isinstance(error, TimeoutError):
        return "control_timeout"
    if isinstance(error, ConnectionError):
        return "control_transport_failure"
    return "evidence_unavailable"


class RecoveryStore(RolloutStore):
    def __init__(self, root=None):
        super().__init__(root or Path.home() / ".local/state/herdr-engineering/pi-recovery")

    def read(self, run_id):
        path = self._path(run_id)
        if path.is_symlink():
            raise ValueError("recovery ledger must not be a symlink")
        data = json.loads(path.read_text())
        if data.get("id") != run_id or data.get("schema") != 1:
            raise ValueError("invalid recovery ledger")
        return data

    def write(self, data):
        # Reuse fsync + atomic replacement; parent validates immutable rollout fields.
        data.setdefault("revision", "0" * 40)
        data.setdefault("inventory", "native-pi-recovery")
        data.setdefault("hosts", {})
        super().write(data)

    def journal(self):
        path = self._path("0" * 32)
        if path.is_symlink():
            raise ValueError("recovery journal must not be a symlink")
        if not path.exists():
            return {"id": "0" * 32, "schema": 1, "attempts": {}, "nextEligibleAt": 0}
        return self.read("0" * 32)

    def save_journal(self, data):
        self.write(data)


class RecoveryTransport(PiTransport):
    def __init__(self, runner=None):
        super().__init__(runner)
        self._plan_cache = None

    def host(self, target, action, **fields):
        source = Path(__file__).with_name("pi_recovery_host.py").read_text()
        if action == "inspect":
            projection = Path(__file__).with_name("pi_recovery_projection.mjs").read_text()
            source = source.replace(
                'if __name__ == "__main__":',
                "CANONICAL_PROJECTION_SCRIPT = "
                + repr(projection)
                + '\nif __name__ == "__main__":',
            )
        result = self._json(target, ["python3", "-c", source, action, json.dumps(fields)])
        if result.get("error"):
            raise EvidenceError(result["error"])
        return result

    def sessions(self, target, root):
        # The stock CLI uses a 1.5s status timeout; owned mission refresh can take longer.
        script = "import {pathToFileURL} from 'node:url'; const m=await import(pathToFileURL(process.argv[1]+'/src/sessionControl/SessionControl.ts').href); console.log(JSON.stringify(await m.queryAllSessions(undefined,{fullStatus:true,timeoutMs:20000,concurrency:32})));"
        key = json.dumps([target, root], sort_keys=True)
        if self._plan_cache is not None and key in self._plan_cache:
            return self._plan_cache[key]
        rows = self._json(target, ["node", "--input-type=module", "-e", script, root])
        if self._plan_cache is not None:
            self._plan_cache[key] = rows
        return rows

    def target_sessions(self, target, root, pids):
        # Use the native same-user client, scoped to foreground processes already
        # attested by HerdR. Do not hydrate every other session during apply.
        script = "import {pathToFileURL} from 'node:url'; const m=await import(pathToFileURL(process.argv[1]+'/src/sessionControl/SessionControl.ts').href); const pids=new Set(JSON.parse(process.argv[2])); const ds=(await m.discoverSessions()).filter(d=>pids.has(d.pid)); console.log(JSON.stringify(await Promise.all(ds.map(async descriptor=>{try{const reply=await m.requestSession(descriptor,{version:1,op:'status'},20000); return {descriptor,reachable:reply.ok&&reply.data.instanceId===descriptor.instanceId,reply};}catch{return {descriptor,reachable:false,error:'session_unresponsive'};}}))));"
        return self._json(
            target, ["node", "--input-type=module", "-e", script, root, json.dumps(pids)]
        )

    def observe(self, target, pane, *, baseline=None):
        root = self.host(target, "root")["root"]
        agents = [a for a in self.native(target) if a.get("pane_id") == pane]
        if len(agents) != 1 or agents[0].get("agent") != "pi":
            raise ValueError("native Pi pane identity unavailable")
        agent = agents[0]
        process = self.process_info(target, pane)
        pids = sorted(
            {
                p["pid"]
                for p in process["foreground_processes"]
                if type(p.get("pid")) is int and p["pid"] > 0
            }
        )
        rows = _rows(self, target, root) if self._plan_cache is not None else []
        matches = [
            r for r in rows if r["descriptor"].get("pid") in pids and r.get("reachable") is True
        ]
        if len(matches) != 1:
            rows = self.target_sessions(target, root, pids)
            if not isinstance(rows, list) or any(
                not isinstance(r, dict) or not isinstance(r.get("descriptor"), dict) for r in rows
            ):
                raise ValueError("invalid Pi session inventory")
        session, proof = _foreground_identity(self, target, agent, rows)
        descriptor, data = _live(rows, session, proof["pid"])
        process = self.process_info(target, pane)
        reference = agent.get("agent_session") or {}
        metadata = self.host(
            target,
            "inspect",
            pid=proof["pid"],
            sessionId=session,
            shellPid=process.get("shell_pid"),
            nativePath=reference.get("value") if reference.get("kind") == "path" else None,
            baseline=baseline,
        )
        metadata.update(
            self.host(
                target,
                "screen",
                session=target["session"],
                pane=pane,
                expectedModel=metadata.get("model"),
            )
        )
        if not metadata.get("birth") or not metadata.get("owned"):
            raise ValueError("same-user process birth proof unavailable")
        # Host metadata read must not cross a pane occupant change.
        fresh = [a for a in self.native(target) if a.get("pane_id") == pane]
        if len(fresh) != 1 or any(
            fresh[0].get(k) != agent.get(k) for k in ("terminal_id", "revision", "state_change_seq")
        ):
            raise ValueError("pane changed while reading recovery metadata")
        process = self.process_info(target, pane)
        return {
            **metadata,
            "shellPid": process.get("shell_pid"),
            "foregroundCwd": agent.get("foreground_cwd"),
            "paneId": pane,
            "terminalId": proof["terminalId"],
            "sessionId": session,
            "instanceId": descriptor["instanceId"],
            "pid": proof["pid"],
            "agentStatus": agent.get("agent_status"),
            "currentTool": data.get("currentTool"),
            "runtime": data.get("runtime", {}),
            "missions": data.get("missions") if data.get("missionSource") == "runtime" else None,
            "sourceRoot": root,
            "installedCommit": Path(root).name,
        }

    def probe(self, target, model):
        self.last_probe = self.host(target, "probe", model=model["id"])
        return self.last_probe.get("healthy") is True

    def _native_action(self, target, pane, arguments, expected_type):
        reply = self._json(target, ["herdr", "--session", target["session"], *arguments])
        result = reply.get("result", {})
        if (
            reply.get("error")
            or result.get("type") != expected_type
            or result.get("agent", {}).get("pane_id") != pane
        ):
            raise ValueError("native action acknowledgement unavailable")
        return result

    def command(self, target, pane, text):
        return self._native_action(target, pane, ["agent", "prompt", pane, text], "agent_prompted")

    def keys(self, target, pane, key):
        # Native send-keys returns a pane input receipt, validated separately.
        reply = self._json(
            target, ["herdr", "--session", target["session"], "agent", "send-keys", pane, key]
        )
        if reply.get("error") or reply.get("result", {}).get("type") != "ok":
            raise ValueError("native key acknowledgement unavailable")
        return reply["result"]


def refresh_interactive(transport, target, entry, current, save, sleep):
    """SDK reload preserves original flags/tools; adopt only a same-process new handler."""
    expected = entry["identity"]

    def check(allow_picker=False):
        fresh = transport.observe(target, entry["paneId"])
        if (
            any(fresh.get(k) != expected[k] for k in IDENTITY if k != "instanceId")
            or fresh.get("lastAssistantId") != entry["baseline"]
            or fresh.get("missions") != []
            or fresh.get("currentTool")
            or fresh.get("agentStatus") not in ("idle", "done")
            or fresh.get("runtime", {}).get("interactiveIdle") is False
            or fresh.get("busyUI") is True
            or fresh.get("operatorPausedUI") is True
            or (not allow_picker and fresh.get("promptReady") is not True)
        ):
            raise ValueError("same-process reload proof unavailable")
        return fresh

    if current.get("modelPicker") is True:
        if check(allow_picker=True).get("modelPicker") is not True:
            raise ValueError("model picker changed before escape")
        entry["refreshStage"] = "exit_model_picker"
        save()
        transport.keys(target, entry["paneId"], "esc")
        sleep(0.5)
    check()
    entry["refreshStage"] = "reload_submitted"
    save()
    transport.command(target, entry["paneId"], "/reload")
    sleep(1)
    fresh = check()
    if fresh.get("reloadAck") is not True:
        raise ValueError("native reload acknowledgement unavailable")
    entry["identity"] = proof(fresh)
    entry["refreshStage"] = "verified"
    save()
    return fresh


def exclusion(item, *, allow_upgrade=False):
    if item.get("operatorPausedUI") is True:
        return "current UI reports an operator pause"
    if item.get("agentStatus") not in ("idle", "done"):
        return "agent is active, blocked or unknown"
    if item.get("currentTool") or item.get("promptReady") is not True or item.get("busyUI") is True:
        return "session is not at a verified settled prompt"
    if item.get("runtime", {}).get("interactiveIdle") is False:
        return "runtime reports active interactive work"
    if any(item.get(k) is None for k in IDENTITY):
        return "missing identity proof"
    missions = item.get("missions")
    if not isinstance(missions, list) or any(not isinstance(m, dict) for m in missions):
        return "owned mission inventory unavailable"
    if any(m.get("operatorPaused") is True or m.get("status") in TERMINAL for m in missions):
        return "operator-paused or terminal mission preserved"
    if missions and (
        len(missions) != 1
        or missions[0].get("resumeEligible") is not True
        or missions[0].get("operatorPaused") is not False
    ):
        return "mission ownership or resume eligibility is ambiguous"
    mission_outage = bool(
        missions
        and re.search(
            r"inference|provider|llm|model.*(?:unavailable|not.found|timeout)|stream.*error|capacity|429|503|502",
            str(missions[0].get("lastError", "")),
            re.I,
        )
    )
    if item.get("failure") != "inference" and not mission_outage:
        return "no current inference-outage failure evidence"
    if type(item.get("requiredContext")) is not int or item["requiredContext"] <= 0:
        return "model context requirements unavailable"
    if not allow_upgrade:
        if (
            item.get("installedCommit")
            and item.get("runtime", {}).get("commit") != item["installedCommit"]
        ):
            return "loaded runtime needs upgrade before recovery"
        if missions and "mission.resume.selected-model.v1" not in item.get("runtime", {}).get(
            "capabilities", []
        ):
            return "loaded runtime lacks exact mission model recovery capability"
        if missions and item.get("runtime", {}).get("interactiveIdle") is not True:
            return "runtime has not attested interactive idleness"
    return None


def compatible(item):
    return [
        m
        for m in item.get("models", [])
        if isinstance(m.get("id"), str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]+", m["id"])
        and type(m.get("contextWindow")) is int
        and m["contextWindow"] >= item["requiredContext"]
        and (not item.get("requiresVision") or "image" in m.get("input", []))
        and (not item.get("requiresReasoning") or m.get("reasoning") is True)
    ]


def mission_fence(item):
    missions = item.get("missions")
    if not isinstance(missions, list) or any(not isinstance(m, dict) for m in missions):
        return None
    return [
        {
            key: m.get(key)
            for key in (
                "id",
                "revision",
                "resumptionGeneration",
                "operatorPaused",
                "resumeEligible",
                "status",
            )
        }
        for m in missions
    ]


def proof(item):
    return {k: item[k] for k in IDENTITY}


def _fresh(transport, target, item, *, settled=True):
    current = transport.observe(target, item["paneId"])
    if proof(current) != item["identity"]:
        raise ValueError("session/process ownership changed")
    if settled and (
        current.get("runtime", {}).get("interactiveIdle") is False
        or current.get("busyUI") is True
        or current.get("operatorPausedUI") is True
    ):
        raise ValueError("current runtime or UI reports active/paused work")
    if settled and mission_fence(current) != item.get("missionFence", []):
        raise ValueError("owned mission state changed since planning")
    if settled and current.get("lastAssistantId") != item["baseline"]:
        raise ValueError("conversation changed since recovery planning")
    if settled and (
        current.get("agentStatus") not in ("idle", "done")
        or current.get("promptReady") is not True
        or current.get("currentTool")
    ):
        raise ValueError("session no longer at a settled prompt")
    return current


def create_plan(store, targets, *, transport=None, progress=None, revision=None, inventory=None):
    transport = transport or RecoveryTransport()
    if isinstance(transport, RecoveryTransport):
        transport._plan_cache = {}
    try:
        return _create_plan(
            store,
            targets,
            transport=transport,
            progress=progress or (lambda event: None),
            revision=revision,
            inventory=inventory,
        )
    finally:
        if isinstance(transport, RecoveryTransport):
            transport._plan_cache = None


def _create_plan(store, targets, *, transport, progress, revision, inventory):
    if os.environ.get("HERDR_ENV") != "1":
        raise ValueError("Native discovery requires a Herdr-managed caller")
    if revision is not None and (not re.fullmatch(r"[0-9a-f]{40}", revision) or inventory is None):
        raise ValueError("upgrade requires an immutable 40-character revision and fleet inventory")
    transport = transport or RecoveryTransport()
    plan = {"id": uuid.uuid4().hex, "schema": 1, "status": "planned", "sessions": [], "hosts": {}}
    if revision:
        plan.update(
            desiredRevision=revision, revision=revision, inventory=str(Path(inventory).resolve())
        )
    for raw in targets:
        progress("Inspecting native Pi sessions on " + raw["host"])
        target = {**raw, "session": raw.get("session") or "default"}
        _target(target)
        plan["hosts"][target["host"]] = {"target": target}
        try:
            for agent in transport.native(target):
                if agent.get("agent") != "pi":
                    continue
                entry = {
                    "host": target["host"],
                    "paneId": agent.get("pane_id"),
                    "status": "excluded",
                }
                plan["sessions"].append(entry)
                progress("Inspecting " + target["host"] + " " + str(entry["paneId"]))
                try:
                    observed = transport.observe(target, entry["paneId"])
                    reason = exclusion(observed, allow_upgrade=bool(revision))
                    models = [] if reason else compatible(observed)
                    entry.update(
                        identity=proof(observed),
                        baseline=observed.get("lastAssistantId"),
                        missionFence=mission_fence(observed),
                        requirements={
                            key: observed.get(key)
                            for key in ("requiredContext", "requiresVision", "requiresReasoning")
                        },
                    )
                    if reason or not models:
                        entry["reason"] = reason or "no compatible configured model"
                        if reason == "loaded runtime needs upgrade before recovery":
                            entry["status"] = "bootstrap_required"
                    else:
                        entry.update(status="eligible", candidates=models)
                except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
                    entry["errorCode"] = error_code(error)
                    entry["reason"] = (
                        "native identity or session metadata unavailable ("
                        + entry["errorCode"]
                        + ")"
                    )
        except (ValueError, OSError, subprocess.SubprocessError):
            plan["hosts"][target["host"]]["error"] = "native host inventory unavailable"
    with store.lock():
        store.write(plan)
    return plan


def _key(entry, target):
    return hashlib.sha256(
        json.dumps(
            [
                target,
                entry["identity"]["sessionId"],
                entry["baseline"],
                [
                    (m.get("id"), m.get("resumptionGeneration"))
                    for m in entry.get("missionFence", [])
                ],
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def staged_root(plan, target, transport, stage, progress):
    desired = plan["desiredRevision"]
    state = plan["hosts"][target["host"]]
    if state.get("stagedRoot"):
        return state["stagedRoot"]
    resolved = [
        {**t, "session": target["session"]}
        for t in inventory_targets(Path(plan["inventory"]), target["host"])
    ]
    if resolved != [target]:
        raise ValueError("fleet inventory destination changed")
    root = transport.host(target, "root").get("root", "")
    if not root.endswith("/.local/share/herdr-engineering/pi/releases/" + desired):
        progress("Staging approved Pi revision on " + target["host"])
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutureTimeout

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                stage, desired, host=target["host"], inventory=Path(plan["inventory"])
            )
            while True:
                try:
                    report = future.result(timeout=30)
                    break
                except FutureTimeout:
                    progress("Still staging Pi revision on " + target["host"])
        if (
            report.get("ok") is not True
            or report.get("revision") != desired
            or report.get("host") != target["host"]
        ):
            raise ValueError("approved runtime staging not verified")
        root = report.get("sourceRoot", "")
    if (
        not root.startswith("/")
        or ".." in Path(root).parts
        or not root.endswith("/.local/share/herdr-engineering/pi/releases/" + desired)
    ):
        raise ValueError("invalid immutable staged runtime path")
    state["stagedRoot"] = root
    return root


def upgrade_session(plan, entry, current, target, root, transport, save, now, sleep):
    expected = dict(entry["identity"])
    entry["instanceId"] = current["instanceId"]
    for key, fields in (
        ("prepare", {}),
        (
            "reload",
            {"expectedCommit": plan["desiredRevision"], "sourceRoot": root, "timeoutMs": 30000},
        ),
    ):
        fresh = transport.observe(target, entry["paneId"])
        if (
            proof(fresh) != expected
            or fresh.get("lastAssistantId") != entry["baseline"]
            or mission_fence(fresh) != entry.get("missionFence", [])
            or exclusion(fresh, allow_upgrade=True)
        ):
            raise ValueError("session no longer eligible for runtime maintenance")
        deadline = now() + 35
        receipt = _operation(plan, entry, key, key, fields, transport, target, root, save)
        while receipt["status"] == "running" and now() < deadline:
            sleep(1)
            try:
                observed = transport.observe(target, entry["paneId"])
            except (ValueError, OSError, subprocess.SubprocessError):
                if key == "reload" and now() < deadline:
                    continue  # Read-only retry across close-old/open-new control handover.
                raise
            if (
                any(observed.get(k) != expected[k] for k in IDENTITY if k != "instanceId")
                or observed.get("lastAssistantId") != entry["baseline"]
                or mission_fence(observed) != entry.get("missionFence", [])
            ):
                raise ValueError("maintenance polling identity changed")
            if key == "reload":
                entry["instanceId"] = observed["instanceId"]
            elif observed["instanceId"] != expected["instanceId"]:
                raise ValueError("unexpected control handler replacement")
            try:
                receipt = _operation(plan, entry, key, key, fields, transport, target, root, save)
            except (ConnectionError, subprocess.TimeoutExpired):
                if key == "reload" and now() < deadline:
                    continue  # Rediscover custody, then poll the same receipt; never reissue reload.
                raise
        if receipt["status"] != "succeeded" or (
            key == "prepare" and receipt.get("result", {}).get("status") != "ready"
        ):
            raise ValueError("runtime maintenance was not completed")
    fresh = transport.observe(target, entry["paneId"])
    if (
        any(fresh.get(k) != expected[k] for k in IDENTITY if k != "instanceId")
        or fresh.get("lastAssistantId") != entry["baseline"]
        or mission_fence(fresh) != entry.get("missionFence", [])
        or fresh.get("runtime", {}).get("commit") != plan["desiredRevision"]
        or fresh.get("runtime", {}).get("pending") is not False
        or fresh.get("runtime", {}).get("interactiveIdle") is not True
        or exclusion(fresh)
    ):
        raise ValueError("upgraded runtime identity/readiness not verified")
    entry["identity"] = proof(fresh)
    entry["instanceId"] = fresh["instanceId"]
    entry["loadedCommit"] = plan["desiredRevision"]
    save()
    return fresh


def apply_recovery(
    store,
    run_id,
    *,
    transport=None,
    now=time.time,
    sleep=time.sleep,
    managed=None,
    progress=None,
    stage=None,
):
    if not (os.environ.get("HERDR_ENV") == "1" if managed is None else managed):
        raise ValueError("Recovery apply requires a Herdr-managed caller")
    transport = transport or RecoveryTransport()
    progress = progress or (lambda event: None)
    if stage is None:
        from .fleet import stage_pi_runtime

        stage = stage_pi_runtime
    with store.lock():
        plan = store.read(run_id)
        journal = store.journal()
        # An interrupted attempt has an unknown completion time: start a fresh cooldown.
        if journal.get("inFlight"):
            journal["nextEligibleAt"] = max(journal["nextEligibleAt"], now() + 60)
            journal.pop("inFlight")
            store.save_journal(journal)
        for entry in plan["sessions"]:
            if entry["status"] != "eligible":
                continue
            target = plan["hosts"][entry["host"]]["target"]
            key = _key(entry, target)
            if key in journal["attempts"]:
                entry.update(
                    status="already_attempted",
                    reason="inspect earlier attempt; automatic replay withheld",
                )
                store.write(plan)
                continue
            while now() < journal["nextEligibleAt"]:
                progress(
                    "Recovery cooldown: "
                    + str(round(journal["nextEligibleAt"] - now(), 1))
                    + " seconds remaining"
                )
                sleep(min(60, journal["nextEligibleAt"] - now()))
            progress("Checking recovery for " + entry["host"] + " " + str(entry["paneId"]))
            attempted = False
            try:
                current = _fresh(transport, target, entry)
                reason = exclusion(current, allow_upgrade=bool(plan.get("desiredRevision")))
                if reason:
                    raise ValueError(reason)
                models = compatible(current)
                model = None
                entry["modelProbes"] = []
                for candidate in models:
                    healthy = transport.probe(target, candidate)
                    detail = getattr(transport, "last_probe", {})
                    evidence = detail.get("evidence", "passed" if healthy else "failed")
                    if not isinstance(evidence, str) or not re.fullmatch(
                        r"[a-z0-9_]{1,50}", evidence
                    ):
                        evidence = "unknown"
                    entry["modelProbes"].append(
                        {"model": candidate["id"], "healthy": healthy, "evidence": evidence}
                    )
                    if healthy:
                        model = candidate
                        break
                if model is None:
                    raise ValueError("no compatible model passed live tool-call probe")
                current = _fresh(transport, target, entry)
                if exclusion(current, allow_upgrade=bool(plan.get("desiredRevision"))):
                    raise ValueError("recovery eligibility changed after model probe")
                journal["attempts"][key] = {"runId": run_id, "status": "attempted", "at": now()}
                journal["inFlight"] = key
                journal["nextEligibleAt"] = now() + 60
                store.save_journal(journal)
                attempted = True
                entry.update(status="attempted", selectedModel=model["id"])
                store.write(plan)
                if (
                    plan.get("desiredRevision")
                    and current.get("runtime", {}).get("commit") != plan["desiredRevision"]
                ):
                    root = staged_root(plan, target, transport, stage, progress)
                    store.write(plan)
                    current = upgrade_session(
                        plan,
                        entry,
                        current,
                        target,
                        root,
                        transport,
                        lambda: store.write(plan),
                        now,
                        sleep,
                    )
                    # The upgraded handler may expose stricter requirements or newer pauses.
                    if (
                        exclusion(current)
                        or not any(m["id"] == model["id"] for m in compatible(current))
                        or not transport.probe(target, model)
                    ):
                        raise ValueError("upgraded runtime/model no longer eligible")
                missions = current["missions"]
                if missions:
                    mission = missions[0]
                    if any(
                        type(mission.get(k)) is not int
                        for k in ("revision", "resumptionGeneration")
                    ):
                        raise ValueError("mission ownership fence unavailable")
                    request = {
                        "operationId": uuid.uuid4().hex,
                        "action": "resume",
                        "expectedInstanceId": current["instanceId"],
                        "expectedCommit": current["runtime"].get("commit"),
                        "missionId": mission["id"],
                        "expectedRevision": mission["revision"],
                        "expectedResumptionGeneration": mission["resumptionGeneration"],
                        "selectedModel": {"provider": "metabolomics", "id": model["id"]},
                    }
                    entry["request"] = request
                    store.write(plan)
                    _fresh(transport, target, entry)
                    entry["instanceId"] = current["instanceId"]
                    fields = {
                        k: v
                        for k, v in request.items()
                        if k not in ("operationId", "action", "expectedInstanceId")
                    }
                    receipt = _operation(
                        plan,
                        entry,
                        "resume",
                        "resume",
                        fields,
                        transport,
                        target,
                        current["sourceRoot"],
                        lambda: store.write(plan),
                    )
                    request = entry["operations"]["resume"]["request"]
                    entry["request"] = request
                    deadline = now() + 35
                    while receipt["status"] == "running" and now() < deadline:
                        sleep(1)
                        observed = _fresh(transport, target, entry, settled=False)
                        entry["instanceId"] = observed["instanceId"]
                        receipt = _operation(
                            plan,
                            entry,
                            "resume",
                            "resume",
                            fields,
                            transport,
                            target,
                            current["sourceRoot"],
                            lambda: store.write(plan),
                        )
                    entry["receipt"] = receipt
                    if receipt["status"] == "running":
                        entry.update(
                            status="maintenance_running",
                            reason="native operation is still running; poll its receipt before taking further action",
                        )
                        continue
                    outcome = receipt.get("result", {})
                    if (
                        receipt.get("operationId") != request["operationId"]
                        or receipt.get("action") != "resume"
                        or receipt.get("status") != "succeeded"
                        or outcome.get("status") != "resumed"
                        or outcome.get("missionId") != mission["id"]
                        or outcome.get("selectedModel") != request["selectedModel"]
                    ):
                        raise ValueError("exact-model mission resume not acknowledged")
                    entry["receipt"] = receipt
                else:
                    transport.command(target, entry["paneId"], "/model metabolomics/" + model["id"])
                    sleep(0.5)
                    current = _fresh(transport, target, entry, settled=False)
                    if current.get("modelPicker") is True:
                        current = refresh_interactive(
                            transport, target, entry, current, lambda: store.write(plan), sleep
                        )
                        transport.command(
                            target, entry["paneId"], "/model metabolomics/" + model["id"]
                        )
                        sleep(0.5)
                        current = _fresh(transport, target, entry, settled=False)
                        if current.get("modelScopeBlocked") is True:
                            raise ValueError("model picker scope excludes selected model")
                    if current.get("modelAck") not in (model["id"], "metabolomics/" + model["id"]):
                        raise ValueError("exact model acknowledgement unavailable; inspect pane")
                    if (
                        current.get("model") != model["id"]
                        or current.get("provider") != "metabolomics"
                    ):
                        raise ValueError("model/provider switch not verified in native transcript")
                    if exclusion(current):
                        raise ValueError("session eligibility changed during model switch")
                    _fresh(transport, target, entry)
                    transport.command(target, entry["paneId"], CONTINUE)
                _fresh(transport, target, entry, settled=False)
                entry["status"] = "accepted"
                entry["reason"] = (
                    "submission accepted; meaningful progress requires subsequent verification"
                )
            except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
                entry["errorCode"] = error_code(error)
                entry.update(
                    status="unknown" if attempted else "blocked",
                    reason="recovery could not be verified ("
                    + entry["errorCode"]
                    + "); inspect native session before retry",
                )
            finally:
                if attempted:
                    journal["attempts"][key]["status"] = entry["status"]
                    journal["nextEligibleAt"] = max(journal["nextEligibleAt"], now() + 60)
                    journal.pop("inFlight", None)
                    store.save_journal(journal)
                store.write(plan)
                progress(entry["host"] + " " + str(entry["paneId"]) + ": " + entry["status"])
        plan["status"] = (
            "needs_verification"
            if any(
                e["status"] in ("accepted", "unknown", "maintenance_running")
                for e in plan["sessions"]
            )
            else "settled"
        )
        store.write(plan)
        return plan
