"""Validated data models shared across the package."""

import ipaddress
import re
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path  # noqa: TC003  (pydantic resolves annotations at runtime)
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
MAX_TTL = 2_147_483_647


def is_tailscale_address(value: str) -> bool:
    """Return True if ``value`` is an IP address inside Tailscale's address ranges."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, IPv4Address):
        return address in TAILSCALE_V4
    return address in TAILSCALE_V6


class Settings(BaseModel):
    """Effective configuration after merging the config file and CLI flags."""

    # Environment values are TOML-parsed by Dynaconf, so an all-digit zone ID arrives as an int.
    model_config = ConfigDict(frozen=True, extra="forbid", coerce_numbers_to_str=True)

    domain: str
    zone_id: str | None = Field(default=None, pattern=r"^[A-Z0-9]{1,32}$")
    ttl: int = Field(default=300, ge=1, le=MAX_TTL)
    aws_profile: str | None = Field(default=None, min_length=1)
    tailscale_path: Path | None = None

    @field_validator("tailscale_path")
    @classmethod
    def _absolute_tailscale_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            msg = f"{str(value)!r} must be an absolute path"
            raise ValueError(msg)
        return value

    @field_validator("domain")
    @classmethod
    def _valid_domain(cls, value: str) -> str:
        domain = value.strip().lower().rstrip(".")
        labels = domain.split(".")
        if len(labels) < 2 or not all(DNS_LABEL.fullmatch(label) for label in labels):  # noqa: PLR2004
            msg = f"{value!r} is not a valid domain name"
            raise ValueError(msg)
        return domain


class Addresses(BaseModel):
    """This host's Tailscale addresses. IPv4 is required, IPv6 is optional."""

    model_config = ConfigDict(frozen=True)

    ipv4: IPv4Address
    ipv6: IPv6Address | None = None

    @model_validator(mode="after")
    def _within_tailscale_ranges(self) -> Self:
        if self.ipv4 not in TAILSCALE_V4:
            msg = f"{self.ipv4} is not in Tailscale's IPv4 range {TAILSCALE_V4}"
            raise ValueError(msg)
        if self.ipv6 is not None and self.ipv6 not in TAILSCALE_V6:
            msg = f"{self.ipv6} is not in Tailscale's IPv6 range {TAILSCALE_V6}"
            raise ValueError(msg)
        return self


class TailscaleSelf(BaseModel):
    """The ``Self`` node of ``tailscale status --json``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    tailscale_ips: list[str] | None = Field(default=None, alias="TailscaleIPs")


class TailscaleStatus(BaseModel):
    """The subset of ``tailscale status --json`` this tool reads."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    backend_state: str = Field(alias="BackendState")
    self_node: TailscaleSelf | None = Field(default=None, alias="Self")


class RecordSet(BaseModel):
    """A Route53 record set, reduced to what this tool compares and writes."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    ttl: int | None = None
    values: tuple[str, ...] = ()
    simple: bool = True


class Change(BaseModel):
    """One planned change to a record set."""

    model_config = ConfigDict(frozen=True)

    action: Literal["UPSERT", "DELETE"]
    record: RecordSet
