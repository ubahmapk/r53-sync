from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from r53_sync import sync
from r53_sync.errors import ConflictError, Route53Error, TailscaleError
from r53_sync.models import Addresses, Settings
from r53_sync.route53 import find_zone_id, normalize_name

from .conftest import create_zone, host_records, put_record

if TYPE_CHECKING:
    from mypy_boto3_route53 import Route53Client
    from pytest_mock import MockerFixture

FQDN = "testhost.example.com"
ADDRESSES = Addresses.model_validate({"ipv4": "100.64.0.10", "ipv6": "fd7a:115c:a1e0::10"})


@pytest.fixture(autouse=True)
def fake_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "short_hostname", lambda: "testhost")
    monkeypatch.setattr(sync, "get_addresses", lambda _binary=None: ADDRESSES)


def settings(zone: str | None = None, **kwargs: object) -> Settings:
    return Settings.model_validate({"domain": "example.com", "zone_id": zone, **kwargs})


def go(
    config: Settings, *, dry_run: bool = False, force: bool = False, delete: bool = False
) -> int:
    return sync.run(config, dry_run=dry_run, force=force, delete=delete)


# --- creating and updating -------------------------------------------------------------------


def test_creates_a_and_aaaa(client: Route53Client, zone_id: str) -> None:
    assert go(settings()) == 0
    assert host_records(client, zone_id, FQDN) == {
        "A": (300, ["100.64.0.10"]),
        "AAAA": (300, ["fd7a:115c:a1e0::10"]),
    }


def test_second_run_makes_no_changes(
    client: Route53Client, zone_id: str, mocker: MockerFixture
) -> None:
    go(settings())
    spy = mocker.spy(sync, "apply_changes")
    assert go(settings()) == 0
    spy.assert_not_called()


def test_tailscale_ip_change_is_upserted(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, FQDN, "A", ["100.64.9.9"])
    go(settings())
    assert host_records(client, zone_id, FQDN)["A"] == (300, ["100.64.0.10"])


def test_ttl_change_is_applied(client: Route53Client, zone_id: str) -> None:
    go(settings())
    go(settings(ttl=60))
    assert host_records(client, zone_id, FQDN)["A"][0] == 60


def test_other_record_types_are_left_alone(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, FQDN, "TXT", ['"hello"'])
    go(settings())
    records = host_records(client, zone_id, FQDN)
    assert records["TXT"] == (300, ['"hello"'])
    assert "A" in records


def test_other_hostnames_are_untouched(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, "www.example.com", "A", ["203.0.113.5"])
    go(settings())
    assert host_records(client, zone_id, "www.example.com")["A"] == (300, ["203.0.113.5"])


def test_missing_ipv6_skips_aaaa_and_keeps_existing(
    client: Route53Client, zone_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_record(client, zone_id, FQDN, "AAAA", ["fd7a:115c:a1e0::99"])
    monkeypatch.setattr(
        sync,
        "get_addresses",
        lambda _binary=None: Addresses.model_validate({"ipv4": "100.64.0.10"}),
    )
    go(settings())
    records = host_records(client, zone_id, FQDN)
    assert records["A"][1] == ["100.64.0.10"]
    assert records["AAAA"][1] == ["fd7a:115c:a1e0::99"]


def test_dry_run_writes_nothing(client: Route53Client, zone_id: str) -> None:
    assert go(settings(), dry_run=True) == 0
    assert host_records(client, zone_id, FQDN) == {}


# --- overwrite guard -------------------------------------------------------------------------


def test_refuses_to_overwrite_non_tailscale_record(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, FQDN, "A", ["203.0.113.5"])
    with pytest.raises(ConflictError, match="--force"):
        go(settings())
    assert host_records(client, zone_id, FQDN) == {"A": (300, ["203.0.113.5"])}


def test_force_overwrites_non_tailscale_record(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, FQDN, "A", ["203.0.113.5"])
    go(settings(), force=True)
    assert host_records(client, zone_id, FQDN)["A"] == (300, ["100.64.0.10"])


def test_guard_failure_is_all_or_nothing(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, FQDN, "AAAA", ["2001:db8::1"])
    with pytest.raises(ConflictError):
        go(settings())
    assert "A" not in host_records(client, zone_id, FQDN)


def test_cname_conflict(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, FQDN, "CNAME", ["other.example.com"])
    with pytest.raises(ConflictError, match="CNAME"):
        go(settings())


def test_routing_policy_record_is_rejected(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, FQDN, "A", ["100.64.0.77"], set_identifier="blue")
    with pytest.raises(ConflictError, match="routing-policy"):
        go(settings())


# --- delete ----------------------------------------------------------------------------------


def test_delete_removes_a_and_aaaa(client: Route53Client, zone_id: str) -> None:
    go(settings())
    put_record(client, zone_id, FQDN, "TXT", ['"keep"'])
    assert go(settings(), delete=True) == 0
    assert host_records(client, zone_id, FQDN) == {"TXT": (300, ['"keep"'])}


def test_delete_with_nothing_to_do(client: Route53Client, zone_id: str) -> None:
    assert go(settings(), delete=True) == 0


def test_delete_does_not_need_tailscale(
    client: Route53Client, zone_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    go(settings())

    def boom(_binary: str | None = None) -> Addresses:
        raise TailscaleError("tailscale is not installed")

    monkeypatch.setattr(sync, "get_addresses", boom)
    go(settings(), delete=True)
    assert host_records(client, zone_id, FQDN) == {}


def test_delete_refuses_non_tailscale_record(client: Route53Client, zone_id: str) -> None:
    put_record(client, zone_id, FQDN, "A", ["203.0.113.5"])
    with pytest.raises(ConflictError):
        go(settings(), delete=True)
    go(settings(), delete=True, force=True)
    assert host_records(client, zone_id, FQDN) == {}


def test_delete_dry_run(client: Route53Client, zone_id: str) -> None:
    go(settings())
    go(settings(), delete=True, dry_run=True)
    assert "A" in host_records(client, zone_id, FQDN)


# --- failure ordering ------------------------------------------------------------------------


def test_tailscale_failure_happens_before_any_aws_call(
    monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture
) -> None:
    def boom(_binary: str | None = None) -> Addresses:
        raise TailscaleError("Tailscale is not connected (state: Stopped)")

    monkeypatch.setattr(sync, "get_addresses", boom)
    make = mocker.spy(sync, "make_client")
    with pytest.raises(TailscaleError):
        go(settings())
    make.assert_not_called()


# --- tailscale binary ------------------------------------------------------------------------


def test_configured_tailscale_path_is_used(
    client: Route53Client, zone_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def fake(binary: str | None = None) -> Addresses:
        seen.append(binary)
        return ADDRESSES

    monkeypatch.setattr(sync, "get_addresses", fake)
    go(settings(tailscale_path="/opt/ts/tailscale"))
    assert seen == ["/opt/ts/tailscale"]


def test_without_tailscale_path_the_binary_is_searched_for(
    client: Route53Client, zone_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def fake(binary: str | None = None) -> Addresses:
        seen.append(binary)
        return ADDRESSES

    monkeypatch.setattr(sync, "get_addresses", fake)
    go(settings())
    assert seen == [None]


# --- zone lookup -----------------------------------------------------------------------------


def test_explicit_zone_id_skips_lookup(
    client: Route53Client, zone_id: str, mocker: MockerFixture
) -> None:
    lookup = mocker.spy(sync, "find_zone_id")
    go(settings(zone_id))
    lookup.assert_not_called()
    assert "A" in host_records(client, zone_id, FQDN)


def test_no_matching_zone(client: Route53Client) -> None:
    create_zone(client, "other.org.")
    with pytest.raises(Route53Error, match="no public hosted zone"):
        go(settings())


def test_longest_suffix_zone_wins(client: Route53Client) -> None:
    parent = create_zone(client, "example.com.")
    child = create_zone(client, "sub.example.com.")
    assert find_zone_id(client, "testhost.sub.example.com") == child
    assert find_zone_id(client, "testhost.example.com") == parent


def test_subdomain_of_zone_resolves_to_parent(client: Route53Client) -> None:
    parent = create_zone(client, "example.com.")
    assert find_zone_id(client, "testhost.lab.example.com") == parent


def test_private_zones_are_ignored(client: Route53Client) -> None:
    create_zone(client, "example.com.", private=True)
    with pytest.raises(Route53Error, match="no public hosted zone"):
        find_zone_id(client, FQDN)


def test_ambiguous_public_zones(client: Route53Client) -> None:
    create_zone(client, "example.com.")
    client.create_hosted_zone(Name="example.com.", CallerReference="second")
    with pytest.raises(Route53Error, match="multiple public hosted zones"):
        find_zone_id(client, FQDN)


def test_unknown_zone_id_is_a_route53_error(client: Route53Client) -> None:
    with pytest.raises(Route53Error, match="cannot read records"):
        go(settings("ZDOESNOTEXIST"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Testhost.Example.COM.", "testhost.example.com"),
        ("\\052.example.com.", "*.example.com"),
        ("plain.example.com", "plain.example.com"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected
