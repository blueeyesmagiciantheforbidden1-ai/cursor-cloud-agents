import unittest

from retrykit import retry_call


class TestRetryVisible(unittest.TestCase):
    def test_success_first_try(self):
        self.assertEqual(retry_call(lambda: 7, attempts=3), 7)


if __name__ == '__main__':
    unittest.main()
