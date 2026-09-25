import unittest

from textutil import collapse_ws


class TestCollapseWs(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(collapse_ws('  hello   world  '), 'hello world')


if __name__ == '__main__':
    unittest.main()
