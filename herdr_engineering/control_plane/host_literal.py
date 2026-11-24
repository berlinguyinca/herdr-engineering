"""Host-name classification shared by the control plane and the dashboard proxy."""
from __future__ import annotations

import ipaddress


def is_ip_or_localhost(name: str) -> bool:
    """True for an IP literal or ``localhost``: a name a DNS-rebinding page cannot make point somewhere else."""
    name = name.strip().lower()
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True
