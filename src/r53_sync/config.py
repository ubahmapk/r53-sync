"""Merge settings from the config file, environment variables and CLI flags using Dynaconf.

Precedence, highest first: CLI flags, ``R53_SYNC_*`` environment variables, the TOML config
file, built-in defaults (applied by the ``Settings`` model). The merged result is validated
by pydantic, which also rejects unknown keys.
"""

from typing import TYPE_CHECKING

from dynaconf import Dynaconf
from platformdirs import user_config_path
from pydantic import ValidationError

from r53_sync.errors import ConfigError
from r53_sync.models import Settings

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

APP_NAME = "r53-sync"
ENV_PREFIX = "R53_SYNC"


def default_config_path() -> Path:
    """Return the per-user config file path as chosen by platformdirs.

    Linux: ``$XDG_CONFIG_HOME/r53-sync/config.toml`` (default ``~/.config/...``).
    macOS: ``~/Library/Application Support/r53-sync/config.toml``.
    """
    return user_config_path(APP_NAME) / "config.toml"


def _check_readable(path: Path) -> None:
    """Fail loudly: Dynaconf only warns and loads nothing when a file cannot be read."""
    try:
        with path.open("rb"):
            pass
    except OSError as exc:
        msg = f"{path}: cannot read config file: {exc.strerror or exc}"
        raise ConfigError(msg) from exc


def load_settings(config_path: Path | None, overrides: Mapping[str, object]) -> Settings:
    """Layer file, environment and CLI values (in that order) and validate the result.

    An explicit ``config_path`` must exist; the default path is optional. ``None`` values in
    ``overrides`` mean "flag not given" and never mask a lower layer.
    """
    path = config_path if config_path is not None else default_config_path()
    files: list[str] = []
    if path.is_file():
        _check_readable(path)
        files.append(str(path))
    elif config_path is not None:
        msg = f"{path}: config file not found"
        raise ConfigError(msg)
    try:
        layered = Dynaconf(
            settings_files=files,
            envvar_prefix=ENV_PREFIX,
            environments=False,
            core_loaders=["TOML"],
        )
        layered.update({key: value for key, value in overrides.items() if value is not None})
        merged = {key.lower(): value for key, value in layered.as_dict().items()}
    except ValueError as exc:  # TOMLDecodeError and UnicodeDecodeError both subclass ValueError
        msg = f"{path}: cannot parse config file: {exc}"
        raise ConfigError(msg) from exc
    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors()
        )
        msg = f"invalid configuration: {details}"
        raise ConfigError(msg) from exc
