from __future__ import annotations

import os
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mypy_boto3_route53 import Route53Client


@pytest.fixture(autouse=True)
def no_ambient_r53_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not inherit R53_SYNC_* variables from the developer's shell."""
    for name in [key for key in os.environ if key.startswith("R53_SYNC_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def isolated_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch real AWS credentials or config during tests."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def client() -> Iterator[Route53Client]:
    with mock_aws():
        yield boto3.client("route53")


def create_zone(client: Route53Client, name: str, *, private: bool = False) -> str:
    """Create a hosted zone and return its bare zone ID."""
    if private:
        response = client.create_hosted_zone(
            Name=name,
            CallerReference=f"ref-{name}-private",
            HostedZoneConfig={"PrivateZone": True},
            VPC={"VPCRegion": "us-east-1", "VPCId": "vpc-12345678"},
        )
    else:
        response = client.create_hosted_zone(Name=name, CallerReference=f"ref-{name}-{id(client)}")
    return response["HostedZone"]["Id"].removeprefix("/hostedzone/")


@pytest.fixture
def zone_id(client: Route53Client) -> str:
    return create_zone(client, "example.com.")


def put_record(  # noqa: PLR0913
    client: Route53Client,
    zone: str,
    name: str,
    record_type: str,
    values: list[str],
    *,
    ttl: int = 300,
    set_identifier: str | None = None,
) -> None:
    record: dict[str, object] = {
        "Name": name,
        "Type": record_type,
        "TTL": ttl,
        "ResourceRecords": [{"Value": value} for value in values],
    }
    if set_identifier:
        record["SetIdentifier"] = set_identifier
        record["Weight"] = 10
    client.change_resource_record_sets(
        HostedZoneId=zone,
        ChangeBatch={"Changes": [{"Action": "UPSERT", "ResourceRecordSet": record}]},  # ty: ignore[invalid-argument-type]
    )


def host_records(client: Route53Client, zone: str, name: str) -> dict[str, tuple[int, list[str]]]:
    """Return {type: (ttl, values)} for records named ``name`` (without the trailing dot)."""
    result: dict[str, tuple[int, list[str]]] = {}
    for page in client.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone):
        for raw in page["ResourceRecordSets"]:
            if raw["Name"].rstrip(".") == name:
                values = sorted(record["Value"] for record in raw.get("ResourceRecords", []))
                result[raw["Type"]] = (raw.get("TTL", 0), values)
    return result
