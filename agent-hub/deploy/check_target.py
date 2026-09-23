"""Read-only, fail-closed checks for the explicitly selected isolated GCP target."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
import sys


FORBIDDEN_ORG_IDS = {"833782852711", "1091262552755"}
TASK_CREATED_AFTER = "2026-09-20T00:00:00Z"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_inputs(args: argparse.Namespace) -> None:
    require(bool(re.fullmatch(r"[0-9]{6,30}", args.new_org)), "Supply the new organization ID.")
    isolation = getattr(args, "isolation", "organization")
    require(isolation in ("organization", "project"), "Select organization or project isolation explicitly.")
    if isolation == "organization":
        require(args.new_org not in FORBIDDEN_ORG_IDS, "The existing organization is forbidden in organization-isolation mode.")
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project)), "Invalid project ID.")
    require(bool(re.fullmatch(r"[A-Z0-9]{6}(?:-[A-Z0-9]{6}){2}", args.billing_account)), "Invalid billing account ID.")
    require(bool(re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", args.account)), "Supply the deployment account email.")
    domain, name = getattr(args, "expected_domain", None), getattr(args, "expected_org_name", None)
    require((domain is None) != (name is None), "Choose exactly one expected domain or organization display name.")
    if domain is not None:
        require(bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", domain)), "Supply the verified organization domain.")
    else:
        require(isinstance(name, str) and 1 <= len(name) <= 120 and name == name.strip()
                and all(ord(char) >= 32 for char in name), "Supply the exact organization display name from Cloud Console.")
    if isolation == "organization":
        require(domain is not None, "Organization-isolation mode retains the verified new-domain requirement.")
        require(args.account.lower().endswith("@" + domain), "Use an operator identity from the new domain.")
        require(getattr(args, "credential_file", None) is None, "Credential-file authorization requires explicit project-isolation mode.")
    credential = getattr(args, "credential_file", None)
    if credential is not None:
        credential_path(credential)
    created_after = parse_timestamp(getattr(args, "created_after", TASK_CREATED_AFTER))
    require(created_after >= parse_timestamp(TASK_CREATED_AFTER), "The project creation cutoff cannot predate this task.")


def parse_timestamp(value) -> datetime:
    require(isinstance(value, str) and len(value) <= 40, "A bounded timezone-aware creation timestamp is required.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("A valid timezone-aware creation timestamp is required.") from None
    require(parsed.tzinfo is not None, "Creation timestamps must include a timezone.")
    return parsed.astimezone(timezone.utc)


def credential_path(value) -> str:
    require(isinstance(value, str) and bool(value) and os.path.isabs(value)
            and "\x00" not in value, "Supply an absolute, explicitly authorized credential-file path.")
    return os.path.normcase(os.path.normpath(value))


def validate_auth_environment(environment: dict[str, str], expected_credential_file: str | None = None) -> None:
    # --account selects stored credentials only after higher-priority token/file
    # overrides. Block unknown future auth environment overrides too. Values are
    # intentionally never included in errors or logs.
    for name, value in environment.items():
        if not name.upper().startswith("CLOUDSDK_AUTH_") or value == "":
            continue
        if name.upper() == "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE" and expected_credential_file is not None:
            require(credential_path(value) == credential_path(expected_credential_file), "Credential-file override does not match the explicitly authorized path.")
            continue
        raise ValueError("Authentication environment overrides are set. Only the explicitly authorized project-mode credential file is permitted.")


def validate_auth_configuration(configuration: dict, expected_credential_file: str | None = None) -> None:
    # `config list auth/` includes explicitly set hidden properties, including
    # credential_file_override. An unset auth section is normal after auth login.
    require(set(configuration) <= {"auth"}, "Unexpected authentication configuration response; target not verified.")
    auth = configuration.get("auth", {})
    require(isinstance(auth, dict), "Unexpected authentication configuration response; target not verified.")
    for name, value in auth.items():
        if value is None or value == "":
            continue
        if name == "disable_credentials" and (value is False or
                                              isinstance(value, str) and value.lower() == "false"):
            continue
        if name == "credential_file_override" and expected_credential_file is not None:
            require(credential_path(value) == credential_path(expected_credential_file), "Configured credential file does not match the explicitly authorized path.")
            continue
        # Outside the explicitly pinned project-mode credential file, reject
        # configured auth customizations rather than guessing
        # whether an unknown/new option can change the effective principal.
        raise ValueError("Authentication overrides are configured. Impersonation, access-token files and unspecified authorization overrides are forbidden.")


def _gcloud_json(args: argparse.Namespace, environment: dict[str, str], *command: str) -> dict:
    executable = shutil.which("gcloud")
    require(executable is not None, "Install Google Cloud CLI and authenticate the explicitly selected operator.")
    try:
        process = subprocess.run(
            [executable, *command, "--format=json", "--quiet", "--verbosity=error",
             f"--account={args.account}", f"--project={args.project}"],
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=120,
            env=environment, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except UnicodeError:
        raise ValueError("Invalid Google Cloud response encoding; target not verified.") from None
    # stderr can contain credential paths, token fragments, or provider details.
    # Keep the rejection useful without forwarding any captured diagnostic data.
    require(process.returncode == 0, f"Read-only gcloud {' '.join(command[:3])} failed; stop and resolve configuration/account/permissions.")
    try:
        value = json.loads(process.stdout)
    except (ValueError, RecursionError):
        raise ValueError("Invalid Google Cloud JSON response; target not verified.") from None
    require(isinstance(value, dict), "Unexpected Google Cloud response; target not verified.")
    return value


def read_json(args: argparse.Namespace, *command: str) -> dict:
    # Recheck before every API read and pass the same environment snapshot to
    # both commands. Never silently remove an override and claim it was verified.
    environment = dict(os.environ)
    expected = getattr(args, "credential_file", None)
    require(expected is None or getattr(args, "isolation", "organization") == "project",
            "Credential-file authorization requires explicit project-isolation mode.")
    validate_auth_environment(environment, expected)
    configuration = _gcloud_json(args, environment, "config", "list", "auth/")
    validate_auth_configuration(configuration, expected)
    if expected is not None:
        configured = configuration.get("auth", {}).get("credential_file_override")
        environment_override = next((value for name, value in environment.items()
                                     if name.upper() == "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE" and value), None)
        effective = environment_override or configured
        require(effective is not None and credential_path(effective) == credential_path(expected),
                "The explicitly authorized credential file is not active; refusing an account fallback.")
    return _gcloud_json(args, environment, *command)


def verify_org_and_billing(args: argparse.Namespace, org: dict, billing: dict) -> None:
    require(org.get("name") == f"organizations/{args.new_org}", "Organization ID mismatch.")
    require(org.get("lifecycleState", org.get("state")) == "ACTIVE", "Organization is not active.")
    domain = getattr(args, "expected_domain", None)
    if domain is not None:
        require(isinstance(org.get("displayName"), str) and org["displayName"].lower() == domain, "Organization domain mismatch; verify the selected domain and ID in Cloud Console.")
    else:
        require(org.get("displayName") == args.expected_org_name, "Organization display-name mismatch; copy the exact selected name from Cloud Console.")
    require(billing.get("name") == f"billingAccounts/{args.billing_account}", "Billing account ID mismatch.")
    require(billing.get("open") is True, "Selected billing account is not open.")
    require(billing.get("parent") == f"organizations/{args.new_org}", "Billing must belong directly to the explicitly selected organization; no different-parent billing fallback.")


def verify_project(args: argparse.Namespace, project: dict, billing_info: dict | None) -> None:
    require(project.get("projectId") == args.project, "Project ID mismatch.")
    require(project.get("lifecycleState") == "ACTIVE", "Project is not active.")
    parent = project.get("parent", {})
    require(isinstance(parent, dict) and parent.get("type") == "organization" and str(parent.get("id")) == args.new_org,
            "Project must be a direct child of the explicitly selected organization; folder/no-organization/other-org targets are rejected.")
    require(project.get("labels", {}).get("agent-hub-isolated") == "true", "Project lacks the dedicated hub label.")
    if getattr(args, "isolation", "organization") == "project":
        created = parse_timestamp(project.get("createTime", project.get("creationTime")))
        require(created >= parse_timestamp(getattr(args, "created_after", TASK_CREATED_AFTER)),
                "Project predates this task; create a new dedicated project instead of reusing one.")
    if billing_info is not None:
        require(billing_info.get("billingEnabled") is True, "Project billing is not enabled.")
        require(billing_info.get("billingAccountName") == f"billingAccounts/{args.billing_account}", "Project uses a different billing account.")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new-org", "--organization", dest="new_org", required=True)
    for option in ("project", "billing-account", "account"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--isolation", choices=("organization", "project"), default="organization",
                        help="Organization mode rejects known existing organizations; project mode explicitly permits a fresh dedicated project in the exact existing organization.")
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--expected-domain")
    identity.add_argument("--expected-org-name")
    parser.add_argument("--credential-file", help="Project mode only: exact approved absolute credential-file override. Does not activate or print credentials.")
    parser.add_argument("--created-after", default=TASK_CREATED_AFTER,
                        help="Project-mode creation cutoff; cannot be earlier than the task start.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--before-create", action="store_true", help="Check org and billing before creating a brand-new project.")
    mode.add_argument("--unbilled", action="store_true", help="Also check project parent before linking selected billing.")
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        validate_inputs(args)
        verify_org_and_billing(args, read_json(args, "organizations", "describe", args.new_org),
                              read_json(args, "billing", "accounts", "describe", args.billing_account))
        if not args.before_create:
            project = read_json(args, "projects", "describe", args.project)
            billing_info = None if args.unbilled else read_json(args, "billing", "projects", "describe", args.project)
            verify_project(args, project, billing_info)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"TARGET REJECTED: {exc}", file=sys.stderr)
        return 1
    print(f"Verified {args.isolation}-isolation target: organizations/{args.new_org}, project={args.project}, billing={args.billing_account}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
