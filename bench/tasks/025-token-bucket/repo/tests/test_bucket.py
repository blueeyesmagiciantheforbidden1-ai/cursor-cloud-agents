import unittest

from ratelimit import TokenBucket


class TestBucketSmoke(unittest.TestCase):
    def test_starts_and_allows(self):
        b = TokenBucket(5, 1.0)
        # After construction with a fixed clock via allow(now=...)
        self.assertTrue(b.allow(1, now=100.0))


if __name__ == '__main__':
    unittest.main()
