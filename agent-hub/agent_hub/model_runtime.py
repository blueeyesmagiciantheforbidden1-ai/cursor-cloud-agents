"""Model launch gate for a controller-owned model catalog bundle.

The caller must mount this bundle and the native CLI from protected immutable
control-plane storage, independently verify authenticated account enrollment,
and provide the expected account reference from separate trusted configuration.
Path/symlink/size checks below are not ownership, ACL, signature or mount checks.

Refreshing independently before each worker launch does NOT establish one
globally distinct model assignment for a room across catalog updates. A room
coordinator must pin a single plan/bundle revision for all participating workers.
This helper selects a candidate stack; it fetches no live catalogs or credentials.
"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from . import adapters
from .model_policy import (AGENTS, MAX_BUNDLE_BYTES, PolicyError, cli_selection_args,
                           select_stack, validate_plan)


MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_DIGEST = re.compile(r'[a-f0-9]{64}')
_INTERPRETERS = frozenset(('cmd', 'powershell', 'pwsh', 'python', 'python3', 'python3.12',
                          'node', 'bash', 'sh', 'wsl', 'env', 'ruby', 'perl'))


class ModelRuntimeError(ValueError):
    """Sanitized launch denial; no paths, account identifiers or file contents."""


def _protected_path(value):
    if not isinstance(value, (str, os.PathLike)):
        raise ModelRuntimeError('invalid_control_plane_path')
    try:
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts or str(path).startswith(('\\\\', '//')):
            raise ModelRuntimeError('absolute_local_path_required')
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
                raise ModelRuntimeError('linked_control_plane_path_forbidden')
        if not path.is_file():
            raise ModelRuntimeError('control_plane_file_unavailable')
        return path.resolve(strict=True)
    except (OSError, ValueError) as error:
        if isinstance(error, ModelRuntimeError):
            raise
        raise ModelRuntimeError('control_plane_file_unavailable') from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_file(path, limit, *, hash_only=False):
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ModelRuntimeError('control_plane_file_size_invalid')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if _identity(before) != _identity(opened):
                raise ModelRuntimeError('control_plane_file_changed')
            if hash_only:
                prefix = stream.read(4)
                # PE, ELF and Mach-O are native binaries; scripts/interpreters
                # with extra argv are intentionally unsupported by this gate.
                if not (prefix.startswith(b'MZ') or prefix == b'\x7fELF'
                        or prefix in (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                                      b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca')):
                    raise ModelRuntimeError('native_cli_required')
                digest, length = hashlib.sha256(prefix), len(prefix)
                while True:
                    chunk = stream.read(min(1024 * 1024, limit - length + 1))
                    if not chunk:
                        break
                    length += len(chunk)
                    if length > limit:
                        raise ModelRuntimeError('control_plane_file_size_invalid')
                    digest.update(chunk)
                result = digest.hexdigest()
            else:
                result = stream.read(limit + 1)
                if len(result) > limit:
                    raise ModelRuntimeError('control_plane_file_size_invalid')
            if _identity(opened) != _identity(os.fstat(stream.fileno())):
                raise ModelRuntimeError('control_plane_file_changed')
        if _identity(opened) != _identity(path.stat()):
            raise ModelRuntimeError('control_plane_file_changed')
        return result
    except OSError:
        raise ModelRuntimeError('control_plane_file_unavailable') from None


def _load_bundle(value):
    path = _protected_path(value)
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ModelRuntimeError('duplicate_catalog_key')
            result[key] = item
        return result
    def constant(value):
        raise ModelRuntimeError('nonfinite_catalog_value')
    try:
        bundle = json.loads(_read_file(path, MAX_BUNDLE_BYTES).decode('utf-8'),
                            object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(bundle, dict):
            raise ModelRuntimeError('invalid_catalog_json')
        pending = [(bundle, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 24:
                raise ModelRuntimeError('catalog_nesting_limit')
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return bundle
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, ModelRuntimeError):
            raise
        raise ModelRuntimeError('invalid_catalog_json') from None


def _canonical_command(agent_id, command, execution_mode):
    if (type(command) is not adapters.Command or not isinstance(command.argv, tuple) or not command.argv
            or any(not isinstance(arg, str) or '\x00' in arg for arg in command.argv)):
        raise ModelRuntimeError('invalid_worker_command')
    executable = _protected_path(command.argv[0])
    if executable.stem.lower() in _INTERPRETERS or executable.suffix.lower() in ('.cmd', '.bat', '.ps1', '.py', '.js'):
        raise ModelRuntimeError('native_cli_required')
    if agent_id in ('codex', 'claude'):
        prompt = command.stdin
    elif agent_id == 'grok':
        prompt = command.prompt_file
    else:
        index = command.prompt_argument
        if type(index) is not int or not 0 <= index < len(command.argv):
            raise ModelRuntimeError('invalid_prompt_binding')
        prompt = command.argv[index]
    if not isinstance(prompt, str):
        raise ModelRuntimeError('invalid_prompt_binding')
    try:
        expected = adapters.build_command(agent_id, prompt, executable=str(executable), execution_mode=execution_mode)
    except (adapters.AdapterError, OSError, ValueError):
        raise ModelRuntimeError('canonical_native_command_required') from None
    if expected != command:
        raise ModelRuntimeError('canonical_native_command_required')
    return executable


def select_worker_model(agent_id, bundle_path, expected_account_ref, command, *, now=None,
                        execution_mode='read_only', active_agents=AGENTS):
    """Select the configured fleet, bind this worker, return (Command, plan).

    Call on the canonical restricted Command BEFORE adding any model flags or
    materializing Grok's prompt file. Returned argv is data for shell=False.
    execution_mode and active_agents come from trusted worker configuration,
    never task data. The default fleet contains all five agents. A smaller
    explicitly enrolled fleet is not evidence that all five agents are ready.
    The caller must still verify the account's actual auth route and provider
    cost enforcement, bind a room-wide immutable plan, and check applied metadata.
    """
    if agent_id not in AGENTS:
        raise ModelRuntimeError('unsupported_agent')
    if execution_mode not in ('read_only', 'project_work'):
        raise ModelRuntimeError('unsupported_execution_mode')
    if not isinstance(active_agents, (tuple, list)) or agent_id not in active_agents:
        raise ModelRuntimeError('worker_not_in_active_fleet')
    if not isinstance(expected_account_ref, str) or not _DIGEST.fullmatch(expected_account_ref):
        raise ModelRuntimeError('expected_account_binding_required')
    executable = _canonical_command(agent_id, command, execution_mode)
    bundle = _load_bundle(bundle_path)
    try:
        plan = select_stack(bundle, agents=active_agents, now=now)
        validate_plan(plan, bundle, now=now)
        selected = plan['selections'][agent_id]
        if selected['account_ref'] != expected_account_ref:
            raise ModelRuntimeError('expected_account_mismatch')
        if _read_file(executable, MAX_EXECUTABLE_BYTES, hash_only=True) != selected['cli_sha256']:
            raise ModelRuntimeError('cli_binary_digest_mismatch')
        extra = cli_selection_args(selected)
        validate_plan(plan, bundle, now=now)
    except PolicyError as error:
        raise ModelRuntimeError('model_policy_' + error.code) from None
    position = len(command.argv) - 1 if agent_id == 'codex' else len(command.argv)
    argv = command.argv[:position] + extra + command.argv[position:]
    def moved(index):
        return index + len(extra) if index is not None and index >= position else index
    return replace(command, argv=argv, prompt_argument=moved(command.prompt_argument),
                   prompt_file_argument=moved(command.prompt_file_argument)), plan
