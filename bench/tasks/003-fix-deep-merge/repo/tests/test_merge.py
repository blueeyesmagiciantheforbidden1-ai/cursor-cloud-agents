import unittest

from dicttools import deep_merge


class TestDeepMergeVisible(unittest.TestCase):
    def test_flat_override(self):
        out = deep_merge({'a': 1}, {'a': 2, 'b': 3})
        self.assertEqual(out['a'], 2)
        self.assertEqual(out['b'], 3)


if __name__ == '__main__':
    unittest.main()
