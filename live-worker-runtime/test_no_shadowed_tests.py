"""A second def of a test name in one class (or module) silently replaces the
first, so the first test never runs. Parallel tasks editing the same test
class can do this without either noticing; this guard fails instead."""

import ast
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def shadowed(tree):
    """[(scope, name)] for every test name defined more than once in one body."""
    found = []
    scopes = [('<module>', tree.body)]
    scopes += [(node.name, node.body) for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    for scope, body in scopes:
        names = Counter(
            node.name for node in body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and (node.name.startswith('test') or node.name.startswith('Test')
                 or (isinstance(node, ast.ClassDef) and scope == '<module>')))
        found += [(scope, name) for name, count in names.items() if count > 1]
    return found


class NoShadowedTests(unittest.TestCase):
    def test_detector_catches_a_duplicate_test_method(self):
        tree = ast.parse('class T:\n    def test_a(self): pass\n    def test_a(self): pass\n')
        self.assertEqual(shadowed(tree), [('T', 'test_a')])

    def test_no_test_name_is_defined_twice_in_one_scope(self):
        paths = sorted(ROOT.glob('test_*.py')) + sorted((ROOT / 'tests').rglob('*.py'))
        self.assertTrue(paths)
        problems = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            problems += [f'{path.relative_to(ROOT)}: {scope}.{name}' for scope, name in shadowed(tree)]
        self.assertEqual(problems, [])


if __name__ == '__main__':
    unittest.main()
