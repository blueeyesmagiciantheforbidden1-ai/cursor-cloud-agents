"""Documented Grok metadata commands, with explicit unsupported-data results.

`inspect --json` and `models` are supported. No account-status, quota, JSON model
switch, or ACP catalog method is invented. These commands never start a session.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone

AGENT = 'grok'
ACCOUNT_REF = '9ddbfe0cce4b6653b86b2057f45c398360541f21a100c1589a67a01cbc80aadc'
NATIVE = '/opt/runcrew/grok/grok'
COMMANDS = (('inspect', '--json'), ('models',))
BROKER_CONFIG = Path('/run/config/credential-broker.json')
ATTEMPT_ROOT = Path('/home/worker')
MAX_OUTPUT = 2 * 1024 * 1024


class MetadataError(ValueError):
    pass


def environment(home):
    result = {'HOME': str(home), 'GROK_HOME': str(home / '.grok'),
              'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
              'TMPDIR': str(home / 'tmp'), 'GROK_DISABLE_AUTOUPDATER': '1',
              'GROK_CRASH_HANDLER': '0', 'GROK_SUBAGENTS': '0', 'GROK_MEMORY': '0',
              'NO_COLOR': '1', 'TERM': 'dumb'}
    for family in ('CLAUDE', 'CURSOR'):
        for feature in ('SKILLS', 'RULES', 'AGENTS', 'MCPS', 'HOOKS'):
            result[f'GROK_{family}_{feature}_ENABLED'] = '0'
    return result


def inspect_metadata(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
        raise MetadataError('native_output_limit')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise MetadataError('inspect_json_invalid') from None
    if not isinstance(data, dict):
        raise MetadataError('inspect_schema_unrecognized')
    # `inspect` is documented as discovery, not effective account/billing status.
    # Never publish arbitrary strings, configuration values, paths, or auth data.
    known = ('rules', 'skills', 'plugins', 'hooks', 'mcp_servers', 'mcpServers',
             'config', 'configuration', 'settings', 'workspace', 'sources')
    fields = {}
    for key in known:
        if key in data:
            value = data[key]
            fields[key] = {'kind': 'object' if isinstance(value, dict) else 'array' if isinstance(value, list) else 'scalar'}
            if isinstance(value, (list, dict)):
                fields[key]['entry_count'] = len(value)
    return {'status': 'observed', 'source': 'native_inspect_json',
            'discovery_fields': fields, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'effective_auth_route_verified': False, 'effective_model_verified': False}


def model_metadata(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
        raise MetadataError('native_output_limit')
    plain = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', raw.decode('utf-8', errors='replace'))
    # The native text output's full schema has not been observed on Linux yet.
    # Return literal model mentions as diagnostic facts, never as eligibility or
    # model-policy records. No static embedded catalog is substituted here.
    mentions = sorted(set(re.findall(r'(?<![A-Za-z0-9_.-])grok-[a-z0-9][a-z0-9_.-]{0,63}(?![A-Za-z0-9_.-])', plain)))
    if len(mentions) > 1000:
        raise MetadataError('model_mentions_bound')
    return {'status': 'native_output_observed', 'source': 'native_models_command',
            'observed_model_mentions': mentions, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'account_eligible_catalog': False, 'reason': 'native_text_schema_and_account_binding_unverified',
            'supported_efforts': None, 'maximum_effort': None,
            'effort_status': 'unavailable', 'maximum_model_verified': False}


def run_native(command, home, renew, deadline):
    if command not in COMMANDS:
        raise MetadataError('metadata_command_not_allowed')
    process = subprocess.Popen([NATIVE, *command], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=home / 'work',
        env=environment(home), shell=False, start_new_session=True, bufsize=0)
    selector = selectors.DefaultSelector()
    for pipe, label in ((process.stdout, 'out'), (process.stderr, 'err')):
        selector.register(pipe, selectors.EVENT_READ, label)
    output, total, next_renew = bytearray(), 0, time.monotonic() + 25
    try:
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline:
                raise MetadataError('native_metadata_timeout')
            if now >= next_renew:
                renew()
                next_renew = time.monotonic() + 25
            for key, _ in selector.select(0.25):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > MAX_OUTPUT:
                    raise MetadataError('native_output_limit')
                if key.data == 'out':
                    output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise MetadataError('native_metadata_command_failed')
        return bytes(output)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        selector.close()
        process.stdout.close(); process.stderr.close()


def collect(session, *, runner=run_native):
    if session.state != 'active' or session.lease.account_ref != ACCOUNT_REF:
        raise MetadataError('active_durable_owner_lease_required')
    session.broker.assert_current(session.lease)
    home = session.home
    for folder in ('work', 'tmp', 'metadata-private'):
        (home / folder).mkdir(mode=0o700)
    observed, deadline = datetime.now(timezone.utc).isoformat(), time.monotonic() + 90
    renew = lambda: session.broker.renew(session.lease)
    try:
        inspection = runner(COMMANDS[0], home, renew, deadline)
        discovery = inspect_metadata(inspection)
        models_raw = runner(COMMANDS[1], home, renew, deadline)
        models = model_metadata(models_raw)
        # Raw native metadata stays private for a subsequent schema audit. Do not
        # print or upload it; it can contain paths and provider diagnostics.
        for name, body in (('inspect.json', inspection), ('models.txt', models_raw)):
            descriptor = os.open(home / 'metadata-private' / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(body); stream.flush(); os.fsync(stream.fileno())
    except Exception:
        session.broker.quarantine(session.lease, 'provider_refresh_uncertain')
        raise
    version = session.finish(native_stopped=True)
    return {'schema_version': 1, 'provider': AGENT, 'observed_at': observed,
            'account': {'status': 'unavailable', 'intended_account_ref': ACCOUNT_REF,
                        'binding_source': 'independent_enrollment_and_durable_broker',
                        'native_identity_verified': False,
                        'reason': 'no_documented_native_account_status_command'},
            'discovery': discovery, 'models': models,
            'quota': {'status': 'unavailable', 'reason': 'no_documented_native_quota_command'},
            'same_process_account_model_quota': False, 'credential_writeback': 'committed',
            'credential_version_ref': hashlib.sha256(version.encode()).hexdigest(),
            'task_execution_enabled': False, 'model_calls': 0, 'sessions_created': 0, 'claims_tasks': False}


def cloud_main():
    from agent_hub.credential_broker_service import load_client_config
    from credential_state import RefreshSession
    broker = load_client_config(BROKER_CONFIG)
    config = broker.config
    if config.provider != AGENT or config.account_ref != ACCOUNT_REF or config.profile != 'blueeyes':
        raise MetadataError('broker_owner_binding_mismatch')
    execution_id = os.environ.get('CLOUD_RUN_EXECUTION', '')
    if not execution_id or broker.execution != config.job_name + '/executions/' + execution_id:
        raise MetadataError('broker_execution_binding_mismatch')
    request_id = uuid.uuid4().hex
    attempt = ATTEMPT_ROOT / ('metadata-' + request_id)
    attempt.mkdir(mode=0o700)
    with (attempt / 'acquisition.json').open('x', encoding='utf-8') as stream:
        json.dump({'request_id': request_id, 'execution': execution_id, 'state': 'acquiring'}, stream)
        stream.flush(); os.fsync(stream.fileno())
    lease = broker.acquire(broker.execution, request_id)
    session = RefreshSession(broker, lease, attempt / 'home')
    try:
        session.restore()
        return collect(session)
    except Exception:
        broker.quarantine(lease, 'provider_refresh_uncertain')
        raise
