import unittest

from schedule import CronExpr


class TestCronSmoke(unittest.TestCase):
    def test_every_minute(self):
        c = CronExpr('* * * * *')
        self.assertTrue(c.matches(0, 0, 1, 1, 0))


if __name__ == '__main__':
    unittest.main()
