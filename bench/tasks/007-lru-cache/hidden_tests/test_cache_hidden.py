import unittest

from lrucache import LRUCache


class TestLruHidden(unittest.TestCase):
    def test_evicts_least_recently_used(self):
        c = LRUCache(2)
        c.set('a', 1)
        c.set('b', 2)
        c.get('a')
        c.set('c', 3)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 1)
        self.assertEqual(c.get('c'), 3)

    def test_update_counts_as_use(self):
        c = LRUCache(2)
        c.set('a', 1)
        c.set('b', 2)
        c.set('a', 9)
        c.set('c', 3)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 9)

    def test_delete(self):
        c = LRUCache(2)
        c.set('a', 1)
        self.assertTrue(c.delete('a'))
        self.assertFalse(c.delete('a'))
        self.assertEqual(len(c), 0)

    def test_default(self):
        c = LRUCache(1)
        self.assertEqual(c.get('missing', 'x'), 'x')

    def test_invalid_maxsize(self):
        with self.assertRaises(ValueError):
            LRUCache(0)
        with self.assertRaises(ValueError):
            LRUCache(-1)

    def test_get_missing_does_not_resize(self):
        c = LRUCache(1)
        c.set('a', 1)
        c.get('nope')
        self.assertEqual(len(c), 1)


if __name__ == '__main__':
    unittest.main()
