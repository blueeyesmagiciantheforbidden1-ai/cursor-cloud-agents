import copy
import unittest
from mergerec import apply_overrides, deep_merge, merge_many


class TestMergeHidden(unittest.TestCase):
    def test_nested_and_lists(self):
        a = {'x': {'y': 1}, 'tags': [1]}
        b = {'x': {'z': 2}, 'tags': [2]}
        self.assertEqual(
            deep_merge(a, b),
            {'x': {'y': 1, 'z': 2}, 'tags': [1, 2]},
        )

    def test_no_mutation(self):
        a = {'x': {'y': 1}, 'tags': [1]}
        b = {'x': {'z': 2}, 'tags': [2]}
        sa, sb = copy.deepcopy(a), copy.deepcopy(b)
        deep_merge(a, b)
        self.assertEqual(a, sa)
        self.assertEqual(b, sb)

    def test_apply_overrides_no_mutate(self):
        base = {'a': [1], 'n': {'k': 1}}
        snap = copy.deepcopy(base)
        out = apply_overrides(base, {'a': [2], 'n': {'m': 2}})
        self.assertEqual(base, snap)
        self.assertEqual(out, {'a': [1, 2], 'n': {'k': 1, 'm': 2}})

    def test_merge_many_fold(self):
        self.assertEqual(merge_many([]), {})
        self.assertEqual(
            merge_many([{'a': [1]}, {'a': [2]}, {'b': 3}]),
            {'a': [1, 2], 'b': 3},
        )

    def test_overwrite_non_dict(self):
        self.assertEqual(deep_merge({'a': 1}, {'a': {'x': 2}}), {'a': {'x': 2}})


if __name__ == "__main__":
    unittest.main()
