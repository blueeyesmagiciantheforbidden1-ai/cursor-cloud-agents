"""Startup authorization tests with all storage, HTTP and provider I/O mocked."""
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from agent_hub import qwen, server
from agent_hub.core import AGENTS


class QwenStartupTests(unittest.TestCase):
    def setUp(self):
        self.tokens = {actor: actor+'x'*40 for actor in ('manager','status',*AGENTS)}
        self.base_env = {'HUB_TOKENS_JSON':json.dumps(self.tokens), 'PORT':'8080'}
        self.cloud_env = {**self.base_env, 'HUB_BACKEND':'firestore', 'K_SERVICE':'fixture-hub',
                          'GOOGLE_CLOUD_PROJECT':'fixture-project', 'HUB_FIRESTORE_DATABASE':'fixture-database'}
        self.enabled_env = {**self.cloud_env, 'HUB_QWEN_ENABLED':'1',
                            'QWEN_API_KEY':'sk-test-fixture-not-a-real-key'}

    @contextmanager
    def runtime(self, environment):
        # Substitute the ledger module, not merely its network transport: even
        # importing/constructing a real cloud client is unnecessary here.
        ledger_module = ModuleType('agent_hub.qwen_ledger')
        ledger_factory = Mock(name='FirestoreQwenLedger')
        ledger_module.FirestoreQwenLedger = ledger_factory
        fake_http = Mock(server_port=8080)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(server.os.environ, environment, clear=True))
            stack.enter_context(patch.dict(sys.modules, {'agent_hub.qwen_ledger':ledger_module}))
            firestore = stack.enter_context(patch.object(server, 'FirestoreStore'))
            sqlite = stack.enter_context(patch.object(server, 'SQLiteStore'))
            http = stack.enter_context(patch.object(server, 'ThreadingHTTPServer', return_value=fake_http))
            handler = stack.enter_context(patch.object(server, 'make_handler'))
            adapter = stack.enter_context(patch.object(qwen, 'QwenAdapter', wraps=qwen.QwenAdapter))
            provider = stack.enter_context(patch.object(qwen, '_http_post', side_effect=AssertionError('No provider calls allowed')))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            state = {'firestore':firestore, 'sqlite':sqlite, 'http':http, 'handler':handler,
                     'adapter':adapter, 'ledger':ledger_factory, 'provider':provider, 'output':output,
                     'server':fake_http}
            yield state
            provider.assert_not_called()

    def test_connection_is_disabled_without_exact_explicit_enable_flag(self):
        for flag in (None,'','0','true','yes','01'):
            environment = {**self.cloud_env, 'QWEN_API_KEY':'sk-test-fixture-not-a-real-key'}
            if flag is not None:
                environment['HUB_QWEN_ENABLED'] = flag
            with self.subTest(flag=flag), self.runtime(environment) as state:
                server.main()
                self.assertIsNone(state['handler'].call_args.kwargs['qwen'])
                state['adapter'].assert_not_called()
                state['ledger'].assert_not_called()
                state['server'].server_close.assert_called_once()

    def test_enabled_qwen_rejects_sqlite_before_starting_http(self):
        for cloud_service in (None,'fixture-hub'):
            environment = {**self.base_env, 'HUB_BACKEND':'sqlite', 'HUB_QWEN_ENABLED':'1',
                           'QWEN_API_KEY':'sk-test-fixture-not-a-real-key'}
            if cloud_service is not None:
                environment['K_SERVICE'] = cloud_service
            with self.subTest(cloud_service=cloud_service), self.runtime(environment) as state:
                with self.assertRaises(ValueError):
                    server.main()
                state['http'].assert_not_called()
                state['ledger'].assert_not_called()
                state['adapter'].assert_not_called()

    def test_firestore_without_cloud_run_does_not_authorize_qwen(self):
        for service_value in (None,''):
            environment = dict(self.enabled_env)
            if service_value is None:
                environment.pop('K_SERVICE')
            else:
                environment['K_SERVICE'] = service_value
            with self.subTest(service_value=service_value), self.runtime(environment) as state:
                with self.assertRaisesRegex(ValueError,'cloud Firestore budget ledger'):
                    server.main()
                state['http'].assert_not_called()
                state['ledger'].assert_not_called()
                state['adapter'].assert_not_called()

    def test_missing_or_wrong_key_fails_closed_before_starting_http(self):
        for key in (None,'','sk-sp-test-coding-plan','not-a-provider-key'):
            environment = dict(self.enabled_env)
            if key is None:
                environment.pop('QWEN_API_KEY')
            else:
                environment['QWEN_API_KEY'] = key
            with self.subTest(key_state='missing' if key is None else 'invalid'), self.runtime(environment) as state:
                with self.assertRaises(qwen.QwenError) as failure:
                    server.main()
                self.assertEqual(failure.exception.code,'pay_as_you_go_key_required')
                state['http'].assert_not_called()
                state['ledger'].return_value.reserve.assert_not_called()
                state['ledger'].return_value.reconcile.assert_not_called()

    def test_explicit_cloud_startup_connects_durable_callbacks_without_inference(self):
        with self.runtime(self.enabled_env) as state:
            server.main()
            state['firestore'].assert_called_once_with('fixture-project','agent_hub_rooms',database='fixture-database')
            state['sqlite'].assert_not_called()
            state['ledger'].assert_called_once_with(state['firestore'].return_value.client)
            state['adapter'].assert_called_once()
            arguments = state['adapter'].call_args.kwargs
            self.assertIs(arguments['reserve'],state['ledger'].return_value.reserve)
            self.assertIs(arguments['reconcile'],state['ledger'].return_value.reconcile)
            self.assertEqual(arguments['profile'].max_completion_tokens,128)
            self.assertEqual(arguments['profile'].per_call_limit_microusd,10000)
            self.assertEqual(arguments['profile'].daily_limit_microusd,100000)
            self.assertIsNotNone(state['handler'].call_args.kwargs['qwen'])
            state['ledger'].return_value.reserve.assert_not_called()
            state['ledger'].return_value.reconcile.assert_not_called()
            self.assertNotIn(self.enabled_env['QWEN_API_KEY'],state['output'].getvalue())
            state['server'].server_close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
