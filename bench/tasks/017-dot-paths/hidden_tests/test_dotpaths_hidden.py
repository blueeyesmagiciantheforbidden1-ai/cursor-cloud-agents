import copy
import unittest
from dotpaths import expand, flatten


class TestDotPathsHidden(unittest.TestCase):
    def test_empty_nested(self):
        self.assertEqual(flatten({'a': {}}), {'a': {}})
        self.assertEqual(expand({'a': {}}), {'a': {}})

    def test_no_mutation_flatten(self):
        obj = {'a': {'b': 1}}
        snap = copy.deepcopy(obj)
        flatten(obj)
        self.assertEqual(obj, snap)

    def test_no_mutation_expand(self):
        flat = {'a.b': 1}
        snap = copy.deepcopy(flat)
        expand(flat)
        self.assertEqual(flat, snap)

    def test_list_is_leaf(self):
        self.assertEqual(flatten({'a': [1, 2]}), {'a': [1, 2]})

    def test_conflict(self):
        with self.assertRaises(ValueError):
            expand({'a': 1, 'a.b': 2})


if __name__ == "__main__":
    unittest.main()
