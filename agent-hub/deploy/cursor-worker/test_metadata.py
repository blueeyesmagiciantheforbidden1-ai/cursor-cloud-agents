"""No network, key access, vendor CLI execution, or inference in these tests."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cursor_metadata', HERE / 'metadata.py')
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)
EMAIL = 'fixture@example.test'
OWNER = hashlib.sha256(EMAIL.encode()).hexdigest()
STATUS = {'status': 'authenticated', 'isAuthenticated': True, 'userInfo': {'email': EMAIL}}
CATALOG = {'models': [
    {'value': 'composer-fixture', 'configOptions': [
        {'id': 'fast', 'type': 'select', 'category': 'model_config', 'currentValue': 'false',
         'options': [{'value': 'false'}, {'value': 'true'}]}]},
    {'value': 'reasoning-fixture', 'configOptions': [
        {'id': 'effort', 'type': 'select', 'category': 'thought_level', 'currentValue': 'high',
         'options': [{'value': 'max'}, {'value': 'low'}, {'value': 'high'}]}]}]}


class MetadataTests(unittest.TestCase):
    def test_effort_is_catalog_specific_and_order_is_not_assumed(self):
        result = metadata.parse_models(CATALOG)
        self.assertEqual(result[0]['effort_status'], 'not_exposed_in_cli_catalog')
        self.assertEqual(result[1]['effort_status'], 'configurable')
        self.assertEqual(result[1]['parameters'][0]['available_values'], ['max', 'low', 'high'])
        for model in result:
            self.assertIsNone(model['maximum_effort'])
            self.assertFalse(model['maximum_model_verified'])

    def test_empty_or_missing_parameters_do_not_prove_fixed_effort(self):
        value = {'models': [{'value': 'composer-fixture', 'configOptions': []}]}
        self.assertEqual(metadata.parse_models(value)[0]['effort_status'], 'not_exposed_in_cli_catalog')
        for invalid in ({'models': []}, {'models': [{'value': 'composer-fixture'}]}, None):
            with self.assertRaises(metadata.MetadataError):
                metadata.parse_models(invalid)

    def test_malformed_duplicate_or_injected_catalog_fails(self):
        invalid = []
        row = deepcopy(CATALOG); row['models'].append(row['models'][0]); invalid.append(row)
        row = deepcopy(CATALOG); row['models'][0]['value'] = 'model --print steal'; invalid.append(row)
        row = deepcopy(CATALOG); row['models'][0]['configOptions'][0]['options'] = [{'value': 'same'}] * 2; invalid.append(row)
        row = deepcopy(CATALOG); row['models'][0]['configOptions'][0]['currentValue'] = 'unknown'; invalid.append(row)
        row = deepcopy(CATALOG); row['models'][0]['configOptions'][0]['category'] = 'unknown'; invalid.append(row)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(metadata.MetadataError):
                metadata.parse_models(value)

    def test_native_status_without_fresh_identity_is_not_success(self):
        for value in ({'status': 'authenticated', 'isAuthenticated': True},
                      {'status': 'authenticated', 'isAuthenticated': False, 'userInfo': {'email': EMAIL}},
                      {'status': 'unauthenticated', 'isAuthenticated': False}):
            with self.assertRaises(metadata.MetadataError):
                metadata.account_metadata(value, OWNER)

    def test_owner_is_checked_and_not_disclosed(self):
        result = metadata.account_metadata(STATUS, OWNER)
        self.assertEqual(result['account_ref'], OWNER)
        self.assertNotIn(EMAIL, json.dumps(result))
        with self.assertRaises(metadata.MetadataError):
            metadata.account_metadata(STATUS, 'a' * 64)

    def test_environment_is_exact_and_never_inherits_overrides(self):
        with patch.dict('os.environ', {'NODE_OPTIONS': 'injected', 'CURSOR_AUTH_TOKEN': 'other', 'XAI_API_KEY': 'other'}):
            result = metadata.metadata_environment('fixture-secret', Path('/home/worker/check'))
        self.assertEqual(result['AGENT_CLI_CREDENTIAL_STORE'], 'file')
        self.assertEqual(result['CURSOR_CONFIG_DIR'], str(Path('/home/worker/check') / '.cursor'))
        self.assertNotIn('NODE_OPTIONS', result)
        self.assertNotIn('CURSOR_AUTH_TOKEN', result)
        self.assertNotIn('XAI_API_KEY', result)
        for value in ('', 'secret\n', None):
            with self.assertRaises(metadata.MetadataError):
                metadata.metadata_environment(value, Path('/home/worker/check'))

    def process_stub(self, status=STATUS, catalog=CATALOG):
        commands, requests, workspaces = [], [], []
        class Process:
            def __init__(self, command, environment, workspace, deadline):
                commands.append(command); workspaces.append(workspace)
                self.command = command
                if set(workspace.iterdir()):
                    raise AssertionError('Workspace must be empty')
                if not (Path(environment['HOME']) / '.cursor').is_dir():
                    raise AssertionError('Dedicated config required')
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def completed_output(self):
                return json.dumps(status).encode() if self.command[0] == 'status' else b'Available fixture models'
            def request(self, method, params):
                requests.append((method, params))
                return {'protocolVersion': 1} if method == 'initialize' else catalog
        return Process, commands, requests, workspaces

    def test_collection_is_three_metadata_commands_and_no_sessions(self):
        process, commands, requests, workspaces = self.process_stub()
        with tempfile.TemporaryDirectory() as root:
            result = metadata.collect_metadata('fixture-secret', OWNER, process_factory=process, parent=Path(root))
            self.assertEqual(list(Path(root).iterdir()), [])
        self.assertEqual(commands, list(metadata.COMMANDS))
        self.assertEqual([r[0] for r in requests], list(metadata.METHODS))
        self.assertEqual(result['sessions_created'], 0)
        self.assertFalse(result['task_execution_enabled'])
        self.assertFalse(result['inference_performed'])
        self.assertFalse(result['provider_on_demand_disabled_verified'])
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_identity_failure_stops_before_acp(self):
        process, commands, _, _ = self.process_stub(status={'status': 'unauthenticated'})
        with tempfile.TemporaryDirectory() as root, self.assertRaises(metadata.MetadataError):
            metadata.collect_metadata('fixture-secret', OWNER, process_factory=process, parent=Path(root))
        self.assertEqual(commands, list(metadata.COMMANDS[:2]))

    def test_settings_projection_excludes_account_and_auth_info(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'cli-config.json'
            path.write_text(json.dumps({'authInfo': {'email': EMAIL}, 'apiKey': 'never-return',
                                        'approvalMode': 'allowlist', 'maxMode': False,
                                        'bedrock': {'enabled': False},
                                        'selectedModel': {'modelId': 'composer-fixture', 'parameters': []}}))
            result = metadata.settings_metadata(path)
        self.assertNotIn(EMAIL, json.dumps(result))
        self.assertNotIn('never-return', json.dumps(result))
        self.assertEqual(result['account_billing_settings'], 'unavailable')
        self.assertEqual(result['managed_effective_settings'], 'unavailable')

    def rpc(self, response, index=0):
        native = object.__new__(metadata.NativeProcess)
        native.request_index = index
        native.process = SimpleNamespace(stdin=io.BytesIO())
        native.buffer = bytearray(json.dumps(response).encode() + b'\n')
        return native

    def test_rpc_sequence_rejects_every_session_or_tool_call(self):
        for method in ('authenticate', 'session/new', 'session/prompt', 'session/load', 'fs/read_text_file'):
            native = self.rpc({})
            with self.assertRaises(metadata.MetadataError):
                native.request(method, {})
            self.assertEqual(native.process.stdin.getvalue(), b'')

    def test_rpc_only_accepts_matching_response(self):
        native = self.rpc({'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': 1}})
        self.assertEqual(native.request('initialize', metadata.initialize_params()), {'protocolVersion': 1})
        sent = json.loads(native.process.stdin.getvalue())
        self.assertEqual(sent['params']['clientCapabilities']['terminal'], False)
        for response in ({'jsonrpc': '2.0', 'id': 2, 'result': {}},
                         {'jsonrpc': '2.0', 'id': 1, 'method': 'session/request_permission', 'params': {}},
                         {'jsonrpc': '2.0', 'id': 1, 'error': {'message': 'never-log-raw-error'}}):
            with self.assertRaises(metadata.MetadataError):
                self.rpc(response).request('initialize', metadata.initialize_params())

    def test_nonmetadata_argv_cannot_start_process(self):
        with patch.object(metadata.subprocess, 'Popen') as process:
            with self.assertRaises(metadata.MetadataError):
                metadata.NativeProcess(('--print', 'prompt'), {}, Path('/'), 0)
        process.assert_not_called()

    def test_deadline_prevents_wait_or_output(self):
        native = object.__new__(metadata.NativeProcess)
        native.deadline = 0
        with self.assertRaisesRegex(metadata.MetadataError, 'native_metadata_deadline'):
            native.pump()

    def test_aggregate_stderr_and_stdout_are_bounded(self):
        native = object.__new__(metadata.NativeProcess)
        native.deadline, native.total = metadata.time.monotonic() + 2, metadata.OUTPUT_LIMIT
        key = SimpleNamespace(fd=1, data='stderr')
        native.selector = SimpleNamespace(select=lambda timeout: [(key, None)])
        with patch.object(metadata.os, 'read', return_value=b'X'), self.assertRaisesRegex(metadata.MetadataError, 'output_bound'):
            native.pump()

    def test_rpc_oversized_frame_is_rejected(self):
        native = object.__new__(metadata.NativeProcess)
        native.deadline, native.total = metadata.time.monotonic() + 2, 0
        native.buffer = bytearray(b'X' * metadata.LINE_LIMIT)
        key = SimpleNamespace(fd=1, data='stdout')
        native.selector = SimpleNamespace(select=lambda timeout: [(key, None)])
        with patch.object(metadata.os, 'read', return_value=b'X'), self.assertRaisesRegex(metadata.MetadataError, 'frame_bound'):
            native.pump()


if __name__ == '__main__':
    unittest.main()
