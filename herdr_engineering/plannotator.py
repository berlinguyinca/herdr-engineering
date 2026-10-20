"""Host-side Plannotator bridge: ``python -m herdr_engineering.plannotator``.

Launches the installed upstream CLI, advertises its runtime-selected URL, and
reports its JSON result to HerdR. Credentials stay in this process; the review
process does not inherit the control-plane key. No fixed port is required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from .control_plane.reviews import MAX_TEXT, safe_review_url
from .session_env import _post


def command_for(kind, plan_file=None, binary="plannotator"):
    if kind == "plan":
        if not plan_file:
            raise ValueError("plan reviews require --plan-file")
        return [binary, "annotate", str(plan_file), "--gate", "--json"]
    if kind == "code":
        return [binary, "review", "--json"]
    raise ValueError("kind must be plan or code")


def _read_json(path, limit=MAX_TEXT * 2):
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Plannotator output exceeded the receipt limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Plannotator returned no JSON object")
    return value


def run_review(*, kind, cwd, control_plane, plan_file=None, repository=None,
               project_id=None, revision=None, mission_id=None, session_id=None,
               title=None, url_host=None, binary="plannotator", post=_post):
    executable = shutil.which(binary)
    if not executable:
        raise ValueError("Plannotator CLI is not installed or is not on PATH")
    workdir = Path(cwd).resolve(strict=True)
    if not workdir.is_dir():
        raise ValueError("review cwd is not a directory")
    plan = None
    if kind == "plan":
        if not plan_file:
            raise ValueError("plan reviews require --plan-file")
        plan_path = Path(plan_file).resolve(strict=True)
        with plan_path.open() as stream:
            plan = stream.read(MAX_TEXT + 1)
        if len(plan) > MAX_TEXT:
            raise ValueError("plan exceeds the review size limit")
    if revision is None:
        try:
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=workdir, text=True,
                stderr=subprocess.DEVNULL, timeout=5).strip()
        except (subprocess.SubprocessError, OSError):
            pass
    rid, instance = f"rev_{uuid4().hex}", uuid4().hex
    binding = {"instance_id": instance, "revision": revision,
               "plan_digest": hashlib.sha256(plan.encode()).hexdigest() if plan is not None else None}
    body = dict(binding, review_id=rid, kind=kind, title=title or f"{kind.title()} review",
                repository=repository, project_id=project_id, mission_id=mission_id,
                session_id=session_id, host_id=socket.gethostname(), plan=plan)
    # The registration must be acknowledged durably before launching a review.
    post(control_plane, "/api/v1/reviews", body)
    path = f"/api/v1/reviews/{rid}"
    child = None
    with tempfile.TemporaryDirectory(prefix="herdr-plannotator-") as temp:
        area = Path(temp)
        ready, output = area / "ready.json", area / "result.json"
        # Review immutable plan bytes, rather than a file the agent may change
        # while a human is deciding. The digest binds the receipt to those bytes.
        frozen_plan = None
        if plan is not None:
            frozen_plan = area / "plan.md"
            frozen_plan.write_text(plan)
            frozen_plan.chmod(0o600)
        env = os.environ.copy()
        # The upstream UI may start helper processes; unrelated credentials
        # must not be inherited by that process tree.
        for key in list(env):
            upper = key.upper()
            if any(part in upper for part in ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "AUTHORIZATION", "CREDENTIAL")) or upper.endswith("_KEY"):
                env.pop(key)
        env["PLANNOTATOR_READY_FILE"] = str(ready)
        env["PLANNOTATOR_PORT"] = "0"
        env["PLANNOTATOR_REMOTE"] = "1" if url_host or env.get("PLANNOTATOR_URL_HOST") else "0"
        env["PLANNOTATOR_SHARE"] = "disabled"
        if url_host:
            env["PLANNOTATOR_URL_HOST"] = url_host
        try:
            with output.open("wb") as stdout:
                child = subprocess.Popen(command_for(kind, frozen_plan, executable),
                                         cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                                         stdout=stdout, stderr=sys.stderr)
                next_heartbeat = 0.0
                attempted_url = None
                while child.poll() is None:
                    now = time.monotonic()
                    ready_url = None
                    if output.stat().st_size > MAX_TEXT * 2:
                        raise ValueError("Plannotator output exceeded the receipt limit")
                    if ready.exists():
                        try:
                            metadata = _read_json(ready)
                        except (OSError, json.JSONDecodeError):
                            metadata = {}  # readiness publication may still be in flight
                        ready_url = safe_review_url(metadata.get("url"))
                    if now >= next_heartbeat or ready_url != attempted_url:
                        heartbeat = dict(binding)
                        if ready_url:
                            heartbeat["review_url"] = ready_url
                        try:
                            post(control_plane, path + "/heartbeat", heartbeat)
                        except RuntimeError:
                            pass  # retain the live review; HerdR will show stale transport
                        attempted_url = ready_url
                        next_heartbeat = now + 30
                    time.sleep(0.2)
                if child.returncode:
                    raise ValueError(f"Plannotator exited with status {child.returncode}")
            result = _read_json(output)
            # Keep a private recovery receipt if delivery fails after the human
            # decided. Never overwrite or lose a completed upstream decision.
            recovery = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "herdr-engineering/review-receipts"
            recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
            receipt_path = recovery / f"{rid}.json"
            fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            receipt = dict(binding, result=result)
            with os.fdopen(fd, "w") as stream:
                json.dump({"review_id": rid, "receipt": receipt}, stream)
            post(control_plane, path + "/result", receipt)
            receipt_path.unlink()
            return result
        except BaseException as exc:
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            # An undelivered decision remains stale and recoverable, never failed.
            if not ("receipt_path" in locals() and receipt_path.exists()):
                try:
                    post(control_plane, path + "/failure", dict(binding, error=f"Review launcher stopped: {type(exc).__name__}"))
                except Exception:
                    pass
            raise


def replay_receipt(receipt_file, control_plane, *, post=_post):
    """Resend a private retained outcome after a control-plane outage."""
    path = Path(receipt_file)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("receipt file must be private and owned by the current user")
    saved = _read_json(path)
    rid = saved.get("review_id")
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", rid):
        raise ValueError("invalid saved review identity")
    receipt = saved.get("receipt")
    if not isinstance(receipt, dict):
        raise ValueError("invalid saved review receipt")
    result = post(control_plane, f"/api/v1/reviews/{rid}/result", receipt)
    path.unlink()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["plan", "code", "replay"])
    parser.add_argument("--plan-file")
    parser.add_argument("--receipt-file")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--control-plane", default=os.environ.get("HERDR_ENGINEERING_CONTROL_PLANE_URL", "http://dev.lan"))
    for option in ("repository", "project-id", "revision", "mission-id", "session-id", "title", "url-host"):
        parser.add_argument("--" + option)
    try:
        args = vars(parser.parse_args(argv))
        receipt_file = args.pop("receipt_file")
        if args["kind"] == "replay":
            if not receipt_file:
                raise ValueError("replay requires --receipt-file")
            result = replay_receipt(receipt_file, args["control_plane"])
        else:
            if receipt_file:
                raise ValueError("--receipt-file is only used with replay")
            result = run_review(**args)
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Plannotator bridge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
