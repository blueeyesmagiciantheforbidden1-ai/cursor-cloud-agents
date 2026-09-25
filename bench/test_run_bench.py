"""Tests for the MH-014 usefulness benchmark runner."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench import run_bench
from bench.run_bench import (
    BreakVisibleAdapter,
    ErrorAdapter,
    ForbiddenPathAdapter,
    NoopAdapter,
    PerfectAdapter,
    RequestChangesReviewer,
    call_adapter,
    load_adapter,
    run_benchmark,
    run_one,
    scope_ok,
    score_workspace,
    shuffled_tasks,
    snapshot_files,
    stage_workspace,
    summarize_mode,
    value_gate,
)

TASKS_ROOT = Path(__file__).resolve().parent / 'tasks'
SAMPLE_TASK = TASKS_ROOT / '001-fix-clamp'


class AssertNoHiddenAdapter:
    """Adapter that fails the run if hidden tests are visible."""

    def __init__(self):
        self.saw_hidden = False

    def run(self, workspace: Path, prompt: str, timeout_s: int) -> dict:
        if (workspace / 'tests_hidden').exists() or (workspace / 'hidden_tests').exists():
            self.saw_hidden = True
            raise AssertionError('hidden tests present while adapter ran')
        return {'status': 'exited', 'seconds': 0.0, 'transcript_tail': 'clean'}


class TestAdapterProtocol(unittest.TestCase):
    def test_load_adapter_by_dotted_path(self):
        ad = load_adapter('bench.run_bench.NoopAdapter')
        self.assertIsInstance(ad, NoopAdapter)
        result = call_adapter(ad, Path('.'), 'hi', 1)
        self.assertEqual(result['status'], 'exited')

    def test_perfect_accepted_mode_a(self):
        adapters = {'implementer': PerfectAdapter(task_dir=SAMPLE_TASK)}
        record = run_one(SAMPLE_TASK, 'A', adapters)
        self.assertTrue(record['accepted'], record)
        self.assertTrue(record['scope_ok'])
        self.assertGreater(record['hidden_pass'], 0)
        self.assertEqual(record['hidden_pass'], record['hidden_total'])

    def test_perfect_accepted_all_modes(self):
        perfect = PerfectAdapter(task_dir=SAMPLE_TASK)
        adapters = {
            'implementer': perfect,
            'reviewer': perfect,
            'planner': perfect,
            'tester': perfect,
        }
        for mode in ('A', 'B', 'C'):
            with self.subTest(mode=mode):
                record = run_one(SAMPLE_TASK, mode, adapters)
                self.assertTrue(record['accepted'], f'mode {mode}: {record}')
                self.assertGreaterEqual(record['adapter_calls'], 1)

    def test_noop_rejected(self):
        adapters = {'implementer': NoopAdapter()}
        record = run_one(SAMPLE_TASK, 'A', adapters)
        self.assertFalse(record['accepted'])
        self.assertLess(record['hidden_pass'], record['hidden_total'])

    def test_forbidden_path_scope_violation(self):
        adapters = {'implementer': ForbiddenPathAdapter()}
        record = run_one(SAMPLE_TASK, 'A', adapters)
        self.assertFalse(record['scope_ok'])
        self.assertFalse(record['accepted'])

    def test_break_visible_not_accepted(self):
        adapters = {'implementer': BreakVisibleAdapter()}
        record = run_one(SAMPLE_TASK, 'A', adapters)
        self.assertFalse(record['accepted'])

    def test_hidden_tests_absent_during_adapter_run(self):
        probe = AssertNoHiddenAdapter()
        adapters = {'implementer': probe}
        # Scoring still runs afterward; adapter itself must not see hidden tests.
        run_one(SAMPLE_TASK, 'A', adapters)
        self.assertFalse(probe.saw_hidden)

        # Also assert from inside PerfectAdapter / ForbiddenPath paths via staging.
        ws = stage_workspace(SAMPLE_TASK)
        try:
            self.assertFalse((ws / 'tests_hidden').exists())
            PerfectAdapter(task_dir=SAMPLE_TASK).run(ws, 'Role: implementer\nfix', 30)
            self.assertFalse((ws / 'tests_hidden').exists())
        finally:
            import shutil
            shutil.rmtree(ws, ignore_errors=True)

    def test_mode_b_revision_when_reviewer_requests_changes(self):
        adapters = {
            'implementer': PerfectAdapter(task_dir=SAMPLE_TASK),
            'reviewer': RequestChangesReviewer(),
        }
        record = run_one(SAMPLE_TASK, 'B', adapters)
        self.assertEqual(record['adapter_calls'], 3)  # impl + review + revision
        self.assertTrue(record['accepted'])

    def test_mode_c_adapter_call_count(self):
        perfect = PerfectAdapter(task_dir=SAMPLE_TASK)
        adapters = {
            'implementer': perfect,
            'reviewer': perfect,
            'planner': perfect,
            'tester': perfect,
        }
        record = run_one(SAMPLE_TASK, 'C', adapters)
        # planner + implementer + tester + reviewer + revision
        self.assertEqual(record['adapter_calls'], 5)
        self.assertTrue(record['accepted'])

    def test_adapter_error_counts_as_intervention(self):
        adapters = {'implementer': ErrorAdapter()}
        record = run_one(SAMPLE_TASK, 'A', adapters)
        self.assertEqual(record['interventions'], 1)
        self.assertFalse(record['accepted'])


class TestScopeAndScoring(unittest.TestCase):
    def test_scope_ok_rejects_tests_hidden(self):
        self.assertTrue(scope_ok(['clampkit/__init__.py', 'tests/test_clamp.py']))
        self.assertFalse(scope_ok(['tests_hidden/cheat.py']))
        self.assertFalse(scope_ok(['pkg/x.py', 'tests_hidden/a.py']))

    def test_expected_files_are_advisory(self):
        """Changing a repo file not listed in expected_files still scopes OK."""
        self.assertTrue(scope_ok(['some_other_module.py']))

    def test_score_installs_hidden_only_after(self):
        ws = stage_workspace(SAMPLE_TASK)
        try:
            baseline = snapshot_files(ws)
            self.assertFalse((ws / 'tests_hidden').exists())
            PerfectAdapter(task_dir=SAMPLE_TASK).run(
                ws, 'Role: implementer\nsolve', 30,
            )
            self.assertFalse((ws / 'tests_hidden').exists())
            scored = score_workspace(SAMPLE_TASK, ws, baseline)
            self.assertTrue((ws / 'tests_hidden').exists())
            self.assertTrue(scored['accepted'])
        finally:
            import shutil
            shutil.rmtree(ws, ignore_errors=True)


class TestOrderingAndBenchmark(unittest.TestCase):
    def test_deterministic_order_for_seed(self):
        tasks = run_bench.list_tasks(TASKS_ROOT)
        self.assertEqual(len(tasks), 10)
        a = [p.name for p in shuffled_tasks(tasks, 42)]
        b = [p.name for p in shuffled_tasks(tasks, 42)]
        c = [p.name for p in shuffled_tasks(tasks, 7)]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(sorted(a), sorted(p.name for p in tasks))

    def test_run_benchmark_writes_jsonl_and_table(self):
        # Use a single cheap task with PerfectAdapter; modes A only to keep runtime low.
        perfect = PerfectAdapter()
        adapters = {
            'implementer': perfect,
            'reviewer': perfect,
            'planner': perfect,
            'tester': perfect,
        }
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / 'out.jsonl'
            # Point at one task directory as the "root" (list_tasks handles meta.json).
            out = run_benchmark(
                SAMPLE_TASK,
                seed=1,
                repeats=1,
                modes=('A',),
                adapters=adapters,
                results_path=results,
            )
            self.assertTrue(results.is_file())
            lines = results.read_text(encoding='utf-8').strip().splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertTrue(row['accepted'])
            self.assertIn('value_gate', out)
            self.assertIn('table', out)
            self.assertIn('A', out['table'])


class TestValueGate(unittest.TestCase):
    def _summary(self, n, accepted_rate, mean_seconds, interventions_mean, calls=1):
        accepted = int(round(accepted_rate * n))
        return {
            'n': n,
            'accepted': accepted,
            'accepted_rate': accepted_rate,
            'mean_seconds': mean_seconds,
            'median_seconds': mean_seconds,
            'adapter_calls_total': calls * n,
            'adapter_calls_mean': float(calls),
            'interventions_total': int(round(interventions_mean * n)),
            'interventions_mean': interventions_mean,
            'cost_per_accepted': (calls * n / accepted) if accepted else float('inf'),
        }

    def test_insufficient_data_on_small_n(self):
        a = self._summary(2, 0.5, 10.0, 1.0)
        b = self._summary(2, 0.5, 5.0, 0.5)
        c = self._summary(2, 0.5, 5.0, 0.5)
        gate = value_gate(a, b, c, min_samples=5)
        self.assertEqual(gate['verdict'], 'INSUFFICIENT_DATA')
        self.assertEqual(gate['samples']['A'], 2)

    def test_pass_when_c_faster_no_regression(self):
        a = self._summary(10, 0.8, 100.0, 1.0)
        b = self._summary(10, 0.8, 100.0, 1.0)
        c = self._summary(10, 0.8, 70.0, 1.0)  # 30% less time
        gate = value_gate(a, b, c, min_samples=5)
        self.assertEqual(gate['verdict'], 'PASS')
        self.assertIn('C', gate['passed_modes'])

    def test_pass_when_b_less_intervention(self):
        a = self._summary(10, 0.7, 50.0, 2.0)
        b = self._summary(10, 0.7, 50.0, 1.0)  # 50% less intervention
        c = self._summary(10, 0.5, 50.0, 2.0)  # quality regression
        gate = value_gate(a, b, c, min_samples=5)
        self.assertEqual(gate['verdict'], 'PASS')
        self.assertIn('B', gate['passed_modes'])
        self.assertNotIn('C', gate['passed_modes'])

    def test_fail_on_quality_regression(self):
        a = self._summary(10, 0.9, 100.0, 2.0)
        b = self._summary(10, 0.5, 50.0, 0.5)  # faster but worse quality
        c = self._summary(10, 0.5, 50.0, 0.5)
        gate = value_gate(a, b, c, min_samples=5)
        self.assertEqual(gate['verdict'], 'FAIL')

    def test_fail_when_no_efficiency_gain(self):
        a = self._summary(10, 0.8, 100.0, 1.0)
        b = self._summary(10, 0.8, 95.0, 0.95)  # <20% improvement
        c = self._summary(10, 0.8, 95.0, 0.95)
        gate = value_gate(a, b, c, min_samples=5)
        self.assertEqual(gate['verdict'], 'FAIL')

    def test_summarize_mode_cost_proxy(self):
        rows = [
            {'accepted': True, 'seconds': 2.0, 'adapter_calls': 4, 'interventions': 0},
            {'accepted': False, 'seconds': 3.0, 'adapter_calls': 4, 'interventions': 1},
        ]
        s = summarize_mode(rows)
        self.assertEqual(s['n'], 2)
        self.assertEqual(s['accepted'], 1)
        self.assertEqual(s['cost_per_accepted'], 8.0)


if __name__ == '__main__':
    unittest.main()
