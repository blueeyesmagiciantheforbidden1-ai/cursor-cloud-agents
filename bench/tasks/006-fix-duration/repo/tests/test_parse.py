import unittest

from durationparse import parse_duration


class TestParseVisible(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(parse_duration('90s'), 90)

    def test_hour_minute(self):
        self.assertEqual(parse_duration('1h30m'), 5400)


if __name__ == '__main__':
    unittest.main()
