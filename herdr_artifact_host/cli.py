"""Command line entry point: ``herdr-artifact-host {public,internal,migrate,import}``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

SUBCOMMANDS: dict[str, str] = {
    "public": "serve the public listener",
    "internal": "serve the internal listener",
    "migrate": "apply database migrations",
    "import": "import artifacts from a control-plane export",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="herdr-artifact-host")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in SUBCOMMANDS.items():
        sub.add_parser(name, help=help_text, description=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        ns = _parser().parse_args(args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    print(f"herdr-artifact-host {ns.command}: not implemented yet", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
