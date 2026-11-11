"""Exception hierarchy. ``ConfigError`` maps to exit code 2, everything else to 1."""


class R53SyncError(Exception):
    """Base class for expected runtime failures."""


class ConfigError(R53SyncError):
    """Missing or invalid configuration."""


class HostnameError(R53SyncError):
    """The machine's hostname cannot be used as a DNS label."""


class TailscaleError(R53SyncError):
    """Tailscale is missing, not running, or returned unusable data."""


class Route53Error(R53SyncError):
    """A Route53 lookup or change failed."""


class ConflictError(R53SyncError):
    """An existing record would be overwritten or cannot be managed safely."""
