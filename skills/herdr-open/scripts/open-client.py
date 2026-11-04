#!/usr/bin/env python3
"""Run the skill's maintained checkout, independently of an older global install."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from herdr_engineering.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["open-client", *sys.argv[1:]]))
