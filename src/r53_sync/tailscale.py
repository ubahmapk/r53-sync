"""Read this host's Tailscale addresses via ``tailscale status --json``.

Tailscale has no Python client, so the CLI is the supported interface. The call uses a list
argument (no shell) and a timeout. Pass an absolute ``binary`` (the ``tailscale_path`` setting)
for any job that runs as root: without one, the binary is found by searching PATH and a few
well-known directories, some of which an ordinary user can write to.
"""

import ipaddress
import json
import os
import shutil
import subprocess
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

from loguru import logger
from pydantic import ValidationError

from r53_sync.errors import TailscaleError
from r53_sync.models import TAILSCALE_V4, TAILSCALE_V6, Addresses, TailscaleStatus

TIMEOUT_SECONDS = 10
KNOWN_BINARIES = (
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def _warn_if_root(found: str) -> None:
    if os.geteuid() == 0:
        logger.warning(
            "running as root and using {} found by searching; set tailscale_path to a "
            "root-owned binary so a user-writable directory cannot change what runs",
            found,
        )


def find_binary() -> str:
    """Locate the tailscale binary on PATH or in well-known install locations."""
    found = shutil.which("tailscale")
    if found:
        _warn_if_root(found)
        return found
    for candidate in KNOWN_BINARIES:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            _warn_if_root(candidate)
            return candidate
    msg = "tailscale is not installed (binary not found on PATH or in standard locations)"
    raise TailscaleError(msg)


def _run_status(binary: str) -> str:
    # The macOS Tailscale binary decides between GUI app and CLI from terminal-related
    # environment variables, which a launchd job lacks. TAILSCALE_BE_CLI=1 forces CLI mode
    # (documented at https://tailscale.com/kb/1080/cli) and is ignored elsewhere.
    env = {**os.environ, "TAILSCALE_BE_CLI": "1"}
    try:
        proc = subprocess.run(  # noqa: S603  (fixed argv, absolute path, no shell)
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"'tailscale status' timed out after {TIMEOUT_SECONDS}s"
        raise TailscaleError(msg) from exc
    except OSError as exc:
        msg = f"cannot run {binary}: {exc.strerror or exc}"
        raise TailscaleError(msg) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        msg = f"'tailscale status' failed: {detail}"
        raise TailscaleError(msg)
    return proc.stdout


def parse_status(raw: str) -> Addresses:
    """Validate ``tailscale status --json`` output and return this node's addresses."""
    try:
        status = TailscaleStatus.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        msg = f"unexpected output from 'tailscale status --json': {exc}"
        raise TailscaleError(msg) from exc
    if status.backend_state != "Running":
        msg = f"Tailscale is not connected (state: {status.backend_state})"
        raise TailscaleError(msg)
    parsed: list[IPv4Address | IPv6Address] = []
    for text in (status.self_node.tailscale_ips if status.self_node else None) or []:
        try:
            parsed.append(ipaddress.ip_address(text))
        except ValueError as exc:
            msg = f"'tailscale status' returned an invalid address {text!r}"
            raise TailscaleError(msg) from exc
    ipv4 = next((ip for ip in parsed if isinstance(ip, IPv4Address) and ip in TAILSCALE_V4), None)
    ipv6 = next((ip for ip in parsed if isinstance(ip, IPv6Address) and ip in TAILSCALE_V6), None)
    if ipv4 is None:
        msg = "Tailscale is running but this node has no Tailscale IPv4 address"
        raise TailscaleError(msg)
    return Addresses(ipv4=ipv4, ipv6=ipv6)


def get_addresses(binary: str | None = None) -> Addresses:
    """Return this host's Tailscale addresses, or raise ``TailscaleError``."""
    return parse_status(_run_status(binary or find_binary()))
