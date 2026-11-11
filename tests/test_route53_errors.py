from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from botocore.exceptions import ClientError

from r53_sync.errors import Route53Error
from r53_sync.models import Change, RecordSet
from r53_sync.route53 import apply_changes, find_zone_id, make_client

if TYPE_CHECKING:
    from mypy_boto3_route53 import Route53Client
    from pytest_mock import MockerFixture


def client_error(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, operation)


def test_unknown_profile_is_a_route53_error() -> None:
    with pytest.raises(Route53Error, match="cannot create AWS client"):
        make_client("no-such-profile")


def test_zone_listing_failure(client: Route53Client, mocker: MockerFixture) -> None:
    mocker.patch.object(client, "get_paginator", side_effect=client_error("ListHostedZones"))
    with pytest.raises(Route53Error, match="cannot list hosted zones"):
        find_zone_id(client, "testhost.example.com")


def test_apply_failure(client: Route53Client, mocker: MockerFixture) -> None:
    mocker.patch.object(
        client, "change_resource_record_sets", side_effect=client_error("ChangeResourceRecordSets")
    )
    change = Change(
        action="UPSERT",
        record=RecordSet(name="testhost.example.com", type="A", ttl=300, values=("100.64.0.1",)),
    )
    with pytest.raises(Route53Error, match="cannot apply changes"):
        apply_changes(client, "ZABC", "testhost.example.com", [change])
