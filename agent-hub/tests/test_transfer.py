"""Synthetic frozen transfer studies, without models or cloud access."""
from contextlib import closing
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_hub.transfer import (ECONOMIC_COSTS, METHOD_FIELDS, RESEARCH_COSTS, TransferArchive,
                                TransferError, digest, economic_break_even, required_units, simultaneous_radius)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='runcrew-synthetic-transfer-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'transfer.sqlite3'
        self.archive = TransferArchive(self.path)
        self.methods = [self.archive.freeze_method({key: digest([key, index]) for key in METHOD_FIELDS})['id'] for index in range(3)]

    def spec(self, *, m=2, k=1, a=1, prefix='synthetic'):
        return {'name': prefix, 'incumbent_id': self.methods[0], 'candidate_ids': self.methods[1:k+1],
                'delta': 0.05, 'sample_size': m,
                'conditions': [{'id': f'condition-{condition}', 'task_family_digest': digest(['family', condition]),
                    'research_horizon': 10, 'research_budget_per_arm': 10, 'execution_budget_per_arm': 5,
                    'resource_unit': 'synthetic_compute_units', 'eta': 0.1, 'evaluator_digest': digest('evaluator'),
                    'common_library_digest': digest('L0'), 'routing_policy_digest': digest('router'),
                    'units': [{'id': f'{prefix}-{condition}-{unit}', 'start_controller_digest': digest(['start', prefix, condition, unit]),
                               'task_batch_digest': digest(['batch', prefix, condition, unit]),
                               'randomness_digest': digest(['rng', prefix, condition, unit]),
                               'holdout_ids': [f'{prefix}-holdout-{condition}-{unit}']} for unit in range(m)]}
                    for condition in range(a)]}

    def result(self, trial, method, condition, unit, *, quality=1, **changes):
        costs = dict.fromkeys(RESEARCH_COSTS, 0)
        costs['search_execution'] = 10
        payload = {'quality': quality, 'descendant_digest': digest(['descendant', method, unit['id']]),
                   'research_costs': costs, 'research_steps': 2, 'execution_cost': 3,
                   'evaluator_digest': condition['evaluator_digest'], 'routing_policy_digest': condition['routing_policy_digest'],
                   'common_library_digest': condition['common_library_digest'], 'research_disabled': True,
                   'private_state_transferred': False, 'contracts_pass': True, **changes}
        return self.archive.record_result(trial['id'], method, condition['id'], unit['id'], **payload)

    def complete(self, spec, trial, qualities=None):
        for condition in spec['conditions']:
            for unit in condition['units']:
                for index, method in enumerate([spec['incumbent_id'], *spec['candidate_ids']]):
                    quality = (qualities or {}).get((condition['id'], method), 0 if index == 0 else 1)
                    self.result(trial, method, condition, unit, quality=quality)

    def test_exact_simultaneous_two_sided_formula_and_sample_size(self):
        self.assertAlmostEqual(simultaneous_radius(4, 3, 0.05, 4940), math.sqrt(2 * math.log(480) / 4940))
        self.assertEqual(required_units(4, 3, 0.05, 0.05), 4940)
        for arguments in ((True, 1, 0.05, 1), (1, 0, 0.05, 2), (1, 1, float('nan'), 2)):
            with self.assertRaises(TransferError):
                simultaneous_radius(*arguments)

    def test_frozen_methods_content_hash_and_no_candidate_mutation(self):
        snapshot = {key: digest(key) for key in METHOD_FIELDS}
        frozen = self.archive.freeze_method(snapshot)
        self.assertEqual(frozen, self.archive.freeze_method(snapshot))
        snapshot['parameters_digest'] = digest('learned-new-parameters')
        self.assertNotEqual(frozen['id'], self.archive.freeze_method(snapshot)['id'])
        snapshot['private_history'] = 'not transferable'
        with self.assertRaises(TransferError):
            self.archive.freeze_method(snapshot)
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE transfer_records SET data='{}' WHERE kind='method'")

    def test_no_partial_peeking_even_when_one_candidate_finished(self):
        spec = self.spec(k=2)
        trial = self.archive.preregister(spec)
        for unit in spec['conditions'][0]['units']:
            for method in self.methods[:2]:
                result = self.result(trial, method, spec['conditions'][0], unit)
                self.assertNotIn('epsilon', result)
                self.assertNotIn('quality', result)
        self.assertEqual(self.archive.progress(trial['id']), {'completed_cells': 4, 'required_cells': 6})
        with self.assertRaises(TransferError):
            self.archive.finalize(trial['id'])
        for unit in spec['conditions'][0]['units']:
            self.result(trial, self.methods[2], spec['conditions'][0], unit)
        self.assertFalse(any(row['eligible'] for row in self.archive.finalize(trial['id'])['methods']))

    def test_promotion_requires_every_condition_and_is_record_only(self):
        spec = self.spec(m=64, k=2, a=2)
        trial = self.archive.preregister(spec)
        self.complete(spec, trial, {('condition-1', self.methods[2]): 0})
        decision = self.archive.finalize(trial['id'])
        self.assertTrue(decision['methods'][0]['eligible'])
        self.assertFalse(decision['methods'][1]['eligible'])
        self.assertTrue(decision['record_only'])
        self.assertEqual(decision['actual_full_research_cost'], 3840)
        self.assertEqual(decision['declared_full_research_budget'], 3840)
        restarted = TransferArchive(self.path)
        self.assertEqual(restarted.finalize(trial['id']), decision)

    def test_reused_units_holdouts_fingerprints_and_name_are_rejected_atomically(self):
        first = self.spec()
        self.archive.preregister(first)
        for change in ('unit', 'holdout', 'fingerprint', 'name'):
            spec = self.spec(prefix='fresh-' + change)
            unit = spec['conditions'][0]['units'][0]
            old = first['conditions'][0]['units'][0]
            if change == 'unit':
                unit['id'] = old['id']
            elif change == 'holdout':
                unit['holdout_ids'] = old['holdout_ids']
            elif change == 'fingerprint':
                for key in ('start_controller_digest', 'task_batch_digest', 'randomness_digest'):
                    unit[key] = old[key]
            else:
                spec['name'] = first['name']
            with self.subTest(change=change), self.assertRaises(TransferError):
                self.archive.preregister(spec)
        self.archive.preregister(self.spec(prefix='fresh-unit'))

    def test_frozen_evaluator_routing_library_horizon_and_budget_enforced(self):
        spec = self.spec()
        trial = self.archive.preregister(spec)
        costs = dict.fromkeys(RESEARCH_COSTS, 0)
        costs['retries'] = 11
        for changes in ({'evaluator_digest': digest('other')}, {'routing_policy_digest': digest('other')},
                        {'common_library_digest': digest('L1')}, {'research_costs': costs},
                        {'execution_cost': 6}, {'research_steps': 11}, {'quality': 1.1}, {'quality': True},
                        {'research_disabled': False}, {'private_state_transferred': True}):
            with self.subTest(changes=changes), self.assertRaises(TransferError):
                self.result(trial, self.methods[1], spec['conditions'][0], spec['conditions'][0]['units'][0], **changes)
        self.assertEqual(self.archive.progress(trial['id'])['completed_cells'], 0)

    def test_all_cost_categories_and_protected_contracts_block_invalid_evidence(self):
        spec = self.spec(m=32)
        trial = self.archive.preregister(spec)
        costs = dict.fromkeys(RESEARCH_COSTS, 0)
        costs['search_execution'] = 9
        for unit in spec['conditions'][0]['units']:
            self.result(trial, self.methods[0], spec['conditions'][0], unit, quality=0)
            self.result(trial, self.methods[1], spec['conditions'][0], unit, quality=1,
                        research_costs=costs, contracts_pass=False)
        outcome = self.archive.finalize(trial['id'])['methods'][0]
        self.assertFalse(outcome['eligible'])
        self.assertFalse(outcome['conditions'][0]['equal_full_research_budget'])
        self.assertFalse(outcome['conditions'][0]['contracts_pass'])

    def test_duplicate_arm_is_not_an_additional_independent_sample(self):
        spec = self.spec()
        trial = self.archive.preregister(spec)
        unit = spec['conditions'][0]['units'][0]
        self.result(trial, self.methods[0], spec['conditions'][0], unit)
        with self.assertRaises(TransferError):
            self.result(trial, self.methods[0], spec['conditions'][0], unit)
        self.assertEqual(self.archive.progress(trial['id'])['completed_cells'], 1)

    def test_exploratory_factorial_is_not_confirmation_and_contaminates_reuse(self):
        spec = self.spec(m=1)
        pilot = self.archive.record_exploratory_2x2(incumbent_id=self.methods[0], candidate_id=self.methods[1],
            initial_library_digest=digest('L0'), accumulated_library_digest=digest('L1'),
            units=spec['conditions'][0]['units'], scores={'m0_l0': [0.1], 'm0_l1': [0.2], 'm1_l0': [0.3], 'm1_l1': [0.9]})
        self.assertAlmostEqual(pilot['mean_interaction'], 0.5)
        self.assertFalse(pilot['eligible'])
        self.assertFalse(pilot['confirmatory'])
        with self.assertRaises(TransferError):
            self.archive.preregister(spec)

    def test_economics_counts_failed_research_and_refuses_undefined_conversion(self):
        costs = dict.fromkeys(ECONOMIC_COSTS, 10)
        outcome = economic_break_even(costs=costs, baseline_per_task=3, candidate_per_task=1,
                                      reuses=30, unit='USD', quality_comparable=True)
        self.assertEqual(outcome['full_discovery_cost'], 50)
        self.assertEqual(outcome['break_even_reuses'], 25)
        self.assertEqual(outcome['break_even_whole_tasks'], 25)
        self.assertEqual(outcome['net_savings'], 10)
        for changes in ({'unit': None}, {'quality_comparable': False}):
            arguments = {'costs': costs, 'baseline_per_task': 3, 'candidate_per_task': 1,
                         'reuses': 30, 'unit': 'USD', 'quality_comparable': True, **changes}
            self.assertFalse(economic_break_even(**arguments)['defined'])
        negative = economic_break_even(costs=costs, baseline_per_task=1, candidate_per_task=2,
                                       reuses=30, unit='USD', quality_comparable=True)
        self.assertIsNone(negative['break_even_reuses'])
        extreme = economic_break_even(costs=costs, baseline_per_task=5e-324, candidate_per_task=0,
                                     reuses=0, unit='USD', quality_comparable=True)
        self.assertEqual(extreme['reason'], 'break_even_outside_numeric_range')
        self.assertIsNone(extreme['break_even_reuses'])
        with self.assertRaises(TransferError):
            economic_break_even(costs=costs, baseline_per_task=3, candidate_per_task=1,
                                reuses=30, unit='milliseconds_per_quality_point', quality_comparable=True)


if __name__ == '__main__':
    unittest.main()
