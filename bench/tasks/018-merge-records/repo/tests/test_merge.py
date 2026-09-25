import unittest
from mergerec import deep_merge, merge_many


class TestMerge(unittest.TestCase):
    def test_shallow(self):
        self.assertEqual(deep_merge({'a': 1}, {'b': 2}), {'a': 1, 'b': 2})

    def test_merge_many_two(self):
        self.assertEqual(merge_many([{'a': 1}, {'b': 2}]), {'a': 1, 'b': 2})


if __name__ == "__main__":
    unittest.main()
