import pytest
from pydantic import ValidationError

from r53_sync.models import Addresses, Settings, is_tailscale_address


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("100.64.0.1", True),
        ("100.127.255.254", True),
        ("100.128.0.1", False),
        ("192.168.1.10", False),
        ("fd7a:115c:a1e0::1", True),
        ("fd7a:115c:a1e1::1", False),
        ("2001:db8::1", False),
        ("not-an-ip", False),
    ],
)
def test_is_tailscale_address(value: str, expected: bool) -> None:
    assert is_tailscale_address(value) is expected


def test_settings_defaults_and_normalization() -> None:
    settings = Settings(domain=" Example.COM. ")
    assert settings.domain == "example.com"
    assert settings.ttl == 300
    assert settings.zone_id is None
    assert settings.aws_profile is None


@pytest.mark.parametrize("domain", ["", "localhost", "bad_domain.com", "-x.com", "a..com"])
def test_settings_rejects_bad_domain(domain: str) -> None:
    with pytest.raises(ValidationError):
        Settings(domain=domain)


@pytest.mark.parametrize("ttl", [0, -5, 2**31])
def test_settings_rejects_bad_ttl(ttl: int) -> None:
    with pytest.raises(ValidationError):
        Settings(domain="example.com", ttl=ttl)


@pytest.mark.parametrize("zone_id", ["", "abc", "/hostedzone/Z123", "Z" + "A" * 40])
def test_settings_rejects_bad_zone_id(zone_id: str) -> None:
    with pytest.raises(ValidationError):
        Settings(domain="example.com", zone_id=zone_id)


def test_settings_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"domain": "example.com", "dommain": "typo.com"})


def test_addresses_accepts_tailscale_ranges() -> None:
    addresses = Addresses.model_validate({"ipv4": "100.64.1.2", "ipv6": "fd7a:115c:a1e0::2"})
    assert str(addresses.ipv4) == "100.64.1.2"


def test_addresses_ipv6_is_optional() -> None:
    assert Addresses.model_validate({"ipv4": "100.64.1.2"}).ipv6 is None


@pytest.mark.parametrize(
    "data",
    [{"ipv4": "192.168.1.5"}, {"ipv4": "100.64.1.2", "ipv6": "2001:db8::1"}],
)
def test_addresses_reject_non_tailscale(data: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Addresses.model_validate(data)


def test_settings_tailscale_path_must_be_absolute() -> None:
    assert Settings(domain="example.com", tailscale_path="/usr/bin/tailscale").tailscale_path
    with pytest.raises(ValidationError, match="absolute"):
        Settings(domain="example.com", tailscale_path="tailscale")
