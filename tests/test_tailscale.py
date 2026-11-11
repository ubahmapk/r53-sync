import json
import subprocess
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from r53_sync import tailscale
from r53_sync.errors import TailscaleError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pytest_mock import MockerFixture

RUNNING = {
    "BackendState": "Running",
    "Self": {
        "HostName": "testhost",
        "TailscaleIPs": ["100.64.0.10", "fd7a:115c:a1e0::10"],
    },
}


def status(**overrides: object) -> str:
    return json.dumps({**RUNNING, **overrides})


def test_parse_running_node() -> None:
    addresses = tailscale.parse_status(status())
    assert str(addresses.ipv4) == "100.64.0.10"
    assert str(addresses.ipv6) == "fd7a:115c:a1e0::10"


def test_parse_ipv4_only_node() -> None:
    addresses = tailscale.parse_status(
        status(Self={"TailscaleIPs": ["100.64.0.9"]}),
    )
    assert addresses.ipv6 is None


def test_parse_picks_tailscale_addresses_regardless_of_order() -> None:
    addresses = tailscale.parse_status(
        status(Self={"TailscaleIPs": ["fd7a:115c:a1e0::1", "100.64.0.9"]}),
    )
    assert str(addresses.ipv4) == "100.64.0.9"


@pytest.mark.parametrize("state", ["Stopped", "NeedsLogin", "Starting", "NoState"])
def test_not_running_is_an_error(state: str) -> None:
    with pytest.raises(TailscaleError, match=state):
        tailscale.parse_status(status(BackendState=state))


@pytest.mark.parametrize(
    "self_node",
    [
        None,
        {},
        {"TailscaleIPs": None},
        {"TailscaleIPs": []},
        {"TailscaleIPs": ["fd7a:115c:a1e0::1"]},
    ],
)
def test_no_ipv4_is_an_error(self_node: object) -> None:
    with pytest.raises(TailscaleError, match="IPv4"):
        tailscale.parse_status(status(Self=self_node))


def test_garbage_ip_is_an_error() -> None:
    with pytest.raises(TailscaleError, match="invalid address"):
        tailscale.parse_status(status(Self={"TailscaleIPs": ["nope"]}))


@pytest.mark.parametrize("raw", ["", "not json", "[]", "{}", '{"BackendState": 5}'])
def test_unexpected_output_is_an_error(raw: str) -> None:
    with pytest.raises(TailscaleError, match="unexpected output"):
        tailscale.parse_status(raw)


def completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["tailscale"], returncode, stdout, stderr)


def test_get_addresses_runs_status_json(mocker: MockerFixture) -> None:
    run = mocker.patch("r53_sync.tailscale.subprocess.run", return_value=completed(0, status()))
    addresses = tailscale.get_addresses("/usr/bin/tailscale")
    assert str(addresses.ipv4) == "100.64.0.10"
    args, kwargs = run.call_args
    assert args[0] == ["/usr/bin/tailscale", "status", "--json"]
    assert kwargs["timeout"] == tailscale.TIMEOUT_SECONDS
    assert kwargs.get("shell") is not True


def test_forces_cli_mode_for_the_macos_binary(
    mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")  # the rest of the environment is passed through
    run = mocker.patch("r53_sync.tailscale.subprocess.run", return_value=completed(0, status()))
    tailscale.get_addresses("/usr/bin/tailscale")
    env = run.call_args.kwargs["env"]
    assert env["TAILSCALE_BE_CLI"] == "1"
    assert env["PATH"] == "/usr/bin"


def test_nonzero_exit_reports_stderr(mocker: MockerFixture) -> None:
    mocker.patch(
        "r53_sync.tailscale.subprocess.run", return_value=completed(1, stderr="failed to connect")
    )
    with pytest.raises(TailscaleError, match="failed to connect"):
        tailscale.get_addresses("/usr/bin/tailscale")


def test_timeout(mocker: MockerFixture) -> None:
    mocker.patch(
        "r53_sync.tailscale.subprocess.run",
        side_effect=subprocess.TimeoutExpired("tailscale", 10),
    )
    with pytest.raises(TailscaleError, match="timed out"):
        tailscale.get_addresses("/usr/bin/tailscale")


def test_binary_cannot_execute(mocker: MockerFixture) -> None:
    mocker.patch("r53_sync.tailscale.subprocess.run", side_effect=PermissionError(13, "denied"))
    with pytest.raises(TailscaleError, match="cannot run"):
        tailscale.get_addresses("/usr/bin/tailscale")


def test_find_binary_prefers_path(mocker: MockerFixture) -> None:
    mocker.patch("r53_sync.tailscale.shutil.which", return_value="/somewhere/tailscale")
    assert tailscale.find_binary() == "/somewhere/tailscale"


def test_find_binary_checks_known_locations(mocker: MockerFixture, tmp_path: Path) -> None:
    fake = tmp_path / "tailscale"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    mocker.patch("r53_sync.tailscale.shutil.which", return_value=None)
    mocker.patch.object(tailscale, "KNOWN_BINARIES", (str(fake),))
    assert tailscale.find_binary() == str(fake)


def test_missing_binary_means_not_installed(mocker: MockerFixture) -> None:
    mocker.patch("r53_sync.tailscale.shutil.which", return_value=None)
    mocker.patch.object(tailscale, "KNOWN_BINARIES", ())
    with pytest.raises(TailscaleError, match="not installed"):
        tailscale.find_binary()


# --- running as root --------------------------------------------------------------------


@pytest.fixture
def log_messages() -> Iterator[list[str]]:
    messages: list[str] = []
    sink_id = logger.add(messages.append, format="{message}", level="WARNING")
    yield messages
    logger.remove(sink_id)


def test_root_searching_for_the_binary_logs_a_warning(
    mocker: MockerFixture, log_messages: list[str]
) -> None:
    mocker.patch("r53_sync.tailscale.os.geteuid", return_value=0)
    mocker.patch("r53_sync.tailscale.shutil.which", return_value="/somewhere/tailscale")
    assert tailscale.find_binary() == "/somewhere/tailscale"
    assert any("tailscale_path" in message for message in log_messages)


def test_root_warning_also_applies_to_the_known_locations(
    mocker: MockerFixture, tmp_path: Path, log_messages: list[str]
) -> None:
    fake = tmp_path / "tailscale"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    mocker.patch("r53_sync.tailscale.os.geteuid", return_value=0)
    mocker.patch("r53_sync.tailscale.shutil.which", return_value=None)
    mocker.patch.object(tailscale, "KNOWN_BINARIES", (str(fake),))
    tailscale.find_binary()
    assert any("tailscale_path" in message for message in log_messages)


def test_non_root_search_is_silent(mocker: MockerFixture, log_messages: list[str]) -> None:
    mocker.patch("r53_sync.tailscale.os.geteuid", return_value=1000)
    mocker.patch("r53_sync.tailscale.shutil.which", return_value="/somewhere/tailscale")
    tailscale.find_binary()
    assert log_messages == []


def test_an_explicit_binary_never_triggers_the_search_or_warning(
    mocker: MockerFixture, log_messages: list[str]
) -> None:
    mocker.patch("r53_sync.tailscale.os.geteuid", return_value=0)
    which = mocker.patch("r53_sync.tailscale.shutil.which")
    mocker.patch("r53_sync.tailscale.subprocess.run", return_value=completed(0, status()))
    tailscale.get_addresses("/usr/bin/tailscale")
    which.assert_not_called()
    assert log_messages == []
