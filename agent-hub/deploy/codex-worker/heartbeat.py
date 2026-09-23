"""Handle --heartbeat-only with no native CLI, provider home, or task claim."""
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import quote

PROJECT_ID = 'project-0c6d31fa-509e-4116-a2c'
HUB_HOST = 'runcrew-hub-kdhodumsza-uc.a.run.app'
HUB_URL = 'https://' + HUB_HOST
CONFIG = Path('/run/config/worker.json')
REPORT_PATH = '/v1/workers/report'
TOKEN_ENV = 'HUB_AGENT_TOKEN'  # Dedicated runcrew-worker-codex-hub secret, version pinned by deployment.
MAX_CONFIG = 16_384
MAX_RESPONSE = 256_000
FIXED = {
    'project_id': PROJECT_ID, 'hub_url': HUB_URL, 'agent_id': 'codex',
    'executable': '/opt/runcrew/codex/bin/codex', 'token_env': TOKEN_ENV,
    'workspaces': {'default': '/workspace/default'}, 'cloud_run_auth_mode': 'metadata',
    'billing_policy': {'mode': 'subscription_only'}, 'model_policy_required': True,
    'model_policy_bundle': '/run/config/model-catalog.json', 'execution_mode': 'read_only',
}


class HeartbeatError(RuntimeError):
    """Fixed error codes only; never include Google/hub response bodies or tokens."""


def require(condition, reason):
    if not condition:
        raise HeartbeatError(reason)


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        require(isinstance(value, dict), 'invalid_json_object')
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise HeartbeatError('invalid_json_object') from None


def worker_config(value):
    allowed = set(FIXED) | {'worker_id', 'timeout_seconds', 'model_policy_agents', 'expected_account_ref'}
    require(isinstance(value, dict) and not set(value) - allowed, 'unsupported_heartbeat_configuration')
    require(not any(key in value and (type(value[key]) is not type(expected) or value[key] != expected)
                    for key, expected in FIXED.items()),
            'fixed_heartbeat_configuration_changed')
    worker_id = value.get('worker_id', 'codex-cloud')
    account_ref = value.get('expected_account_ref')
    timeout = value.get('timeout_seconds', 180)
    agents = value.get('model_policy_agents', ['codex'])
    require(isinstance(worker_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,48}', worker_id), 'worker_id_invalid')
    require(isinstance(account_ref, str) and re.fullmatch(r'[a-f0-9]{64}', account_ref), 'owner_binding_required')
    require(type(timeout) is int and 30 <= timeout <= 300, 'timeout_invalid')
    require(isinstance(agents, list) and 'codex' in agents
            and all(isinstance(item, str) and item in ('codex', 'claude', 'cursor', 'copilot', 'grok') for item in agents)
            and len(set(agents)) == len(agents), 'model_policy_agents_invalid')
    # These are configuration bindings only, not evidence of provider login,
    # account ownership, model access or project dispatch readiness.
    return {**FIXED, 'worker_id': worker_id, 'expected_account_ref': account_ref,
            'timeout_seconds': timeout, 'model_policy_agents': agents}


def read_config():
    require(CONFIG.resolve(strict=True) == CONFIG and not CONFIG.is_symlink(), 'configuration_path_invalid')
    info = CONFIG.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and not os.access(CONFIG, os.W_OK) and 0 < info.st_size <= MAX_CONFIG,
            'protected_configuration_required')
    with CONFIG.open('rb') as stream:
        raw = stream.read(MAX_CONFIG + 1)
    require(len(raw) <= MAX_CONFIG, 'configuration_size_invalid')
    return worker_config(parse_json(raw))


def valid_token(value):
    require(isinstance(value, str) and 20 <= len(value) <= 16_384
            and all(32 < ord(char) < 127 for char in value), 'dedicated_hub_token_required')
    return value


class HeartbeatTransport:
    """Only fixed metadata GET and hub report POST; no proxies or redirects."""
    def identity(self):
        connection = http.client.HTTPConnection('metadata.google.internal', timeout=5)
        path = ('/computeMetadata/v1/instance/service-accounts/default/identity?audience='
                + quote(HUB_URL, safe='') + '&format=full')
        try:
            connection.request('GET', path, headers={'Metadata-Flavor': 'Google'})
            response = connection.getresponse()
            require(response.status == 200 and response.getheader('Metadata-Flavor') == 'Google',
                    'metadata_identity_unavailable')
            raw = response.read(16_385)
            require(len(raw) <= 16_384, 'metadata_identity_limit')
            try:
                token = raw.decode('ascii').strip()
            except UnicodeError:
                raise HeartbeatError('metadata_identity_invalid') from None
            require(token and all(32 < ord(char) < 127 for char in token), 'metadata_identity_invalid')
            return token
        except (OSError, http.client.HTTPException, ValueError):
            raise HeartbeatError('metadata_identity_unavailable') from None
        finally:
            connection.close()

    def report(self, config, token):
        identity = self.identity()
        body = {'worker_id': config['worker_id'], 'status': 'offline', 'auth_status': 'unknown',
                'current_room_id': None, 'last_exit_code': None, 'usage': []}
        connection = http.client.HTTPSConnection(HUB_HOST, timeout=10)
        try:
            connection.request('POST', REPORT_PATH, body=json.dumps(body, separators=(',', ':')).encode(),
                headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                         'X-Hub-Token': token, 'X-Hub-Agent': 'codex', 'Authorization': 'Bearer ' + identity})
            response = connection.getresponse()
            require(response.status == 200, 'hub_heartbeat_rejected')
            raw = response.read(MAX_RESPONSE + 1)
            require(len(raw) <= MAX_RESPONSE, 'hub_response_limit')
            require(parse_json(raw).get('accepted') is True, 'hub_heartbeat_unacknowledged')
        except (OSError, http.client.HTTPException, ValueError):
            raise HeartbeatError('hub_heartbeat_unavailable') from None
        finally:
            connection.close()


def run():
    require(sys.platform == 'linux' and os.geteuid() == 10001, 'rootless_linux_worker_required')
    os.umask(0o077)
    config = read_config()
    token = valid_token(os.environ.get(TOKEN_ENV))
    # Neither inherited API keys/proxies nor the hub role secret remain in the
    # environment. No provider subprocess is imported, constructed, or started.
    os.environ.clear()
    os.environ.update({'HOME': '/home/worker', 'PATH': '/usr/local/bin:/usr/bin:/bin',
                       'LANG': 'C.UTF-8', 'TMPDIR': '/tmp'})
    HeartbeatTransport().report(config, token)
    return {'mode': 'heartbeat-only', 'telemetry_delivered': True, 'worker_status': 'offline',
            'provider_login_verified': False, 'inference_performed': False,
            'claims_or_completes_tasks': False}


def main():
    try:
        print(json.dumps(run()), flush=True)
        return 0
    except (HeartbeatError, OSError, ValueError):
        print(json.dumps({'mode': 'heartbeat-only', 'telemetry_delivered': False,
            'status': 'rejected', 'provider_login_verified': False, 'inference_performed': False,
            'claims_or_completes_tasks': False}), file=sys.stderr, flush=True)
        return 1
