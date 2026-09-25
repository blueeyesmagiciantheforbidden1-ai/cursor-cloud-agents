import unittest

from ratelimit import TokenBucket


class TestBucketHidden(unittest.TestCase):
    def test_starts_full(self):
        b = TokenBucket(3, 1.0)
        self.assertTrue(b.allow(3, now=0.0))
        self.assertFalse(b.allow(1, now=0.0))

    def test_refill_and_clamp(self):
        b = TokenBucket(5, 2.0)
        self.assertTrue(b.allow(5, now=0.0))
        # after 10s would add 20 but clamp to 5
        self.assertTrue(b.allow(5, now=10.0))
        self.assertFalse(b.allow(1, now=10.0))

    def test_partial_refill(self):
        b = TokenBucket(10, 1.0)
        self.assertTrue(b.allow(10, now=0.0))
        self.assertFalse(b.allow(1, now=0.5))
        self.assertTrue(b.allow(1, now=1.0))

    def test_insufficient_does_not_deduct(self):
        b = TokenBucket(2, 0.1)
        self.assertTrue(b.allow(2, now=0.0))
        self.assertFalse(b.allow(1, now=0.0))
        # still empty; small refill then allow 1
        self.assertTrue(b.allow(1, now=10.0))

    def test_request_over_capacity(self):
        b = TokenBucket(5, 1.0)
        self.assertFalse(b.allow(6, now=0.0))
        self.assertTrue(b.allow(5, now=0.0))

    def test_validation(self):
        with self.assertRaises(ValueError):
            TokenBucket(0, 1.0)
        with self.assertRaises(ValueError):
            TokenBucket(5, 0)
        b = TokenBucket(5, 1.0)
        with self.assertRaises(ValueError):
            b.allow(0, now=0.0)
        with self.assertRaises(ValueError):
            b.allow(-1, now=0.0)


if __name__ == '__main__':
    unittest.main()
