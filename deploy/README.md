# Deploying r53-sync

Files in this directory:

| File | Purpose |
| --- | --- |
| `systemd/r53-sync.service` | Linux: the one-shot job, sandboxed, run as a dynamic user |
| `systemd/r53-sync.timer` | Linux: 2 minutes after boot, then every 12 hours |
| `launchd/dev.r53-sync.plist` | macOS: a LaunchDaemon, at load and then every 12 hours |
| `iam-policy.json` | Per-host IAM policy template |
| `config.example.toml` | Example configuration |

Everything here is designed to be written by a configuration manager (Ansible, Salt, Puppet):
fixed paths and no interactive steps. The shell commands below that create the config and
credentials files do so only if the file is missing, so re-running them never overwrites an
edited config or a host's keys; a configuration manager should use its own equivalent (a
template task that does not replace an existing credentials file).

> **Verification status:** the unit files, plist and policy are syntax-checked and covered by
> tests that pin their key settings. They have **not** been run on a real Linux host, macOS
> daemon or AWS account yet. See [Not yet verified](#not-yet-verified).

## One IAM user per host

Render `iam-policy.json` for each host by replacing two placeholders:

- `<ZONE_ID>` with the hosted zone ID (both occurrences)
- `<HOST_FQDN>` with the host's record name: lowercase, **no trailing dot**, for example
  `testhost.example.com` (the format AWS requires for this condition key)

If the host's config sets `zone_id`, delete the `FindHostedZone` statement.

The policy lets the host change only that name, only `A` and `AAAA` records, and only `UPSERT`
and `DELETE`. It can still read every record in the zone (`ListResourceRecordSets` cannot be
limited by name), which reveals nothing that a public zone does not already publish.

Create the user, attach the policy and create an access key with your normal AWS tooling.

## Credentials

Both platforms give the program the IAM user's keys as a file in AWS shared-credentials
format, and point boto3 at it with `AWS_SHARED_CREDENTIALS_FILE`. The program never reads the
file itself; boto3 does. The file is root-owned with mode 0600:

```ini
[default]
aws_access_key_id = ...
aws_secret_access_key = ...
```

- **Linux:** `/etc/r53-sync/aws-credentials`, delivered with `LoadCredential=`. The service
  manager reads it and gives the service alone a read-only copy at `%d/aws-credentials`. systemd
  documents that environment variables are unsuitable for secrets (visible over D-Bus,
  inherited by child processes); credentials are not. **Requires systemd 247 or newer.**
- **macOS:** `/usr/local/etc/r53-sync/aws-credentials`, read by the root daemon.
- **Metadata lookup:** both set `AWS_EC2_METADATA_DISABLED=true`. When boto3 finds no
  credentials it otherwise probes the EC2 metadata address (`169.254.169.254`); AWS documents
  this setting for hosts that are not on EC2.
- **Precedence:** boto3 checks environment variables before the credentials file, so an
  `AWS_ACCESS_KEY_ID` in the environment would override this file. The shipped units never set one.
- **A missing or unreadable file:** systemd's docs say a missing credential is non-fatal only
  when the source path is omitted or is a plain credential name, so an absolute path should fail
  the service at start (not yet confirmed on a real host). On macOS boto3 finds no credentials
  and the run fails with "Unable to locate credentials".

### Rotating a host's key

An IAM user can hold two access keys, which makes rotation safe. AWS's IAM best-practices page
gives no fixed rotation interval (it says to update keys when needed, for example when someone
leaves or a key may be exposed), so choose your own schedule:

1. Create a second access key for the host's IAM user (`aws iam create-access-key`).
2. Replace the contents of the host's credentials file with the new key.
3. Run the service once and confirm it succeeds. Check the old key's last use
   (`aws iam get-access-key-last-used`).
4. Make the old key inactive (`aws iam update-access-key --status Inactive`) and wait.
5. If nothing broke, delete it (`aws iam delete-access-key`).

AWS's own guidance is that long-term IAM user keys are a last resort and that IAM Roles
Anywhere is the stated alternative for servers outside AWS. It needs a certificate authority,
a trust anchor and a certificate per host. This tool would use it unchanged through
`credential_process`, but that is untested here and its cost was not checked.

## Linux (systemd)

Install the program into its own virtual environment so the sandboxed service can read it
(a `ProtectHome=yes` service cannot run anything under `/home`):

```sh
sudo UV_PYTHON_INSTALL_DIR=/opt/uv/python uv venv --python 3.14 /opt/r53-sync
sudo UV_PYTHON_INSTALL_DIR=/opt/uv/python uv pip install --python /opt/r53-sync/bin/python \
    /path/to/r53-sync        # or: git+https://github.com/ubahmapk/r53-sync
```

Configuration and credentials (systemd 247 or newer):

```sh
sudo install -d -m 0755 /etc/r53-sync
test -e /etc/r53-sync/config.toml || \
    sudo install -m 0644 config.example.toml /etc/r53-sync/config.toml     # then edit it
test -e /etc/r53-sync/aws-credentials || \
    sudo install -m 0600 -o root -g root /dev/null /etc/r53-sync/aws-credentials
# then put the IAM user's keys in that file in the format shown under "Credentials" above
```

Units, a trial run, then the timer:

```sh
sudo install -m 0644 systemd/r53-sync.service systemd/r53-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo /opt/r53-sync/bin/r53-sync --config /etc/r53-sync/config.toml --dry-run
sudo systemctl start r53-sync.service && journalctl -u r53-sync.service -n 20
sudo systemctl enable --now r53-sync.timer
```

### Changing the interval

Do not edit the shipped timer; add a drop-in. An empty assignment resets **all** timer
settings (including `OnBootSec=`), so restate the ones you want:

```ini
# /etc/systemd/system/r53-sync.timer.d/interval.conf
[Timer]
OnBootSec=
OnBootSec=2min
OnUnitActiveSec=6h
```

Then `sudo systemctl daemon-reload && sudo systemctl restart r53-sync.timer`.

### Retries

A failed run (for example `tailscaled` still starting after boot) is retried every 60 seconds,
up to 5 times in 10 minutes, then the unit stays failed until the next timer run. Exit code 2
(configuration error) is never retried.

## macOS (launchd)

```sh
sudo UV_PYTHON_INSTALL_DIR=/opt/uv/python uv venv --python 3.14 /opt/r53-sync
sudo UV_PYTHON_INSTALL_DIR=/opt/uv/python uv pip install --python /opt/r53-sync/bin/python \
    /path/to/r53-sync
sudo install -d -m 0755 /usr/local/etc/r53-sync
test -e /usr/local/etc/r53-sync/config.toml || \
    sudo install -m 0644 config.example.toml /usr/local/etc/r53-sync/config.toml   # then edit it
test -e /usr/local/etc/r53-sync/aws-credentials || \
    sudo install -m 0600 -o root -g wheel /dev/null /usr/local/etc/r53-sync/aws-credentials
# then put the IAM user's keys in that file in the format shown under "Credentials" above
# and set tailscale_path in config.toml (see "The daemon runs as root" below)
sudo install -m 0644 -o root -g wheel launchd/dev.r53-sync.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/dev.r53-sync.plist
```

To change the interval, edit `StartInterval` (seconds) and bootout/bootstrap again. launchd
does not replay an interval that elapsed while the Mac slept. Logs go to
`/var/log/r53-sync.log`, which nothing rotates.

### The daemon runs as root

The LaunchDaemon runs as root and executes the `tailscale` binary. If r53-sync located that
binary by searching, anyone who can write to a directory it searched could replace it and run
code as root at the next scheduled run. Homebrew's `/opt/homebrew/bin` (and often
`/usr/local/bin`) belong to the login user, so they must not be trusted. Therefore:

- the plist's `PATH` lists only root-owned system directories, and
- you should set `tailscale_path` in `config.toml` to a binary only root or an administrator
  can change, for example `/Applications/Tailscale.app/Contents/MacOS/Tailscale`. Check
  `ls -ld` on it and on its parent directories first; administrators can already become root,
  ordinary users must not be able to write there.

When r53-sync runs as root and has to search for the binary, it logs a warning saying so. On
Linux the service is not root (it runs as a dynamic user), so the risk does not apply there.

## Not yet verified

- The service and timer on a real host: `systemd-analyze verify`, `systemd-analyze security
  r53-sync.service`, and a run as the dynamic user. In particular:
  - that `LoadCredential=` reads the root-owned 0600 `/etc/r53-sync/aws-credentials` and that
    boto3 can read the copy at `%d/aws-credentials` as the dynamic user (the systemd docs say
    the source "do[es] not have to be directly accessible to the unit's processes" and show
    `%d` inside `Environment=`, but not the file mode or owner of the copy),
  - that the dynamic user can read `tailscale status` (Tailscale's source grants read access to
    any local user on the world-connectable socket, but the docs do not say so),
  - that the socket and address-family restrictions do not break name resolution.
- `SystemCallFilter=` and `MemoryDenyWriteExecute=` are deliberately left off until tested.
- The IAM policy against real AWS: an allowed change must succeed, and a change to another
  name, another record type or a `CREATE` must be denied. The AWS Service Authorization
  Reference page for Route 53 would not load during review, so that `ListHostedZones` needs
  `Resource: "*"` comes from AWS's general behavior, not from a page read here.
- launchd: that a root daemon can run the Tailscale CLI, notably for the App Store build of
  Tailscale (the tool sets `TAILSCALE_BE_CLI=1`, as Tailscale documents for scripts).
- `WantedBy=timers.target` and `launchctl bootstrap` are standard practice but were not
  confirmed from the pages read.
