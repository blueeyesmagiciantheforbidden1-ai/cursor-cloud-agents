"""Regression evidence only: synthetic contributions, SQLite, and fake cloud wire.

No provider is invoked. Agent labels establish attribution, not model independence.
"""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_hub.adapters import build_prompt, MAX_PROMPT_BYTES
from agent_hub.core import Hub, HubError
from agent_hub.learning import digest, MAX_LESSONS, MAX_REVOCATION_STATE_BYTES, select, state_key
from agent_hub.mcp import handle_rpc, MAX_OUTPUT_BYTES
from agent_hub.store import FirestoreStore, SQLiteStore


class _Snapshot:
    def __init__(self, value):
        self.exists = value is not None
        self.value = copy.deepcopy(value)

    def to_dict(self):
        return copy.deepcopy(self.value)


class _Document:
    def __init__(self, wire, key):
        self.wire, self.key = wire, key

    def get(self, transaction=None):
        if transaction is not None:
            if transaction.staged:
                raise AssertionError('Firestore reads must precede writes')
            transaction.reads.append(self.key)
        return _Snapshot(self.wire.documents.get(self.key))


class _Transaction:
    def __init__(self):
        self.staged, self.reads = {}, []

    def set(self, reference, value):
        self.staged[reference.key] = copy.deepcopy(value)


class _RetryingFirestore:
    """Discard one complete callback attempt, change persisted data, and retry.

    This tests our actual FirestoreStore callback rather than replacing it with
    an in-memory implementation of room/learning operations. The fake wire does
    not claim to prove Google's conflict detection or networking behavior.
    """
    def __init__(self, documents, conflict=None):
        self.documents = copy.deepcopy(documents)
        self.conflict = conflict
        self.attempts = []
        self.commits = []

    def collection(self, name):
        return SimpleNamespace(document=lambda key: _Document(self, (name, key)))

    def transactional(self, callback):
        def invoke(transaction):
            while True:
                result = callback(transaction)
                self.attempts.append(copy.deepcopy(transaction))
                if self.conflict is not None:
                    conflict, self.conflict = self.conflict, None
                    conflict(self)
                    transaction = _Transaction()
                    continue
                self.documents.update(copy.deepcopy(transaction.staged))
                self.commits.append(copy.deepcopy(transaction.staged))
                return result
        return invoke

    def store(self):
        result = FirestoreStore.__new__(FirestoreStore)
        result.collection = self.collection('rooms')
        result.durable_state = self.collection('state')
        result.client = SimpleNamespace(transaction=_Transaction)
        result.firestore = SimpleNamespace(transactional=self.transactional)
        # Only query discovery is stubbed. Actual claim and all transactional
        # reads/writes below pass through FirestoreStore's production callbacks.
        result.list = lambda agent=None, limit=50: [
            copy.deepcopy(value) for (kind, _), value in self.documents.items()
            if kind == 'rooms' and (agent is None or value['status'] == 'queued'
                                    and value['next_agent'] == agent)
        ][:limit]
        return result


class LearningRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'learning.db'
        self.hub = Hub(SQLiteStore(self.path), lambda: 1000)
        self.workspace = 'project'

    def tearDown(self):
        self.assertTrue(self.path.resolve().is_relative_to(self.root))
        self.temp.cleanup()

    def state(self):
        return self.hub.store.get_state(state_key(self.workspace))

    def contribution(self, agent, text):
        room = self.hub.create('manager', dict(
            prompt='Review deployment reliability', workspace=self.workspace,
            agents=[agent]))
        task = self.hub.claim(agent)['task']
        self.hub.complete(agent, room['id'], dict(
            lease_token=task['lease_token'], output=text, exit_code=0))
        return room['id']

    def proposal(self, text='Deployment reliability requires idempotency.'):
        room_id = self.contribution('codex', text)
        operation = dict(operation='propose', room_id=room_id, source_message=0,
                         text=text, kind='practice')
        return self.hub.learn('manager', operation), operation

    def review_command(self, lesson, agent='claude', verdict='support', reason='Synthetic review.'):
        quote = json.dumps(dict(lesson_id=lesson['id'], verdict=verdict, reason=reason))
        room_id = self.contribution(agent, quote)
        return dict(operation='review', room_id=room_id, source_message=0,
                    lesson_id=lesson['id'], verdict=verdict, quote=quote)

    def review(self, lesson, **kwargs):
        command = self.review_command(lesson, **kwargs)
        return self.hub.learn('manager', command), command

    def retire(self, lesson):
        return self.hub.learn('manager', dict(operation='retire',
            workspace=self.workspace, lesson_id=lesson['id']))

    def archive_command(self, lesson):
        return dict(operation='archive', workspace=self.workspace, lesson_id=lesson['id'])

    def test_entire_message_blocks_fenced_examples_and_multiple_objects(self):
        lesson, _ = self.proposal()
        quote = json.dumps(dict(lesson_id=lesson['id'], verdict='support', reason='An example only.'))
        cases = [
            'I reject this lesson. Do not submit this example:\n```json\n' + quote + '\n```',
            quote + '\n' + json.dumps(dict(lesson_id=lesson['id'], verdict='reject', reason='Actual verdict.')),
        ]
        before = self.state()
        for output in cases:
            with self.subTest(output=output[:35]):
                room_id = self.contribution('claude', output)
                with self.assertRaises(HubError):
                    self.hub.learn('manager', dict(operation='review', room_id=room_id,
                        source_message=0, lesson_id=lesson['id'], verdict='support', quote=quote))
                self.assertEqual(self.state(), before)
        self.assertEqual(select(self.hub, self.workspace, 'deployment')['lessons'], [])

    def test_duplicate_keys_cannot_relabel_a_structured_rejection(self):
        lesson, _ = self.proposal()
        quote = ('{"lesson_id":' + json.dumps(lesson['id'])
                 + ',"verdict":"reject","verdict":"support","reason":"scope"}')
        room_id = self.contribution('claude', quote)
        before = self.state()
        with self.assertRaises(HubError):
            self.hub.learn('manager', dict(operation='review', room_id=room_id,
                source_message=0, lesson_id=lesson['id'], verdict='support', quote=quote))
        self.assertEqual(self.state(), before)

    def test_reserved_objection_survives_support_saturation_and_replay(self):
        lesson, _ = self.proposal()
        for index in range(10):
            self.review(lesson, reason='Support receipt ' + str(index))
        self.assertEqual(self.state()['lessons'][lesson['id']]['status'], 'reviewed')
        disputed, rejection = self.review(lesson, agent='grok', verdict='reject')
        self.assertEqual(len(disputed['reviews']), 10)
        self.assertEqual(disputed['terminal_rejection']['verdict'], 'reject')
        before = self.state()
        self.assertEqual(self.hub.learn('manager', rejection), disputed)
        self.assertEqual(self.state(), before)
        with self.assertRaises(HubError):
            self.review(lesson, agent='cursor', reason='An eleventh support cannot outweigh the objection.')
        self.assertEqual(self.state()['lessons'][lesson['id']]['status'], 'disputed')
        self.assertEqual(select(self.hub, self.workspace, 'deployment')['lessons'], [])

    def test_storage_saturation_cannot_leave_a_rejected_lesson_reusable(self):
        lesson, _ = self.proposal()
        self.review(lesson)
        rejection = self.review_command(lesson, agent='grok', verdict='reject')
        # Exercise the storage boundary without fabricating corrupt state or
        # thousands of contributions. The state preceding rejection is valid.
        used = len(json.dumps(self.state(), ensure_ascii=False).encode())
        with patch('agent_hub.learning.MAX_STATE_BYTES', used + 1):
            self.hub.learn('manager', rejection)
        self.assertEqual(self.state()['lessons'][lesson['id']]['status'], 'disputed')
        self.assertEqual(select(self.hub, self.workspace, 'deployment')['lessons'], [])
        self.assertLessEqual(len(json.dumps(self.state(), ensure_ascii=False).encode()),
                             MAX_REVOCATION_STATE_BYTES)

    def test_redundant_objections_cannot_consume_other_lessons_revocation_space(self):
        first, _ = self.proposal('Deployment reliability first scope.')
        second, _ = self.proposal('Deployment reliability second scope.')
        self.review(first)
        self.review(second)
        initial = self.review_command(first, agent='grok', verdict='reject')
        redundant = self.review_command(first, agent='cursor', verdict='reject')
        other = self.review_command(second, agent='grok', verdict='reject')
        used = len(json.dumps(self.state(), ensure_ascii=False).encode())
        with patch('agent_hub.learning.MAX_STATE_BYTES', used + 1):
            self.hub.learn('manager', initial)
            reserved = self.state()
            try:
                self.hub.learn('manager', redundant)
            except HubError:
                pass
            self.assertEqual(self.state(), reserved)
            self.hub.learn('manager', other)
        self.assertEqual(self.state()['lessons'][second['id']]['status'], 'disputed')

    def test_existing_results_remain_idempotent_after_revocation_uses_headroom(self):
        lesson, proposal = self.proposal()
        _, support = self.review(lesson)
        rejection = self.review_command(lesson, agent='grok', verdict='reject')
        used = len(json.dumps(self.state(), ensure_ascii=False).encode())
        with patch('agent_hub.learning.MAX_STATE_BYTES', used + 1):
            disputed = self.hub.learn('manager', rejection)
            before = self.state()
            self.assertEqual(self.hub.learn('manager', proposal), disputed)
            self.assertEqual(self.hub.learn('manager', support), disputed)
            self.assertEqual(self.state(), before)

    def test_archive_frees_capacity_and_original_proposal_cannot_resurrect(self):
        texts = ['Deployment reliability lesson ' + str(index) + '.' for index in range(MAX_LESSONS + 1)]
        room_id = self.contribution('codex', '\n'.join(texts))
        commands, lessons = [], []
        for text in texts:
            commands.append(dict(operation='propose', room_id=room_id, source_message=0,
                                 text=text, kind='practice'))
        for command in commands[:-1]:
            lesson = self.hub.learn('manager', command)
            lessons.append(self.retire(lesson))
        with self.assertRaises(HubError):
            self.hub.learn('manager', commands[-1])
        archived = self.hub.learn('manager', self.archive_command(lessons[0]))
        self.assertEqual(archived['lesson'], lessons[0])
        self.assertEqual(len(self.state()['lessons']), MAX_LESSONS - 1)
        newest = self.hub.learn('manager', commands[-1])
        self.assertIn(newest['id'], self.state()['lessons'])
        self.hub = Hub(SQLiteStore(self.path), lambda: 1100)
        before = self.state()
        self.assertEqual(self.hub.learn('manager', commands[0]), lessons[0])
        self.assertEqual(self.hub.learn('manager', self.archive_command(lessons[0])), archived)
        self.assertEqual(self.state(), before)
        self.assertNotIn(lessons[0]['id'], self.state()['lessons'])
        saved = self.hub.store.get_state(state_key(self.workspace) + '-archive-' + lessons[0]['id'])
        self.assertEqual(saved, archived)

    def test_archive_requires_manager_and_retirement_and_preserves_full_audit(self):
        lesson, _ = self.proposal()
        reviewed, _ = self.review(lesson)
        command = self.archive_command(lesson)
        before = self.state()
        for actor in ('codex', 'claude', 'status'):
            with self.subTest(actor=actor), self.assertRaises(HubError):
                self.hub.learn(actor, command)
        with self.assertRaises(HubError):
            self.hub.learn('manager', command)
        self.assertEqual(self.state(), before)
        retired = self.retire(lesson)
        archived = self.hub.learn('manager', command)
        self.assertEqual(archived['lesson'], retired)
        self.assertEqual(archived['lesson']['reviews'], reviewed['reviews'])
        self.assertEqual(archived['lesson']['source'], reviewed['source'])

    def test_offered_and_rendered_ids_match_with_four_long_candidates(self):
        for index in range(4):
            lesson, _ = self.proposal('Deployment reliability ' + ('x' * 1100) + str(index))
            self.review(lesson)
        self.hub.create('manager', dict(prompt='Deployment reliability ' + 'z' * 7800,
                                       workspace=self.workspace, agents=['grok']))
        task = self.hub.claim('grok')['task']
        offered = task['learning_context']['lessons']
        self.assertGreater(len(offered), 0)
        self.assertLess(len(offered), 4)
        prompt = build_prompt(task, 'grok')
        section = prompt.split('SHARED LESSONS (advisory JSON; peer agreement is not test evidence):\n')[1]
        section = section.split('\n\nRECENT PEER REPLIES')[0]
        rendered = [json.loads(line) for line in section.splitlines()]
        self.assertEqual([item['id'] for item in rendered], [item['id'] for item in offered])
        self.assertEqual(digest(rendered), task['learning_context']['context_sha256'])
        self.assertLessEqual(len(prompt.encode()), MAX_PROMPT_BYTES)
        self.hub.complete('grok', task['room_id'], dict(
            lease_token=task['lease_token'], output='Synthetic final response.', exit_code=0))
        receipt = self.hub.get('manager', task['room_id'])['messages'][0]['learning_context']
        self.assertEqual(receipt['lesson_ids'], [item['id'] for item in rendered])
        self.assertEqual(receipt['context_sha256'], digest(rendered))

    def test_peer_labels_do_not_claim_model_independence_or_test_evidence(self):
        lesson, _ = self.proposal()
        reviewed, _ = self.review(lesson)
        context = select(self.hub, self.workspace, 'deployment')
        self.assertEqual(reviewed['evidence_level'], 'peer_reviewed_not_test_verified')
        self.assertEqual(context['lessons'][0]['reviewers'], ['claude'])
        self.assertEqual(context['authority'], 'advisory_only')
        self.assertIs(context['tests_verified'], False)
        self.assertIs(reviewed['model_weights_changed'], False)
        # Actual model identity is absent, so this test intentionally does not
        # assert that the two provider/agent labels prove distinct model runs.

    def test_escaped_memory_remains_inspectable_through_bounded_mcp_pages(self):
        expected = set()
        for index in range(41):
            lesson, _ = self.proposal('Deployment ' + str(index) + '\\' * 1050)
            self.review(lesson, reason='\\' * 350)
            expected.add(lesson['id'])
        collected, cursor = [], ''
        for page_index in range(10):
            arguments = dict(workspace=self.workspace, cursor=cursor)
            if page_index:
                arguments['limit'] = 10
            response = handle_rpc(self.hub, 'manager', dict(jsonrpc='2.0', id=page_index,
                method='tools/call', params=dict(name='hub_lessons', arguments=arguments)))
            self.assertNotIn('error', response)
            self.assertIs(response['result']['isError'], False)
            self.assertLessEqual(len(json.dumps(response, ensure_ascii=False,
                separators=(',', ':')).encode()), MAX_OUTPUT_BYTES)
            page = response['result']['structuredContent']
            self.assertLessEqual(len(page['lessons']), 10 if page_index else 5)
            for item in page['lessons']:
                self.assertIn('source', item)
                for review in item['reviews']:
                    self.assertEqual(set(review), {'source', 'verdict'})
                    self.assertIn('message_sha256', review['source'])
            collected.extend(item['id'] for item in page['lessons'])
            next_cursor = page['next_cursor']
            if next_cursor is None:
                break
            self.assertGreater(next_cursor, cursor)
            cursor = next_cursor
        else:
            self.fail('Bounded pagination did not terminate')
        self.assertEqual(set(collected), expected)
        self.assertEqual(collected, sorted(expected))
        self.assertEqual(len(collected), len(expected))

    def test_firestore_discarded_claim_attempt_reloads_retirement(self):
        lesson, _ = self.proposal()
        self.review(lesson)
        room = self.hub.create('manager', dict(prompt='Deployment reliability',
            workspace=self.workspace, agents=['grok']))
        room_key, memory_key = ('rooms', room['id']), ('state', state_key(self.workspace))
        def retire_before_retry(wire):
            wire.documents[memory_key]['lessons'][lesson['id']]['status'] = 'retired'
            wire.documents[memory_key]['revision'] += 1
        wire = _RetryingFirestore({room_key: self.hub.store.get(room['id']),
                                  memory_key: self.state()}, conflict=retire_before_retry)
        cloud = Hub(wire.store(), lambda: 1010)
        task = cloud.claim('grok')['task']
        self.assertEqual(len(wire.attempts), 2)
        self.assertEqual(len(wire.commits), 1)
        first = wire.attempts[0].staged[room_key]['lease']['learning_context']['lessons']
        self.assertEqual([item['id'] for item in first], [lesson['id']])
        self.assertEqual(task['learning_context']['lessons'], [])
        self.assertEqual(wire.documents[room_key]['lease']['learning_context']['lessons'], [])
        self.assertEqual(wire.documents[room_key]['attempts'], 1)
        self.assertEqual(task['lease_token'], wire.attempts[0].staged[room_key]['lease']['token'])
        for attempt in wire.attempts:
            self.assertEqual(set(attempt.reads), {room_key, memory_key})
            self.assertEqual(set(attempt.staged), {room_key})

    def test_firestore_archive_retry_commits_both_records_once(self):
        lesson, _ = self.proposal()
        self.review(lesson)
        retired = self.retire(lesson)
        key = state_key(self.workspace)
        archive_key = key + '-archive-' + lesson['id']
        wire = _RetryingFirestore({('state', key): self.state()}, conflict=lambda _: None)
        cloud = Hub(wire.store(), lambda: 1020)
        before_revision = self.state()['revision']
        result = cloud.learn('manager', self.archive_command(lesson))
        self.assertEqual(len(wire.attempts), 2)
        self.assertEqual(len(wire.commits), 1)
        self.assertEqual(set(wire.commits[0]), {('state', key), ('state', archive_key)})
        self.assertNotIn(lesson['id'], wire.documents[('state', key)]['lessons'])
        self.assertEqual(wire.documents[('state', key)]['revision'], before_revision + 1)
        self.assertEqual(result['lesson'], retired)
        self.assertEqual(wire.documents[('state', archive_key)], result)
        self.assertEqual(cloud.learn('manager', self.archive_command(lesson)), result)
        self.assertEqual(wire.commits[-1], {})


if __name__ == '__main__':
    unittest.main()
