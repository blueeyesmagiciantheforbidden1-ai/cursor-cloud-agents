"""Commissioning must not execute provider code, claim work, or mask failure."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from agent_hub import worker
from agent_hub.adapters import Command
from agent_hub.billing_policy import BillingPolicy


class HeartbeatOnlyTests(unittest.TestCase):
    def run_mode(self, *, response=None, error=None):
        # Intentionally nonexistent: heartbeat cannot read a model catalog.
        config = worker.Config('https://hub.example.run.app', 'claude', 'fixture-hub-token',
                               'HUB_AGENT_TOKEN', {'default': Path('/workspace/default')},
                               executable='/opt/runcrew/claude/claude', cloud_run_auth_mode='metadata',
                               worker_id='claude-cloud', billing_policy=BillingPolicy(),
                               model_policy_required=True, model_policy_bundle=Path('/does-not-exist/catalog.json'),
                               expected_account_ref='a' * 64, execution_mode='project_work')
        client = Mock()
        client.post.return_value = {'accepted': True} if response is None else response
        client.post.side_effect = error
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(worker, 'load_config', return_value=config))
            build = stack.enter_context(patch.object(worker, 'build_command', return_value=Command((config.executable,))))
            stack.enter_context(patch.object(worker, 'HubClient', return_value=client))
            forbidden = [stack.enter_context(patch(target, side_effect=AssertionError('Forbidden during heartbeat')))
                         for target in ('agent_hub.worker.TelemetryReporter', 'agent_hub.worker.execute_task',
                                        'agent_hub.worker.run_command', 'agent_hub.worker.subprocess.Popen',
                                        'agent_hub.worker.subprocess.run', 'agent_hub.worker.preflight_auth_route',
                                        'agent_hub.model_runtime.select_worker_model')]
            stack.enter_context(redirect_stdout(output)); stack.enter_context(redirect_stderr(errors))
            result = worker.main(['--config', 'fixture.json', '--heartbeat-only'])
        build.assert_called_once()
        for operation in forbidden:
            operation.assert_not_called()
        client.post.assert_called_once_with('/v1/workers/report', {
            'worker_id': 'claude-cloud', 'status': 'offline', 'auth_status': 'unknown',
            'current_room_id': None, 'last_exit_code': None, 'usage': [],
        })
        return result, output.getvalue(), errors.getvalue()

    def test_missing_catalog_can_publish_only_offline_unknown_heartbeat(self):
        result, output, errors = self.run_mode()
        self.assertEqual(result, 0)
        receipt = json.loads(output)
        self.assertTrue(receipt['telemetry_delivered'])
        self.assertFalse(receipt['provider_login_verified'])
        self.assertFalse(receipt['inference_performed'])
        self.assertFalse(receipt['claims_or_completes_tasks'])
        self.assertEqual(receipt['worker_status'], 'offline')
        self.assertEqual(errors, '')
        self.assertNotIn('ready', output)

    def test_delivery_failure_surfaces_as_nonzero(self):
        result, output, errors = self.run_mode(error=worker.WorkerError('Hub request failed (HTTP 403)'))
        self.assertEqual(result, 1)
        self.assertEqual(output, '')
        self.assertIn('HTTP 403', errors)

    def test_missing_or_nonboolean_acknowledgement_is_not_success(self):
        for response in ({}, {'accepted': False}, {'accepted': 'true'}):
            with self.subTest(response=response):
                result, output, errors = self.run_mode(response=response)
                self.assertEqual(result, 1)
                self.assertEqual(output, '')
                self.assertIn('did not acknowledge', errors)

    def test_conflicting_modes_fail_before_startup(self):
        for flag in ('--once', '--dry-run'):
            with self.subTest(flag=flag), patch.object(worker, 'load_config') as config, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    worker.main(['--heartbeat-only', flag])
                self.assertEqual(raised.exception.code, 2)
                config.assert_not_called()


if __name__ == '__main__':
    unittest.main()
