import tempfile
import unittest
import sqlite3
from pathlib import Path
from ryan_frontier.promotion import PromotionRegistry, GateError

class PromotionTests(unittest.TestCase):
    def test_inconclusive_evidence_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as d:
            r=PromotionRegistry(Path(d)/'registry.db')
            p=r.stage('a'*64,'b'*64,'c'*64,-0.1)
            with self.assertRaises(GateError): r.approve(p,'a'*64,'owner')
            self.assertIsNone(r.current())
            r.close()

    def test_exact_binding_restart_and_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'registry.db'
            r=PromotionRegistry(path)
            p=r.stage('a'*64,'b'*64,'c'*64,0.2)
            with self.assertRaises(GateError): r.activate(p,candidate='a'*64,evaluator='b'*64,evidence='c'*64)
            r.approve(p,'a'*64,'owner')
            with self.assertRaises(GateError): r.activate(p,candidate='a'*64,evaluator='b'*64,evidence='d'*64)
            first=r.activate(p,candidate='a'*64,evaluator='b'*64,evidence='c'*64)
            r.close()
            r=PromotionRegistry(path)
            self.assertEqual(r.current()['candidate'],'a'*64)
            p=r.stage('d'*64,'b'*64,'e'*64,0.3)
            r.approve(p,'d'*64,'owner')
            r.activate(p,candidate='d'*64,evaluator='b'*64,evidence='e'*64)
            self.assertEqual(r.rollback(first['sequence'],'owner')['candidate'],'a'*64)
            with self.assertRaises(GateError): r.rollback(999,'owner')
            r.close()

    def test_nonfinite_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            r=PromotionRegistry(Path(d)/'registry.db')
            with self.assertRaises(GateError): r.stage('a'*64,'b'*64,'c'*64,float('nan'))
            r.close()

    def test_compound_gate_is_durable_and_cannot_be_approved_when_one_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'registry.db'
            registry = PromotionRegistry(path)
            proposal = registry.stage('a'*64, 'b'*64, 'c'*64, 0.4, checks={
                'fresh:method_gain': True, 'shift:memory_nonregression': False,
            })
            registry.close()
            registry = PromotionRegistry(path)
            try:
                row = registry.proposal(proposal)
                self.assertEqual(row['lower_bound'], 0.4)
                self.assertEqual(row['status'], 'inconclusive')
                self.assertFalse(row['checks']['shift:memory_nonregression'])
                with self.assertRaises(GateError):
                    registry.approve(proposal, 'a'*64, 'owner')
            finally:
                registry.close()

    def test_evidence_gate_rejects_non_numeric_boolean_and_unbounded_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = PromotionRegistry(Path(directory) / 'registry.db')
            try:
                for value in (True, '0.2', 10 ** 1000, float('inf')):
                    with self.subTest(value=value), self.assertRaises(GateError):
                        registry.stage('a'*64, 'b'*64, 'c'*64, value)
                for checks in ({'gate': 1}, {'': True}, []):
                    with self.subTest(checks=checks), self.assertRaises(GateError):
                        registry.stage('a'*64, 'b'*64, 'c'*64, 0.2, checks=checks)
            finally:
                registry.close()

    def test_existing_registry_schema_is_migrated_without_losing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE proposals (id TEXT PRIMARY KEY, candidate TEXT NOT NULL, '
                    'evaluator TEXT NOT NULL, evidence TEXT NOT NULL, lower_bound REAL NOT NULL, '
                    'margin REAL NOT NULL, status TEXT NOT NULL, approver TEXT, created REAL NOT NULL)')
                connection.execute('INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?)',
                    ('old', 'a'*64, 'b'*64, 'c'*64, -0.1, 0.0, 'inconclusive', None, 0))
            registry = PromotionRegistry(path)
            try:
                self.assertEqual(registry.proposal('old')['status'], 'inconclusive')
                self.assertEqual(registry.proposal('old')['checks'], {})
                new = registry.stage('a'*64, 'b'*64, 'c'*64, 0.2, checks={'pass': True})
                self.assertEqual(registry.proposal(new)['status'], 'review_required')
            finally:
                registry.close()
