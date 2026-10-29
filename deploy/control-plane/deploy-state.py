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


# Docker's own words for "there is no such container". Anything else (daemon down,
# permission denied, a timeout) is NOT absence and must still fail the operation.
_ABSENT = (b"No such object", b"No such container")


def inspect_container(name):
    """A container's inspect document, or None only when Docker says it does not exist."""
    result = subprocess.run(["docker", "inspect", name], capture_output=True)
    if result.returncode != 0:
        if any(marker in result.stderr for marker in _ABSENT):
            return None
        raise RuntimeError("container inspection failed")
    return json.loads(result.stdout)[0]


# Services that run the herdr-engineering image (collab-edge runs a digest-pinned nginx
# from the compose file itself). An ABSENT one of these is pinned to the web's image.
HERDR_IMAGE_SERVICES = ("control-plane-web", "control-plane-worker", "llm-proxy", "collab-public")


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


def previous_receipt(state, previous_image, web=None):
    """Bind a logical receipt to the actual image, never just a mutable tag."""
    pinned = previous_image.startswith("sha256:")
    if not pinned and not re.fullmatch(r"herdr-engineering:[0-9a-f]{12}", previous_image):
        return "", {}
    tag = previous_image.removeprefix("herdr-engineering:")
    if not pinned and not (Path(state) / f"{tag}.json").exists():
        return tag, {}
    web = web if web is not None else inspect("inspect", container("control-plane-web"))
    actual_id = image_id(web["Image"])
    if web["Config"]["Image"] != previous_image:
        raise ValueError("running image changed")
    if pinned and image_id(previous_image) != actual_id:
        raise ValueError("configured image does not match running image")
    metadata = inspect("image", "inspect", actual_id)
    if image_id(metadata["Id"]) != actual_id:
        raise ValueError("image inspection mismatch")
    stamps = [entry.split("=", 1)[1] for entry in metadata.get("Config", {}).get("Env", []) or []
              if entry.startswith("HERDR_ENGINEERING_COMMIT=")]
    if pinned or stamps:
        if len(stamps) != 1 or not re.fullmatch(r"(?:[0-9a-f]{12}|[0-9a-f]{40})", stamps[0]):
            raise ValueError("invalid image buildstamp")
    tag = stamps[0][:12] if pinned else tag
    receipt_path = Path(state) / f"{tag}.json"
    receipt = json.loads(receipt_path.read_text())
    commit = receipt.get("commit", "")
    if (not re.fullmatch(r"[0-9a-f]{40}", commit) or commit[:12] != tag
            or receipt.get("image") != f"herdr-engineering:{tag}"
            or (stamps and stamps[0] not in (commit, commit[:12]))):
        raise ValueError("receipt does not match image buildstamp")
    # Older receipts lack an ID. Their tag is usable only when it still resolves
    # to the running immutable image, whose buildstamp was checked above.
    expected_id = receipt.get("image_id")
    if expected_id is None:
        expected_id = inspect("image", "inspect", f"herdr-engineering:{tag}")["Id"]
    if image_id(expected_id) != actual_id:
        raise ValueError("receipt image does not match running image")
    return tag, receipt


def protected_snapshot(state, source):
    source = Path(source).resolve(strict=True)
    if (not source.is_relative_to(state) or source.stat().st_mode & 0o077
            or source.parent.stat().st_mode & 0o077):
        raise ValueError("unprotected snapshot")
    return source


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
        # A service container may be missing (an interrupted recreate removed it): record
        # it as absent instead of failing, so this deploy - or its rollback - recreates it.
        live, absent = {}, []
        for s in services:
            document = inspect_container(container(s))
            if document is None:
                absent.append(s)
            else:
                live[s] = document
        if "control-plane-web" in absent:
            raise ValueError("running web container disappeared during preparation")
        images = {s: image_id(d["Image"]) for s, d in live.items()}
        # Rollback recreates an absent herdr-engineering service from the previous web's
        # immutable image (they share it); verify-rollback then checks it like the rest.
        for s in absent:
            if s in HERDR_IMAGE_SERVICES:
                images[s] = images["control-plane-web"]
        web = live["control-plane-web"]
        # A receipt snapshot preserves the last deployed config even if its canonical
        # source was edited in place for this candidate. Legacy receipts bootstrap
        # from the actual running bind mount, never from the selected candidate path.
        _, receipt = previous_receipt(state, previous_image, web)
        if previous_commit and receipt.get("commit") != previous_commit:
            raise ValueError("previous receipt changed during preparation")
        mount = next(m for m in web["Mounts"] if m["Destination"] == "/etc/herdr/fleet")
        source = Path(mount["Source"]) / "fleet-config.yaml"
        saved = receipt.get("fleet_config_snapshot")
        pinned = previous_image.startswith("sha256:")
        if pinned:
            # A rollback mounts its own protected snapshot. The original success
            # receipt may describe a different config; preserve what is live now.
            source = protected_snapshot(state, source)
        elif saved:
            source = protected_snapshot(state, saved)
        previous = {"commit": previous_commit, "image": previous_image, "images": images,
                    "absent": absent,
                    "fleet_config_source": str(source) if pinned else receipt.get("fleet_config_source", str(source)),
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
    elif action == "resolve-previous":
        tag, _ = previous_receipt(*args)
        print(tag)
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
