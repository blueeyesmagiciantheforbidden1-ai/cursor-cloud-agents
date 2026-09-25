import unittest

from clampkit import clamp


class TestClampHidden(unittest.TestCase):
    def test_upper_bound_inclusive(self):
        self.assertEqual(clamp(10, 0, 10), 10)

    def test_lower_bound_inclusive(self):
        self.assertEqual(clamp(0, 0, 10), 0)

    def test_above_high(self):
        self.assertEqual(clamp(11, 0, 10), 10)

    def test_floats(self):
        self.assertEqual(clamp(0.5, 0.0, 1.0), 0.5)
        self.assertEqual(clamp(1.0, 0.0, 1.0), 1.0)

    def test_low_greater_than_high_raises_value_error(self):
        with self.assertRaises(ValueError):
            clamp(1, 5, 2)

    def test_equal_bounds(self):
        self.assertEqual(clamp(3, 3, 3), 3)
        self.assertEqual(clamp(0, 3, 3), 3)
        self.assertEqual(clamp(9, 3, 3), 3)


if __name__ == '__main__':
    unittest.main()
