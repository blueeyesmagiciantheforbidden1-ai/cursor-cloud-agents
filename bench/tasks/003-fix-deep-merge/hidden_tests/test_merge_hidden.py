import copy
import unittest

from dicttools import deep_merge


class TestDeepMergeHidden(unittest.TestCase):
    def test_nested_merge(self):
        base = {'a': {'x': 1, 'y': 2}, 'b': 0}
        override = {'a': {'y': 9, 'z': 3}}
        out = deep_merge(base, override)
        self.assertEqual(out, {'a': {'x': 1, 'y': 9, 'z': 3}, 'b': 0})

    def test_does_not_mutate_base(self):
        base = {'a': {'x': 1}, 'b': 2}
        original = copy.deepcopy(base)
        deep_merge(base, {'a': {'y': 2}, 'c': 3})
        self.assertEqual(base, original)

    def test_does_not_mutate_override(self):
        override = {'a': {'y': 2}}
        original = copy.deepcopy(override)
        deep_merge({'a': {'x': 1}}, override)
        self.assertEqual(override, original)

    def test_override_non_dict_replaces(self):
        out = deep_merge({'a': {'x': 1}}, {'a': [1, 2]})
        self.assertEqual(out['a'], [1, 2])

    def test_empty_override(self):
        base = {'a': 1}
        out = deep_merge(base, {})
        self.assertEqual(out, {'a': 1})
        self.assertIsNot(out, base)

    def test_deeper_nesting(self):
        out = deep_merge({'a': {'b': {'c': 1}}}, {'a': {'b': {'d': 2}}})
        self.assertEqual(out['a']['b'], {'c': 1, 'd': 2})


if __name__ == '__main__':
    unittest.main()
