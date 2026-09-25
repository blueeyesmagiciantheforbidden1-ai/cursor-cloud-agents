import unittest

from cache import LRUCache


class TestLruSmoke(unittest.TestCase):
    def test_put_get(self):
        c = LRUCache(2)
        c.put('a', 1)
        self.assertEqual(c.get('a'), 1)


if __name__ == '__main__':
    unittest.main()
