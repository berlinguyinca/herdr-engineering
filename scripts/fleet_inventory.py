#!/usr/bin/env python3
"""Thin shim: the private-inventory helper lives in `herdr_engineering.fleet_inventory`.

Kept so `scripts/fleet-join.sh` and `from scripts.fleet_inventory import ...` keep working from a
checkout. The package module is what the installed wheel ships.
"""
import sys
from pathlib import Path

if __name__ == "__main__":  # run as a file: the package sits beside scripts/, not necessarily installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from herdr_engineering.fleet_inventory import (  # noqa: E402,F401
    add_host,
    list_hosts,
    main,
    remove_host,
    write_host_vars,
)

if __name__ == "__main__":
    sys.exit(main())
