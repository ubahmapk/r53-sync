"""Keep a host's Route53 A/AAAA records pointed at its Tailscale addresses."""

from importlib.metadata import version

__version__ = version("r53-sync")

__all__ = ["__version__"]
