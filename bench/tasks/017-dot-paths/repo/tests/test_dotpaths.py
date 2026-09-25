import unittest
from dotpaths import expand, flatten


class TestDotPaths(unittest.TestCase):
    def test_roundtrip(self):
        obj = {'a': {'b': 1}, 'c': 2}
        self.assertEqual(flatten(obj), {'a.b': 1, 'c': 2})
        self.assertEqual(expand({'a.b': 1, 'c': 2}), obj)


if __name__ == "__main__":
    unittest.main()
