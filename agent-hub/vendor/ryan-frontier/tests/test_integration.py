import hashlib
import json
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from ryan_frontier.artifacts import ArtifactStore
from ryan_frontier.cli import _confirmation_gate, execute
from ryan_frontier.ledger import Ledger
from ryan_frontier.promotion import PromotionRegistry
from ryan_frontier.research import CONDITIONS, run_pilot

class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.small_report = run_pilot(units=1, budget=2)

    def test_complete_offline_run_has_evidence_and_no_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            result=execute(directory,units=2,budget=6)
            self.assertEqual(result['ledger']['tasks'],{'completed':1})
            self.assertEqual(result['ledger']['spent'],0)
            self.assertEqual(result['ledger']['reserved'],0)
            self.assertEqual(result['promotion_status'],'inconclusive')
            self.assertIsNone(result['active_release'])
            self.assertEqual(result['model_calls'],0)
            report=Path(directory)/'report.json'
            self.assertEqual(hashlib.sha256(report.read_bytes()).hexdigest(),result['report_sha256'])
            self.assertTrue(list((Path(directory)/'generated').glob('method_*.py')))
            self.assertTrue(list((Path(directory)/'generated').glob('candidate_*.py')))
            stored=json.loads(report.read_text())
            self.assertFalse(stored['confirmation']['promoted'])

            objects = ArtifactStore(Path(directory) / 'objects')
            receipt = json.loads(objects.get(result['receipt_sha256']))
            self.assertEqual(receipt['report_sha256'], result['report_sha256'])
            self.assertEqual(receipt['task_id'], result['task_id'])
            self.assertEqual(json.loads((Path(result['run_directory']) / 'receipt.json').read_bytes()), receipt)
            pipeline = json.loads(objects.get(receipt['pipeline_sha256']))
            self.assertEqual(pipeline['parameters'], {'seed': result['seed'], 'units': 2, 'budget': 6})
            self.assertTrue({'cli.py', 'research.py', 'domain.py', 'promotion.py'} <= set(pipeline['sources']))
            for name, digest in pipeline['sources'].items():
                self.assertTrue(objects.verify(digest), name)
            evaluator = json.loads(objects.get(result['evaluator_sha256']))
            self.assertEqual(evaluator['pipeline_sha256'], receipt['pipeline_sha256'])
            self.assertEqual(evaluator['design'], stored['design'])
            candidate = json.loads(objects.get(receipt['candidate_sha256']))
            self.assertEqual(candidate['frozen_methods_sha256'], result['frozen_methods_sha256'])
            for artifact in candidate['executable_artifacts'].values():
                self.assertTrue(objects.verify(artifact['source_sha256']))
                self.assertTrue(objects.verify(artifact['spec_sha256']))
            ledger = Ledger(Path(directory) / 'controller.db', budget_limit=0)
            self.assertEqual(ledger.get_task(result['task_id'])['result']['receipt_sha256'], result['receipt_sha256'])
            registry = PromotionRegistry(Path(directory) / 'releases.db')
            try:
                proposal = registry.proposal(result['proposal_id'])
                self.assertEqual(proposal['candidate'], result['candidate_sha256'])
                self.assertEqual(len(proposal['checks']), len(CONDITIONS) * 2)
            finally:
                registry.close()

    def test_repeated_exports_preserve_prior_report_and_receipt_identity(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('ryan_frontier.cli.run_pilot', side_effect=lambda **_: deepcopy(self.small_report)):
            first = execute(directory, units=1, budget=2)
            first_report = (Path(first['run_directory']) / 'report.json').read_bytes()
            first_receipt = (Path(first['run_directory']) / 'receipt.json').read_bytes()
            second = execute(directory, units=1, budget=2)
            self.assertNotEqual(first['run_id'], second['run_id'])
            self.assertEqual(first['report_sha256'], second['report_sha256'])
            self.assertEqual((Path(first['run_directory']) / 'report.json').read_bytes(), first_report)
            self.assertEqual((Path(first['run_directory']) / 'receipt.json').read_bytes(), first_receipt)
            self.assertEqual(json.loads((Path(directory) / 'summary.json').read_bytes())['run_id'], second['run_id'])
            self.assertEqual(second['ledger']['tasks'], {'completed': 2})

    def test_unknown_remote_cost_is_not_inferred_from_offline_failure_cleanup(self):
        # This CLI deliberately has no provider adapter: a failed local function
        # has a *known* zero API charge. The invocation is resolved, not uncertain.
        with tempfile.TemporaryDirectory() as directory, \
                patch('ryan_frontier.cli.run_pilot', side_effect=RuntimeError('local evaluator failed')):
            with self.assertRaisesRegex(RuntimeError, 'local evaluator failed'):
                execute(directory, units=1, budget=2)
            ledger = Ledger(Path(directory) / 'controller.db', budget_limit=0)
            self.assertEqual(ledger.summary()['tasks'], {'completed': 1})
            self.assertEqual(ledger.summary()['spent'], 0)
            self.assertEqual(ledger.summary()['uncertain'], 0)
            with sqlite3.connect(Path(directory) / 'controller.db') as connection:
                task_id = connection.execute('SELECT task_id FROM tasks').fetchone()[0]
                self.assertEqual(connection.execute('SELECT status,actual_amount FROM invocations').fetchone(), ('settled', 0))
            failure = ledger.get_task(task_id)['result']
            self.assertEqual(failure['outcome'], 'failed')
            self.assertEqual(failure['error_type'], 'RuntimeError')
            self.assertEqual(failure['message'], 'local evaluator failed')
            self.assertFalse((Path(directory) / 'summary.json').exists())

    def test_cleanup_failure_preserves_original_error(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('ryan_frontier.cli.run_pilot', side_effect=RuntimeError('original research failure')), \
                patch.object(Ledger, 'settle', side_effect=OSError('disk failure during cleanup')):
            with self.assertRaisesRegex(RuntimeError, 'original research failure') as caught:
                execute(directory, units=1, budget=2)
            self.assertTrue(any('disk failure during cleanup' in note for note in caught.exception.__notes__))

    def test_huge_or_malformed_parameters_fail_before_any_output_is_created(self):
        cases = ({'units': 257}, {'units': 10 ** 100}, {'units': True},
                 {'budget': 0}, {'budget': 33}, {'seed': 1 << 63}, {'seed': '1'})
        with tempfile.TemporaryDirectory() as directory:
            for index, parameters in enumerate(cases):
                output = Path(directory) / f'invalid-{index}'
                with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                    execute(output, **parameters)
                self.assertFalse(output.exists())

    def test_gate_rechecks_memory_nonregression_and_preserves_actual_bound(self):
        report = deepcopy(self.small_report)
        for row in report['confirmation']['intervals'].values():
            row['method_without_memory']['lower'] = 0.25
            row['method_with_memory']['lower'] = 0.1
        first = next(iter(report['confirmation']['intervals'].values()))
        first['method_with_memory']['lower'] = -0.01
        report['confirmation']['decision'] = 'inconclusive'
        lower, margin, checks = _confirmation_gate(report)
        self.assertEqual(lower, 0.25)
        self.assertFalse(all(checks.values()))
        with tempfile.TemporaryDirectory() as directory:
            registry = PromotionRegistry(Path(directory) / 'registry.db')
            try:
                proposal = registry.stage('a' * 64, 'b' * 64, 'c' * 64, lower, margin, checks=checks)
                self.assertEqual(registry.proposal(proposal)['lower_bound'], 0.25)
                self.assertEqual(registry.proposal(proposal)['status'], 'inconclusive')
            finally:
                registry.close()
        report['confirmation']['decision'] = 'evidence_passed_manual_review_required'
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            _confirmation_gate(report)
        first['method_with_memory']['lower'] = 0
        self.assertTrue(all(_confirmation_gate(report)[2].values()))
        report['confirmation']['intervals'].pop(next(iter(CONDITIONS)))
        with self.assertRaisesRegex(ValueError, 'every predefined condition'):
            _confirmation_gate(report)

    def test_modified_export_cannot_be_recorded_as_the_frozen_executable(self):
        report = deepcopy(self.small_report)
        artifact = next(iter(report['artifacts'].values()))
        artifact['sha256'] = '0' * 64
        with tempfile.TemporaryDirectory() as directory, \
                patch('ryan_frontier.cli.run_pilot', return_value=report):
            with self.assertRaisesRegex(ValueError, 'source identity'):
                execute(directory, units=1, budget=2)
            ledger = Ledger(Path(directory) / 'controller.db', budget_limit=0)
            self.assertEqual(ledger.summary()['tasks'], {'completed': 1})
            registry = PromotionRegistry(Path(directory) / 'releases.db')
            try:
                self.assertEqual(registry.db.execute('SELECT COUNT(*) FROM proposals').fetchone()[0], 0)
            finally:
                registry.close()
