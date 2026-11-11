"""Plan and apply the record changes for this host."""

from typing import TYPE_CHECKING

from loguru import logger

from r53_sync.errors import ConflictError
from r53_sync.hostname import short_hostname
from r53_sync.models import Addresses, Change, RecordSet, Settings, is_tailscale_address
from r53_sync.route53 import apply_changes, find_zone_id, get_records, make_client
from r53_sync.tailscale import get_addresses

if TYPE_CHECKING:
    from collections.abc import Sequence

MANAGED_TYPES = ("A", "AAAA")


def _check_manageable(fqdn: str, existing: Sequence[RecordSet]) -> dict[str, RecordSet]:
    """Index existing A/AAAA records by type; reject anything this tool cannot handle safely."""
    by_type: dict[str, RecordSet] = {}
    for record in existing:
        if record.type == "CNAME":
            msg = f"{fqdn} is a CNAME; a CNAME cannot coexist with A/AAAA records"
            raise ConflictError(msg)
        if record.type not in MANAGED_TYPES:
            continue
        if not record.simple:
            msg = (
                f"{fqdn} has a {record.type} alias or routing-policy record that "
                "r53-sync does not manage"
            )
            raise ConflictError(msg)
        by_type[record.type] = record
    return by_type


def _guard_overwrite(fqdn: str, record: RecordSet, *, force: bool) -> None:
    if force or all(is_tailscale_address(value) for value in record.values):
        return
    msg = (
        f"{fqdn} {record.type} is {', '.join(record.values)}, which is not a Tailscale "
        "address; refusing to overwrite (use --force to override)"
    )
    raise ConflictError(msg)


def plan_upsert(
    fqdn: str,
    existing: Sequence[RecordSet],
    addresses: Addresses,
    *,
    ttl: int,
    force: bool,
) -> list[Change]:
    """Return the changes needed to point ``fqdn`` at ``addresses`` (empty if up to date)."""
    by_type = _check_manageable(fqdn, existing)
    desired: dict[str, tuple[str, ...]] = {"A": (str(addresses.ipv4),)}
    if addresses.ipv6 is not None:
        desired["AAAA"] = (str(addresses.ipv6),)
    else:
        logger.warning("no Tailscale IPv6 address on this node; leaving any AAAA record alone")
    changes: list[Change] = []
    for record_type, values in desired.items():
        current = by_type.get(record_type)
        if current is not None:
            if sorted(current.values) == sorted(values) and current.ttl == ttl:
                continue
            _guard_overwrite(fqdn, current, force=force)
        record = RecordSet(name=fqdn, type=record_type, ttl=ttl, values=values)
        changes.append(Change(action="UPSERT", record=record))
    return changes


def plan_delete(fqdn: str, existing: Sequence[RecordSet], *, force: bool) -> list[Change]:
    """Return DELETE changes for this host's A/AAAA records."""
    by_type = _check_manageable(fqdn, existing)
    changes: list[Change] = []
    for record in by_type.values():
        _guard_overwrite(fqdn, record, force=force)
        changes.append(Change(action="DELETE", record=record))
    return changes


def run(settings: Settings, *, dry_run: bool, force: bool, delete: bool) -> int:
    """Sync (or delete) this host's records. Raises ``R53SyncError`` subclasses on failure."""
    fqdn = f"{short_hostname()}.{settings.domain}"
    binary = str(settings.tailscale_path) if settings.tailscale_path else None
    addresses = None if delete else get_addresses(binary)
    client = make_client(settings.aws_profile)
    zone_id = settings.zone_id or find_zone_id(client, fqdn)
    existing = get_records(client, zone_id, fqdn)
    if addresses is None:
        changes = plan_delete(fqdn, existing, force=force)
    else:
        changes = plan_upsert(fqdn, existing, addresses, ttl=settings.ttl, force=force)
    if not changes:
        logger.info("{} is already up to date", fqdn)
        return 0
    prefix = "[dry-run] would " if dry_run else ""
    for change in changes:
        logger.info(
            "{}{} {} {} -> {} (ttl {})",
            prefix,
            change.action,
            change.record.type,
            fqdn,
            ", ".join(change.record.values),
            change.record.ttl,
        )
    if dry_run:
        return 0
    apply_changes(client, zone_id, fqdn, changes)
    logger.info("applied {} change(s) to {}", len(changes), fqdn)
    return 0
