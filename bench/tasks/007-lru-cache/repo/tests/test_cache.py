import unittest

from lrucache import LRUCache


class TestLruVisible(unittest.TestCase):
    def test_set_get(self):
        c = LRUCache(2)
        c.set('a', 1)
        self.assertEqual(c.get('a'), 1)
        self.assertEqual(len(c), 1)


if __name__ == '__main__':
    unittest.main()
