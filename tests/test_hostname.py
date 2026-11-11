import pytest

from r53_sync.errors import HostnameError
from r53_sync.hostname import short_hostname


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("testhost", "testhost"),
        ("Testhost.local", "testhost"),
        ("web-01.example.com", "web-01"),
        ("  Mixed.Case  ", "mixed"),
    ],
)
def test_valid_hostnames(raw: str, expected: str) -> None:
    assert short_hostname(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".local",
        "localhost",
        "LOCALHOST.localdomain",
        "my_host",
        "-bad",
        "bad-",
        "a" * 64,
    ],
)
def test_invalid_hostnames(raw: str) -> None:
    with pytest.raises(HostnameError):
        short_hostname(raw)


def test_defaults_to_os_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("r53_sync.hostname.socket.gethostname", lambda: "Box.Lan")
    assert short_hostname() == "box"
