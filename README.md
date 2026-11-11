# r53-sync

Keep `<short-hostname>.<domain>` in Route53 pointed at this host's Tailscale addresses
(A and AAAA). It is a one-shot command meant to run from a systemd timer or launchd job
(every 12 hours by default; the scheduler owns the interval and it is configurable).

> **Status:** core behavior implemented and tested against a mocked Route53. The systemd
> units, launchd plist and IAM policy are in [`deploy/`](deploy/README.md) but have not been
> run on a real host or against real AWS yet.

## Install

```sh
uv tool install git+https://github.com/ubahmapk/r53-sync     # Python 3.14 or later
```

For scheduled use on a host, install system-wide as described in
[`deploy/README.md`](deploy/README.md).

## Usage

```sh
r53-sync --domain example.com --dry-run   # show what would change
r53-sync --domain example.com             # apply
r53-sync --domain example.com --delete    # remove this host's A/AAAA records
```

The config file lives in the per-user config directory chosen by
[platformdirs](https://pypi.org/project/platformdirs/): `$XDG_CONFIG_HOME/r53-sync/config.toml`
(default `~/.config/r53-sync/config.toml`) on Linux, and
`~/Library/Application Support/r53-sync/config.toml` on macOS. Use `--config` for another
path. CLI flags override the file:

```toml
domain = "example.com"      # required
zone_id = "Z0123456789ABC"  # optional; skips the zone lookup
ttl = 300                   # optional, seconds
aws_profile = "default"     # optional; otherwise the boto3 default credential chain
tailscale_path = "/usr/bin/tailscale"  # optional absolute path; pin it for jobs that run as root
```

Settings are merged by [Dynaconf](https://www.dynaconf.com/). Precedence, highest first:

1. CLI flags
2. Environment variables: `R53_SYNC_DOMAIN`, `R53_SYNC_ZONE_ID`, `R53_SYNC_TTL`,
   `R53_SYNC_AWS_PROFILE`, `R53_SYNC_TAILSCALE_PATH` (the prefix is required and
   case-sensitive)
3. The config file (a `config.local.toml` next to it, if present, overrides it; Dynaconf
   loads that sibling automatically)
4. Defaults (`ttl = 300`)

Nothing else is read: no `settings.toml` or `.secrets.toml` from the working directory, and
no `.env` file. The merged result is validated by pydantic, so unknown keys (in the file or
as `R53_SYNC_*` variables) and bad values fail loudly with exit code 2. Environment values
are parsed as TOML, so `R53_SYNC_TTL=60` is a number; an unreadable or unparsable config
file is an error rather than being skipped.

## Behavior

- **Hostname:** OS hostname up to the first dot, lowercased and validated as a DNS label.
  `localhost` and invalid names are refused.
- **Addresses:** read once from `tailscale status --json`; requires `BackendState == "Running"`.
  If Tailscale is missing, stopped or has no IPv4 address the command exits 1 and touches
  nothing. There is no fallback address. A missing IPv6 address logs a warning and leaves
  any existing AAAA record alone.
- **Safety:** an existing record is only overwritten if every current value is a Tailscale
  address (100.64.0.0/10, fd7a:115c:a1e0::/48). Anything else at that name stops the run
  unless `--force`. CNAMEs, alias records and routing-policy records are never touched.
  Both record types are planned first, so a refusal changes nothing.
- **Writes:** one atomic change batch (UPSERT, or DELETE for `--delete`). No write at all
  when records and TTL already match. `--delete` does not need Tailscale.
- **Zone lookup:** longest matching public zone for the host's FQDN; private zones are
  ignored; two public zones with the same name is an error (set `zone_id`).
- **Public zone note:** records in a public zone are visible to anyone, though Tailscale
  addresses are only reachable from inside your tailnet.

Exit codes: `0` ok or unchanged, `1` runtime error, `2` config error.

## AWS permissions

Use one IAM user per host. Needed actions:

- `route53:ListHostedZones` on `*` (not needed if `zone_id` is set)
- `route53:ListResourceRecordSets` and `route53:ChangeResourceRecordSets` on the hosted zone

Credentials come from boto3's default chain (environment, `~/.aws`, profile). The shipped
systemd unit and launchd plist deliver them as a root-only credentials file instead of
environment variables; see [`deploy/README.md`](deploy/README.md#credentials).

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run ty check
uv run pytest
```

Requires Python 3.14 or later. Stack: Typer (CLI), loguru (logging, to stderr), pydantic
(validation), platformdirs (config path), Dynaconf (settings merge), boto3 (Route53), moto (tests).

## License

MIT
