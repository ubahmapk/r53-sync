"""Pin the settings in the shipped deploy files that the design depends on."""

import configparser
import json
import plistlib
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any, get_args

import pytest

from r53_sync.config import ENV_PREFIX
from r53_sync.models import Change, Settings

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def read_unit(name: str) -> configparser.ConfigParser:
    # systemd unit files allow repeated keys and case-sensitive names; configparser's
    # non-strict mode keeps the last value, which is enough for these checks.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # ty: ignore[invalid-assignment]
    parser.read(DEPLOY / "systemd" / name, encoding="utf-8")
    return parser


@pytest.fixture(scope="module")
def service() -> configparser.ConfigParser:
    return read_unit("r53-sync.service")


@pytest.fixture(scope="module")
def timer() -> configparser.ConfigParser:
    return read_unit("r53-sync.timer")


@pytest.fixture(scope="module")
def plist() -> dict[str, Any]:
    with (DEPLOY / "launchd" / "dev.r53-sync.plist").open("rb") as handle:
        return plistlib.load(handle)


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    return json.loads((DEPLOY / "iam-policy.json").read_text(encoding="utf-8"))


# --- systemd service ---------------------------------------------------------------------


def test_service_waits_for_network_and_tailscale(service: configparser.ConfigParser) -> None:
    unit = service["Unit"]
    for key in ("Wants", "After"):
        assert set(unit[key].split()) == {"network-online.target", "tailscaled.service"}


def test_service_retries_failures_but_not_config_errors(service: configparser.ConfigParser) -> None:
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["Restart"] == "on-failure"
    assert service["Service"]["RestartPreventExitStatus"] == "2"
    assert service["Unit"]["StartLimitBurst"] == "5"
    assert service["Unit"]["StartLimitIntervalSec"] == "10min"


def test_service_runs_unprivileged_and_sandboxed(service: configparser.ConfigParser) -> None:
    section = service["Service"]
    assert section["DynamicUser"] == "yes"
    assert section["NoNewPrivileges"] == "yes"
    assert section["ProtectSystem"] == "strict"
    assert section["ProtectHome"] == "yes"
    # The tailscaled socket is AF_UNIX; AWS is AF_INET/AF_INET6.
    assert set(section["RestrictAddressFamilies"].split()) == {"AF_UNIX", "AF_INET", "AF_INET6"}


def test_service_receives_credentials_through_loadcredential(
    service: configparser.ConfigParser,
) -> None:
    section = service["Service"]
    assert "EnvironmentFile" not in section  # secrets must not travel as environment variables
    name, source = section["LoadCredential"].split(":", 1)
    assert source == "/etc/r53-sync/aws-credentials"
    env = dict(item.split("=", 1) for item in shlex.split(section["Environment"]))
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == f"%d/{name}"  # %d = the credentials directory
    assert not [key for key in env if key.startswith(("AWS_ACCESS", "AWS_SECRET", "AWS_SESSION"))]


def test_service_disables_the_ec2_metadata_lookup(service: configparser.ConfigParser) -> None:
    env = dict(item.split("=", 1) for item in shlex.split(service["Service"]["Environment"]))
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"


def test_service_file_contains_no_secrets() -> None:
    text = (DEPLOY / "systemd" / "r53-sync.service").read_text(encoding="utf-8")
    assert "AWS_SECRET_ACCESS_KEY=" not in text
    assert "AKIA" not in text


def test_service_executable_lives_outside_home(service: configparser.ConfigParser) -> None:
    exe = service["Service"]["ExecStart"].split()[0]
    assert exe.startswith("/opt/")  # ProtectHome=yes hides /home and /root


# --- systemd timer -----------------------------------------------------------------------


def test_timer_runs_after_boot_then_every_twelve_hours(timer: configparser.ConfigParser) -> None:
    assert timer["Timer"]["OnBootSec"] == "2min"
    assert timer["Timer"]["OnUnitActiveSec"] == "12h"
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_timer_does_not_use_persistent(timer: configparser.ConfigParser) -> None:
    # systemd documents Persistent= as only effective with OnCalendar=.
    assert "Persistent" not in timer["Timer"]
    assert "OnCalendar" not in timer["Timer"]


# --- launchd -----------------------------------------------------------------------------


def test_plist_runs_at_load_and_every_twelve_hours(plist: dict[str, Any]) -> None:
    assert plist["RunAtLoad"] is True
    assert plist["StartInterval"] == 12 * 60 * 60


def test_plist_uses_absolute_program_and_explicit_config(plist: dict[str, Any]) -> None:
    program, flag, config = plist["ProgramArguments"]
    assert program.startswith("/")
    assert flag == "--config"
    assert config.startswith("/")


def test_plist_holds_no_secrets(plist: dict[str, Any]) -> None:
    env = plist["EnvironmentVariables"]
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["AWS_SHARED_CREDENTIALS_FILE"].startswith("/")
    assert "PATH" in env  # launchd jobs do not inherit a shell PATH


def test_plist_disables_the_ec2_metadata_lookup(plist: dict[str, Any]) -> None:
    assert plist["EnvironmentVariables"]["AWS_EC2_METADATA_DISABLED"] == "true"


def test_service_and_plist_run_the_same_program(
    service: configparser.ConfigParser, plist: dict[str, Any]
) -> None:
    assert service["Service"]["ExecStart"].split()[0] == plist["ProgramArguments"][0]


def test_root_daemon_path_holds_only_root_owned_system_directories(
    plist: dict[str, Any],
) -> None:
    # Homebrew's directories belong to the login user; a root job must not search them.
    entries = plist["EnvironmentVariables"]["PATH"].split(":")
    assert set(entries) <= {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}


# --- deploy guide ------------------------------------------------------------------------


def test_commands_that_create_config_and_credentials_do_not_clobber_existing_files() -> None:
    lines = (DEPLOY / "README.md").read_text(encoding="utf-8").splitlines()
    creating = [i for i, line in enumerate(lines) if "install" in line and "config" in line]
    creating += [i for i, line in enumerate(lines) if "install" in line and "/dev/null" in line]
    assert creating, "expected install commands in the guide"
    for index in creating:
        if "config.example.toml" in lines[index] or "/dev/null" in lines[index]:
            assert lines[index - 1].lstrip().startswith("test -e "), lines[index]


# --- IAM policy --------------------------------------------------------------------------


def statements(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {statement["Sid"]: statement for statement in policy["Statement"]}


def test_policy_is_a_valid_template(policy: dict[str, Any]) -> None:
    assert policy["Version"] == "2012-10-17"
    text = json.dumps(policy)
    assert text.count("<ZONE_ID>") == 2
    assert text.count("<HOST_FQDN>") == 1
    assert all(statement["Effect"] == "Allow" for statement in policy["Statement"])


def test_policy_scopes_changes_to_this_hosts_address_records(policy: dict[str, Any]) -> None:
    change = statements(policy)["ChangeOnlyThisHostsAddressRecords"]
    assert change["Action"] == "route53:ChangeResourceRecordSets"
    assert change["Resource"] == "arn:aws:route53:::hostedzone/<ZONE_ID>"
    condition = change["Condition"]["ForAllValues:StringEquals"]
    assert condition["route53:ChangeResourceRecordSetsNormalizedRecordNames"] == ["<HOST_FQDN>"]
    assert condition["route53:ChangeResourceRecordSetsRecordTypes"] == ["A", "AAAA"]
    assert set(condition["route53:ChangeResourceRecordSetsActions"]) == {"UPSERT", "DELETE"}


def test_policy_allows_every_action_the_code_issues(policy: dict[str, Any]) -> None:
    issued = set(get_args(Change.model_fields["action"].annotation))
    allowed = set(
        statements(policy)["ChangeOnlyThisHostsAddressRecords"]["Condition"][
            "ForAllValues:StringEquals"
        ]["route53:ChangeResourceRecordSetsActions"]
    )
    assert issued <= allowed


def test_policy_only_uses_wildcard_resource_for_zone_lookup(policy: dict[str, Any]) -> None:
    wildcard = [s["Sid"] for s in policy["Statement"] if s["Resource"] == "*"]
    assert wildcard == ["FindHostedZone"]
    assert statements(policy)["FindHostedZone"]["Action"] == "route53:ListHostedZones"


def test_policy_grants_exactly_the_calls_the_tool_makes(policy: dict[str, Any]) -> None:
    actions = {s["Action"] for s in policy["Statement"]}
    assert actions == {
        "route53:ListHostedZones",
        "route53:ListResourceRecordSets",
        "route53:ChangeResourceRecordSets",
    }


# --- CI workflow -------------------------------------------------------------------------


def test_ci_actions_are_pinned_to_full_commit_shas() -> None:
    workflow = (DEPLOY.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", workflow, flags=re.MULTILINE)
    assert uses, "expected the workflow to use at least one action"
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"not SHA-pinned: {ref}"


def test_ci_matrix_python_is_applied_to_every_step() -> None:
    # .python-version pins 3.14; without UV_PYTHON the 3.15 leg would silently run 3.14.
    workflow = (DEPLOY.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "UV_PYTHON: ${{ matrix.python }}" in workflow


def test_ci_workflow_has_read_only_permissions() -> None:
    workflow = (DEPLOY.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow


# --- example config ----------------------------------------------------------------------


def test_example_config_is_valid_settings() -> None:
    data = tomllib.loads((DEPLOY / "config.example.toml").read_text(encoding="utf-8"))
    assert Settings.model_validate(data).domain == "example.com"


def test_example_config_documents_the_env_prefix() -> None:
    text = (DEPLOY / "config.example.toml").read_text(encoding="utf-8")
    assert f"{ENV_PREFIX}_<KEY>" in text


def test_example_config_documents_every_setting() -> None:
    text = (DEPLOY / "config.example.toml").read_text(encoding="utf-8")
    for field in Settings.model_fields:
        assert field in text, f"config.example.toml does not mention {field}"
