import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_hub.adapters import Command, build_command, extract_output_state
from agent_hub.core import AGENTS, DEFAULT_AGENTS, Hub, HubError
from agent_hub.server import load_tokens
from agent_hub.store import SQLiteStore
from agent_hub.worker import run_command
from agent_hub.mcp import handle_rpc


class FiveAgentTests(unittest.TestCase):
    def test_mcp_exposes_and_hands_work_to_all_five(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(SQLiteStore(Path(directory) / 'test.sqlite3'))
            def tool(actor, name, arguments):
                response = handle_rpc(hub, actor, {'jsonrpc': '2.0', 'id': 1,
                    'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
                return response['result']['structuredContent']
            room = tool('manager', 'hub_start', {'prompt': 'Offline five-agent fixture', 'agents': list(AGENTS)})
            for agent in AGENTS:
                task = tool(agent, 'hub_claim', {})['task']
                tool(agent, 'hub_complete', {'room_id': room['id'], 'lease_token': task['lease_token'],
                                           'output': 'synthetic '+agent, 'exit_code': 0})
            final = tool('manager', 'hub_get', {'room_id': room['id']})
            self.assertEqual(final['status'], 'completed')
            self.assertEqual([item['agent'] for item in final['messages']], list(AGENTS))

    def test_explicit_five_agent_room_completes_actual_handoffs_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            hub = Hub(SQLiteStore(Path(directory) / 'test.sqlite3'))
            room = hub.create('manager', {'prompt': 'Review a test fixture', 'agents': list(AGENTS)})
            for index, agent in enumerate(AGENTS):
                task = hub.claim(agent)['task']
                self.assertEqual(task['step'], index)
                self.assertEqual([item['agent'] for item in task['messages']], list(AGENTS[:index]))
                hub.complete(agent, room['id'], {'lease_token': task['lease_token'],
                             'output': 'synthetic offline '+agent+' contribution', 'exit_code': 0})
            self.assertEqual(hub.get('manager', room['id'])['status'], 'completed')
            legacy = hub.create('manager', {'prompt': 'Legacy request'})
            self.assertEqual(legacy['agents'], list(DEFAULT_AGENTS))
            with self.assertRaises(HubError):
                hub.create('grok', {'prompt': 'Cannot become manager'})

    def test_saved_four_agent_tokens_still_work_and_grok_is_separate(self):
        tokens = {actor: actor+'x'*40 for actor in ('manager', *DEFAULT_AGENTS)}
        self.assertEqual(load_tokens(json.dumps(tokens)), tokens)
        expanded = {**tokens, 'grok': 'grok'+'x'*40, 'status': 'status'+'x'*40}
        self.assertEqual(load_tokens(json.dumps(expanded)), expanded)
        for bad in ({**expanded, 'grok': tokens['manager']}, {**expanded, 'unknown': 'y'*40},
                    {key: value for key, value in expanded.items() if key != 'claude'}):
            with self.assertRaises(ValueError):
                load_tokens(json.dumps(bad))


class GrokAdapterTests(unittest.TestCase):
    def test_restricted_command_keeps_prompt_out_of_argv_and_preview(self):
        prompt = 'Private task fixture with shell syntax $(ignored) & `ignored`'
        with patch('agent_hub.adapters.resolve_executable', return_value=('grok.exe',)):
            command = build_command('grok', prompt)
        self.assertEqual(command.prompt_file, prompt)
        self.assertNotIn(prompt, str(command.argv))
        self.assertNotIn(prompt, str(command.preview()))
        for flag in ('--prompt-file', '--verbatim', '--disable-web-search', '--no-subagents'):
            self.assertIn(flag, command.argv)
        self.assertEqual(command.argv[command.argv.index('--tools')+1], '')
        self.assertEqual(command.argv[command.argv.index('--max-turns')+1], '1')
        self.assertEqual(command.argv[command.argv.index('--permission-mode')+1], 'plan')

    def test_native_stream_requires_terminal_success_and_unclipped_text(self):
        chunks = '\n'.join(json.dumps(value) for value in (
            {'type': 'text', 'data': 'first '}, {'type': 'text', 'data': 'second'}))
        final = json.dumps({'type': 'end', 'stopReason': 'end_turn'})
        good = chunks+'\n'+final
        self.assertEqual(extract_output_state('grok', good), ('first second', True))
        for raw in (chunks, good+'\n'+final, '[Earlier output truncated]\n'+good,
                    good+'\n'+json.dumps({'type': 'text', 'data': 'late'}),
                    chunks+'\n'+json.dumps({'type': 'end', 'stopReason': 'cancelled'}),
                    chunks+'\n'+json.dumps({'type': 'error', 'message': 'test'})+'\n'+final,
                    '{"result":"unrecognized format"}', final):
            with self.subTest(raw=raw):
                self.assertFalse(extract_output_state('grok', raw)[1])

    def test_prompt_file_exists_only_during_bounded_call_even_on_exception(self):
        paths = []
        command = Command(('grok.exe', '--prompt-file', 'placeholder'),
                          prompt_file='private synthetic fixture', prompt_file_argument=2)
        def inspect(actual, workspace, timeout, heartbeat, **kwargs):
            path = Path(actual.argv[2])
            paths.append(path)
            self.assertEqual(path.read_text(encoding='utf-8'), command.prompt_file)
            self.assertIsNone(actual.prompt_file)
            self.assertLessEqual(timeout, 10)
            self.assertEqual(kwargs['private_env'], ('HUB_GROK_TOKEN',))
            return (0, 'synthetic')
        with patch('agent_hub.worker._run_command', side_effect=inspect):
            self.assertEqual(run_command(command, Path.cwd(), 10, lambda: True,
                                        private_env=('HUB_GROK_TOKEN',)), (0, 'synthetic'))
        self.assertFalse(paths[-1].exists())
        def fail(*args, **kwargs):
            inspect(*args, **kwargs)
            raise OSError('synthetic launch failure')
        with patch('agent_hub.worker._run_command', side_effect=fail), self.assertRaises(OSError):
            run_command(command, Path.cwd(), 10, lambda: True, private_env=('HUB_GROK_TOKEN',))
        self.assertFalse(paths[-1].exists())


if __name__ == '__main__':
    unittest.main()
