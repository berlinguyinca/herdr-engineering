"""Host-side primitives for a fixed Pi package activation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SHA256 = re.compile(r"sha256:[a-f0-9]{64}\Z")
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
LIMIT_KEYS = {"memory_bytes", "process_count", "cpu_quota_percent", "wall_time_seconds"}


class RunnerError(RuntimeError):
    pass


def _exact(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def tree_digest(root: Path) -> str:
    root = root.resolve(strict=True)
    rows = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RunnerError("runtime artifact contains a link or special file")
        rows.append(path.relative_to(root).as_posix().encode() + b"\0"
                    + hashlib.sha256(path.read_bytes()).digest())
    if not rows:
        raise RunnerError("runtime artifact is empty")
    return "sha256:" + hashlib.sha256(b"\n".join(rows)).hexdigest()


def stage_runtime(source: Path, root: Path) -> tuple[Path, str]:
    source = source.resolve(strict=True)
    manifest = json.loads((source / "package.json").read_text())
    if manifest.get("name") != "@earendil-works/pi-coding-agent":
        raise RunnerError("unrecognized Pi package")
    if not (source / "dist/bundle/cli.js").is_file():
        raise RunnerError("fixed Pi entrypoint missing")
    for path in source.rglob("*"):
        info = path.lstat()
        if path.is_relative_to(source / "node_modules/.bin"):
            continue
        if not (stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
            raise RunnerError("source runtime contains an untrusted link or special file")
    scratch = root / ("stage-" + os.urandom(8).hex())
    scratch.mkdir(mode=0o700, parents=True)
    staged = scratch / "runtime"
    try:
        shutil.copytree(source, staged, symlinks=True,
                        ignore=shutil.ignore_patterns(".bin"))
        digest = tree_digest(staged)
    except Exception:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    final = root / digest.removeprefix("sha256:")
    if final.exists():
        shutil.rmtree(scratch)
        if tree_digest(final) != digest:
            raise RunnerError("existing runtime digest mismatch")
        return final, digest
    staged.rename(final)
    scratch.rmdir()
    for path in final.rglob("*"):
        path.chmod(0o500 if path.is_dir() else 0o400)
    final.chmod(0o500)
    return final, digest


def validate_limits(value):
    if not _exact(value, LIMIT_KEYS):
        raise RunnerError("invalid limits")
    bounds = {
        "memory_bytes": (268435456, 17179869184), "process_count": (16, 1024),
        "cpu_quota_percent": (10, 800), "wall_time_seconds": (30, 86400),
    }
    if any(type(value[k]) is not int or not lo <= value[k] <= hi
           for k, (lo, hi) in bounds.items()):
        raise RunnerError("invalid limits")
    return dict(value)


def validate_claim(value, host_id, nonce):
    required = {"runtime", "credential", "device_id", "claim_token", "lease_expires_at",
                "package", "selected_model", "actions"}
    if not _exact(value, required) or value.get("actions") != ["execute", "read"]:
        raise RunnerError("invalid claim")
    package = value["package"]
    if not _exact(package, {"package_id", "digest", "runtime_artifact_digest", "profile", "limits",
                            "required_role_bindings"}):
        raise RunnerError("invalid claim package")
    if package["profile"] != "task-restricted-v1" or not SHA256.fullmatch(package["digest"] or ""):
        raise RunnerError("claim profile mismatch")
    if not SHA256.fullmatch(package["runtime_artifact_digest"] or ""):
        raise RunnerError("claim runtime digest invalid")
    validate_limits(package["limits"])
    for key in ("credential", "device_id", "claim_token", "selected_model"):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 1000:
            raise RunnerError("invalid claim field")
    if not SAFE.fullmatch(value["selected_model"]):
        raise RunnerError("invalid selected model")
    runtime = value["runtime"]
    if not isinstance(runtime, dict) or runtime.get("host_id") != host_id:
        raise RunnerError("claim attestation mismatch")
    if not isinstance(value["lease_expires_at"], (int, float)):
        raise RunnerError("invalid claim lease")
    return value


class OperatorClient:
    def __init__(self, origin: str):
        if "?" in origin or "#" in origin:
            raise RunnerError("operator origin must be loopback")
        parsed = urllib.parse.urlsplit(origin)
        try:
            port = parsed.port
        except ValueError:
            raise RunnerError("operator origin must be loopback") from None
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/")):
            raise RunnerError("operator origin must be loopback")
        self.origin = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        request = urllib.request.Request(self.origin + "/api/v1/collab/" + path, data=data,
                                         method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(1_000_001)
        except urllib.error.HTTPError as exc:
            raise RunnerError(f"operator API rejected request ({exc.code})") from None
        except OSError:
            raise RunnerError("operator API unavailable") from None
        if len(raw) > 1_000_000:
            raise RunnerError("operator response too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RunnerError("invalid operator response")
        return value

    def attest(self, attestation):
        return self.call("POST", "fleet/assistant-runtime-hosts", attestation)

    def queued(self):
        return self.call("GET", "fleet/assistant-runtimes")

    def claim(self, runtime_id, host_id, nonce):
        if not SAFE.fullmatch(runtime_id):
            raise RunnerError("invalid runtime id")
        return self.call("POST", f"fleet/assistant-runtimes/{runtime_id}/claim",
                         {"host_id": host_id, "attestation_nonce": nonce})

    def heartbeat(self, runtime_id, claim_token, nonce, sequence, phase, output_sha256=None):
        checkpoint = {"sequence": sequence, "phase": phase, "output_sha256": output_sha256}
        return self.call("POST", f"fleet/assistant-runtimes/{runtime_id}/heartbeat",
                         {"claim_token": claim_token, "attestation_nonce": nonce,
                          "checkpoint": checkpoint})

    def report(self, runtime_id, body):
        return self.call("POST", f"fleet/assistant-runtimes/{runtime_id}/report", body)

    def cancel_status(self, runtime_id, claim_token):
        return self.call("POST", f"fleet/assistant-runtimes/{runtime_id}/cancel-status",
                         {"claim_token": claim_token})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fixed_pi_command(model: str, role_count: int = 0) -> str:
    if not SAFE.fullmatch(model):
        raise RunnerError("invalid model")
    if not 0 <= role_count <= 8:
        raise RunnerError("invalid role count")
    role = "".join(
        f' --append-system-prompt "$PI_CODING_AGENT_DIR/role-{index:02d}.md"'
        for index in range(role_count))
    return ("/usr/bin/node /opt/herdr-pi/dist/bundle/cli.js --print --no-tools "
            "--no-extensions --no-mcp --no-skills --no-prompt-templates --no-themes "
            "--no-context-files --no-session --provider herdr --model " + model
            + " --api-key broker-bound" + role)


def write_private(path: Path, value: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)


def load_role_registry(path: Path | None) -> dict[tuple[str, str, str], Path]:
    if path is None:
        return {}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > 65536):
            raise RunnerError("role registry must be a private regular file")
        value = json.loads(stream.read().decode())
    if not _exact(value, {"roles"}) or not isinstance(value["roles"], list) or len(value["roles"]) > 32:
        raise RunnerError("invalid role registry")
    result = {}
    for row in value["roles"]:
        if not _exact(row, {"id", "version", "digest", "path"}):
            raise RunnerError("invalid role registry")
        key = (row["id"], row["version"], row["digest"])
        if not all(isinstance(x, str) and x for x in key) or not SHA256.fullmatch(row["digest"]):
            raise RunnerError("invalid role registry")
        role_path = Path(row["path"])
        if not role_path.is_absolute() or key in result:
            raise RunnerError("invalid role registry")
        result[key] = role_path
    return result


def read_verified_role(path: Path, expected_digest: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size < 1 or info.st_size > 65536):
            raise RunnerError("role artifact must be a private regular file")
        data = stream.read()
    if "sha256:" + hashlib.sha256(data).hexdigest() != expected_digest:
        raise RunnerError("role artifact digest mismatch")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise RunnerError("role artifact must be UTF-8") from None


def verified_role_refs(registry: dict[tuple[str, str, str], Path]) -> list[dict]:
    refs = []
    for role_id, version, digest in sorted(registry):
        read_verified_role(registry[(role_id, version, digest)], digest)
        refs.append({"id": role_id, "version": version, "digest": digest})
    return refs


def prepare_activation(root: Path, claim: dict, runtime_dir: Path, sandbox: Path,
                       worker_template: Path, collab_base: str, egress_sock: Path,
                       role_registry=None):
    package = claim["package"]
    collab = urllib.parse.urlsplit(collab_base)
    loopback_http = collab.scheme == "http" and collab.hostname == "127.0.0.1"
    if ((collab.scheme != "https" and not loopback_http) or not collab.hostname
            or collab.username is not None or collab.password is not None
            or collab.query or collab.fragment or collab.path not in ("", "/")):
        raise RunnerError("collaboration base must be HTTPS or loopback HTTP")
    collab_base = f"{collab.scheme}://{collab.hostname}" + (f":{collab.port}" if collab.port else "")
    if tree_digest(runtime_dir) != package["runtime_artifact_digest"]:
        raise RunnerError("runtime artifact does not match package")
    activation = root / ("activation-" + hashlib.sha256(claim["claim_token"].encode()).hexdigest()[:20])
    activation.mkdir(mode=0o700, parents=True)
    home = activation / "home"
    work = home / "herdr-agents/work/agent"
    harness = activation / "harness/pi"
    for path in (home, home / "herdr-agents", home / "herdr-agents/work", work,
                 activation / "harness", harness, activation / "audit"):
        path.mkdir(mode=0o700, exist_ok=True)
        path.chmod(0o700)
    models = {"providers": {"herdr": {"baseUrl": "http://127.0.0.1:3128/svc/llm",
              "api": "openai-completions", "apiKey": "broker-bound",
              "models": [{"id": claim["selected_model"]}]}}}
    write_private(harness / "models.json", json.dumps(models, separators=(",", ":")))
    required_roles = package.get("required_role_bindings", [])
    if not isinstance(required_roles, list) or len(required_roles) > 8:
        raise RunnerError("invalid required role bindings")
    verified_roles = []
    if required_roles:
        ordered = sorted(required_roles, key=lambda row: (
            row.get("id", ""), row.get("version", ""), row.get("digest", "")))
        if len({json.dumps(row, sort_keys=True) for row in ordered}) != len(ordered):
            raise RunnerError("duplicate required role binding")
        for index, row in enumerate(ordered):
            if not _exact(row, {"id", "version", "digest"}):
                raise RunnerError("invalid required role binding")
            key = (row["id"], row["version"], row["digest"])
            role_path = (role_registry or {}).get(key)
            if role_path is None:
                raise RunnerError("required role is not configured")
            role_text = read_verified_role(role_path, row["digest"])
            target = harness / f"role-{index:02d}.md"
            write_private(target, role_text); target.chmod(0o400)
            verified_roles.append(dict(row))
    write_private(activation / "agent.json", json.dumps({
        "credential": claim["credential"], "device_id": claim["device_id"], "name": "agent",
    }, separators=(",", ":")))
    worker = worker_template.read_text().replace("__HERDR_BASE__", collab_base)
    needle = "  call tasks_wait '{\"seconds\":20}' > \"$TMP/r.json\" || { sleep 5; continue; }"
    replacement = needle + '\n  [ -z "${HERDR_READY_FILE:-}" ] || { : > "$HERDR_READY_FILE"; chmod 600 "$HERDR_READY_FILE"; }'
    if needle not in worker:
        raise RunnerError("worker readiness hook unavailable")
    write_private(activation / "worker.sh", worker.replace(needle, replacement))
    (activation / "worker.sh").chmod(0o700)
    limits = validate_limits(package["limits"])
    env = {
        "HOME": str(home), "HERDR_AGENT_CMD": fixed_pi_command(
            claim["selected_model"], len(verified_roles)),
        "HERDR_SANDBOX": str(sandbox), "HERDR_SANDBOX_PROFILE": "task-restricted-v1",
        "HERDR_SANDBOX_RUNTIME_DIR": str(runtime_dir),
        "HERDR_SANDBOX_HARNESS_DIR": str(activation / "harness"),
        "HERDR_EGRESS_SOCK": str(egress_sock), "HERDR_EGRESS_SERVICES": "llm",
        "HERDR_SANDBOX_MEM_BYTES": str(limits["memory_bytes"]),
        "HERDR_SANDBOX_TASKS": str(limits["process_count"]),
        "HERDR_SANDBOX_CPU_PERCENT": str(limits["cpu_quota_percent"]),
        "HERDR_SANDBOX_TIMEOUT_SECONDS": str(limits["wall_time_seconds"]),
        "HERDR_TASK_TIMEOUT": str(limits["wall_time_seconds"]),
        "HERDR_AUDIT": str(activation / "audit/tasks.log"),
        "HERDR_READY_FILE": str(activation / "ready"),
        "HERDR_SANDBOX_UNIT": "herdr-" + activation.name.replace("activation-", "")[:20],
    }
    return activation, env, verified_roles


def strict_preflight(sandbox: Path, runtime_dir: Path, egress_sock: Path, limits: dict,
                     scratch_parent: Path) -> dict:
    """Prove this exact script/artifact/broker/limit tuple before attesting it."""
    limits = validate_limits(limits)
    scratch_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="preflight-", dir=scratch_parent))
    try:
        home = root / "home"; work_root = home / "herdr-agents/work"; work = work_root / "probe"
        harness = root / "harness"; (harness / "pi").mkdir(parents=True, mode=0o700)
        for path in (home, home / "herdr-agents", work_root, work, harness):
            path.mkdir(mode=0o700, exist_ok=True); path.chmod(0o700)
        command = ("test ! -e \"$HOME/.ssh\"; test ! -S /var/run/docker.sock; "
                   "! touch /usr/herdr-preflight; "
                   "! /usr/bin/timeout 2 /usr/bin/env bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'; "
                   "grep -Eq '^NoNewPrivs:[[:space:]]+1$' /proc/self/status; "
                   "/usr/bin/node /opt/herdr-pi/dist/bundle/cli.js --version; "
                   "printf HERDR_STRICT_READY")
        unit = "herdr-preflight-" + secrets.token_hex(6)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home),
               "HERDR_SANDBOX_PROFILE": "task-restricted-v1",
               "HERDR_SANDBOX_RUNTIME_DIR": str(runtime_dir),
               "HERDR_SANDBOX_HARNESS_DIR": str(harness),
               "HERDR_EGRESS_SOCK": str(egress_sock), "HERDR_EGRESS_SERVICES": "llm",
               "HERDR_SANDBOX_MEM_BYTES": str(limits["memory_bytes"]),
               "HERDR_SANDBOX_TASKS": str(limits["process_count"]),
               "HERDR_SANDBOX_CPU_PERCENT": str(limits["cpu_quota_percent"]),
               "HERDR_SANDBOX_TIMEOUT_SECONDS": str(limits["wall_time_seconds"]),
               "HERDR_SANDBOX_UNIT": unit}
        for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        result = subprocess.run(["bash", str(sandbox), str(work), command], env=env,
                                capture_output=True, text=True, timeout=45)
        stop_sandbox_unit(unit)
        if result.returncode != 0 or "HERDR_STRICT_READY" not in result.stdout:
            raise RunnerError("strict sandbox preflight failed")
        return {"bubblewrap": True, "cgroup_v2": True, "broker_only": True,
                "task_only": True, "no_new_privileges": True, "limits": limits}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def launch_worker(activation: Path, env: dict) -> subprocess.Popen:
    safe_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                "USER": os.environ.get("USER", "agent"), **env}
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        if os.environ.get(name):
            safe_env[name] = os.environ[name]
    return subprocess.Popen(
        ["bash", str(activation / "worker.sh"), str(activation / "agent.json")],
        cwd=activation, env=safe_env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )


def cancel_process(process: subprocess.Popen):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(5)


def profile_digest(sandbox: Path, limits: dict) -> str:
    body = json.dumps({"profile": "task-restricted-v1", "limits": validate_limits(limits),
                       "sandbox_sha256": hashlib.sha256(sandbox.read_bytes()).hexdigest()},
                      sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def attestation(host_id, artifact_digest, sandbox, limits, models, nonce, enforcement,
                role_bindings=None):
    now = time.time()
    return {
        "schema_version": 1, "host_id": host_id, "profile": "task-restricted-v1",
        "profile_digest": profile_digest(sandbox, limits),
        "runtime": {"kind": "pi", "artifact_digest": artifact_digest,
                    "entrypoint_id": "pi-fixed-v1"},
        "enforcement": enforcement,
        "approved_models": list(models), "issued_at": now,
        "expires_at": now + 240, "nonce": nonce,
        "role_bindings": list(role_bindings or []),
    }


def run_activation(client, runtime_id, host_id, nonce, claim_value, root, runtime_dir,
                   sandbox, worker, collab_base, egress_sock, enforcement, profile_hash,
                   role_registry=None):
    claim_value = validate_claim(claim_value, host_id, nonce)
    if time.time() >= claim_value["lease_expires_at"]:
        raise RunnerError("claim lease expired")
    activation, env, verified_roles = prepare_activation(
        root, claim_value, runtime_dir, sandbox, worker, collab_base, egress_sock, role_registry)
    if profile_digest(sandbox, claim_value["package"]["limits"]) != profile_hash:
        raise RunnerError("sandbox profile changed after attestation")
    token = claim_value["claim_token"]
    runtime_digest = claim_value["package"]["runtime_artifact_digest"]
    launch = {"profile_digest": profile_hash, "runtime_artifact_digest": runtime_digest,
              "enforcement": enforcement, "role_bindings": verified_roles}
    process = None
    sequence = 1
    terminal_reported = False
    cleaned = False
    try:
        client.report(runtime_id, {"claim_token": token, "attestation_nonce": nonce,
                      "status": "starting", "checkpoint": {
                          "sequence": sequence, "phase": "starting", "output_sha256": None}})
        process = launch_worker(activation, env)
        deadline = min(time.time() + 30, claim_value["lease_expires_at"])
        while time.time() < deadline and process.poll() is None and not (activation / "ready").exists():
            time.sleep(0.25)
        if process.poll() is not None or not (activation / "ready").is_file():
            raise RunnerError("worker did not establish task polling")
        sequence += 1
        client.report(runtime_id, {"claim_token": token, "attestation_nonce": nonce,
                      "status": "ready", "checkpoint": {
                          "sequence": sequence, "phase": "ready", "output_sha256": None},
                      "launch_attestation": launch, "instance_id": f"pi:{process.pid}"})
        while process.poll() is None:
            time.sleep(10)
            cancelled = client.cancel_status(runtime_id, token)
            if cancelled.get("cancel_requested") is True:
                sequence += 1
                cancel_process(process)
                stop_sandbox_unit(env["HERDR_SANDBOX_UNIT"])
                cleaned = True
                client.report(runtime_id, {"claim_token": token, "attestation_nonce": nonce,
                              "status": "cancelled", "checkpoint": {
                                  "sequence": sequence, "phase": "finished",
                                  "output_sha256": None}})
                terminal_reported = True
                return
            sequence += 1
            client.heartbeat(runtime_id, token, nonce, sequence, "ready")
        raise RunnerError("worker exited")
    except Exception as error:
        if not terminal_reported:
            if process is not None:
                cancel_process(process)
            stop_sandbox_unit(env["HERDR_SANDBOX_UNIT"])
            cleaned = True
            sequence += 1
            try:
                client.report(runtime_id, {"claim_token": token, "attestation_nonce": nonce,
                              "status": "failed", "checkpoint": {"sequence": sequence,
                                  "phase": "failed", "output_sha256": None},
                              "error_code": "worker_exited" if str(error) == "worker exited" else "runner_failed"})
            except Exception:
                pass
        raise
    finally:
        if not cleaned and process is not None:
            cancel_process(process)
        if not cleaned:
            stop_sandbox_unit(env["HERDR_SANDBOX_UNIT"])


def stop_sandbox_unit(unit: str):
    if not SAFE.fullmatch(unit):
        raise RunnerError("invalid sandbox unit")
    name = unit + ".scope"
    subprocess.run(["systemctl", "--user", "stop", name], timeout=15, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    status = subprocess.run(
        ["systemctl", "--user", "show", name, "-p", "LoadState", "-p", "ActiveState",
         "-p", "SubState"], timeout=5, check=False, capture_output=True, text=True)
    if status.returncode != 0:
        raise RunnerError("sandbox scope stop could not be verified")
    state = dict(line.split("=", 1) for line in status.stdout.splitlines() if "=" in line)
    removed = state.get("LoadState") == "not-found"
    stopped = state.get("ActiveState") in {"inactive", "failed"} and state.get("SubState") in {"dead", "failed"}
    if not (removed or stopped):
        raise RunnerError("sandbox scope did not stop")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="assistant-pi-runner")
    parser.add_argument("--operator-origin", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--pi-source", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--activation-root", required=True)
    parser.add_argument("--sandbox", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--collab-base", required=True)
    parser.add_argument("--egress-sock", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument("--role-registry")
    parser.add_argument("--memory-bytes", type=int, default=536870912)
    parser.add_argument("--process-count", type=int, default=64)
    parser.add_argument("--cpu-quota-percent", type=int, default=100)
    parser.add_argument("--wall-time-seconds", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    models = tuple(x for x in args.models.split(",") if SAFE.fullmatch(x))
    if not models or len(models) != len(args.models.split(",")):
        raise RunnerError("invalid approved models")
    limits = validate_limits({"memory_bytes": args.memory_bytes,
                              "process_count": args.process_count,
                              "cpu_quota_percent": args.cpu_quota_percent,
                              "wall_time_seconds": args.wall_time_seconds})
    from .pi_recovery_host import trusted_sdk
    sdk = trusted_sdk(Path.home(), os.getpid())
    trusted_source = Path(sdk["module"]).resolve().parents[2]
    if Path(args.pi_source).resolve(strict=True) != trusted_source:
        raise RunnerError("Pi source does not match the trusted installed SDK")
    runtime_dir, digest = stage_runtime(trusted_source, Path(args.runtime_root))
    role_registry = load_role_registry(Path(args.role_registry)) if args.role_registry else {}
    sandbox = Path(args.sandbox).resolve(strict=True)
    client = OperatorClient(args.operator_origin)
    while True:
        nonce = secrets.token_urlsafe(24)
        enforcement = strict_preflight(sandbox, runtime_dir, Path(args.egress_sock), limits,
                                       Path(args.activation_root))
        proof = attestation(args.host_id, digest, sandbox, limits, models, nonce, enforcement,
                            verified_role_refs(role_registry))
        client.attest(proof)
        refresh_at = proof["expires_at"] - 15
        while time.time() < refresh_at:
            queued = client.queued().get("runtimes", [])
            candidate = next((x for x in queued if x.get("host_id") == args.host_id), None)
            if candidate:
                runtime_id = candidate.get("runtime_id")
                claimed = client.claim(runtime_id, args.host_id, nonce)
                try:
                    run_activation(client, runtime_id, args.host_id, nonce, claimed,
                                   Path(args.activation_root), runtime_dir, sandbox,
                                   Path(args.worker), args.collab_base, Path(args.egress_sock),
                                   proof["enforcement"], proof["profile_digest"], role_registry)
                except RunnerError:
                    if args.once:
                        return 1
                break
            if args.once:
                return 0
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
