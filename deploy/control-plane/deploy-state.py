#!/usr/bin/env python3
"""Private deployment recovery artifacts. Never log configuration or Docker errors."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def private_write(path, data):
    """Atomic, synced writes; snapshots and receipts never inherit a loose umask."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, data):
    private_write(path, (json.dumps(data) + "\n").encode())


def inspect(*args):
    result = subprocess.run(["docker", *args], capture_output=True, check=True)
    return json.loads(result.stdout)[0]


def image_id(value):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("invalid image ID")
    return value


def container(service):
    return {"control-plane-web": "herdr-cp-web", "control-plane-worker": "herdr-cp-worker",
            "llm-proxy": "herdr-llm-proxy", "collab-public": "herdr-collab-public",
            "collab-edge": "herdr-collab-edge"}[service]


def snapshot(attempt, name, source):
    directory = attempt / name
    directory.mkdir(mode=0o700)
    target = directory / "fleet-config.yaml"
    private_write(target, source.read_bytes())
    return str(target)


def prepare(state, central, commit, previous_commit, previous_image, collab):
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=state))
    source = Path(central).resolve(strict=True)
    candidate_id = image_id(inspect("image", "inspect", f"herdr-engineering:{commit[:12]}")["Id"])
    candidate = {"commit": commit, "image_id": candidate_id,
                 "fleet_config_source": str(source),
                 "fleet_config_snapshot": snapshot(attempt, "candidate", source)}
    previous = {}
    services = ["control-plane-web", "control-plane-worker", "llm-proxy"]
    if collab == "1":
        services += ["collab-public", "collab-edge"]
    if previous_image:
        live = {s: inspect("inspect", container(s)) for s in services}
        images = {s: image_id(d["Image"]) for s, d in live.items()}
        web = live["control-plane-web"]
        # A receipt snapshot preserves the last deployed config even if its canonical
        # source was edited in place for this candidate. Legacy receipts bootstrap
        # from the actual running bind mount, never from the selected candidate path.
        tag = previous_image.removeprefix("herdr-engineering:")
        receipt_path = state / f"{tag}.json" if re.fullmatch(r"[0-9a-f]{12}", tag) else None
        receipt = json.loads(receipt_path.read_text()) if receipt_path and receipt_path.exists() else {}
        mount = next(m for m in web["Mounts"] if m["Destination"] == "/etc/herdr/fleet")
        source = Path(mount["Source"]) / "fleet-config.yaml"
        saved = receipt.get("fleet_config_snapshot")
        if saved:
            if receipt.get("image_id") != images["control-plane-web"]:
                raise ValueError("receipt image does not match running image")
            saved = Path(saved).resolve(strict=True)
            if (not saved.is_relative_to(state) or saved.stat().st_mode & 0o077
                    or saved.parent.stat().st_mode & 0o077):
                raise ValueError("unprotected snapshot")
            source = saved
        previous = {"commit": previous_commit, "image": previous_image, "images": images,
                    "fleet_config_source": receipt.get("fleet_config_source", str(source)),
                    "fleet_config_snapshot": snapshot(attempt, "previous", source)}
        # Image-only override: retain the previous commit's commands, including the
        # collaboration operator URL, while making mutable tags irrelevant.
        write_json(attempt / "rollback-images.json",
                   {"services": {s: {"image": i} for s, i in images.items()}})
    write_json(attempt / "metadata.json", {"candidate": candidate, "previous": previous,
                                         "collab": collab == "1", "outcome": "prepared"})
    sync_directory(state)
    print(attempt)


def main():
    action, *args = sys.argv[1:]
    if action == "prepare":
        prepare(*args)
    elif action == "outcome":
        attempt, outcome = args
        path = Path(attempt) / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["outcome"] = outcome
        write_json(path, metadata)
    elif action == "receipt":
        attempt, out, ref, image, previous_image, collab_hash, at = args
        metadata = json.loads((Path(attempt) / "metadata.json").read_text())
        candidate = metadata["candidate"]
        write_json(Path(out), {**candidate, "ref": ref, "image": image,
                              "previous_commit": metadata["previous"].get("commit", ""),
                              "previous_image": previous_image, "collab_slice_sha256": collab_hash,
                              "at": at, "rollback_metadata": str(Path(attempt) / "metadata.json")})
    elif action in ("verify-rollback", "verify-forward"):
        metadata = json.loads((Path(args[0]) / "metadata.json").read_text())
        if action == "verify-rollback":
            images = metadata["previous"]["images"]
            config = metadata["previous"]["fleet_config_snapshot"]
        else:
            services = ["control-plane-web", "control-plane-worker", "llm-proxy"]
            if metadata["collab"]:
                services.append("collab-public")
            images = {s: metadata["candidate"]["image_id"] for s in services}
            config = metadata["candidate"]["fleet_config_source"]
        for service, expected in images.items():
            live = inspect("inspect", container(service))
            if live["Image"] != expected:
                raise ValueError("deployment image mismatch")
            if service in ("control-plane-web", "control-plane-worker"):
                source = next(m["Source"] for m in live["Mounts"]
                              if m["Destination"] == "/etc/herdr/fleet")
                if Path(source).resolve() != Path(config).parent:
                    raise ValueError("deployment config mount mismatch")
    else:
        raise ValueError("unknown operation")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Neither exception messages nor Docker stderr are safe to print.
        print("deployment recovery state operation failed; no secret details logged", file=sys.stderr)
        sys.exit(1)
