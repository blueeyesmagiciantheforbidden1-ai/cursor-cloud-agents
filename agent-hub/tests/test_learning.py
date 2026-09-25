import concurrent.futures
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from agent_hub.adapters import build_prompt, MAX_PROMPT_BYTES
from agent_hub.core import Hub, HubError
from agent_hub.learning import select, state_key, MAX_LESSONS
from agent_hub.mcp import handle_rpc
from agent_hub.store import SQLiteStore, FirestoreStore


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'hub.db'
        self.hub = Hub(SQLiteStore(self.path), lambda: 1000)
        self.text = 'Use an idempotency key when publishing a deployment.'

    def tearDown(self):
        self.assertTrue(self.path.resolve().is_relative_to(Path(self.temp.name).resolve()))
        self.temp.cleanup()

    def contribution(self, agent, text, workspace='project', code=0):
        room = self.hub.create('manager', dict(prompt='Review deployment idempotency', workspace=workspace, agents=[agent]))
        task = self.hub.claim(agent)['task']
        self.hub.complete(agent, room['id'], dict(lease_token=task['lease_token'], output=text, exit_code=code))
        return room

    def propose(self, text=None, workspace='project', code=0, kind='practice'):
        text = text or self.text
        room = self.contribution('codex', text, workspace, code)
        data = dict(operation='propose', room_id=room['id'], source_message=0, text=text, kind=kind)
        return self.hub.learn('manager', data), data

    def review(self, lesson, agent='claude', verdict='support', workspace='project', supplied=None):
        quote = json.dumps(dict(lesson_id=lesson['id'], verdict=verdict, reason='Checked the stated scope.'))
        room = self.contribution(agent, quote, workspace)
        data = dict(operation='review', room_id=room['id'], source_message=0, lesson_id=lesson['id'], verdict=supplied or verdict, quote=quote)
        return self.hub.learn('manager', data), data

    def test_reconnect_keeps_shared_memory_after_a_brief_revocation(self):
        lesson, _ = self.propose()
        before = self.hub.store.get_state(state_key('project'))['revision']
        attached = self.hub.learn('claude', dict(operation='reconnect', workspace='project'))
        self.assertTrue(attached['reconnected'])
        self.assertEqual(attached['lessons'], 1)
        self.assertEqual(attached['revision'], before)
        stored = self.hub.store.get_state(state_key('project'))
        self.assertEqual(stored['lessons'][lesson['id']]['text'], self.text)
        self.assertEqual(stored['revision'], before)
        empty = self.hub.learn('manager', dict(operation='reconnect', workspace='other-desk'))
        self.assertTrue(empty['reconnected'])
        self.assertEqual(empty['lessons'], 0)
        self.assertEqual(empty['revision'], 0)
        again = self.hub.learn('claude', dict(operation='reconnect', workspace='other-desk'))
        self.assertTrue(again['reconnected'])
        self.assertEqual(again['revision'], 0)
        self.assertEqual(again['lessons'], 0)
        with self.assertRaises(HubError):
            self.hub.learn('status', dict(operation='reconnect', workspace='project'))

    def test_lesson_survives_restart_and_is_shared_with_another_agent(self):
        lesson, _ = self.propose()
        self.review(lesson)
        self.hub = Hub(SQLiteStore(self.path), lambda: 1010)
        self.hub.create('manager', dict(prompt='Improve deployment idempotency', workspace='project', agents=['grok']))
        task = self.hub.claim('grok')['task']
        self.assertEqual(task['learning_context']['lessons'][0]['text'], self.text)
        self.assertFalse(task['learning_context']['tests_verified'])
        self.assertIn(self.text, build_prompt(task, 'grok'))
        self.hub.complete('grok', task['room_id'], dict(lease_token=task['lease_token'], output='Examined the deployment.', exit_code=0))
        receipt = self.hub.get('manager', task['room_id'])['messages'][0]['learning_context']
        self.assertEqual(receipt['lesson_ids'], [lesson['id']])

    def test_proposals_require_existing_exact_attributed_output(self):
        lesson, data = self.propose()
        with self.assertRaises(HubError):
            self.hub.learn('manager', dict(data, text='An invented recommendation'))
        with self.assertRaises(HubError):
            self.hub.learn('claude', data)
        with self.assertRaises(HubError):
            self.hub.learn('status', data)
        self.assertEqual(lesson['source']['agent'], 'codex')
        self.assertEqual(select(self.hub, 'project', 'deployment')['lessons'], [])

    def test_manager_cannot_relabel_an_agents_rejection_as_support(self):
        lesson, _ = self.propose()
        with self.assertRaisesRegex(HubError, 'verdict must match'):
            self.review(lesson, verdict='reject', supplied='support')
        self.assertEqual(select(self.hub, 'project', 'deployment')['lessons'], [])

    def test_cannot_extract_support_from_inside_a_quoted_example(self):
        lesson, _ = self.propose()
        quote = json.dumps(dict(lesson_id=lesson['id'], verdict='support', reason='example'))
        room = self.contribution('claude', 'Do not emit this example: ' + quote)
        with self.assertRaises(HubError):
            self.hub.learn('manager', dict(operation='review', room_id=room['id'], source_message=0, lesson_id=lesson['id'], verdict='support', quote=quote))

    def test_self_review_is_rejected(self):
        lesson, _ = self.propose()
        with self.assertRaisesRegex(HubError, 'different agent'):
            self.review(lesson, agent='codex')

    def test_cross_workspace_review_and_retrieval_are_isolated(self):
        lesson, _ = self.propose()
        with self.assertRaises(HubError):
            self.review(lesson, workspace='unrelated')
        self.review(lesson)
        self.assertEqual(select(self.hub, 'unrelated', 'deployment')['lessons'], [])
        self.assertEqual(select(self.hub, 'project', 'bananas oranges')['lessons'], [])
        with self.assertRaises(HubError):
            self.hub.lessons('cursor', 'project')

    def test_disagreement_stops_reuse_and_support_does_not_outvote_it(self):
        lesson, _ = self.propose()
        self.review(lesson)
        self.assertTrue(select(self.hub, 'project', 'deployment')['lessons'])
        self.review(lesson, 'cursor', 'reject')
        self.review(lesson, 'grok', 'support')
        self.assertEqual(select(self.hub, 'project', 'deployment')['lessons'], [])
        self.assertEqual(self.hub.lessons('manager', 'project')['lessons'][0]['status'], 'disputed')

    def test_replayed_proposal_and_review_do_not_change_revision(self):
        lesson, data = self.propose()
        reviewed, review = self.review(lesson)
        before = self.hub.lessons('manager', 'project')
        self.assertEqual(self.hub.learn('manager', data), reviewed)
        self.hub.learn('manager', review)
        self.assertEqual(self.hub.lessons('manager', 'project'), before)

    def test_concurrent_reviews_do_not_overwrite_each_other(self):
        lesson, _ = self.propose()
        commands = []
        for agent, verdict in [('claude', 'support'), ('cursor', 'reject')]:
            quote = json.dumps(dict(lesson_id=lesson['id'], verdict=verdict, reason='scope'))
            room = self.contribution(agent, quote)
            commands.append(dict(operation='review', room_id=room['id'], source_message=0, lesson_id=lesson['id'], verdict=verdict, quote=quote))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda data: self.hub.learn('manager', data), commands))
        item = self.hub.lessons('manager', 'project')['lessons'][0]
        self.assertEqual(item['status'], 'disputed')
        self.assertEqual(len(item['reviews']), 2)

    def test_failed_outputs_are_failure_notes_not_success_facts(self):
        with self.assertRaises(HubError):
            self.propose(code=1)
        lesson, _ = self.propose(code=1, kind='failure')
        self.assertEqual(lesson['source']['exit_code'], 1)

    def test_retirement_is_irreversible_and_retains_audit(self):
        lesson, _ = self.propose()
        self.review(lesson)
        operation = dict(operation='retire', workspace='project', lesson_id=lesson['id'])
        with self.assertRaises(HubError):
            self.hub.learn('claude', operation)
        self.hub.learn('manager', operation)
        self.assertEqual(select(self.hub, 'project', 'deployment')['lessons'], [])
        with self.assertRaises(HubError):
            self.review(lesson, 'grok')
        self.assertTrue(self.hub.lessons('manager', 'project')['lessons'][0]['reviews'])

    def test_known_secrets_are_rejected_without_persisting_them(self):
        with self.assertRaises(HubError):
            self.propose(text='Bearer ' + 'A' * 40)
        self.assertEqual(self.hub.store.get_state(state_key('project')), {})

    def test_storage_failure_rolls_back_and_bounded_memory_does_not_evict(self):
        self.hub.store.mutate_state(state_key('project'), lambda state: state.update(schema_version=1, revision=0, lessons={str(i): {'status': 'retired'} for i in range(MAX_LESSONS)}))
        before = self.hub.store.get_state(state_key('project'))
        with self.assertRaises(HubError):
            self.propose()
        self.assertEqual(self.hub.store.get_state(state_key('project')), before)

    def test_prompt_keeps_original_request_and_treats_memory_as_advisory(self):
        lesson, _ = self.propose()
        self.review(lesson)
        prompt = build_prompt(dict(prompt='X' * 8000, messages=[{'agent': 'claude', 'text': 'Z' * 16000}], learning_context=select(self.hub, 'project', 'deployment')), 'cursor')
        self.assertIn('X' * 8000, prompt)
        self.assertIn('advisory JSON', prompt)
        self.assertIn('not instructions', prompt)
        self.assertLessEqual(len(prompt.encode()), MAX_PROMPT_BYTES)

    def test_mcp_exposes_manager_tools_and_enforces_worker_permissions(self):
        def call(actor, name, arguments):
            return handle_rpc(self.hub, actor, dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(name=name, arguments=arguments)))
        result = call('manager', 'hub_lessons', {'workspace': 'project'})
        self.assertEqual(result['result']['structuredContent']['lessons'], [])
        self.assertTrue(call('codex', 'hub_lessons', {'workspace': 'project'})['result']['isError'])
        self.assertIn('error', call('manager', 'hub_learn', {'operation': 'invent'}))

    def test_firestore_transaction_retries_are_pure_and_noop_does_not_write(self):
        saved = {}
        writes = []
        ref = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(exists=bool(saved), to_dict=lambda: copy.deepcopy(saved)))
        transaction = SimpleNamespace(set=lambda ref, value: writes.append(copy.deepcopy(value)))
        store = FirestoreStore.__new__(FirestoreStore)
        store.durable_state = SimpleNamespace(document=lambda key: ref)
        store.client = SimpleNamespace(transaction=lambda: transaction)
        store.firestore = SimpleNamespace(transactional=lambda function: function)
        store.mutate_state('test', lambda state: state.update(revision=1))
        self.assertEqual(writes, [{'revision': 1}])
        saved.update(writes[0])
        store.mutate_state('test', lambda state: state.get('revision'))
        self.assertEqual(len(writes), 1)


if __name__ == '__main__':
    unittest.main()
