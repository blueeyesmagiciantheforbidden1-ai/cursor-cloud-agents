"""Credential-free reporting of Claude's *observed* authentication route.

This is deliberately a narrow Linux cloud-image contract, not an arbitrary
``auth status`` command runner. The image pins the native vendor executable;
tasks cannot select its binary, environment, profile, arguments or provenance.
The only credential is injected by the worker operator through the documented
CLAUDE_CODE_OAUTH_TOKEN variable. No credential value is parsed or reported.

Claude Code 2.1.275's JSON auth status identifies that source as ``oauth_token``
with ``apiProvider=firstParty``. It checks source presence, NOT token validity,
plan allowance or extra-usage settings. A zero exit is therefore not a health
check. More importantly, safe mode still applies managed policy, and an OAuth
token can trigger a later server-managed policy fetch. This separate process
cannot prove the inference process's effective policy. It never sets
AuthEvidence.local_configuration_checked. claude_runtime performs the separate
same-process initialize/settings audit before releasing a task to that process.

Runtime image contract:
* Native ELF at /opt/runcrew/claude/claude; root-owned, worker-unwritable path.
* Root-owned runtime-profile.json beside it: {schema: 1, cli_version: '2.1.275',
  sha256: '<SHA256 of that verified vendor ELF>'}. Installation must verify the
  vendor's published checksum before producing this manifest.
* Rootless Linux. Fixed fresh HOME=/home/worker and config=.claude, workspace
  /workspace/default. No settings, credentials, remote policy or profile cache.
* Exact child environment returned by claude_environment(); the hub controller
  may separately use HUB_* variables, which worker.py removes before preflight.
* The fixed restricted adapter for the locally configured execution mode, plus exact --model and --effort
  arguments selected by the independent trusted model policy. This module
  validates route-affecting flags; it does not rank models or verify allowance.

Primary references (verified 2026-09-20):
https://code.claude.com/docs/en/authentication
https://code.claude.com/docs/en/cli-reference
https://code.claude.com/docs/en/server-managed-settings
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time

from .adapters import CLAUDE_MODE_TOOLS, Command
from .billing_policy import AuthEvidence


CLAUDE_EXECUTABLE = Path('/opt/runcrew/claude/claude')
CLAUDE_PROFILE = Path('/opt/runcrew/claude/runtime-profile.json')
CLAUDE_HOME = Path('/home/worker')
CLAUDE_CONFIG = CLAUDE_HOME / '.claude'
CLAUDE_WORKSPACE = Path('/workspace/default')
CLAUDE_MANAGED = Path('/etc/claude-code')
_AUDITED_VERSION = '2.1.275'
_MAX_STATUS_BYTES = 16_384
_PROBE_TIMEOUT_SECONDS = 5
_FIXED_ENV = {
    'HOME': str(CLAUDE_HOME),
    'CLAUDE_CONFIG_DIR': str(CLAUDE_CONFIG),
    'PATH': '/usr/local/bin:/usr/bin:/bin',
    'LANG': 'C.UTF-8',
    'TMPDIR': '/tmp',
    'DISABLE_AUTOUPDATER': '1',
    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
}
_TOKEN_ENV = 'CLAUDE_CODE_OAUTH_TOKEN'
_REQUIRED_SWITCHES = {'-p', '--safe-mode', '--restricted', '--strict-mcp-config'}
_REQUIRED_PAIRS = {
    '--output-format': 'json',
    '--setting-sources': '',
    '--tools': 'Read,Grep,Glob',
    '--allowedTools': 'Read,Grep,Glob',
    '--disallowedTools': 'mcp__*',
    '--permission-mode': 'dontAsk',
}
# Exact local startup keys observed in an isolated 2.1.275 auth-status run.
# No opaque unknown fields may conceal a gateway identity/configuration.
_STARTUP_KEYS = frozenset({
    'firstStartTime', 'firstStartVersion', 'machineID',
    'opusProMigrationComplete', 'sonnet1m45MigrationComplete',
    'seenNotifications', 'hasResetAutoModeOptInForDefaultOffer', 'migrationVersion',
})


@dataclass(frozen=True)
class ClaudeAuthAudit:
    """Safe to include in health telemetry; never includes raw vendor output."""
    code: str
    observed_at: float | None = None
    auth_route: str = 'unknown'
    local_configuration_checked: bool = False
    auth_status: str = 'unknown'

    def billing_evidence(self) -> AuthEvidence | None:
        if self.auth_route != 'subscription_login' or self.observed_at is None:
            return None
        return AuthEvidence('claude', self.auth_route, self.observed_at,
                            'vendor_cli_status', self.local_configuration_checked)


def claude_environment(oauth_token: str) -> dict[str, str]:
    """Construct, rather than filter, the dedicated child environment.

    The token comes from the operator's secret injection. Valid syntax is not
    evidence of identity or entitlement. Do not pass this mapping to logs.
    """
    if not _token_present(oauth_token):
        raise ValueError('The dedicated Claude subscription credential is missing or malformed')
    return {**_FIXED_ENV, _TOKEN_ENV: oauth_token}


def _token_present(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 16_384
            and not any(char.isspace() or ord(char) < 32 for char in value))


def _environment_matches(environment: Mapping[str, str]) -> bool:
    return (isinstance(environment, Mapping)
            and set(environment) == set(_FIXED_ENV) | {_TOKEN_ENV}
            and all(environment[key] == value for key, value in _FIXED_ENV.items())
            and _token_present(environment[_TOKEN_ENV]))


def _command_matches(command: Command, *, execution_mode: str = 'read_only') -> bool:
    if execution_mode not in CLAUDE_MODE_TOOLS:
        return False
    required_pairs = {**_REQUIRED_PAIRS, '--tools': CLAUDE_MODE_TOOLS[execution_mode],
                      '--allowedTools': CLAUDE_MODE_TOOLS[execution_mode]}
    if (not isinstance(command, Command) or not command.argv
            or command.argv[0] != str(CLAUDE_EXECUTABLE)
            or not isinstance(command.stdin, str)
            or command.prompt_argument is not None or command.prompt_file is not None
            or command.prompt_file_argument is not None):
        return False
    seen: dict[str, str | None] = {}
    arguments = iter(command.argv[1:])
    for flag in arguments:
        if flag in seen:
            return False
        if flag in _REQUIRED_SWITCHES:
            seen[flag] = None
        elif flag in required_pairs or flag in ('--model', '--effort'):
            value = next(arguments, None)
            if value is None:
                return False
            seen[flag] = value
        else:
            return False
    if set(seen) != _REQUIRED_SWITCHES | set(required_pairs) | {'--model', '--effort'}:
        return False
    if any(seen[key] != value for key, value in required_pairs.items()):
        return False
    # Reject aliases, provider paths, URLs and positional prompt/flag injection.
    # Account-effective model availability/strength belong to model_policy.
    model = seen['--model']
    return (isinstance(model, str)
            and re.fullmatch(r'claude-[a-z][a-z0-9.-]{0,90}-[0-9][a-z0-9.-]*(?:\[1m\])?', model) is not None
            and seen['--effort'] in ('low', 'medium', 'high', 'xhigh', 'max'))


def _strict_json(raw: bytes) -> object:
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def _immutable_path(path: Path, *, regular: bool) -> bool:
    """Root ownership is only meaningful in the rootless pinned Linux image."""
    try:
        if path.resolve(strict=True) != path:
            return False
        for index, item in enumerate((path, *path.parents)):
            metadata = item.lstat()
            if metadata.st_uid != 0 or metadata.st_mode & 0o022 or item.is_symlink():
                return False
            if os.access(item, os.W_OK):
                return False
            expected = stat.S_ISREG if index == 0 and regular else stat.S_ISDIR
            if not expected(metadata.st_mode):
                return False
        return True
    except (OSError, RuntimeError):
        return False


def _profile_matches() -> bool:
    if not _immutable_path(CLAUDE_PROFILE, regular=True) or not _immutable_path(CLAUDE_EXECUTABLE, regular=True):
        return False
    try:
        if CLAUDE_PROFILE.stat().st_size > 2_048:
            return False
        profile = _strict_json(CLAUDE_PROFILE.read_bytes())
        if (not isinstance(profile, dict) or set(profile) != {'schema', 'cli_version', 'sha256'}
                or type(profile['schema']) is not int or profile['schema'] != 1
                or profile['cli_version'] != _AUDITED_VERSION
                or not isinstance(profile['sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', profile['sha256']) is None):
            return False
        with CLAUDE_EXECUTABLE.open('rb') as handle:
            if handle.read(4) != b'\x7fELF':
                return False
            handle.seek(0)
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        return digest == profile['sha256'] and os.access(CLAUDE_EXECUTABLE, os.X_OK)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return False


def _empty_or_missing(path: Path) -> bool:
    if path.is_symlink():
        return False
    if not path.exists():
        return True
    return path.is_dir() and not any(path.iterdir())


def _local_configuration_clean(workspace: Path) -> bool:
    """No credential files are opened. Configuration files are bounded/nonsecret."""
    try:
        if (workspace != CLAUDE_WORKSPACE or workspace.resolve(strict=True) != workspace
                or not workspace.is_dir()):
            return False
        for directory in (CLAUDE_HOME, CLAUDE_CONFIG):
            if (directory.resolve(strict=True) != directory or not directory.is_dir()
                    or directory.stat().st_uid != os.geteuid()
                    or directory.stat().st_mode & 0o077):
                return False
        if not _empty_or_missing(CLAUDE_MANAGED):
            return False
        if not _empty_or_missing(CLAUDE_HOME / '.config'):
            return False
        # The workspace may contain arbitrary source files. None of its Claude
        # customizations/configuration may be active in this dedicated profile.
        for directory in (workspace, *workspace.parents):
            if not _empty_or_missing(directory / '.claude'):
                return False
            if (directory / '.claude.json').exists() or (directory / '.claude.json').is_symlink():
                return False
        # Auth status creates only .claude.json plus backup files in a fresh
        # config home. Reject other entries without reading potential secrets.
        for entry in CLAUDE_CONFIG.iterdir():
            if entry.is_symlink() or entry.name not in {'.claude.json', 'backups'}:
                return False
            if entry.name == 'backups':
                if not entry.is_dir():
                    return False
                for backup in entry.iterdir():
                    if (backup.is_symlink() or not backup.is_file()
                            or re.fullmatch(r'\.claude\.json\.backup\.[0-9]+', backup.name) is None):
                        return False
            else:
                if not entry.is_file() or entry.stat().st_size > 16_384:
                    return False
                value = _strict_json(entry.read_bytes())
                if not isinstance(value, dict) or set(value) - _STARTUP_KEYS:
                    return False
        if any(entry.name != '.claude' for entry in CLAUDE_HOME.iterdir()):
            return False
        return True
    except (OSError, ValueError, RuntimeError, UnicodeError, RecursionError):
        return False


def _capture_status(environment: Mapping[str, str], workspace: Path) -> tuple[int, bytes] | None:
    """Run only a vendor status command, with bounded output and wall time."""
    argv = (str(CLAUDE_EXECUTABLE), '--safe-mode', '--restricted',
            '--setting-sources', '', 'auth', 'status', '--json')
    process = None
    output = bytearray()
    oversized = threading.Event()
    def read_output():
        try:
            while chunk := process.stdout.read(1024):
                if len(output) + len(chunk) > _MAX_STATUS_BYTES:
                    oversized.set()
                    break
                output.extend(chunk)
        finally:
            process.stdout.close()
    try:
        process = subprocess.Popen(argv, cwd=workspace, env=dict(environment),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False, start_new_session=True)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
        while process.poll() is None and not oversized.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        if process.poll() is None or oversized.is_set():
            return None
        reader.join(timeout=0.2)
        if reader.is_alive() or oversized.is_set():
            return None
        return process.returncode, bytes(output)
    except (OSError, ValueError):
        return None
    finally:
        if process is not None:
            # The fixed native CLI should not leave descendants. Kill its
            # dedicated process group on every path, including normal exit.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()


def claude_launch_context_error(command: Command, workspace: Path,
                                environment: Mapping[str, str], *,
                                execution_mode: str = 'read_only') -> str | None:
    """Validate only the trusted local launch context; never execute the CLI."""
    if sys.platform != 'linux' or not hasattr(os, 'geteuid') or os.geteuid() == 0:
        return 'unsupported_runtime'
    if not _command_matches(command, execution_mode=execution_mode):
        return 'unapproved_command'
    if not _environment_matches(environment):
        return 'unapproved_environment'
    if not _profile_matches():
        return 'unverified_native_binary'
    if not _local_configuration_clean(workspace):
        return 'unapproved_local_configuration'
    return None


def audit_claude_subscription(command: Command, workspace: Path,
                              environment: Mapping[str, str], *,
                              execution_mode: str = 'read_only') -> ClaudeAuthAudit:
    """Fresh route observation. No model call, token validation or raw output."""
    error = claude_launch_context_error(command, workspace, environment, execution_mode=execution_mode)
    if error is not None:
        return ClaudeAuthAudit(error)
    result = _capture_status(environment, workspace)
    observed_at = time.time()
    if result is None:
        return ClaudeAuthAudit('status_unavailable')
    exit_code, raw = result
    try:
        body = _strict_json(raw)
        if not isinstance(body, dict):
            raise ValueError('Unexpected status')
    except (ValueError, UnicodeError, RecursionError):
        return ClaudeAuthAudit('invalid_status')
    if (exit_code != 0 or body.get('loggedIn') is not True
            or body.get('authMethod') != 'oauth_token'
            or body.get('apiProvider') != 'firstParty'
            or body.get('apiKeySource') not in (None, 'none')
            or body.get('forcedLoginMethod') not in (None, 'claudeai')
            or body.get('configDirectory') != str(CLAUDE_CONFIG)):
        return ClaudeAuthAudit('subscription_route_unverified', observed_at)
    if not _local_configuration_clean(workspace):
        return ClaudeAuthAudit('local_configuration_changed', observed_at)
    # This is useful authenticated-source evidence, but separate-process status
    # cannot establish remote policy in the inference process. Never upgrade it
    # from task data, a manifest Boolean, or an operator-supplied assertion.
    return ClaudeAuthAudit('effective_managed_policy_unverified', observed_at,
                           'subscription_login', local_configuration_checked=False)


def preflight_subscription_route(agent_id: str, command: Command, workspace: Path,
                                 environment: Mapping[str, str], *,
                                 execution_mode: str = 'read_only') -> AuthEvidence | None:
    if agent_id != 'claude':
        return None
    return audit_claude_subscription(command, workspace, environment,
                                     execution_mode=execution_mode).billing_evidence()
