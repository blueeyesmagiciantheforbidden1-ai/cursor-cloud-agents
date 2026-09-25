import unittest

from schedule import CronExpr


class TestCronHidden(unittest.TestCase):
    def test_exact(self):
        c = CronExpr('5 10 1 1 0')
        self.assertTrue(c.matches(5, 10, 1, 1, 0))
        self.assertFalse(c.matches(6, 10, 1, 1, 0))

    def test_range_and_list(self):
        c = CronExpr('0 9-11,14 * * *')
        self.assertTrue(c.matches(0, 9, 1, 1, 0))
        self.assertTrue(c.matches(0, 14, 1, 1, 0))
        self.assertFalse(c.matches(0, 12, 1, 1, 0))

    def test_step(self):
        c = CronExpr('*/15 * * * *')
        self.assertTrue(c.matches(0, 0, 1, 1, 0))
        self.assertTrue(c.matches(45, 0, 1, 1, 0))
        self.assertFalse(c.matches(7, 0, 1, 1, 0))

    def test_step_range(self):
        c = CronExpr('0 8-18/2 * * *')
        self.assertTrue(c.matches(0, 8, 1, 1, 0))
        self.assertTrue(c.matches(0, 10, 1, 1, 0))
        self.assertFalse(c.matches(0, 9, 1, 1, 0))

    def test_bad_expr(self):
        with self.assertRaises(ValueError):
            CronExpr('* * *')
        with self.assertRaises(ValueError):
            CronExpr('60 * * * *')
        with self.assertRaises(ValueError):
            CronExpr('*/0 * * * *')

    def test_bad_args(self):
        c = CronExpr('* * * * *')
        with self.assertRaises(ValueError):
            c.matches(60, 0, 1, 1, 0)
        with self.assertRaises(ValueError):
            c.matches(0, 0, 0, 1, 0)

    def test_next_valid(self):
        c = CronExpr('*/15 * * * *')
        self.assertEqual(c.next_valid_minute(7, 1, 1, 1, 0), (15, 1))
        self.assertEqual(c.next_valid_minute(0, 1, 1, 1, 0), (0, 1))

    def test_next_wraps_hour(self):
        c = CronExpr('0 5 * * *')
        self.assertEqual(c.next_valid_minute(30, 4, 1, 1, 0), (0, 5))


if __name__ == '__main__':
    unittest.main()
