"""Command-line entry point."""

import sys
from pathlib import Path  # noqa: TC003  (Typer resolves annotations at runtime)
from typing import Annotated

import typer
from loguru import logger

from r53_sync import __version__
from r53_sync.config import load_settings
from r53_sync.errors import ConfigError, R53SyncError
from r53_sync.sync import run

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2

LOG_FORMAT = "{level: <8} {message}"

app = typer.Typer(
    add_completion=False,
    help="Point <short-hostname>.<domain> at this host's Tailscale IPs in Route53.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"r53-sync {__version__}")
        raise typer.Exit(EXIT_OK)


@app.command()
def main(  # noqa: PLR0913
    *,
    config: Annotated[Path | None, typer.Option(help="Path to config.toml.")] = None,
    domain: Annotated[str | None, typer.Option(help="Domain the host record lives in.")] = None,
    zone_id: Annotated[
        str | None, typer.Option(help="Route53 hosted zone ID (skips lookup).")
    ] = None,
    ttl: Annotated[int | None, typer.Option(min=1, help="Record TTL in seconds.")] = None,
    profile: Annotated[str | None, typer.Option(help="AWS profile name.")] = None,
    tailscale_path: Annotated[
        Path | None, typer.Option(help="Absolute path of the tailscale binary.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show changes without applying them.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite a non-Tailscale record.")
    ] = False,
    delete: Annotated[bool, typer.Option("--delete", help="Delete this host's records.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging.")] = False,
    version: Annotated[  # noqa: ARG001
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
        ),
    ] = False,
) -> None:
    """Sync this host's Route53 records."""
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO", format=LOG_FORMAT)
    try:
        settings = load_settings(
            config,
            {
                "domain": domain,
                "zone_id": zone_id,
                "ttl": ttl,
                "aws_profile": profile,
                "tailscale_path": tailscale_path,
            },
        )
        code = run(settings, dry_run=dry_run, force=force, delete=delete)
    except ConfigError as exc:
        logger.error("{}", exc)
        raise typer.Exit(EXIT_CONFIG_ERROR) from exc
    except R53SyncError as exc:
        logger.error("{}", exc)
        raise typer.Exit(EXIT_RUNTIME_ERROR) from exc
    raise typer.Exit(code)
