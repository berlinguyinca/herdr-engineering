"""Fixed remote program: request data is JSON on stdin, never shell source."""
REMOTE_SCRIPT = r'''
import base64, hashlib, json, os, platform, subprocess, sys, tempfile
from pathlib import Path

def main():
    request = json.load(sys.stdin)
    result = {"transfer": "not_needed", "dispatch": "failed"}
    system = platform.system()
    if system == "Darwin":
        if Path("/dev/console").stat().st_uid != os.getuid():
            result["error"] = "SSH user does not own the active desktop"
            return result
        command = ["open"]
    elif system == "Linux":
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) or not os.environ.get("XDG_RUNTIME_DIR"):
            result["error"] = "No attached graphical desktop in the client environment"
            return result
        command = ["xdg-open"]
    else:
        result["error"] = "Desktop adapter unavailable for this OS"
        return result
    if "url" in request:
        destination = request["url"]
    else:
        cache = Path.home() / ".cache" / "herdr-open"
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cache.is_symlink() or cache.stat().st_uid != os.getuid() or cache.stat().st_mode & 0o077:
            raise ValueError("preview cache must be private and owned by the desktop user")
        items = [request] + request.get("assets", [])
        if len(items) > 101:
            raise ValueError("too many assets")
        total = 0
        decoded = []
        for item in items:
            name = item["name"]
            path = Path(name)
            if not name or path.is_absolute() or ".." in path.parts or name == ".":
                raise ValueError("invalid filename")
            content = base64.b64decode(item["data"], validate=True)
            total += len(content)
            if total > 33554432 or hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise ValueError("transfer integrity check failed")
            decoded.append((name, content, item["sha256"]))
        folder = Path(tempfile.mkdtemp(prefix="view-", dir=cache))
        for name, content, digest in decoded:
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(content)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("materialized file checksum mismatch")
        destination = folder / request["name"]
        result.update(transfer="verified", path=str(destination))
        destination = str(destination)
    try:
        completed = subprocess.run(command + [destination], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        result["error"] = "Desktop opener unavailable or timed out: " + type(error).__name__
        return result
    if completed.returncode == 0:
        result["dispatch"] = "requested"
    else:
        result["error"] = "Desktop opener rejected the request"
    return result

try:
    print(json.dumps(main()))
except Exception as error:
    print(json.dumps({"dispatch": "failed", "error": type(error).__name__}))
'''
