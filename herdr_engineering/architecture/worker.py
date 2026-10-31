"""Sandboxed analysis entry point. Run as ``python3 -I worker.py <json-request-on-stdin>``.

Trusted code only: it reads blobs of one commit through git and emits the
snapshot JSON on stdout. It never imports or executes analysed files.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def main() -> int:
    from herdr_engineering.architecture.analyze import analyze_commit  # noqa: PLC0415
    req = json.loads(sys.stdin.read())
    out = analyze_commit(**req)
    sys.stdout.write(json.dumps(out, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - reported to the supervisor as a failure
        sys.stderr.write(f"{type(exc).__name__}: {str(exc)[:300]}")
        sys.exit(2)
