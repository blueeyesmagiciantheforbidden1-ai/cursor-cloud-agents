"""Cursor image verification and hub heartbeat; task execution awaits integration."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

APP = Path('/opt/runcrew/app')
DISTRIBUTION = Path('/opt/runcrew/cursor')
CONFIG = Path('/run/config/worker.json')
HUB_URL = 'https://runcrew-hub-kdhodumsza-uc.a.run.app'
VERSION = '2026.09.18-9a7762b'
ARCHIVE_SHA256 = 'b1308f5a2fc05458b9d8966752986bb23a971bbcc67c842c1df94c4b8132bad9'
BLOCKERS = ('provider_on_demand_disabled_not_verified', 'fresh_own_model_and_effective_effort_required',
            'cursor_project_adapter_and_same_process_model_validation_pending')


def private_environment(inherited):
    token = inherited.get('HUB_AGENT_TOKEN', '')
    if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(c.isspace() or ord(c) < 32 for c in token):
        raise ValueError('Dedicated Cursor hub role is missing or malformed')
    return {'HOME': '/home/worker', 'PATH': '/usr/local/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'TMPDIR': '/tmp', 'HUB_AGENT_TOKEN': token}


def prepare_config(value):
    if not isinstance(value, dict) or set(value) - {'worker_id', 'expected_account_ref'}:
        raise ValueError('Unsupported Cursor commissioning configuration')
    owner = value.get('expected_account_ref')
    worker = value.get('worker_id', 'cursor-cloud')
    if not isinstance(owner, str) or not re.fullmatch('[a-f0-9]{64}', owner):
        raise ValueError('Verified intended-account reference required')
    if not isinstance(worker, str) or not re.fullmatch('[A-Za-z0-9_-]{1,48}', worker):
        raise ValueError('Invalid worker label')
    return {'hub_url': HUB_URL, 'agent_id': 'cursor', 'token_env': 'HUB_AGENT_TOKEN',
            'workspaces': {'default': '/workspace/default'}, 'worker_id': worker,
            'executable': str(DISTRIBUTION / 'cursor-agent'), 'cloud_run_auth_mode': 'metadata',
            'expected_account_ref': owner, 'billing_policy': {'mode': 'subscription_only'},
            'execution_mode': 'read_only', 'model_policy_required': True,
            'model_policy_bundle': '/run/config/model-catalog.json'}


def immutable(path):
    if path.is_symlink():
        return False
    stat = path.stat()
    return stat.st_uid == 0 and stat.st_mode & 0o022 == 0


def image_check():
    if sys.platform != 'linux' or os.geteuid() != 10001:
        raise ValueError('Dedicated rootless Linux image required')
    for path in (DISTRIBUTION, *DISTRIBUTION.parents):
        if not immutable(path):
            raise ValueError('Distribution ownership differs')
    profile_path = DISTRIBUTION / 'runtime-profile.json'
    if not immutable(profile_path) or profile_path.stat().st_size > 2_000_000:
        raise ValueError('Invalid immutable native profile')
    profile = json.loads(profile_path.read_text())
    if (profile.get('schema') != 1 or profile.get('version') != VERSION
            or profile.get('platform') != 'linux-x64' or profile.get('archive_sha256') != ARCHIVE_SHA256):
        raise ValueError('Native profile does not match audited release')
    files = profile.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= 20000:
        raise ValueError('Invalid distribution manifest')
    required = {'cursor-agent', 'cursor-agent-sea', 'node', 'index.js'}
    if not required <= set(files):
        raise ValueError('Missing runtime entrypoints')
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name
                or not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)):
            raise ValueError('Invalid native manifest member')
        path = DISTRIBUTION / name
        if not path.resolve(strict=True).is_relative_to(DISTRIBUTION) or not immutable(path) or not path.is_file():
            raise ValueError('Unsafe runtime file')
        with path.open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != expected:
                raise ValueError('Runtime file checksum changed')
    actual = {p.relative_to(DISTRIBUTION).as_posix() for p in DISTRIBUTION.rglob('*') if not p.is_dir()}
    if actual != set(files) | {'runtime-profile.json'}:
        raise ValueError('Unexpected runtime content')
    if any(not immutable(p) for p in DISTRIBUTION.rglob('*')):
        raise ValueError('Runtime descendants are not immutable')
    for name in ('cursor-agent', 'node', 'cursor-agent-sea'):
        if not os.access(DISTRIBUTION / name, os.X_OK):
            raise ValueError('Runtime executable bit is missing')
    for name in ('node', 'cursor-agent-sea'):
        with (DISTRIBUTION / name).open('rb') as handle:
            if handle.read(4) != b'\x7fELF':
                raise ValueError('Expected native Linux runtime')
    for folder in (Path('/home/worker'), Path('/home/worker/.cursor'), Path('/workspace/default')):
        stat = folder.stat()
        if folder.is_symlink() or stat.st_uid != 10001 or stat.st_mode & 0o077:
            raise ValueError('Private runtime directory differs')
    return {'image_profile_verified': True, 'provider_login_verified': False,
            'inference_performed': False, 'hub_connected': False, 'task_execution_enabled': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--heartbeat-only', action='store_true')
    modes.add_argument('--metadata-only', action='store_true')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(APP))
    try:
        checked = image_check()
        if args.check:
            print(json.dumps(checked))
            return 0
        if not args.heartbeat_only and not args.metadata_only:
            print(json.dumps({'status': 'task_execution_blocked', 'reasons': BLOCKERS,
                              'provider_calls': 0, 'claims_tasks': False}))
            return 2
        if CONFIG.is_symlink() or CONFIG.stat().st_size > 16384:
            raise ValueError('Invalid bounded commissioning configuration')
        config = prepare_config(json.loads(CONFIG.read_text(encoding='utf-8')))
        if args.metadata_only:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from metadata import collect_metadata, MetadataError
            api_key = os.environ.get('CURSOR_API_KEY', '')
            os.environ.clear()
            os.environ.update({'HOME': '/home/worker', 'PATH': '/usr/local/bin:/usr/bin:/bin',
                               'LANG': 'C.UTF-8', 'TMPDIR': '/tmp'})
            os.umask(0o077)
            try:
                result = collect_metadata(api_key, config['expected_account_ref'])
                print(json.dumps(result, separators=(',', ':')))
                return 0
            except MetadataError as exc:
                print(json.dumps({'status': 'metadata_unavailable', 'code': str(exc),
                                  'inference_performed': False, 'task_execution_enabled': False}))
                return 1
        environment = private_environment(dict(os.environ))
        os.environ.clear(); os.environ.update(environment)
        os.umask(0o077)
        os.chdir('/workspace/default')
        from agent_hub.worker import main as worker_main
        with tempfile.TemporaryDirectory(prefix='runcrew-cursor-', dir='/tmp') as temporary:
            path = Path(temporary) / 'worker.json'
            path.write_text(json.dumps(config), encoding='utf-8'); path.chmod(0o600)
            return worker_main(['--config', str(path), '--heartbeat-only'])
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'startup_rejected', 'error_type': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
