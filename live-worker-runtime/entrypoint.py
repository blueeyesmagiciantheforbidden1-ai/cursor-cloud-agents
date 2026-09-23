"""Protected cloud job entrypoint for a single warmed native worker."""
from pathlib import Path
import importlib
import json
import os
import re
import signal
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler
import uuid

# Only image-owned immutable modules. Neither task workspaces nor HOME enter
# the Python module path. The process runs with -I and a non-root UID.
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), '/opt/runcrew', '/opt/runcrew/app']

from agent_hub.worker import Config, HubClient, NoRedirect, WorkerError
from dynamic_broker import load_client
from live_loop import Settings, Worker
import broker_renew
import provider_errors

HUB = 'https://runcrew-hub-kdhodumsza-uc.a.run.app'


class Client(HubClient):
    def __init__(self, config):
        super().__init__(config)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def get_room(self, room_id):
        if not re.fullmatch('[a-f0-9]{32}', room_id): raise WorkerError('room_id_invalid')
        request = Request(HUB + '/v1/rooms/' + room_id,
            headers={'Authorization': 'Bearer ' + self._identity(), 'X-Hub-Token': self.config.token,
                     'X-Hub-Agent': self.config.agent_id})
        with self.opener.open(request, timeout=10) as response: raw = response.read(256001)
        if len(raw) > 256000: raise WorkerError('room_context_too_large')
        result = json.loads(raw)
        if not isinstance(result, dict): raise WorkerError('room_response_invalid')
        return result


PROVIDERS = ('grok', 'codex', 'copilot', 'claude', 'cursor')


def image_check():
    """Build-time gate: the image's provider module, credential handler and native binary import cleanly.

    No credential, cloud client, prompt or provider process is involved.
    """
    agent = os.environ.get('RUNCREW_LIVE_PROVIDER', '')
    if agent not in PROVIDERS: raise RuntimeError('live_provider_env_required')
    if os.name != 'posix' or os.geteuid() == 0: raise RuntimeError('cloud_rootless_linux_required')
    adapter = importlib.import_module('providers.' + agent)
    for name in ('prepare', 'maintain', 'execute', 'close'):
        if not callable(getattr(adapter, name, None)): raise RuntimeError('provider_contract_incomplete')
    credential = importlib.import_module('credential_state')
    if getattr(credential, 'AGENT', agent) != agent and getattr(credential, 'PROFILES', None) is None:
        raise RuntimeError('credential_state_provider_mismatch')
    native = {'grok': '/opt/runcrew/grok/grok', 'codex': '/opt/runcrew/codex/bin/codex',
              'copilot': '/opt/runcrew/copilot/copilot', 'claude': '/opt/runcrew/claude/claude',
              'cursor': '/opt/runcrew/cursor/cursor-agent'}[agent]
    if not (Path(native).is_file() and os.access(native, os.X_OK)): raise RuntimeError('native_binary_missing')
    return {'status': 'image_ok', 'provider': agent, 'native': native, 'credentials_included': False}


def main():
    if sys.argv[1:] == ['--check']:
        print(json.dumps(image_check()), flush=True)
        return 0
    if sys.argv[1:]: raise RuntimeError('unsupported_arguments')
    if (os.name != 'posix' or os.geteuid() == 0 or not os.environ.get('CLOUD_RUN_EXECUTION')
            or os.environ.get('CLOUD_RUN_TASK_ATTEMPT') not in (None, '0')):
        raise RuntimeError('cloud_single_attempt_required')
    broker = load_client('/run/config/live-worker.json')
    agent = broker.config.provider
    if agent not in PROVIDERS: raise RuntimeError('provider_not_packaged')
    # The image is provider-specific; a config naming another provider is a deployment error.
    if os.environ.get('RUNCREW_LIVE_PROVIDER', agent) != agent: raise RuntimeError('provider_image_mismatch')
    token = os.environ.pop('HUB_AGENT_TOKEN', '')
    if not token or '\r' in token or '\n' in token: raise RuntimeError('hub_token_required')
    # Acquire before native starts. The request ID is unique for this one
    # process. There is no second acquisition or execution retry in this job.
    request_id = uuid.uuid4().hex
    home = Path('/home/worker') / ('live-' + request_id)
    intent = Path('/home/worker') / ('acquire-' + request_id + '.json')
    with intent.open('x', encoding='utf-8') as stream:
        json.dump({'request_id': request_id, 'execution_uid': broker.execution_uid}, stream)
        stream.flush(); os.fsync(stream.fileno())
    acquire_started = time.monotonic()
    lease = broker.acquire(broker.execution, request_id)
    broker_renew.record_acquire_start(lease.lease_id, acquire_started)
    from credential_state import RefreshSession
    session = RefreshSession(broker, lease, home)
    worker = None
    try:
        session.restore()
        adapter = importlib.import_module('providers.' + agent)
        worker_id = agent + '-live-' + broker.execution_uid.replace('-', '')
        settings = Settings(agent, worker_id)
        config = Config(hub_url=HUB, agent_id=agent, token=token,
                        token_env='HUB_AGENT_TOKEN', workspaces={'default': home},
                        cloud_run_auth=True, cloud_run_auth_mode='metadata', worker_id=worker_id)
        def emit(value):
            if isinstance(value, dict) and value.get('kind') == 'runcrew_live_span':
                print(json.dumps(value), flush=True)
                return
            print(json.dumps({'kind': 'runcrew_live_result',
                'execution_uid': broker.execution_uid, 'worker_id': worker_id, **value}), flush=True)
        worker = Worker(settings, Client(config), adapter, session,
                        log=emit, trace_id=broker.execution_uid)
        def stop(signum, frame):
            # Interrupt at most once and never inside the credential close or
            # the completion POST (Worker.on_signal); run() turns the interrupt
            # into one worker_stopping completion for a claimed room.
            if worker.on_signal():
                raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        result = worker.run()
        if result['outcome'] in ('completed', 'idle_drained'):
            return 0
        return worker.last_exit if worker.last_exit == provider_errors.QUOTA_EXIT_CODE else 1
    finally:
        # prepare() owns cleanup if a native child was started but its handle
        # could not be returned. Only a known zero-native case can finish here.
        if worker is None and session.state == 'active':
            session.finish(native_stopped=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('{"status":"stopped","error":"cloud_job_stopped"}', flush=True)
        raise SystemExit(130)
    except Exception:
        print('{"status":"stopped","error":"live_worker_startup_failed"}', flush=True)
        raise SystemExit(1)
