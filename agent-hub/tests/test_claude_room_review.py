"""Offline only: real parser + in-memory native stream and fake hub responses."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_claude_eligibility_probe import Process, NOW, probe
from tests import test_claude_eligibility_probe as fixtures

sys.modules['eligibility_probe'] = probe
PATH = Path(__file__).resolve().parents[1] / 'deploy/claude-worker/room_review.py'
SPEC = importlib.util.spec_from_file_location('claude_room_review_test_module', PATH)
room = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(room)


class NativeReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProbeTests()
        self.fixture.setUp()

    def run_native(self, process=None, heartbeat=None):
        self.process = process or Process()
        self.process.result['result'] = 'A useful independent review of the supplied allocation policy.'
        with patch.object(probe, '_local_check'), patch.object(probe.subprocess, 'Popen', return_value=self.process) as spawn, \
                patch.object(probe, '_terminate', side_effect=lambda p: p.terminate()), \
                patch.object(probe.time, 'time', return_value=NOW):
            self.spawn = spawn
            return probe.run_review(self.fixture.operator, self.fixture.billing, self.fixture.enrollment,
                environment=self.fixture.environment, prompt='Review this real room, no tools.',
                heartbeat=heartbeat or (lambda: True), timeout_seconds=2)

    def test_actual_native_answer_separate_from_log_receipt(self):
        receipt, output = self.run_native()
        self.assertEqual(receipt['outcome'], 'verified_review')
        self.assertEqual(output, self.process.result['result'])
        self.assertEqual(receipt['output_sha256'], hashlib.sha256(output.encode()).hexdigest())
        self.assertNotIn(output, json.dumps(receipt))
        self.assertEqual(self.process.received[-1]['message']['content'], 'Review this real room, no tools.')
        self.assertEqual(self.spawn.call_count, 1)

    def test_lost_lease_before_prompt_never_sends_task(self):
        receipt, output = self.run_native(heartbeat=lambda: False)
        self.assertFalse(receipt['prompt_sent'])
        self.assertIsNone(output)
        self.spawn.assert_not_called()

    def test_unknown_trailing_frame_does_not_release_answer(self):
        receipt, output = self.run_native(Process(trailing_frames=lambda _: [{'type': 'secret-unknown'}]))
        self.assertEqual(receipt['outcome'], 'failed')
        self.assertIsNone(output)
        self.assertTrue(receipt['server_accepted'])
        self.assertNotIn('secret-unknown', json.dumps(receipt))

    def test_word_limit_is_enforced_without_truncating_or_retrying(self):
        process = Process()
        original = process.accept
        def accept(message):
            if message['type'] == 'user':
                process.result['result'] = 'word ' * 451
            original(message)
        process.accept = accept
        receipt, output = self.run_native(process)
        self.assertEqual(receipt['error_code'], 'bounded_one_turn_review_required')
        self.assertIsNone(output)
        self.assertEqual(self.spawn.call_count, 1)


class RoomReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProbeTests()
        self.fixture.setUp()
        self.prompt = 'Review the supplied 25 percent improvement and 75 percent project allowance policy.'
        self.auth = {'operator': self.fixture.operator, 'billing': self.fixture.billing,
            'enrollment': self.fixture.enrollment, 'room': {'schema_version': 1,
                'room_id': room.ROOM_ID, 'workspace': 'default', 'claim_mode': 'legacy_guarded_global',
                'prompt_sha256': hashlib.sha256(self.prompt.encode()).hexdigest(),
                'max_output_words': 450, 'max_output_bytes': 8000}}
        self.snapshot = {'id': room.ROOM_ID, 'status': 'queued', 'agents': ['claude'],
                         'rounds': 1, 'step': 0, 'workspace': 'default', 'messages': [], 'prompt': self.prompt}
        self.task = {'room_id': room.ROOM_ID, 'workspace': 'default', 'messages': [],
                     'step': 0, 'prompt': self.prompt, 'lease_token': 'private-lease-token',
                     'timeout_seconds': 180, 'deadline': NOW + 180}
        self.output = 'Useful provider review, delivered only after native completion.'
        self.native = {'outcome': 'verified_review', 'prompt_sent': True,
                       'output_sha256': hashlib.sha256(self.output.encode()).hexdigest()}
        self.client = Mock(config=SimpleNamespace(agent_id='claude', hub_url=room.HUB_URL,
                           cloud_run_auth_mode='metadata'))
        self.client.post.side_effect = self.post
        self.complete_failure = False

    def post(self, path, body):
        if path.endswith('/claim'):
            return {'task': self.task}
        if path.endswith('/heartbeat'):
            return {'active': True, 'deadline': self.task['deadline'], 'server_time': NOW}
        if path.endswith('/complete'):
            if self.complete_failure:
                raise OSError('private-transport-details')
            return {'room_id': room.ROOM_ID, 'status': 'completed' if body['exit_code'] == 0 else 'failed'}
        if path == '/v1/workers/report':
            return {'accepted': True}
        raise AssertionError('Unexpected hub route')

    def run_room(self):
        with patch.object(room, '_local_check'), patch.object(room, '_get_room', return_value=self.snapshot), \
                patch.object(room, 'run_review', return_value=(self.native, self.output)) as native, \
                patch.object(room.time, 'time', return_value=NOW):
            self.call_native = native
            return room.run_room_review(self.auth, environment=self.fixture.environment, client=self.client)

    def completions(self):
        return [call for call in self.client.post.call_args_list if call.args[0].endswith('/complete')]

    def test_guarded_legacy_claim_delivers_real_answer_once(self):
        report = self.run_room()
        self.assertEqual(report['outcome'], 'completed')
        self.assertEqual(report['claim_mode'], 'legacy_guarded_global')
        self.assertTrue(report['completion_acknowledged'])
        self.assertEqual(self.client.post.call_args_list[0].args, ('/v1/tasks/claim', {}))
        self.assertEqual(self.completions()[0].args[1]['output'], self.output)
        self.assertEqual(len(self.completions()), 1)
        self.call_native.assert_called_once()
        text = json.dumps(report)
        for secret in ('private-lease-token', self.prompt, self.output, self.fixture.environment['CLAUDE_CODE_OAUTH_TOKEN']):
            self.assertNotIn(secret, text)
        self.assertIn(self.prompt, self.call_native.call_args.kwargs['prompt'])
        self.assertNotIn('private-lease-token', self.call_native.call_args.kwargs['prompt'])

    def test_exact_mode_never_falls_back_to_global(self):
        self.auth['room']['claim_mode'] = 'exact_room'
        self.assertEqual(self.run_room()['outcome'], 'completed')
        paths = [c.args[0] for c in self.client.post.call_args_list]
        self.assertEqual(paths[0], '/v1/rooms/' + room.ROOM_ID + '/claim')
        self.assertNotIn('/v1/tasks/claim', paths)

    def test_mismatched_claim_stops_without_prompt_or_completion(self):
        self.task['room_id'] = 'f' * 32
        report = self.run_room()
        self.assertEqual(report['claimed_room_id'], 'f' * 32)
        self.assertEqual(report['error_code'], 'claimed_room_does_not_match_authorized_review')
        self.call_native.assert_not_called()
        self.assertEqual(self.completions(), [])

    def test_changed_claim_prompt_stops_before_provider(self):
        self.task['prompt'] += ' Unapproved replacement.'
        self.assertEqual(self.run_room()['outcome'], 'failed')
        self.call_native.assert_not_called()
        self.assertEqual(self.completions(), [])

    def test_room_precheck_failure_does_not_claim(self):
        self.snapshot['status'] = 'running'
        self.assertEqual(self.run_room()['error_code'], 'expected_room_not_queued_or_binding_changed')
        self.call_native.assert_not_called()
        self.assertFalse(any(c.args[0].endswith('/claim') for c in self.client.post.call_args_list))

    def test_replay_refuses_before_second_claim(self):
        self.run_room()
        previous = self.client.post.call_count
        with self.assertRaisesRegex(probe.ProbeError, 'already_consumed'):
            self.run_room()
        self.assertEqual(self.client.post.call_count, previous)

    def test_stale_billing_cannot_claim_or_infer(self):
        self.auth['billing']['observed_at'] = NOW - 3600
        self.auth['operator']['billing_sha256'] = probe.receipt_digest(self.auth['billing'])
        with self.assertRaisesRegex(probe.ProbeError, 'fresh_existing_credit'):
            self.run_room()
        self.client.post.assert_not_called()
        self.call_native.assert_not_called()

    def test_failed_native_does_not_submit_fabricated_contribution(self):
        self.native = {'outcome': 'failed', 'prompt_sent': True, 'server_accepted': None}
        self.output = None
        report = self.run_room()
        self.assertEqual(report['error_code'], 'native_review_not_verified')
        self.assertTrue(report['failure_acknowledged'])
        self.assertEqual(len(self.completions()), 1)
        self.assertEqual(self.completions()[0].args[1]['exit_code'], 1)
        self.assertIn('worker error, not a Claude answer', self.completions()[0].args[1]['output'])
        self.call_native.assert_called_once()

    def test_failed_native_error_delivery_does_not_retry_on_lost_ack(self):
        self.native = {'outcome': 'failed', 'prompt_sent': True}
        self.output = None
        self.complete_failure = True
        report = self.run_room()
        self.assertEqual(report['outcome'], 'failed')
        self.assertFalse(report['failure_acknowledged'])
        self.assertEqual(len(self.completions()), 1)
        self.call_native.assert_called_once()

    def test_completion_lost_ack_is_uncertain_and_never_replays_provider(self):
        self.complete_failure = True
        report = self.run_room()
        self.assertEqual(report['outcome'], 'completion_unknown')
        self.assertFalse(report['completion_acknowledged'])
        self.call_native.assert_called_once()
        self.assertEqual(len(self.completions()), 1)
        self.assertNotIn('private-transport', json.dumps(report))

    def test_short_or_changed_lease_blocks_launch(self):
        self.task['deadline'] = NOW + 5
        report = self.run_room()
        self.assertEqual(report['error_code'], 'review_lease_inactive_before_native_launch')
        self.call_native.assert_not_called()

    def test_bad_native_output_binding_cannot_complete(self):
        self.native['output_sha256'] = '0' * 64
        report = self.run_room()
        self.assertEqual(report['error_code'], 'native_review_output_binding_invalid')
        self.assertEqual(len(self.completions()), 1)
        self.assertEqual(self.completions()[0].args[1]['exit_code'], 1)
        self.assertNotIn(self.output, self.completions()[0].args[1]['output'])


class RoomEntrypointTests(unittest.TestCase):
    def setUp(self):
        path = PATH.parent / 'entrypoint.py'
        spec = importlib.util.spec_from_file_location('claude_room_entrypoint_test', path)
        self.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.entry)

    def test_explicit_mode_routes_only_to_room_runner(self):
        with patch.object(sys, 'argv', ['entrypoint.py', '--room-review']), \
                patch.object(self.entry, 'immutable_image_check', return_value={}), \
                patch.object(self.entry, 'room_review', return_value=0) as run, \
                patch.object(self.entry, 'eligibility_probe') as empty_probe, \
                patch('agent_hub.worker.main') as general_worker:
            self.assertEqual(self.entry.main(), 0)
        run.assert_called_once()
        empty_probe.assert_not_called()
        general_worker.assert_not_called()

    def test_mutually_exclusive_modes_cannot_fall_back_to_general_worker(self):
        import io
        with patch.object(sys, 'argv', ['entrypoint.py', '--room-review', '--heartbeat-only']), \
                patch.object(sys, 'stderr', io.StringIO()), \
                patch.object(self.entry, 'room_review') as run:
            with self.assertRaises(SystemExit) as caught:
                self.entry.main()
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
