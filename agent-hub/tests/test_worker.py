"""Worker tests use harmless Python fixtures and mocked Google HTTP responses."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler

from agent_hub.adapters import (AdapterError, Command, MAX_OUTPUT_BYTES,
                                MAX_PROMPT_BYTES, bounded_text, build_command,
                                build_prompt, extract_output, extract_output_state)
from agent_hub.core import Hub
from agent_hub.store import SQLiteStore
from agent_hub.worker import (Config, HubClient, LeaseLost, NoRedirect, WorkerError,
                              execute_task, load_config, redact, run_command)


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, exit_code)) and exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path('/proc') / str(pid) / 'stat'
    if status.is_file() and ') Z ' in status.read_text():
        return False
    return True


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-hub-worker-test-')
        self.root = Path(self.temp.name).resolve()
        self.config = Config('https://example.run.app', 'codex', 'worker-test-secret-value',
                             'TEST_WORKER_PRIVATE', {'project': self.root})
        self.task = {'room_id': 'room-test', 'lease_token': 'test-lease', 'prompt': 'Review only',
                     'messages': [], 'workspace': 'project', 'timeout_seconds': 10}

    def tearDown(self):
        self.assertTrue(self.root.is_absolute())
        self.assertEqual(Path(self.temp.name).resolve(), self.root)
        self.temp.cleanup()

    def script(self, name, text):
        path = self.root / name
        self.assertTrue(path.resolve().is_relative_to(self.root))
        path.write_text(text, encoding='utf-8')
        return path

    def fake_client(self):
        client = Mock()
        client.identity_token = None
        client.post.return_value = {'active': True}
        return client

    def process_tree(self, *, cancel=False):
        child = self.script('child.py', 'import time\ntime.sleep(60)\n')
        parent = self.script('parent.py', '''import json, os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, sys.argv[1]])
Path(sys.argv[2]).write_text(json.dumps([os.getpid(), child.pid]))
print('fixture ready', flush=True)
time.sleep(60)
''')
        pid_file = self.root / 'pids.json'
        command = Command((sys.executable, str(parent), str(child), str(pid_file)))
        heartbeat = (lambda: False) if cancel else (lambda: True)
        started = time.monotonic()
        try:
            code, output = run_command(command, self.root, 3,
                                       heartbeat, heartbeat_seconds=0.6 if cancel else 10)
            self.assertEqual(code, 124)
            self.assertIn('cancelled' if cancel else 'timeout', output)
            self.assertLess(time.monotonic() - started, 10)
            self.assertTrue(pid_file.is_file(), 'fixture did not create its child before cancellation')
            pids = json.loads(pid_file.read_text())
            deadline = time.monotonic() + 3
            while any(process_alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(any(process_alive(pid) for pid in pids), 'CLI process tree survived cleanup')
        finally:
            # Only exact process IDs created and recorded by this fixture qualify.
            if pid_file.is_file():
                for pid in json.loads(pid_file.read_text()):
                    if isinstance(pid, int) and pid > 0 and process_alive(pid):
                        if os.name == 'nt':
                            subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32' / 'taskkill.exe'),
                                            '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=5)
                        else:
                            os.kill(pid, signal.SIGKILL)

class WorkerTests(WorkerFixture):
    def test_timeout_kills_real_process_tree(self):
        self.process_tree()

    def test_cancel_kills_real_process_tree(self):
        self.process_tree(cancel=True)

    def test_stdin_is_delivered_and_private_env_is_removed(self):
        script = self.script('env.py', '''import json, os, sys
print(json.dumps({'prompt':sys.stdin.read(),'hub':os.environ.get('HUB_AGENT_TOKEN'),
                  'private':os.environ.get('TEST_WORKER_PRIVATE'),
                  'provider':os.environ.get('PROVIDER_TEST_KEY')}))
''')
        with patch.dict(os.environ, {'HUB_AGENT_TOKEN': 'private-hub-value',
                                     'TEST_WORKER_PRIVATE': 'private-worker-value',
                                     'PROVIDER_TEST_KEY': 'retained-provider-value'}):
            code, output = run_command(Command((sys.executable, str(script)), stdin='bounded prompt'),
                                       self.root, 5, lambda: True, private_env=('TEST_WORKER_PRIVATE',))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {'prompt': 'bounded prompt', 'hub': None,
                                             'private': None, 'provider': 'retained-provider-value'})

    def test_nonreading_stdin_still_times_out(self):
        script = self.script('ignore.py', 'import time\ntime.sleep(60)\n')
        code, output = run_command(Command((sys.executable, str(script)), stdin='x' * 100_000),
                                   self.root, 0.5, lambda: True)
        self.assertEqual(code, 124)
        self.assertIn('timeout', output)

    def test_truncated_actual_output_is_reported_as_failure(self):
        script = self.script('verbose.py', "print('x' * 110_000)\n")
        client = self.fake_client()
        with patch('agent_hub.worker.build_command', return_value=Command((sys.executable, str(script)))):
            code = execute_task(self.config, client, self.task)
        payload = client.post.call_args.args[1]
        self.assertNotEqual(code, 0)
        self.assertNotEqual(payload['exit_code'], 0)
        self.assertIn('incomplete', payload['output'])
        self.assertLessEqual(len(payload['output'].encode('utf-8')), MAX_OUTPUT_BYTES)

    def test_clipped_diagnostic_logs_with_intact_final_json_are_successful(self):
        script = self.script('verbose-final.py', "import json\nprint('x' * 110_000)\nprint(json.dumps({'type':'result','result':'Complete final answer'}))\n")
        client = self.fake_client()
        config = replace(self.config, agent_id='claude')
        with patch('agent_hub.worker.build_command', return_value=Command((sys.executable, str(script)))):
            self.assertEqual(execute_task(config, client, self.task), 0)
        self.assertEqual(client.post.call_args.args[1]['output'], 'Complete final answer')

    def test_complete_stream_final_markers_survive_clipped_earlier_logs(self):
        cases = {
            'codex': [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Complete final answer'}},
                      {'type': 'turn.completed', 'usage': {}}],
            'copilot': [{'type': 'assistant.message', 'data': {'content': 'Complete final answer'}},
                        {'type': 'assistant.turn_end', 'data': {}}, {'type': 'result', 'exitCode': 0}],
            'cursor': [{'type': 'result', 'result': 'Complete final answer'}],
        }
        for agent, events in cases.items():
            with self.subTest(agent=agent):
                raw = '[Earlier output truncated]\n' + '\n'.join(json.dumps(event) for event in events)
                self.assertEqual(extract_output_state(agent, raw), ('Complete final answer', True))
                client = self.fake_client()
                with patch('agent_hub.worker.build_command', return_value=Command((sys.executable,))), \
                        patch('agent_hub.worker.run_command', return_value=(0, raw)):
                    self.assertEqual(execute_task(replace(self.config, agent_id=agent), client, self.task), 0)

    def test_clipped_final_reply_remains_failure_even_with_terminal_event(self):
        client = self.fake_client()
        raw = '[Earlier output truncated]\n' + json.dumps({'type': 'result', 'result': '\U0001f600' * 4001})
        with patch('agent_hub.worker.build_command', return_value=Command((sys.executable,))), \
                patch('agent_hub.worker.run_command', return_value=(0, raw)):
            self.assertEqual(execute_task(replace(self.config, agent_id='claude'), client, self.task), 1)
        self.assertIn('hub output limit', client.post.call_args.args[1]['output'])

    def test_setup_elapsed_and_server_deadline_reserve_completion_time(self):
        client = self.fake_client()
        client.post.side_effect = [{'active': True, 'deadline': 1030, 'server_time': 1006}, {'status': 'completed'}]
        task = dict(self.task, timeout_seconds=30, deadline=1030)
        with patch('agent_hub.worker.time.monotonic', side_effect=[100, 106, 108]), \
                patch('agent_hub.worker.time.time', side_effect=AssertionError('VPS wall clock must not set the budget')), \
                patch('agent_hub.worker.build_command', return_value=Command((sys.executable,))), \
                patch('agent_hub.worker.run_command', return_value=(0, 'Reviewed')) as run:
            self.assertEqual(execute_task(self.config, client, task), 0)
        # Hub has 24s left; conservatively subtract the 2s heartbeat round trip
        # and reserve 15s for cleanup/result delivery. Cold-start time is counted.
        self.assertEqual(run.call_args.args[2], 7)

    def test_insufficient_deadline_budget_skips_cli_and_delivers_timeout(self):
        client = self.fake_client()
        client.post.side_effect = [{'active': True, 'deadline': 1030, 'server_time': 1027}, {'status': 'failed'}]
        with patch('agent_hub.worker.build_command', return_value=Command((sys.executable,))), \
                patch('agent_hub.worker.run_command') as run:
            self.assertEqual(execute_task(self.config, client, dict(self.task, timeout_seconds=30, deadline=1030)), 124)
        run.assert_not_called()
        self.assertEqual(client.post.call_args.args[1]['exit_code'], 124)

    def test_timeout_result_reaches_real_hub_before_authoritative_deadline(self):
        offset = [0]
        hub = Hub(SQLiteStore(self.root / 'deadline.sqlite3'), clock=lambda: time.time() + offset[0])
        room = hub.create('manager', {'prompt': 'Review only', 'agents': ['codex'], 'workspace': 'project', 'timeout_seconds': 30})
        task = hub.claim('codex')['task']
        offset[0] = 14  # Simulate time spent on auth/dispatch; VPS wall clock differs.
        client = self.fake_client()
        def local_post(path, body):
            if path.endswith('/heartbeat'):
                return hub.heartbeat('codex', room['id'], body['lease_token'])
            return hub.complete('codex', room['id'], body)
        client.post.side_effect = local_post
        script = self.script('deadline.py', 'import time\ntime.sleep(60)\n')
        with patch('agent_hub.worker.build_command', return_value=Command((sys.executable, str(script)))):
            self.assertEqual(execute_task(self.config, client, task), 124)
        final = hub.get('manager', room['id'])
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['messages'][0]['exit_code'], 124)
        self.assertLess(final['messages'][0]['time'], task['deadline'])

    def test_unicode_output_budget_and_secret_redaction(self):
        client = self.fake_client()
        secret = self.config.token
        with patch('agent_hub.worker.build_command', return_value=Command((sys.executable,))), \
             patch('agent_hub.worker.run_command', return_value=(0, secret + '\n' + '\U0001f600' * 10_000)):
            self.assertEqual(execute_task(self.config, client, self.task), 1)
        output = client.post.call_args.args[1]['output']
        self.assertLessEqual(len(output.encode('utf-8')), MAX_OUTPUT_BYTES)
        self.assertIn('incomplete', output)
        self.assertNotIn(secret, output)
        self.assertEqual(redact(secret, self.config, client), '[REDACTED]')
        self.assertLessEqual(len(extract_output('claude', json.dumps({'result': '\U0001f600' * 10_000})).encode()), MAX_OUTPUT_BYTES)

    def test_no_rerun_when_completion_retries(self):
        client = self.fake_client()
        client.post.side_effect = [{'active': True}, WorkerError('temporary'), {'status': 'complete'}]
        with patch('agent_hub.worker.build_command', return_value=Command((sys.executable,))), \
             patch('agent_hub.worker.run_command', return_value=(0, 'completed once')) as run, \
             patch('agent_hub.worker.time.sleep'):
            self.assertEqual(execute_task(self.config, client, self.task), 0)
        run.assert_called_once()
        self.assertEqual(client.post.call_count, 3)

    def test_hub_rejection_of_completed_output_returns_failure(self):
        client = self.fake_client()
        client.post.side_effect = [{'active': True}, {'room_id': 'room-test', 'status': 'failed'}]
        with patch('agent_hub.worker.build_command', return_value=Command((sys.executable,))), \
             patch('agent_hub.worker.run_command', return_value=(0, 'successful CLI output')):
            self.assertEqual(execute_task(self.config, client, self.task), 1)

    def test_bad_workspace_is_completed_as_failure_without_execution(self):
        task = dict(self.task, workspace='../secrets')
        client = self.fake_client()
        with patch('agent_hub.worker.run_command') as run:
            self.assertEqual(execute_task(self.config, client, task), 1)
        run.assert_not_called()
        self.assertIn('not allowed', client.post.call_args.args[1]['output'])

    def test_invalid_task_lease_fails_without_network(self):
        client = self.fake_client()
        with self.assertRaises(WorkerError):
            execute_task(self.config, client, dict(self.task, lease_token=None))
        client.post.assert_not_called()

    def test_prompt_budget_and_missing_executable_fail_explicitly(self):
        prompt = build_prompt({'prompt': '\U0001f600' * 20_000,
                               'messages': [{'agent': 'peer', 'text': 'x' * 40_000}] * 50}, 'codex')
        self.assertLessEqual(len(prompt.encode()), MAX_PROMPT_BYTES)
        self.assertLessEqual(len(bounded_text('\U0001f600' * 10, 2).encode()), 2)
        with self.assertRaises(AdapterError):
            build_command('codex', 'text', str(self.root / 'not-installed'))
        with self.assertRaises(AdapterError):
            build_command('cursor', 'text')

    def test_prompt_preserves_prior_failure_codes_and_states_utf8_reply_limit(self):
        prompt = build_prompt({'prompt': 'Review', 'messages': [
            {'agent': 'claude', 'text': 'Failed auth', 'exit_code': 1},
            {'agent': 'cursor', 'text': 'Reviewed', 'exit_code': 0}]}, 'codex')
        self.assertIn('16000 UTF-8 bytes', prompt)
        self.assertIn('"exit_code": 1', prompt)
        self.assertIn('"exit_code": 0', prompt)


class AuthTests(WorkerFixture):
    def test_metadata_audience_headers_no_proxy_and_cache(self):
        config = replace(self.config, cloud_run_auth_mode='metadata')
        client = HubClient(config)
        metadata = Mock()
        metadata.open.return_value = Response(b'mock.metadata.jwt', {'Metadata-Flavor': 'Google'})
        with patch('agent_hub.worker.build_opener', return_value=metadata) as builder, \
             patch('agent_hub.worker.subprocess.run') as gcloud:
            self.assertEqual(client._identity(), 'mock.metadata.jwt')
            self.assertEqual(client._identity(), 'mock.metadata.jwt')
        gcloud.assert_not_called()
        metadata.open.assert_called_once()
        handlers = builder.call_args.args
        self.assertTrue(any(isinstance(h, NoRedirect) for h in handlers))
        self.assertEqual(next(h.proxies for h in handlers if isinstance(h, ProxyHandler)), {})
        request = metadata.open.call_args.args[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual(parsed.hostname, 'metadata.google.internal')
        self.assertEqual(parse_qs(parsed.query)['audience'], [config.hub_url])
        self.assertEqual(parse_qs(parsed.query)['format'], ['full'])
        self.assertEqual(request.get_header('Metadata-flavor'), 'Google')
        self.assertEqual(metadata.open.call_args.kwargs['timeout'], 5)
        self.assertLessEqual(client.identity_expires - time.monotonic(), 600)

    def test_default_mode_never_queries_google_identity(self):
        client = HubClient(self.config)
        client.opener = Mock()
        client.opener.open.return_value = Response(b'{"task":null}')
        with patch.object(client, '_identity') as identity:
            self.assertEqual(client.post('/v1/tasks/claim', {}), {'task': None})
        identity.assert_not_called()
        request = client.opener.open.call_args.args[0]
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(request.get_header('X-hub-token'), self.config.token)

    def test_auth_refreshes_once_on_401_and_403(self):
        for status in (401, 403):
            with self.subTest(status=status):
                client = HubClient(replace(self.config, cloud_run_auth_mode='metadata'))
                client.opener = Mock()
                client.opener.open.side_effect = [HTTPError(self.config.hub_url, status, 'auth', {}, None),
                                                  Response(b'{"task":null}')]
                with patch.object(client, '_identity', side_effect=['old.jwt', 'new.jwt']) as identity:
                    self.assertEqual(client.post('/v1/tasks/claim', {}), {'task': None})
                self.assertEqual(identity.call_count, 2)
                requests = client.opener.open.call_args_list
                self.assertEqual(requests[0].args[0].get_header('Authorization'), 'Bearer old.jwt')
                self.assertEqual(requests[1].args[0].get_header('Authorization'), 'Bearer new.jwt')

    def test_permanent_auth_failure_has_only_one_refresh(self):
        client = HubClient(replace(self.config, cloud_run_auth=True))
        client.opener = Mock()
        client.opener.open.side_effect = lambda *a, **k: (_ for _ in ()).throw(
            HTTPError(self.config.hub_url, 403, 'auth', {}, None))
        with patch.object(client, '_identity', return_value='mock.jwt') as identity:
            with self.assertRaisesRegex(WorkerError, 'HTTP 403'):
                client.post('/v1/tasks/claim', {})
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(client.opener.open.call_count, 2)

    def test_metadata_errors_never_expose_response_or_allow_http(self):
        client = HubClient(replace(self.config, cloud_run_auth_mode='metadata'))
        cases = [(b'sensitive-token', {}), (b'x' * 16_385, {'Metadata-Flavor': 'Google'}),
                 (b'bad token', {'Metadata-Flavor': 'Google'})]
        for body, headers in cases:
            with self.subTest(length=len(body)):
                metadata = Mock()
                metadata.open.return_value = Response(body, headers)
                with patch('agent_hub.worker.build_opener', return_value=metadata):
                    with self.assertRaises(WorkerError) as error:
                        client._metadata_identity()
                self.assertNotIn('sensitive-token', str(error.exception))
                self.assertNotIn('bad token', str(error.exception))
        insecure = HubClient(replace(self.config, hub_url='http://127.0.0.1:8080', cloud_run_auth_mode='metadata'))
        with patch('agent_hub.worker.build_opener') as builder:
            with self.assertRaises(WorkerError):
                insecure._metadata_identity()
        builder.assert_not_called()

    def test_config_auth_modes_and_validation(self):
        source = {'hub_url': self.config.hub_url, 'agent_id': 'codex',
                  'workspaces': {'project': str(self.root)}}
        path = self.root / 'worker.json'
        with patch.dict(os.environ, {}, clear=True):
            for auth, expected in [({}, None), ({'cloud_run_auth': True}, 'gcloud'),
                                   ({'cloud_run_auth_mode': 'metadata'}, 'metadata')]:
                path.write_text(json.dumps({**source, **auth}))
                self.assertEqual(load_config(str(path), dry_run=True).cloud_run_auth_mode, expected)
            invalid = [{'cloud_run_auth_mode': 'automatic'}, {'cloud_run_auth': 'true'},
                       {'cloud_run_auth_mode': 'metadata', 'hub_url': 'http://localhost:8080'},
                       {'hub_url': 'https://user:secret@example.run.app'},
                       {'poll_seconds': float('nan')}, {'workspaces': {'project': '../bad'}}]
            for settings in invalid:
                with self.subTest(settings=settings):
                    path.write_text(json.dumps({**source, **settings}))
                    with self.assertRaises(WorkerError):
                        load_config(str(path), dry_run=True)


if __name__ == '__main__':
    unittest.main()
