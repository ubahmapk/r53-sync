from typing import TYPE_CHECKING

import pytest

from r53_sync.config import default_config_path, load_settings
from r53_sync.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point platformdirs' per-user config directory at a temp dir."""
    root = tmp_path / "userconfig"
    monkeypatch.setattr("r53_sync.config.user_config_path", lambda appname: root / appname)
    return root


def write_config(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_default_path_comes_from_platformdirs(xdg: Path) -> None:
    assert default_config_path() == xdg / "r53-sync" / "config.toml"


def test_default_path_is_a_per_user_toml_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # use the real platformdirs lookup, no filesystem access
    path = default_config_path()
    assert path.name == "config.toml"
    assert path.parent.name == "r53-sync"
    assert path.is_absolute()


def test_loads_default_file(xdg: Path) -> None:
    write_config(xdg / "r53-sync" / "config.toml", 'domain = "example.com"\nttl = 120\n')
    settings = load_settings(None, {})
    assert (settings.domain, settings.ttl) == ("example.com", 120)


def test_cli_overrides_beat_file(tmp_path: Path) -> None:
    path = write_config(tmp_path / "c.toml", 'domain = "file.com"\nttl = 120\n')
    settings = load_settings(path, {"domain": "cli.com", "ttl": None, "zone_id": None})
    assert (settings.domain, settings.ttl) == ("cli.com", 120)


def test_cli_only_works_without_a_file() -> None:
    assert load_settings(None, {"domain": "example.com"}).domain == "example.com"


def test_missing_domain_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="domain"):
        load_settings(None, {})


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml", {"domain": "example.com"})


def test_invalid_toml(tmp_path: Path) -> None:
    path = write_config(tmp_path / "c.toml", "domain = ")
    with pytest.raises(ConfigError, match="cannot parse"):
        load_settings(path, {})


def test_non_utf8_file_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_bytes(b'domain = "\xff\xfe"\n')
    with pytest.raises(ConfigError, match="cannot parse"):
        load_settings(path, {})


# --- layering: CLI > environment > file > defaults ---------------------------------------


def test_environment_beats_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path / "c.toml", 'domain = "file.com"\nttl = 120\n')
    monkeypatch.setenv("R53_SYNC_DOMAIN", "env.com")
    settings = load_settings(path, {})
    assert (settings.domain, settings.ttl) == ("env.com", 120)


def test_cli_beats_environment_beats_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path / "c.toml", 'domain = "file.com"\nttl = 120\nzone_id = "ZFILE"\n')
    monkeypatch.setenv("R53_SYNC_DOMAIN", "env.com")
    monkeypatch.setenv("R53_SYNC_TTL", "60")
    monkeypatch.setenv("R53_SYNC_ZONE_ID", "ZENV")
    settings = load_settings(path, {"domain": "cli.com", "ttl": None, "zone_id": None})
    # domain: CLI wins; ttl and zone_id: CLI absent, environment beats file.
    assert (settings.domain, settings.ttl, settings.zone_id) == ("cli.com", 60, "ZENV")


def test_defaults_apply_when_no_layer_sets_a_value() -> None:
    settings = load_settings(None, {"domain": "example.com"})
    assert (settings.ttl, settings.zone_id, settings.aws_profile) == (300, None, None)


def test_environment_only_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R53_SYNC_DOMAIN", "env.com")
    monkeypatch.setenv("R53_SYNC_AWS_PROFILE", "work")
    settings = load_settings(None, {})
    assert (settings.domain, settings.aws_profile) == ("env.com", "work")


def test_unprefixed_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMAIN", "wrong.com")
    monkeypatch.setenv("TTL", "1")
    with pytest.raises(ConfigError, match="domain"):
        load_settings(None, {})


def test_all_digit_zone_id_from_environment_is_kept_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("R53_SYNC_ZONE_ID", "1234567890")  # Dynaconf parses this as an int
    assert load_settings(None, {"domain": "example.com"}).zone_id == "1234567890"


def test_bad_environment_value_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R53_SYNC_TTL", "soon")
    with pytest.raises(ConfigError, match="ttl"):
        load_settings(None, {"domain": "example.com"})


def test_unknown_environment_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R53_SYNC_DOMIAN", "typo.com")
    with pytest.raises(ConfigError, match="domian"):
        load_settings(None, {"domain": "example.com"})


# --- Dynaconf behaviors the docs do not state (verified on 3.3.5) ------------------------


def test_local_sibling_file_overrides_the_config_file(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.toml", 'domain = "file.com"\nttl = 120\n')
    write_config(tmp_path / "config.local.toml", "ttl = 999\n")
    assert load_settings(path, {}).ttl == 999


def test_settings_toml_in_the_working_directory_is_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path / "work" / "settings.toml", 'domain = "cwd.com"\n')
    write_config(tmp_path / "work" / ".secrets.toml", 'domain = "secret.com"\n')
    monkeypatch.chdir(tmp_path / "work")
    with pytest.raises(ConfigError, match="domain"):
        load_settings(None, {})


def test_dotenv_file_is_not_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path / "work" / ".env", "R53_SYNC_DOMAIN=dotenv.com\n")
    monkeypatch.chdir(tmp_path / "work")
    with pytest.raises(ConfigError, match="domain"):
        load_settings(None, {})


def test_unreadable_file(tmp_path: Path) -> None:
    path = write_config(tmp_path / "c.toml", 'domain = "example.com"\n')
    path.chmod(0)
    try:
        with pytest.raises(ConfigError, match="cannot read"):
            load_settings(path, {})
    finally:
        path.chmod(0o600)


def test_validation_errors_name_the_field(tmp_path: Path) -> None:
    path = write_config(tmp_path / "c.toml", 'domain = "example.com"\nttl = 0\nbogus = 1\n')
    with pytest.raises(ConfigError) as exc:
        load_settings(path, {})
    assert "ttl" in str(exc.value)
    assert "bogus" in str(exc.value)


def test_tailscale_path_from_environment_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R53_SYNC_TAILSCALE_PATH", "/env/tailscale")
    assert str(load_settings(None, {"domain": "example.com"}).tailscale_path) == "/env/tailscale"
    from_cli = load_settings(None, {"domain": "example.com", "tailscale_path": "/cli/tailscale"})
    assert str(from_cli.tailscale_path) == "/cli/tailscale"
