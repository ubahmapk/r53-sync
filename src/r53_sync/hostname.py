"""Derive the short host name used as the DNS host entry."""

import socket

from r53_sync.errors import HostnameError
from r53_sync.models import DNS_LABEL

_RESERVED = frozenset({"localhost"})


def short_hostname(raw: str | None = None) -> str:
    """Return the OS hostname up to the first dot, lowercased and validated as a DNS label."""
    source = socket.gethostname() if raw is None else raw
    name = source.split(".", 1)[0].strip().lower()
    if not name:
        msg = "the machine's hostname is empty"
        raise HostnameError(msg)
    if name in _RESERVED:
        msg = f"hostname {name!r} is reserved and cannot be published"
        raise HostnameError(msg)
    if not DNS_LABEL.fullmatch(name):
        msg = f"hostname {name!r} is not a valid DNS label (letters, digits and hyphens only)"
        raise HostnameError(msg)
    return name
