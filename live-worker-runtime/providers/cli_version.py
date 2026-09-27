"""Start-time CLI --version probe for capability manifests.

Never invents a version. On any failure the caller omits the whole manifest.
"""
from __future__ import annotations

import re
import shutil
import subprocess

# Match live_loop._CLI_VERSION / hub telemetry._CLI_VERSION.
_CLI_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,31}')
# stderr fallback when stdout is empty: "<name> <version>" on the first line.
_STDERR_VERSION_LINE = re.compile(r'^\S+ v?(\d[\w.+-]*)')
_PROBE_TIMEOUT_SECONDS = 10


def sanitise_version(text):
    """Return one hub-valid cli_version token from --version output, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    tokens = []
    for raw in re.split(r'\s+', text.strip()):
        token = raw.strip().strip('(),[]')
        if not token:
            continue
        candidates = [token]
        if len(token) > 1 and token[0] in 'vV' and token[1].isdigit():
            candidates.append(token[1:])
        for candidate in candidates:
            if _CLI_VERSION.fullmatch(candidate):
                tokens.append(candidate)
    if not tokens:
        first = text.strip().splitlines()[0].strip()
        if _CLI_VERSION.fullmatch(first):
            return first
        return None
    for token in tokens:
        if any(ch.isdigit() for ch in token):
            return token
    return tokens[0]


def resolve_executable(executable):
    """Absolute path or PATH lookup. None when nothing runnable is found."""
    if not isinstance(executable, str) or not executable.strip():
        return None
    path = executable.strip()
    try:
        from pathlib import Path
        candidate = Path(path)
        if candidate.is_file():
            return str(candidate)
    except OSError:
        pass
    found = shutil.which(path)
    if found:
        return found
    # Basename on PATH when an absolute image path is missing locally.
    name = path.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
    if name and name != path:
        return shutil.which(name)
    return None


def probe_cli_version(executable, *, timeout=_PROBE_TIMEOUT_SECONDS, runner=subprocess.run):
    """Run ``<executable> --version`` once.

    Returns (version_or_None, reason_or_None). reason is set only on failure.
    """
    resolved = resolve_executable(executable)
    if resolved is None:
        return None, 'cli_missing'
    try:
        completed = runner(
            [resolved, '--version'],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None, 'cli_missing'
    except subprocess.TimeoutExpired:
        return None, 'cli_version_timeout'
    except OSError:
        return None, 'cli_version_probe_failed'
    except Exception:
        return None, 'cli_version_probe_failed'
    if completed.returncode != 0:
        # An error message can carry digits ("request failed (401)"); taking a
        # token from it would publish a version the CLI never reported.
        return None, 'cli_version_exit_nonzero'
    # Prefer stdout alone. stderr is only a fallback when stdout is empty
    # (exit already 0) and its first line has a "<name> <version>" shape.
    stdout = completed.stdout or ''
    if stdout.strip():
        version = sanitise_version(stdout)
        if version is None:
            return None, 'cli_version_unparseable'
        return version, None
    stderr = completed.stderr or ''
    first = ''
    for line in stderr.splitlines():
        if line.strip():
            first = line.strip()
            break
    matched = _STDERR_VERSION_LINE.match(first)
    if matched is None:
        return None, 'cli_version_unparseable'
    version = matched.group(1)
    if not _CLI_VERSION.fullmatch(version):
        return None, 'cli_version_unparseable'
    return version, None


def bind_cli_version(adapter, *, log=None, runner=subprocess.run):
    """Probe once and set adapter.CLI_VERSION / CLI_VERSION_REASON.

    Idempotent: a non-None CLI_VERSION already on the adapter is left alone
    (tests arm manifests without a real binary). A previous failed probe is
    not retried within the same process.
    """
    current = getattr(adapter, 'CLI_VERSION', None)
    if isinstance(current, str) and current:
        if getattr(adapter, 'CLI_VERSION_REASON', None) is not None:
            try:
                adapter.CLI_VERSION_REASON = None
            except Exception:
                pass
        return current, None
    if getattr(adapter, '_cli_version_probed', False):
        return getattr(adapter, 'CLI_VERSION', None), getattr(adapter, 'CLI_VERSION_REASON', None)

    executable = getattr(adapter, 'CLI_EXECUTABLE', None)
    if not isinstance(executable, str) or not executable:
        name = getattr(adapter, 'CLI_NAME', None)
        executable = name if isinstance(name, str) else None

    version, reason = probe_cli_version(executable, runner=runner)
    try:
        adapter._cli_version_probed = True
        adapter.CLI_VERSION = version
        adapter.CLI_VERSION_REASON = reason
    except Exception:
        pass
    if reason and callable(log):
        try:
            log({'kind': 'runcrew_capability_omit', 'reason': reason,
                 'cli_name': getattr(adapter, 'CLI_NAME', None)})
        except Exception:
            pass
    return version, reason
