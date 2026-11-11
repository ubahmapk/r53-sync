"""Thin Route53 wrapper: zone lookup, record reads, and change batches."""

import re
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from r53_sync.errors import Route53Error
from r53_sync.models import Change, RecordSet

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mypy_boto3_route53 import Route53Client
    from mypy_boto3_route53.type_defs import ResourceRecordSetOutputTypeDef

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
CHANGE_COMMENT = "managed by r53-sync"


def normalize_name(name: str) -> str:
    """Lowercase a Route53 name, decode octal escapes and drop the trailing dot."""
    decoded = _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), name)
    return decoded.lower().rstrip(".")


def _zone_key(zone_id: str) -> str:
    return zone_id.removeprefix("/hostedzone/")


def make_client(profile: str | None = None) -> Route53Client:
    """Create a Route53 client using the default credential chain (or a named profile)."""
    try:
        return boto3.Session(profile_name=profile).client("route53")
    except (BotoCoreError, ClientError) as exc:
        msg = f"cannot create AWS client: {exc}"
        raise Route53Error(msg) from exc


def find_zone_id(client: Route53Client, fqdn: str) -> str:
    """Find the public hosted zone that contains ``fqdn`` (longest matching suffix wins)."""
    candidates: list[tuple[str, str]] = []
    try:
        for page in client.get_paginator("list_hosted_zones").paginate():
            for zone in page["HostedZones"]:
                if zone.get("Config", {}).get("PrivateZone"):
                    continue
                zone_name = normalize_name(zone["Name"])
                if fqdn == zone_name or fqdn.endswith(f".{zone_name}"):
                    candidates.append((zone_name, _zone_key(zone["Id"])))
    except (BotoCoreError, ClientError) as exc:
        msg = f"cannot list hosted zones: {exc}"
        raise Route53Error(msg) from exc
    if not candidates:
        msg = f"no public hosted zone found for {fqdn}; check the domain or set zone_id"
        raise Route53Error(msg)
    longest = max(len(name) for name, _ in candidates)
    best = [(name, zone_id) for name, zone_id in candidates if len(name) == longest]
    if len(best) > 1:
        msg = f"multiple public hosted zones named {best[0][0]}; set zone_id explicitly"
        raise Route53Error(msg)
    return best[0][1]


def _to_record_set(raw: ResourceRecordSetOutputTypeDef) -> RecordSet:
    simple = "AliasTarget" not in raw and "SetIdentifier" not in raw
    return RecordSet(
        name=normalize_name(raw["Name"]),
        type=raw["Type"],
        ttl=raw.get("TTL"),
        values=tuple(record["Value"] for record in raw.get("ResourceRecords", [])),
        simple=simple,
    )


def get_records(client: Route53Client, zone_id: str, fqdn: str) -> list[RecordSet]:
    """Return every record set whose name is exactly ``fqdn``.

    The list API starts *at* a name rather than filtering, so stop at the first other name.
    """
    found: list[RecordSet] = []
    try:
        paginator = client.get_paginator("list_resource_record_sets")
        for page in paginator.paginate(HostedZoneId=zone_id, StartRecordName=fqdn):
            for raw in page["ResourceRecordSets"]:
                record = _to_record_set(raw)
                if record.name != fqdn:
                    return found
                found.append(record)
    except (BotoCoreError, ClientError) as exc:
        msg = f"cannot read records for {fqdn}: {exc}"
        raise Route53Error(msg) from exc
    return found


def apply_changes(
    client: Route53Client, zone_id: str, fqdn: str, changes: Sequence[Change]
) -> None:
    """Submit all changes as one atomic batch."""
    batch = [
        {
            "Action": change.action,
            "ResourceRecordSet": {
                "Name": fqdn,
                "Type": change.record.type,
                "TTL": change.record.ttl or 300,
                "ResourceRecords": [{"Value": value} for value in change.record.values],
            },
        }
        for change in changes
    ]
    try:
        client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={"Comment": CHANGE_COMMENT, "Changes": batch},  # ty: ignore[invalid-argument-type]
        )
    except (BotoCoreError, ClientError) as exc:
        msg = f"cannot apply changes to {fqdn}: {exc}"
        raise Route53Error(msg) from exc
