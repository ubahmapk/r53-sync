import re
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from r53_sync import __version__, cli
from r53_sync.cli import EXIT_CONFIG_ERROR, EXIT_RUNTIME_ERROR, app
from r53_sync.errors import TailscaleError
from r53_sync.models import Settings

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(output: str) -> str:
    """Strip ANSI styling. Typer forces coloured output when GITHUB_ACTIONS, FORCE_COLOR or
    PY_COLORS is set (typer.rich_utils), which splits option names such as ``--dry-run``
    into styled fragments, so assertions must not depend on the environment."""
    return _ANSI.sub("", output)


@pytest.fixture(autouse=True)
def empty_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "userconfig"
    monkeypatch.setattr("r53_sync.config.user_config_path", lambda appname: root / appname)


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in plain(result.output)


def test_help_lists_flags() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for flag in ("--dry-run", "--force", "--delete", "--zone-id", "--ttl", "--domain"):
        assert flag in plain(result.output)


def test_rejects_non_positive_ttl() -> None:
    assert runner.invoke(app, ["--ttl", "0"]).exit_code == EXIT_CONFIG_ERROR


def test_missing_domain_is_exit_2() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == EXIT_CONFIG_ERROR
    assert "domain" in plain(result.output)


def test_bad_domain_is_exit_2() -> None:
    result = runner.invoke(app, ["--domain", "localhost"])
    assert result.exit_code == EXIT_CONFIG_ERROR


def test_runtime_error_is_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(settings: Settings, **_: bool) -> int:
        raise TailscaleError("Tailscale is not connected (state: Stopped)")

    monkeypatch.setattr(cli, "run", boom)
    result = runner.invoke(app, ["--domain", "example.com"])
    assert result.exit_code == EXIT_RUNTIME_ERROR
    assert "not connected" in plain(result.output)


def test_flags_reach_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run(settings: Settings, **flags: bool) -> int:
        seen.update(settings=settings, **flags)
        return 0

    config = tmp_path / "c.toml"
    config.write_text('domain = "file.example.com"\nttl = 120\n')
    monkeypatch.setattr(cli, "run", fake_run)
    result = runner.invoke(
        app,
        ["--config", str(config), "--ttl", "60", "--dry-run", "--force", "--zone-id", "ZABC123"],
    )
    assert result.exit_code == 0
    settings = seen["settings"]
    assert isinstance(settings, Settings)
    assert (settings.domain, settings.ttl, settings.zone_id) == ("file.example.com", 60, "ZABC123")
    assert (seen["dry_run"], seen["force"], seen["delete"]) == (True, True, False)


def test_verbose_enables_debug_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(settings: Settings, **_: bool) -> int:
        cli.logger.debug("debug-marker")
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    quiet = runner.invoke(app, ["--domain", "example.com"])
    loud = runner.invoke(app, ["--domain", "example.com", "-v"])
    assert "debug-marker" not in plain(quiet.output)
    assert "debug-marker" in plain(loud.output)


def test_tailscale_path_flag_reaches_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(settings: Settings, **_: bool) -> int:
        seen["path"] = settings.tailscale_path
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    result = runner.invoke(app, ["--domain", "example.com", "--tailscale-path", "/opt/ts/ts"])
    assert result.exit_code == 0
    assert str(seen["path"]) == "/opt/ts/ts"


def test_relative_tailscale_path_is_a_config_error() -> None:
    result = runner.invoke(app, ["--domain", "example.com", "--tailscale-path", "tailscale"])
    assert result.exit_code == EXIT_CONFIG_ERROR
