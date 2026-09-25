import unittest

from durationparse import parse_duration


class TestParseHidden(unittest.TestCase):
    def test_zero_seconds(self):
        self.assertEqual(parse_duration('0s'), 0)

    def test_days(self):
        self.assertEqual(parse_duration('1d'), 86400)

    def test_strip_outer_whitespace(self):
        self.assertEqual(parse_duration('  2m  '), 120)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_duration('')

    def test_bare_number_raises(self):
        with self.assertRaises(ValueError):
            parse_duration('90')

    def test_out_of_order_raises(self):
        with self.assertRaises(ValueError):
            parse_duration('30m1h')

    def test_duplicate_unit_raises(self):
        with self.assertRaises(ValueError):
            parse_duration('1h1h')

    def test_internal_space_raises(self):
        with self.assertRaises(ValueError):
            parse_duration('1h 30m')

    def test_full_combo(self):
        self.assertEqual(parse_duration('1d2h3m4s'), 86400 + 7200 + 180 + 4)


if __name__ == '__main__':
    unittest.main()
