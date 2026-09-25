import unittest

from cache import LRUCache


class TestLruHidden(unittest.TestCase):
    def test_capacity_validation(self):
        with self.assertRaises(ValueError):
            LRUCache(0)
        with self.assertRaises(ValueError):
            LRUCache(-1)

    def test_evicts_least_recent(self):
        c = LRUCache(2)
        c.put('a', 1)
        c.put('b', 2)
        c.get('a')
        c.put('c', 3)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 1)
        self.assertEqual(c.get('c'), 3)
        self.assertEqual(len(c), 2)

    def test_update_refreshes(self):
        c = LRUCache(2)
        c.put('a', 1)
        c.put('b', 2)
        c.put('a', 9)
        c.put('c', 3)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 9)

    def test_missing_get(self):
        c = LRUCache(1)
        self.assertIsNone(c.get('nope'))

    def test_capacity_one(self):
        c = LRUCache(1)
        c.put('a', 1)
        c.put('b', 2)
        self.assertIsNone(c.get('a'))
        self.assertEqual(c.get('b'), 2)


if __name__ == '__main__':
    unittest.main()
